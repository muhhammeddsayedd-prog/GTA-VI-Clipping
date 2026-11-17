"""Replay and live detection on synthetic chat: the milestone's acceptance tests 1-8 and the rules around
them. No network, no database: the config comes from the seed file (see conftest)."""

import random
from dataclasses import replace

import pytest

from sixstars.chat import synthetic
from sixstars.chat.models import ChatMessage, Platform
from sixstars.spikes.baseline import compute_baseline
from sixstars.spikes.buckets import Bucket, Counted
from sixstars.spikes.config import SpikeConfig
from sixstars.spikes.detector import (
    DetectedSpike,
    LiveSpikeDetector,
    SpikeTracker,
    analyse_replay,
    learn_bots,
    sample_chat,
    scan_replay,
)
from sixstars.spikes.scoring import Scored

UNIX = 1_800_000_000.0  # a stream start on the live clock


def shifted(messages: list[ChatMessage], by: float, platform: Platform = "twitch") -> list[ChatMessage]:
    return [replace(message, t=message.t + by, platform=platform) for message in messages]


def run_live(
    messages: list[ChatMessage],
    cfg: SpikeConfig,
    *,
    stream_start: float = 0.0,
    until: float | None = None,
    batch_s: float = 0.0,
) -> tuple[list[DetectedSpike], LiveSpikeDetector]:
    """Play messages through a live detector, ticking every `live_step_s`. With `batch_s`, messages reach
    the detector late and in shuffled batches (as YouTube live delivers them) and ticks lag by `batch_s`."""
    detector = LiveSpikeDetector("twitch", stream_start, config=cfg, listen_start=stream_start)
    ordered = sorted(messages, key=lambda message: message.t)
    end = (until if until is not None else ordered[-1].t + 120.0) if ordered else stream_start
    rng = random.Random(7)
    spikes: list[DetectedSpike] = []
    index = 0
    now = stream_start
    while now <= end:
        batch: list[ChatMessage] = []
        while index < len(ordered) and ordered[index].t < now:
            batch.append(ordered[index])
            index += 1
        if batch_s:
            rng.shuffle(batch)
        for message in batch:
            detector.feed(message)
        spikes += detector.tick(now - batch_s)
        now += cfg.live_step_s
    return spikes, detector


# 1 -----------------------------------------------------------------------------------------------------
def test_one_big_burst_after_flat_chat_is_one_top_tier_spike(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 120, 80))
    spikes = scan_replay(chat, config=cfg)
    assert len(spikes) == 1
    spike = spikes[0]
    assert spike.mode == "replay" and spike.tier in ("S", "A")
    assert spike.peak_t == 1200.0 and spike.first_t == 1200.0
    assert (spike.msgs, spike.uniq) == (125, 85)  # the burst plus the usual 5
    assert spike.score_z >= cfg.z_s and spike.tier == "S"
    assert set(spike.parts) == {"msgs_z", "uniq_z", "react_z", "clipreq_z", "audio_z"}
    assert spike.parts["msgs_z"] == 10.0 and spike.parts["audio_z"] == 0.0
    assert spike.reaction_count > 100 and spike.clip_requests > 0
    assert (spike.window_start, spike.spike_floor) == (1180.0, 1200.0)
    assert spike.window_end == 1215.0  # the score fell in the bucket at 1210, plus tail_s
    assert (spike.pad_start, spike.pad_end) == (1165.0, 1230.0)
    assert spike.baseline_mean["msgs"] == pytest.approx(5.0)


# 2 -----------------------------------------------------------------------------------------------------
def test_the_same_burst_from_three_users_is_not_s_tier(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 120, 3))
    spikes = scan_replay(chat, config=cfg)
    assert all(spike.tier != "S" for spike in spikes)
    assert spikes == []  # 3 users x 3 counted messages is below spikes.min_messages


def test_a_burst_from_few_users_can_be_a_spike_but_never_s(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 120, 8))
    (spike,) = scan_replay(chat, config=cfg)
    assert (spike.msgs, spike.uniq) == (29, 13)  # 3 messages per burst user at most, plus the usual 5
    assert spike.score_z >= cfg.z_s and spike.tier == "A"  # 13 < 3 x the usual unique chatters


# 3 -----------------------------------------------------------------------------------------------------
def test_a_sub_train_is_not_a_spike(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.sub_train(1200.0, 200, spread_s=60.0))
    assert scan_replay(chat, config=cfg) == []
    live, _ = run_live(chat, cfg)
    assert live == []


