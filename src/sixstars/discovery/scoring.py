"""Pure scoring of discovered creators from viewer counts, growth, chat activity and past clip performance.
Implemented in M09.

The formulas are those of `docs/06-discovery.md` section 3: every part is normalised to 0..1, weighted with
`discovery.weights_live` / `discovery.weights_vod`, and the sum is scaled to 0..100. Nothing here touches
the network or the database.

How the score favours active chat (clips come from chat spikes, and the spike engine finds nothing on a
thin chat):
- Live: the `chat_rate` part uses the creator's measured chat rate when a chat of theirs was already read.
  Otherwise it is estimated from the audience (`viewers x discovery.chat_mpm_per_viewer`) instead of the
  flat 0.5 of the original design, so a small stream scores low on this part and a busy one high.
  A live stream whose audience is unknown (YouTube hides it for some small channels) scores 0 on the
  viewers and chat parts: an audience nobody can verify is not worth a recording slot.
- Videos: the chat-replay bonus (`discovery.chat_replay_bonus`) is scaled by the estimated size of the chat
  replay against `discovery.chat_min_messages`, so a past stream nobody talked in gets almost none of it.
- Videos: views that nobody reacted to count less. When `(likes + 2 x comments) / views` is below
  `discovery.passive_views_engagement` the views-per-hour part and the view-based chat estimate are scaled
  down in proportion (`activity`). This is what keeps "Shorts feed" live streams with millions of passive
  views and no comments from outranking streams with a real, talking audience.

How the score favours videos that are likely to yield clips (hardening, from the VOD lane's real runs:
13.7 hours of chat-replay streams gave one clip, one 25-minute edited upload gave six through the
transcript fallback):
- An edited upload or stream highlight (not a past live stream, at most `discovery.edited_max_duration_s`
  long) gets up to `discovery.edited_upload_bonus` points, in proportion to its engagement
  (`(likes + 2 x comments) / views` against `discovery.edited_engagement_full`). Unknown engagement gives
  half of it.
- A long past stream (at least `discovery.long_stream_min_s`) whose chat replay is estimated below
  `discovery.chat_min_messages` (the 1,500-message finding of M04) has its score multiplied by a factor
  between `discovery.thin_chat_factor` (no chat at all) and 1 (enough chat). A long upload without any
  chat replay that is too long to be an edit gets the full factor: nothing suggests it is dense.

Language: `detect_language` adds what YouTube's `defaultAudioLanguage` cannot be trusted for. Channels set
that field once and often wrongly (seen live: Hindi streams marked `en`), so the script of the title, the
channel name and the description, and a list of words that announce a language
(`discovery.non_english_terms`), can overrule a reported `en`. A stream whose metadata is entirely English
is still taken as English: only the transcript could tell, and discovery does not have one.
"""

import math
import unicodedata
from collections.abc import Sequence
from typing import Any, Final

from sixstars.discovery.config import ScoreConfig

UNKNOWN: Final = 0.5  # value of a part whose input is not known
LOG_VIEWERS_FULL: Final = 5.0  # log10(100,000 viewers or views per hour) scores 1
LOG_CHAT_FULL: Final = 3.0  # log10(1,000 messages per minute) scores 1
ENGAGEMENT_FULL: Final = 0.08  # (likes + 2 x comments) / views that scores 1
SIZE_RATIO_FULL: Final = 1.0  # views / followers that scores 1
MIN_FOLLOWERS: Final = 1000  # floor of the follower count in the size ratio
REACH_ENGLISH: Final = 1.0
REACH_OTHER: Final = 0.6
REACH_SPREAD_BONUS: Final = 0.2
SPREAD_THRESHOLD: Final = 0.5
CLAIM_PENALTY: Final = 0.5
NON_LATIN_SHARE: Final = 0.3  # default of `discovery.non_latin_share`
DESCRIPTION_CHARS: Final = 500  # how much of a description the script check reads
ALWAYS_BLOCKED: Final[tuple[str, ...]] = ("leak",)  # M09: skipped whatever the blocklist setting holds


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def log_part(value: float, full: float) -> float:
    """`min(1, log10(value + 1) / full)`, 0 for a negative value."""
    return clamp(math.log10(max(value, 0.0) + 1.0) / full)


