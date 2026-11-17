"""Spike detectors for live (sliding) and replay (batch) modes, both built on the same bucketing, baseline
and scoring core. Implemented in M04.

Both modes feed scored buckets into the same `SpikeTracker`, so they agree on what a spike is:

- a spike opens at the first candidate bucket (`score_z >= z_b` and enough messages);
- it stays open while the score is at least `end_z`; the peak is its best candidate bucket;
- it closes at the first bucket whose score is below `end_z`, or when it has lasted `max_len_s - lead_s`
  seconds (then nothing new opens until the score has fallen below `end_z`).

Replay mode scores back-to-back buckets from the start of the video and merges spikes whose peaks are
closer than `merge_gap_s` (the higher one is kept). Live mode scores the newest `bucket_s` every
`live_step_s`; it cannot take back a spike it already emitted, so a later spike inside the merge gap is
dropped instead (kept in `LiveSpikeDetector.suppressed`).

Time bases: replay `t` is seconds from the start of the video, live `t` is unix seconds. Messages are
always bucketed by their own `t`, never by arrival time (YouTube live delivers batches).
"""

import math
from bisect import bisect_left, insort
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Final, Literal

from sixstars.chat import filters
from sixstars.chat.models import ChatMessage, Platform, sort_messages
from sixstars.logging import get_logger
from sixstars.spikes.baseline import baseline_for
from sixstars.spikes.buckets import Counted, LoudnessTrack, TokenMatcher, count_message, make_buckets
from sixstars.spikes.config import SpikeConfig
from sixstars.spikes.scoring import Scored, Tier, is_candidate, score_bucket, tier_for
from sixstars.spikes.window import clip_window, in_cooldown, merge_by_peak

Mode = Literal["live", "replay"]

SAMPLE_TEXT_CHARS: Final = 200  # longest chat text kept in a chat sample
SAMPLE_BEFORE_SHARE: Final = 4  # one message in this many of a chat sample comes from before the peak
MAX_CATCH_UP_STEPS: Final = 120  # a late `tick` evaluates at most this many missed steps
_ROUND: Final = 4

log = get_logger("spikes")


@dataclass(slots=True)
class DetectedSpike:
    """One detected spike with its numbers, provisional tier and clip window.

    All times are on the clock of the chat: seconds from the start of the video in replay mode, unix
    seconds in live mode. `parts` holds the clamped z-score of every feature at the peak (`msgs_z`,
    `uniq_z`, `react_z`, `clipreq_z`, `audio_z`). `spike_floor` is the start of the first spike bucket:
    later stages may never move the clip start after it. `absorbed` lists the lower spikes merged into
    this one (replay mode).
    """

    mode: Mode
    peak_t: float
    first_t: float
    score_z: float
    parts: dict[str, float]
    msgs: int
    uniq: int
    reaction_count: int
    clip_requests: int
    tier: Tier
    window_start: float
    window_end: float
    spike_floor: float
    pad_start: float
    pad_end: float
    chat_sample: list[dict[str, Any]]
    baseline_mean: dict[str, float] = field(default_factory=dict)
    absorbed: list["DetectedSpike"] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RawSpike:
    """What the tracker knows when a spike closes. `end_t` is the start of the first bucket below
    `end_z`, or None when the spike was cut at its maximum length."""

    first: Scored
    peak: Scored
    end_t: float | None


class SpikeTracker:
    """The spike state machine shared by both modes. Feed it scored buckets in time order."""

    def __init__(self) -> None:
        self._first: Scored | None = None
        self._peak: Scored | None = None
        self._draining = False

    @property
    def is_open(self) -> bool:
        """True while a spike is open."""
        return self._first is not None

    def _close(self, end_t: float | None) -> RawSpike | None:
        first, peak = self._first, self._peak
        self._first = self._peak = None
        return RawSpike(first, peak, end_t) if first is not None and peak is not None else None

    def step(self, scored: Scored, cfg: SpikeConfig, min_msgs: float) -> RawSpike | None:
        """Take the next scored bucket; returns a spike when this bucket closes one."""
        if self._draining:
            self._draining = scored.score_z >= cfg.end_z
            return None
        candidate = is_candidate(scored, cfg, min_msgs)
        if self._first is None or self._peak is None:
            if candidate:
                self._first = self._peak = scored
            return None
        if scored.score_z < cfg.end_z:
            return self._close(scored.bucket.t)
        if candidate and (scored.score_z, scored.bucket.msgs) > (self._peak.score_z, self._peak.bucket.msgs):
            self._peak = scored
        if scored.bucket.t + cfg.bucket_s - self._first.bucket.t >= cfg.max_len_s - cfg.lead_s:
            self._draining = True
            return self._close(None)
        return None

    def flush(self, end_t: float) -> RawSpike | None:
        """Close the open spike at the end of the data (replay mode)."""
        return self._close(end_t)