def test_a_named_bot_flood_is_not_a_spike(cfg: SpikeConfig) -> None:
    flood = [
        ChatMessage("twitch", 1200.0 + i * 0.1, f"id{i % 3}", f"promo{i % 3}bot", "LMAO LMAO CLIP IT", "chat")
        for i in range(100)
    ]
    assert scan_replay(synthetic.merge(synthetic.flat_chat(1500, 5), flood), config=cfg) == []


# 4 -----------------------------------------------------------------------------------------------------
def test_two_bursts_40_seconds_apart_are_one_spike(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(
        synthetic.flat_chat(1500, 5),
        synthetic.burst(1200.0, 120, 80),
        synthetic.burst(1240.0, 60, 40, seed=5, user_prefix="again"),
    )
    spikes = scan_replay(chat, config=cfg)
    assert [spike.peak_t for spike in spikes] == [1200.0]
    assert [other.peak_t for other in spikes[0].absorbed] == [1240.0]
    live, detector = run_live(chat, cfg)
    assert len(live) == 1 and abs(live[0].peak_t - 1200.0) <= cfg.bucket_s
    assert len(detector.suppressed) == 1  # the second burst fell into the cool-down


def test_two_bursts_far_apart_are_two_spikes(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(
        synthetic.flat_chat(1800, 5),
        synthetic.burst(1200.0, 120, 80),
        synthetic.burst(1400.0, 60, 40, seed=5, user_prefix="again"),
    )
    assert [spike.peak_t for spike in scan_replay(chat, config=cfg)] == [1200.0, 1400.0]


# 5 -----------------------------------------------------------------------------------------------------
def test_window_of_a_spike_whose_first_bucket_is_at_990(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(
        synthetic.flat_chat(1500, 5),
        synthetic.burst(990.0, 40, 30, seed=8, user_prefix="early"),
        synthetic.burst(1000.0, 120, 80),
    )
    (spike,) = scan_replay(chat, config=cfg)
    assert (spike.first_t, spike.peak_t) == (990.0, 1000.0)
    assert (spike.window_start, spike.spike_floor, spike.window_end) == (970.0, 990.0, 1015.0)


def test_a_long_hype_is_cut_at_the_maximum_length_and_reported_once(cfg: SpikeConfig) -> None:
    hype = [synthetic.burst(1200.0 + 10 * i, 100, 70, seed=20 + i, user_prefix=f"h{i}_") for i in range(9)]
    chat = synthetic.merge(synthetic.flat_chat(1800, 5), *hype)
    (spike,) = scan_replay(chat, config=cfg)
    assert spike.first_t == 1200.0
    assert (spike.window_start, spike.window_end) == (1180.0, 1240.0)  # max_len_s, start kept
    live, _ = run_live(chat, cfg)
    assert len(live) == 1 and live[0].window_end - live[0].window_start == cfg.max_len_s


# 6 -----------------------------------------------------------------------------------------------------
def test_a_big_steady_chat_does_not_spike_on_noise(cfg: SpikeConfig) -> None:
    rng = random.Random(11)
    chat: list[ChatMessage] = []
    for bucket in range(180):  # 30 minutes
        for _ in range(300 + rng.randint(-30, 30)):
            user = f"viewer{rng.randrange(4000)}"
            text = rng.choice(synthetic.CALM_TEXTS + synthetic.REACTION_TEXTS[:2])
            chat.append(
                ChatMessage("twitch", bucket * 10 + rng.random() * 10, f"id_{user}", user, text, "chat")
            )
    assert scan_replay(chat, config=cfg) == []
    live, _ = run_live(chat, cfg, until=1000.0)
    assert live == []


# 7 -----------------------------------------------------------------------------------------------------
def mixed_stream() -> list[ChatMessage]:
    return synthetic.merge(
        synthetic.flat_chat(3600, 8),
        synthetic.burst(900.0, 150, 100, seed=31, user_prefix="a"),
        synthetic.burst(1701.0, 90, 12, seed=32, user_prefix="b"),  # few users: A at most
        synthetic.burst(2206.0, 70, 45, seed=33, user_prefix="c"),
        synthetic.burst(3001.0, 200, 150, seed=34, user_prefix="d"),
    )


def test_live_and_replay_find_the_same_spikes(cfg: SpikeConfig) -> None:
    chat = mixed_stream()
    replay = scan_replay(chat, config=cfg)
    live, _ = run_live(shifted(chat, UNIX), cfg, stream_start=UNIX)
    assert len(replay) == 4
    assert len(live) == len(replay)
    for from_replay, from_live in zip(replay, live, strict=True):
        assert from_live.mode == "live" and from_replay.mode == "replay"
        assert abs((from_live.peak_t - UNIX) - from_replay.peak_t) <= cfg.bucket_s
        assert from_live.tier == from_replay.tier
        assert abs((from_live.window_start - UNIX) - from_replay.window_start) <= cfg.bucket_s
    assert [spike.tier for spike in replay] == ["S", "A", "S", "S"]


def test_live_handles_late_shuffled_batches_like_youtube(cfg: SpikeConfig) -> None:
    chat = shifted(mixed_stream(), UNIX, "youtube")
    in_order, _ = run_live(chat, cfg, stream_start=UNIX)
    batched, _ = run_live(chat, cfg, stream_start=UNIX, batch_s=5.0)
    assert [(s.peak_t, s.tier, s.msgs) for s in batched] == [(s.peak_t, s.tier, s.msgs) for s in in_order]


def test_live_spike_is_emitted_as_soon_as_the_score_has_fallen(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 120, 80))
    detector = LiveSpikeDetector("twitch", 0.0, config=cfg, listen_start=0.0)
    emitted_at: float | None = None
    index, now = 0, 0.0
    while now <= 1400.0 and emitted_at is None:
        while index < len(chat) and chat[index].t < now:
            detector.feed(chat[index])
            index += 1
        if detector.tick(now):
            emitted_at = now
        now += cfg.live_step_s
    assert emitted_at is not None and emitted_at <= 1200.0 + 2 * cfg.bucket_s + cfg.tail_s


def test_a_late_tick_catches_up_on_missed_steps(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 120, 80))
    detector = LiveSpikeDetector("twitch", 0.0, config=cfg, listen_start=0.0)
    for message in chat:
        if message.t < 1190.0:
            detector.feed(message)
    assert detector.tick(1190.0) == []
    for message in chat:
        if message.t >= 1190.0:
            detector.feed(message)
    spikes = detector.tick(1300.0)  # one tick, 22 steps later
    assert len(spikes) == 1 and abs(spikes[0].peak_t - 1200.0) <= cfg.bucket_s


# 8 -----------------------------------------------------------------------------------------------------
def test_no_detection_with_fewer_than_12_baseline_buckets(cfg: SpikeConfig) -> None:
    # Warm-up ends at 300 s; the 30 s before a bucket are not baseline, so the bucket at 440 has only 11.
    early = synthetic.merge(synthetic.flat_chat(900, 5), synthetic.burst(440.0, 120, 80))
    assert scan_replay(early, config=cfg) == []
    live, _ = run_live(early, cfg)
    assert live == []
    enough = synthetic.merge(synthetic.flat_chat(900, 5), synthetic.burst(450.0, 120, 80))
    assert [spike.peak_t for spike in scan_replay(enough, config=cfg)] == [450.0]


def test_warmup_messages_are_ignored(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(100.0, 300, 200))
    assert scan_replay(chat, config=cfg) == []


def test_a_live_detector_that_joins_late_waits_for_its_own_baseline(cfg: SpikeConfig) -> None:
    chat = [
        m
        for m in synthetic.merge(synthetic.flat_chat(3000, 5), synthetic.burst(2100.0, 120, 80))
        if m.t >= 2000
    ]
    detector = LiveSpikeDetector("twitch", UNIX, config=cfg)  # stream started long ago, we connect at +2000 s
    spikes: list[DetectedSpike] = []
    index, now = 0, 2000.0
    while now <= 2400.0:
        while index < len(chat) and chat[index].t < now:
            detector.feed(replace(chat[index], t=chat[index].t + UNIX))
            index += 1
        spikes += detector.tick(now + UNIX)
        now += cfg.live_step_s
    assert spikes == []  # the empty time before connecting is not a baseline of zero messages


# other rules -------------------------------------------------------------------------------------------
def test_replay_candidates_need_twice_the_whole_file_median(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(
        synthetic.flat_chat(3000, 12, users=400), synthetic.burst(1500.0, 10, 10, texts=("LMAO", "CLIP IT"))
    )
    result = analyse_replay(chat, config=cfg)
    assert result.baseline_rate == 12.0
    assert result.spikes == []  # the bucket has 22 messages: over min_messages, under 2 x the median of 12
    relaxed = analyse_replay(chat, config=replace(cfg, replay_median_multiple=1.0))
    assert [spike.peak_t for spike in relaxed.spikes] == [1500.0]


def test_thresholds_are_read_from_the_config_not_hard_coded(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 120, 80))
    assert scan_replay(chat, config=replace(cfg, z_b=9.9, z_a=9.95, z_s=9.99)) == []
    assert scan_replay(chat, config=replace(cfg, min_messages=126)) == []
    (spike,) = scan_replay(chat, config=replace(cfg, lead_s=30.0, tail_s=10.0, context_pad_s=5.0))
    assert (spike.window_start, spike.window_end, spike.pad_start) == (1170.0, 1220.0, 1165.0)
    (b_only,) = scan_replay(chat, config=replace(cfg, z_s=99.0, z_a=98.0))
    assert b_only.tier == "B"
    (wide,) = scan_replay(chat, config=replace(cfg, bucket_s=20.0))
    assert wide.peak_t == 1200.0 and wide.msgs > 120


def test_chat_offset_shifts_the_window_start_per_platform(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(
        synthetic.flat_chat(1500, 5, platform="youtube"), synthetic.burst(1200.0, 120, 80, platform="youtube")
    )
    (spike,) = scan_replay(chat, config=replace(cfg, chat_offset_s={"twitch": 0.0, "youtube": 6.0}))
    assert (spike.window_start, spike.spike_floor) == (1174.0, 1200.0)


def test_audio_jump_is_optional_and_adds_to_the_score(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(
        synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 20, 18, texts=("what", "huh"))
    )
    quiet = scan_replay(chat, config=cfg)
    loudness = [(float(t), -30.0 if not 1200 <= t < 1210 else -10.0) for t in range(0, 1500)]
    loud = scan_replay(chat, config=cfg, loudness=loudness)
    assert len(quiet) == 1 and len(loud) == 1
    assert quiet[0].parts["audio_z"] == 0.0 and loud[0].parts["audio_z"] == 10.0
    assert loud[0].score_z == pytest.approx(quiet[0].score_z + cfg.weights["audio"] * 10.0, abs=1e-3)


def test_live_audio_readings_feed_the_score(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 120, 80))
    detector = LiveSpikeDetector("twitch", 0.0, config=cfg, listen_start=0.0)
    spikes: list[DetectedSpike] = []
    index, now = 0, 0.0
    while now <= 1300.0:
        while index < len(chat) and chat[index].t < now:
            detector.feed(chat[index])
            index += 1
        for second in range(int(now - cfg.live_step_s), int(now)):
            if second >= 0:
                detector.feed_loudness(float(second), -12.0 if 1200 <= second < 1210 else -30.0)
        spikes += detector.tick(now)
        now += cfg.live_step_s
    assert len(spikes) == 1 and spikes[0].parts["audio_z"] > 5.0


def test_replay_learns_bots_from_the_whole_file(cfg: SpikeConfig) -> None:
    text = "LMAO join the giveaway now at example dot com"
    flood = [
        ChatMessage("twitch", 1200.0 + i * 0.05, f"id_spam{i % 40}", "dealsbot9000x", text, "chat")
        for i in range(200)
    ] + synthetic.bot_flood(400.0, 600.0, text=text)
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), flood)
    result = analyse_replay(chat, config=cfg)
    assert result.bot_names == {"dealsbot9000x"} and result.baseline_rate == 5.0
    assert result.spikes == []
    assert len(analyse_replay(chat, config=cfg, bot_names=[]).spikes) == 1  # without learning it would spike