def title_blocked(title: str, blocklist: Sequence[str]) -> bool:
    """True when the title contains a blocklist term or "leak" (case-insensitive)."""
    lowered = title.casefold()
    return any(term.casefold() in lowered for term in (*blocklist, *ALWAYS_BLOCKED) if term.strip())


def matches_terms(title: str, terms: Sequence[str]) -> bool:
    """True when the title names the game: it contains one of the search terms, ignoring case, spaces and
    punctuation ("GTA6", "gta-6" and "GTA 6" are the same)."""

    def squash(text: str) -> str:
        return "".join(char for char in text.casefold() if char.isalnum())

    squashed = squash(title)
    return any(squash(term) in squashed for term in terms if squash(term))


def non_latin_share(text: str) -> float | None:
    """Share of the letters of `text` that are not Latin script (0..1); None when it has no letters."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return None
    foreign = sum(1 for char in letters if "LATIN" not in unicodedata.name(char, ""))
    return foreign / len(letters)


def title_language(title: str, threshold: float = NON_LATIN_SHARE) -> str | None:
    """`"und"` (not English) when most letters of the title are not Latin script, else None (unknown)."""
    share = non_latin_share(title)
    return "und" if share is not None and share > threshold else None


def hashtags(text: str) -> str:
    """The hashtags of a description, joined (creators announce the language there: `#gta5hindi`)."""
    return " ".join(word for word in text.split() if word.startswith("#"))


def language_signal(
    *,
    title: str,
    channel_name: str = "",
    description: str = "",
    tags: Sequence[str] = (),
    terms: Sequence[str] = (),
    share_threshold: float = NON_LATIN_SHARE,
) -> str | None:
    """Why this video is probably not in English, or None when nothing says so.

    Two cheap signals, no API call: (1) the share of non-Latin letters in the title, the channel name or
    the start of the description is above `share_threshold` (`"script:title"`, ...); (2) the title, the
    channel name, a tag or a hashtag of the description contains one of `terms` as a substring, ignoring
    case (`"term:hindi"`). The body of the description is not searched for terms: an English creator may
    write "also in Hindi" there.
    """
    described = description[:DESCRIPTION_CHARS]
    for name, text in (("title", title), ("channel", channel_name), ("description", described)):
        share = non_latin_share(text)
        if share is not None and share > share_threshold:
            return f"script:{name}"
    haystack = " ".join([title, channel_name, *tags, hashtags(description)]).casefold()
    for term in terms:
        wanted = term.strip().casefold()
        if wanted and wanted in haystack:
            return f"term:{wanted}"
    return None


def detect_language(reported: str | None, signal: str | None) -> str | None:
    """The language discovery works with: a reported non-English language stands; a reported `en` or no
    report becomes `"und"` (not English, language unknown) when `signal` says so; otherwise the report."""
    if reported is not None and not is_english(reported):
        return reported
    return "und" if signal is not None else reported


def is_english(language: str | None) -> bool:
    """English or unknown counts as English: YouTube rarely reports the spoken language."""
    return language is None or language.casefold().split("-")[0] == "en"


def percentile(value: float, population: Sequence[float]) -> float:
    """Share of `population` at or below `value` (0..1). An empty population gives 0.5."""
    if not population:
        return UNKNOWN
    return sum(1 for other in population if other <= value) / len(population)


def history_part(avg_clip_views: float | None, population: Sequence[float], claims: int) -> float:
    """Percentile of the creator's average clip views among our creators (0.5 when unknown), minus 0.5 per
    claim against us, never below 0."""
    base = UNKNOWN if avg_clip_views is None else percentile(avg_clip_views, population)
    return max(0.0, base - CLAIM_PENALTY * max(claims, 0))


def reach_part(language: str | None, audience_spread: float | None) -> float:
    base = REACH_ENGLISH if is_english(language) else REACH_OTHER
    if audience_spread is not None and audience_spread > SPREAD_THRESHOLD:
        base += REACH_SPREAD_BONUS
    return min(1.0, base)


def growth_part(viewers_now: int | None, viewers_then: int | None) -> float:
    """`clamp(now / then - 1, 0, 1)`: doubling in the growth window scores 1. Unknown history: 0.5."""
    if viewers_now is None or viewers_then is None or viewers_then <= 0:
        return UNKNOWN
    return clamp(viewers_now / viewers_then - 1.0)


def live_chat_rate(viewers: int | None, measured_mpm: float | None, config: ScoreConfig) -> float | None:
    """Chat messages per minute of a live stream: measured for this creator, else estimated from viewers."""
    if measured_mpm is not None:
        return measured_mpm
    if viewers is None:
        return None
    return viewers * config.chat_mpm_per_viewer


def _finish(
    parts: dict[str, float],
    weights: dict[str, float],
    language: str | None,
    preferred: bool,
    bonus: float,
    config: ScoreConfig,
    factor: float = 1.0,
) -> float:
    score = 100.0 * sum(weights.get(name, 0.0) * value for name, value in parts.items()) * factor
    if not is_english(language):
        score *= config.non_english_penalty
    if preferred:
        score += config.preferred_bonus
    return round(clamp(score + bonus, 0.0, 100.0), 2)


def score_live(
    *,
    viewers: int | None,
    viewers_then: int | None,
    measured_chat_mpm: float | None,
    language: str | None,
    audience_spread: float | None,
    history: float,
    preferred: bool,
    config: ScoreConfig,
) -> tuple[float, dict[str, Any]]:
    """Trend score of a live stream and the parts it was built from."""
    chat_mpm = live_chat_rate(viewers, measured_chat_mpm, config)
    parts = {
        "viewers": 0.0 if viewers is None else log_part(viewers, LOG_VIEWERS_FULL),
        "growth": growth_part(viewers, viewers_then),
        "chat_rate": 0.0 if chat_mpm is None else log_part(chat_mpm, LOG_CHAT_FULL),
        "global_reach": reach_part(language, audience_spread),
        "history": history,
    }
    score = _finish(parts, config.weights_live, language, preferred, 0.0, config)
    detail: dict[str, Any] = {name: round(value, 4) for name, value in parts.items()}
    detail["chat_mpm"] = None if chat_mpm is None else round(chat_mpm, 2)
    detail["chat_measured"] = measured_chat_mpm is not None
    return score, detail


def audience_activity(
    views: int | None, likes: int | None, comments: int | None, config: ScoreConfig
) -> float:
    """How actively a video's audience reacted, 0..1: 1 when `(likes + 2 x comments) / views` reaches
    `discovery.passive_views_engagement`, less in proportion below it. 1 when nothing is known."""
    if views is None or views <= 0 or (likes is None and comments is None):
        return 1.0
    if config.passive_views_engagement <= 0:
        return 1.0
    ratio = ((likes or 0) + 2 * (comments or 0)) / views
    return clamp(ratio / config.passive_views_engagement)


def estimate_chat_messages(
    *,
    duration_s: int | None,
    views: int | None,
    peak_viewers: int | None,
    measured_mpm: float | None,
    config: ScoreConfig,
    activity: float = 1.0,
) -> float | None:
    """Estimated size of a finished stream's chat replay: the creator's measured rate, else the audience we
    saw while it was live, else its view count. None when nothing is known."""
    minutes = (duration_s or 0) / 60.0
    if measured_mpm is not None and minutes > 0:
        return measured_mpm * minutes
    if peak_viewers is not None and minutes > 0:
        return peak_viewers * config.chat_mpm_per_viewer * minutes
    if views is not None:
        return views * activity * config.chat_msgs_per_view
    return None


def video_kind(duration_s: int | None, has_chat: bool, config: ScoreConfig) -> str:
    """`stream` (a past live stream with a chat replay), `edited` (an upload or highlight short enough
    to be cut material), `long_upload` (no chat replay and too long to be an edit) or `unknown` (an upload
    whose length nobody reported: no adjustment)."""
    if has_chat:
        return "stream"
    if duration_s is None:
        return "unknown"
    return "edited" if duration_s <= config.edited_max_duration_s else "long_upload"


def yield_adjustment(
    *,
    kind: str,
    duration_s: int | None,
    engagement_ratio: float | None,
    chat_share: float | None,
    config: ScoreConfig,
) -> tuple[float, float]:
    """`(bonus points, score factor)` for how likely a video is to yield clips. See the module docstring.

    `engagement_ratio` is `(likes + 2 x comments) / views` (None when unknown), `chat_share` the estimated
    chat replay size against `discovery.chat_min_messages`, 0..1 (None when unknown).
    """
    if kind == "edited":
        if config.edited_engagement_full <= 0:
            return config.edited_upload_bonus, 1.0
        share = (
            UNKNOWN if engagement_ratio is None else clamp(engagement_ratio / config.edited_engagement_full)
        )
        return config.edited_upload_bonus * share, 1.0
    long_enough = duration_s is not None and duration_s >= config.long_stream_min_s
    if kind not in ("stream", "long_upload") or not long_enough:
        return 0.0, 1.0
    low = clamp(config.thin_chat_factor)
    if kind == "long_upload":
        return 0.0, low
    share = UNKNOWN if chat_share is None else clamp(chat_share)
    return 0.0, low + (1.0 - low) * share


def score_vod(
    *,
    views: int | None,
    age_hours: float | None,
    likes: int | None,
    comments: int | None,
    followers: int | None,
    duration_s: int | None,
    has_chat: bool,
    peak_viewers: int | None,
    measured_chat_mpm: float | None,
    language: str | None,
    audience_spread: float | None,
    history: float,
    preferred: bool,
    config: ScoreConfig,
) -> tuple[float, dict[str, Any], float | None]:
    """Trend score of a finished video, its parts, and its views per hour (None when unknown)."""
    vph = None if views is None or age_hours is None else views / max(age_hours, 1.0)
    ratio: float | None = None
    if views is None or views <= 0 or (likes is None and comments is None):
        engagement = UNKNOWN
    else:
        ratio = ((likes or 0) + 2 * (comments or 0)) / views
        engagement = clamp(ratio / ENGAGEMENT_FULL)
    if views is None or followers is None:
        size_ratio = UNKNOWN
    else:
        size_ratio = clamp(views / max(followers, MIN_FOLLOWERS) / SIZE_RATIO_FULL)
    activity = audience_activity(views, likes, comments, config)
    parts = {
        "views_per_hour": UNKNOWN if vph is None else log_part(vph, LOG_VIEWERS_FULL) * activity,
        "engagement": engagement,
        "size_ratio": size_ratio,
        "global_reach": reach_part(language, audience_spread),
        "history": history,
    }
    chat_messages: float | None = None
    chat_share: float | None = None
    bonus = 0.0
    if has_chat:
        chat_messages = estimate_chat_messages(
            duration_s=duration_s,
            views=views,
            peak_viewers=peak_viewers,
            measured_mpm=measured_chat_mpm,
            config=config,
            activity=activity,
        )
        if chat_messages is not None and config.chat_min_messages > 0:
            chat_share = clamp(chat_messages / config.chat_min_messages)
        bonus = config.chat_replay_bonus * (UNKNOWN if chat_share is None else chat_share)
    kind = video_kind(duration_s, has_chat, config)
    yield_bonus, factor = yield_adjustment(
        kind=kind, duration_s=duration_s, engagement_ratio=ratio, chat_share=chat_share, config=config
    )
    score = _finish(parts, config.weights_vod, language, preferred, bonus + yield_bonus, config, factor)
    detail: dict[str, Any] = {name: round(value, 4) for name, value in parts.items()}
    detail["kind"] = kind
    detail["yield_bonus"] = round(yield_bonus, 2)
    detail["yield_factor"] = round(factor, 4)
    detail["activity"] = round(activity, 4)
    detail["chat_bonus"] = round(bonus, 2)
    detail["chat_messages_est"] = None if chat_messages is None else round(chat_messages)
    detail["chat_measured"] = measured_chat_mpm is not None
    return score, detail, None if vph is None else round(vph, 2)