def learn_bots(messages: Iterable[ChatMessage]) -> set[str]:
    """Bot names for one stream: names ending in "bot", plus `filters.learn_bots` applied to the messages
    that are at least as long as its comparison prefix. Short texts are left out because a viewer who
    posts the same emote 20 times is a fan, not a bot (noted as an open issue by M03)."""
    listed = list(messages)
    named = {
        name
        for name in (filters.normalise_name(message.username) for message in listed)
        if name.endswith(filters.BOT_NAME_SUFFIX)
    }
    long_texts = [message for message in listed if len(message.text.strip()) >= filters.BOT_PREFIX_CHARS]
    return named | filters.learn_bots(long_texts)


def sample_chat(
    items: Sequence[Counted], peak_t: float, size: int, max_per_user: int
) -> list[dict[str, Any]]:
    """Up to `size` counted messages around the peak for Claude: a quarter from just before the peak
    bucket, the rest from its start onwards, at most `max_per_user` per user on each side. Each entry is
    `{"t_rel": seconds from the peak, "user": name, "text": text}`."""
    split = bisect_left(items, peak_t, key=lambda item: item.t)

    def take(candidates: Iterable[Counted], limit: int) -> list[Counted]:
        taken: list[Counted] = []
        per_user: dict[str, int] = {}
        for item in candidates:
            if len(taken) >= limit:
                break
            if per_user.get(item.user_id, 0) < max_per_user:
                per_user[item.user_id] = per_user.get(item.user_id, 0) + 1
                taken.append(item)
        return taken

    before = take((items[index] for index in range(split - 1, -1, -1)), size // SAMPLE_BEFORE_SHARE)
    after = take((items[index] for index in range(split, len(items))), size - len(before))
    return [
        {
            "t_rel": round(item.t - peak_t, 1),
            "user": item.msg.username,
            "text": item.msg.text[:SAMPLE_TEXT_CHARS],
        }
        for item in [*reversed(before), *after]
    ]


def build_spike(
    mode: Mode,
    raw: RawSpike,
    cfg: SpikeConfig,
    *,
    platform: str,
    items: Sequence[Counted],
    stream_start: float,
) -> DetectedSpike:
    """Turn a closed spike into the output record: tier, clip window and chat sample."""
    peak, first = raw.peak, raw.first
    window = clip_window(
        first.bucket.t, raw.end_t, cfg, chat_offset_s=cfg.offset_for(platform), earliest=stream_start
    )
    return DetectedSpike(
        mode=mode,
        peak_t=peak.bucket.t,
        first_t=first.bucket.t,
        score_z=round(peak.score_z, _ROUND),
        parts={f"{feature}_z": round(value, _ROUND) for feature, value in peak.z.items()},
        msgs=peak.bucket.msgs,
        uniq=peak.bucket.uniq,
        reaction_count=peak.bucket.reaction_count,
        clip_requests=peak.bucket.clipreq,
        tier=tier_for(peak.score_z, peak.bucket.uniq, peak.baseline.mean["uniq"], cfg),
        window_start=window.start,
        window_end=window.end,
        spike_floor=window.spike_floor,
        pad_start=window.pad_start,
        pad_end=window.pad_end,
        chat_sample=sample_chat(items, peak.bucket.t, cfg.chat_sample_size, cfg.max_per_user),
        baseline_mean={feature: round(value, _ROUND) for feature, value in peak.baseline.mean.items()},
    )


@dataclass(frozen=True, slots=True)
class ReplayScan:
    """The result of scanning a whole chat replay. `bot_names` and `baseline_rate` (median messages per
    bucket over the whole stream) are what `chat_files` stores next to the spikes."""

    spikes: list[DetectedSpike]
    bot_names: frozenset[str]
    baseline_rate: float
    counted_messages: int
    buckets: int


def analyse_replay(
    messages: Iterable[ChatMessage],
    stream_start: float = 0.0,
    *,
    config: SpikeConfig | None = None,
    bot_names: Iterable[str] | None = None,
    loudness: Iterable[tuple[float, float]] | None = None,
) -> ReplayScan:
    """Scan a whole chat replay. Pure when `config` is passed; with `config=None` the settings are read
    once from the table.

    `bot_names=None` learns the bots from the whole file. `loudness` is optional `(t, dB)` readings on
    the chat clock for the audio-jump feature; without it the audio part of the score is 0.
    """
    cfg = config or SpikeConfig.from_settings()
    ordered = sort_messages(messages)
    bots = frozenset(learn_bots(ordered) if bot_names is None else map(filters.normalise_name, bot_names))
    ctx = cfg.filter_context(stream_start, bots)
    reactions, clips = (
        TokenMatcher.from_tokens(cfg.reaction_tokens),
        TokenMatcher.from_tokens(cfg.clip_tokens),
    )
    items = [
        count_message(message, reactions, clips) for message in ordered if filters.is_counted(message, ctx)
    ]
    if not items:
        return ReplayScan([], bots, 0.0, 0, 0)
    platform = items[0].msg.platform
    count = int((items[-1].t - stream_start) // cfg.bucket_s) + 1
    buckets = make_buckets(
        items,
        stream_start,
        cfg.bucket_s,
        count,
        max_per_user=cfg.max_per_user,
        loudness=LoudnessTrack(loudness) if loudness is not None else None,
        audio_ref_s=cfg.audio_ref_s,
    )
    coverage_start = stream_start + cfg.warmup_s
    measured = [bucket.msgs for bucket in buckets if bucket.t >= coverage_start]
    baseline_rate = float(median(measured)) if measured else 0.0
    min_msgs = max(cfg.min_messages, cfg.replay_median_multiple * baseline_rate)

    tracker = SpikeTracker()
    raws: list[RawSpike] = []
    for index, bucket in enumerate(buckets):
        baseline = baseline_for(buckets, index, cfg, coverage_start)
        if baseline is None:
            continue
        raw = tracker.step(score_bucket(bucket, baseline, cfg), cfg, min_msgs)
        if raw is not None:
            raws.append(raw)
    last = tracker.flush(buckets[-1].t + cfg.bucket_s)
    if last is not None:
        raws.append(last)

    found = [
        build_spike("replay", raw, cfg, platform=platform, items=items, stream_start=stream_start)
        for raw in raws
    ]
    spikes: list[DetectedSpike] = []
    for kept, absorbed in merge_by_peak(
        found, cfg.merge_gap_s, peak_t=lambda spike: spike.peak_t, score=lambda spike: spike.score_z
    ):
        kept.absorbed = absorbed
        spikes.append(kept)
    log.debug(
        "replay_scanned",
        platform=platform,
        messages=len(ordered),
        counted=len(items),
        buckets=count,
        baseline_rate=baseline_rate,
        bots=len(bots),
        spikes=len(spikes),
    )
    return ReplayScan(spikes, bots, baseline_rate, len(items), count)


def scan_replay(
    messages: Iterable[ChatMessage],
    stream_start: float = 0.0,
    *,
    config: SpikeConfig | None = None,
    bot_names: Iterable[str] | None = None,
    loudness: Iterable[tuple[float, float]] | None = None,
) -> list[DetectedSpike]:
    """The spikes of a whole chat replay, in time order. See `analyse_replay`."""
    return analyse_replay(
        messages, stream_start, config=config, bot_names=bot_names, loudness=loudness
    ).spikes


class LiveSpikeDetector:
    """Sliding-window spike detection over real-time chat.

    Call `feed` for every message as it arrives and `tick(now)` every `spikes.live_step_s` seconds; `tick`
    returns the spikes that closed. `stream_start` is the unix time the stream started (for the warm-up
    rule and as the anchor of the step grid). `listen_start` is when chat reading began; by default it is
    the earliest time seen, so the backlog YouTube sends on connect extends the baseline.

    With `config=None` the settings are re-read on every tick (they are cached for 30 seconds), so a
    threshold change reaches a running detector. Sources that deliver late batches (YouTube polls every
    5 s) should tick with `now` set that far in the past, so each bucket is complete when it is scored.
    """

    def __init__(
        self,
        platform: Platform,
        stream_start: float,
        *,
        config: SpikeConfig | None = None,
        bot_names: Iterable[str] = (),
        listen_start: float | None = None,
    ) -> None:
        self.platform: Platform = platform
        self.stream_start = stream_start
        self.suppressed: list[DetectedSpike] = []
        self._fixed_config = config
        self._cfg: SpikeConfig | None = config
        self._listen_start = listen_start
        self._bots: set[str] = {filters.normalise_name(name) for name in bot_names}
        self._learning: list[ChatMessage] | None = []
        self._items: list[Counted] = []
        self._loudness = LoudnessTrack()
        self._tracker = SpikeTracker()
        self._matchers: tuple[tuple[str, ...], tuple[str, ...], TokenMatcher, TokenMatcher] | None = None
        self._last_step: int | None = None
        self._last_emitted_peak: float | None = None

    @property
    def bot_names(self) -> frozenset[str]:
        """Bots known so far (given at start, named "...bot", or learned after the first 10 minutes).
        The live lane stores this in `live_sessions.bot_names`."""
        return frozenset(self._bots)

    @property
    def bots_learned(self) -> bool:
        """True once the learning period is over."""
        return self._learning is None

    def _config(self) -> SpikeConfig:
        if self._cfg is None:
            self._cfg = SpikeConfig.from_settings()
        return self._cfg

    def _token_matchers(self, cfg: SpikeConfig) -> tuple[TokenMatcher, TokenMatcher]:
        cached = self._matchers
        if cached is None or cached[0] != cfg.reaction_tokens or cached[1] != cfg.clip_tokens:
            cached = (
                cfg.reaction_tokens,
                cfg.clip_tokens,
                TokenMatcher.from_tokens(cfg.reaction_tokens),
                TokenMatcher.from_tokens(cfg.clip_tokens),
            )
            self._matchers = cached
        return cached[2], cached[3]

    def _saw(self, t: float) -> None:
        if self._listen_start is None or t < self._listen_start:
            self._listen_start = t

    def feed(self, msg: ChatMessage) -> None:
        """Take one live message. Order of arrival does not matter: the message is placed by its `t`."""
        cfg = self._config()
        self._saw(msg.t)
        name = filters.normalise_name(msg.username)
        if name.endswith(filters.BOT_NAME_SUFFIX):
            self._bots.add(name)
        if self._learning is not None:
            self._learning.append(msg)
        if name in self._bots or not filters.is_counted(msg, cfg.filter_context(self.stream_start)):
            return
        reactions, clips = self._token_matchers(cfg)
        insort(self._items, count_message(msg, reactions, clips), key=lambda item: item.t)

    def feed_loudness(self, t: float, level_db: float) -> None:
        """Take one loudness reading (dB at unix time `t`) from the live buffer, for the audio jump."""
        self._loudness.add(t, level_db)

    def _finish_learning(self, cfg: SpikeConfig, now: float) -> None:
        if self._learning is None or self._listen_start is None or now - self._listen_start < cfg.bot_learn_s:
            return
        learned = learn_bots(self._learning) - self._bots
        self._learning = None
        if learned:
            self._bots |= learned
            self._items = [
                item for item in self._items if filters.normalise_name(item.msg.username) not in learned
            ]
            log.info("live_bots_learned", platform=self.platform, bots=sorted(learned))

    def _evaluate(self, end: float, cfg: SpikeConfig) -> DetectedSpike | None:
        """Score the bucket `[end - bucket_s, end)` against its own trailing baseline."""
        back = int(math.floor(cfg.baseline_s / cfg.bucket_s + 1e-6))
        start = end - cfg.bucket_s
        buckets = make_buckets(
            self._items,
            start - back * cfg.bucket_s,
            cfg.bucket_s,
            back + 1,
            max_per_user=cfg.max_per_user,
            loudness=self._loudness if len(self._loudness) else None,
            audio_ref_s=cfg.audio_ref_s,
        )
        listen_start = self._listen_start if self._listen_start is not None else end
        coverage_start = max(self.stream_start + cfg.warmup_s, listen_start)
        baseline = baseline_for(buckets, back, cfg, coverage_start)
        if baseline is None:
            return None
        raw = self._tracker.step(score_bucket(buckets[back], baseline, cfg), cfg, cfg.min_messages)
        if raw is None:
            return None
        spike = build_spike(
            "live", raw, cfg, platform=self.platform, items=self._items, stream_start=self.stream_start
        )
        if in_cooldown(spike.peak_t, self._last_emitted_peak, cfg.merge_gap_s):
            self.suppressed.append(spike)
            log.info(
                "live_spike_suppressed", platform=self.platform, peak_t=spike.peak_t, score_z=spike.score_z
            )
            return None
        self._last_emitted_peak = spike.peak_t
        return spike

    def tick(self, now: float) -> list[DetectedSpike]:
        """Advance the sliding window to `now` (unix seconds) and return the spikes that closed.

        Steps lie on a grid of `live_step_s` anchored at `stream_start`; every step since the previous
        tick is evaluated, so a late tick loses nothing.
        """
        if self._fixed_config is None:
            self._cfg = SpikeConfig.from_settings()
        cfg = self._config()
        self._saw(now)
        self._finish_learning(cfg, now)
        newest = int(math.floor((now - self.stream_start) / cfg.live_step_s + 1e-6))
        if self._last_step is None:
            steps = [newest]
        else:
            steps = list(range(max(self._last_step + 1, newest - MAX_CATCH_UP_STEPS + 1), newest + 1))
        spikes: list[DetectedSpike] = []
        for step in steps:
            spike = self._evaluate(self.stream_start + step * cfg.live_step_s, cfg)
            if spike is not None:
                spikes.append(spike)
        if steps:
            self._last_step = newest
            keep_from = now - cfg.baseline_s - cfg.max_len_s - 2 * cfg.bucket_s
            del self._items[: bisect_left(self._items, keep_from, key=lambda item: item.t)]
            self._loudness.trim(keep_from - cfg.audio_ref_s)
        return spikes