def test_learn_bots_spares_a_viewer_who_repeats_a_short_emote() -> None:
    fan = [ChatMessage("twitch", float(i), "id_fan", "superfan", "KEKW", "chat") for i in range(40)]
    spam = synthetic.bot_flood(0.0, 100.0)
    named = [ChatMessage("twitch", 5.0, "id_n", "Nightbot", "hello", "chat")]
    assert learn_bots([*fan, *spam, *named]) == {"dealsbot9000x", "nightbot"}


def test_live_learns_bots_after_the_learning_period(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.bot_flood(0.0, 1500.0, every_s=1.0))
    _, detector = run_live(chat, cfg, until=590.0)
    assert not detector.bots_learned and detector.bot_names == frozenset()
    _, detector = run_live(chat, cfg, until=700.0)
    assert detector.bots_learned and detector.bot_names == {"dealsbot9000x"}
    preset = LiveSpikeDetector("twitch", 0.0, config=cfg, bot_names=["@KnownSpammer"])
    assert preset.bot_names == {"knownspammer"}


def test_empty_and_fully_filtered_input(cfg: SpikeConfig) -> None:
    assert scan_replay([], config=cfg) == []
    result = analyse_replay(synthetic.sub_train(1000.0, 50), config=cfg)
    assert (result.spikes, result.counted_messages, result.buckets) == ([], 0, 0)
    assert LiveSpikeDetector("youtube", UNIX, config=cfg).tick(UNIX + 5) == []


def test_chat_sample_has_40_messages_around_the_peak(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(synthetic.flat_chat(1500, 5), synthetic.burst(1200.0, 120, 80))
    (spike,) = scan_replay(chat, config=cfg)
    sample = spike.chat_sample
    assert len(sample) == 40
    assert all(set(entry) == {"t_rel", "user", "text"} for entry in sample)
    assert [entry["t_rel"] for entry in sample] == sorted(entry["t_rel"] for entry in sample)
    assert sum(entry["t_rel"] < 0 for entry in sample) == 10
    assert all(-30.0 <= entry["t_rel"] <= 10.0 for entry in sample)
    assert sample[-1]["user"].startswith("hype")


def test_sample_chat_caps_each_user_and_cuts_long_texts() -> None:
    def item(t: float, user: str, text: str) -> Counted:
        return Counted(t, user, False, False, ChatMessage("twitch", t, user, user, text, "chat"))

    items = [item(100.0 + i * 0.1, "spammer", "x" * 500) for i in range(30)] + [item(104.0, "other", "LMAO")]
    sample = sample_chat(items, 100.0, 40, 3)
    assert [entry["user"] for entry in sample] == ["spammer"] * 3 + ["other"]
    assert len(sample[0]["text"]) == 200 and sample[-1]["t_rel"] == 4.0


# the state machine -------------------------------------------------------------------------------------
def scored(t: float, score: float, msgs: int = 50) -> Scored:
    return Scored(Bucket(t=t, msgs=msgs, uniq=msgs), compute_baseline([], {}), {}, score)


def track(cfg: SpikeConfig, scores: list[float], msgs: int = 50) -> list[tuple[float, float, float | None]]:
    tracker = SpikeTracker()
    closed = []
    for index, score in enumerate(scores):
        raw = tracker.step(scored(index * 10.0, score, msgs), cfg, cfg.min_messages)
        if raw is not None:
            closed.append((raw.first.bucket.t, raw.peak.bucket.t, raw.end_t))
    last = tracker.flush(len(scores) * 10.0)
    if last is not None:
        closed.append((last.first.bucket.t, last.peak.bucket.t, last.end_t))
    return closed


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([0, 0, 0], []),
        ([0, 5, 0], [(10.0, 10.0, 20.0)]),
        ([0, 3, 6, 4, 0], [(10.0, 20.0, 40.0)]),  # consecutive candidates: one spike, peak at the best
        ([0, 5, 2, 0], [(10.0, 10.0, 30.0)]),  # 2.0 is not a candidate but still above end_z: stays open
        ([0, 5, 2, 7, 0], [(10.0, 30.0, 40.0)]),
        ([0, 5, 1, 7, 0], [(10.0, 10.0, 20.0), (30.0, 30.0, 40.0)]),  # fell below end_z: two spikes
        ([0, 2.4, 2.4, 0], []),  # never a candidate
        ([0, 5, 5, 5, 5, 5, 5, 5, 0, 5, 0], [(10.0, 10.0, None), (90.0, 90.0, 100.0)]),  # cut at 40 s
        ([0, 5], [(10.0, 10.0, 20.0)]),  # still open at the end of the file
    ],
)
def test_tracker_state_machine(
    cfg: SpikeConfig, scores: list[float], expected: list[tuple[float, float, float | None]]
) -> None:
    assert track(cfg, scores) == expected


def test_tracker_ignores_high_scores_with_too_few_messages(cfg: SpikeConfig) -> None:
    assert track(cfg, [0, 9, 9, 0], msgs=14) == []


def test_end_guard_drops_the_goodbye_spike_at_the_end_of_a_stream(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(
        synthetic.flat_chat(3000, 5),
        synthetic.burst(1200.0, 120, 80),
        synthetic.burst(2900.0, 120, 80, seed=9, user_prefix="bye"),
    )
    assert [spike.peak_t for spike in scan_replay(chat, config=cfg)] == [1200.0, 2900.0]
    assert [spike.peak_t for spike in scan_replay(chat, config=cfg, end_guard_s=300.0)] == [1200.0]
