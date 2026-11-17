"""Inputs and outputs of the quality gate decision (docs/10). Plain frozen dataclasses: no database, no
network. `sixstars.gate.decision.decide` is the only function that turns them into a verdict.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

HOURLY = "hourly"
EXPRESS = "express"
PROFILES = (HOURLY, EXPRESS)
LANE_SPIKE = "spike"
LANE_HOURLY = "hourly"

TIER_PREFERRED = "preferred"
TIER_AUTO = "auto"
# Decision D10: creators who forbid clipping. A clip of theirs is never approved.
FORBIDDEN_TIERS = ("restricted", "blocked")
KNOWN_TIERS = (TIER_PREFERRED, TIER_AUTO, *FORBIDDEN_TIERS)


class Verdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    HOLD = "hold"  # waits for the YouTube pre-check (clip status `precheck_pending`)


class PrecheckState(StrEnum):
    """What is known about the YouTube claim pre-check when the gate decides."""

    UNAVAILABLE = "unavailable"  # no OAuth refresh token (decision D103)
    DISABLED = "disabled"  # `precheck.enabled` or `gate.hourly.precheck_risky` is false
    QUOTA_USED = "quota_used"  # `precheck.daily_max` uploads were already made today
    AVAILABLE = "available"  # the pre-check can run now
    PASSED = "passed"  # it ran for this clip and found nothing
    CLAIMED = "claimed"  # it ran and the private copy was rejected, blocked or removed
    INCONCLUSIVE = "inconclusive"  # it ran and gave no answer


# States in which the pre-check did not and will not run for this clip: the stricter music rule applies.
PRECHECK_NOT_RUN = (PrecheckState.UNAVAILABLE, PrecheckState.DISABLED, PrecheckState.QUOTA_USED)


class ReasonCode(StrEnum):
    """Why a clip was rejected or held. The value is what `clips.reject_reason` and events carry."""

    CREATOR_FORBIDDEN = "creator_forbidden"
    CREATOR_DAILY_CAP = "creator_daily_cap"
    CREATOR_TIER_EXPRESS = "creator_tier_express"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    LENGTH_UNKNOWN = "length_unknown"
    MUSIC_UNKNOWN = "music_unknown"
    MUSIC_OVER_LIMIT = "music_over_limit"
    MUSIC_STRICT = "music_strict"
    SPOILER_BLOCKED = "spoiler_blocked"
    SPOILER_IN_TEXT = "spoiler_in_text"
    SAFETY_FLAG = "safety_flag"
    SAFETY_UNKNOWN = "safety_unknown"
    REJECT_TOPIC = "reject_topic"
    WATERMARK = "watermark"
    PLATFORM_LOGO = "platform_logo"
    MENU_OR_LOADING = "menu_or_loading"
    PAUSE_SCREEN = "pause_screen"
    EMPTY_FACECAM = "empty_facecam"
    UNREADABLE_SUBTITLES = "unreadable_subtitles"
    VISUAL_UNVERIFIED = "visual_unverified"
    BLACK_FRAMES = "black_frames"
    FROZEN_VIDEO = "frozen_video"
    SUBTITLE_SYNC = "subtitle_sync"
    SCORE_TOO_LOW = "score_too_low"
    SCORE_UNKNOWN = "score_unknown"
    DUPLICATE_TOPIC = "duplicate_topic"
    DUPLICATE_TRANSCRIPT = "duplicate_transcript"
    DUPLICATE_SOURCE_RANGE = "duplicate_source_range"
    FILE_MISSING = "file_missing"
    FILE_RESOLUTION = "file_resolution"
    FILE_NO_AUDIO = "file_no_audio"
    FILE_DURATION = "file_duration"
    FILE_TOO_LARGE = "file_too_large"
    PRECHECK_REQUIRED = "precheck_required"  # the only code of a `hold`
    PRECHECK_CLAIM = "precheck_claim"
    PRECHECK_INCONCLUSIVE = "precheck_inconclusive"
    GATE_ERROR = "gate_error"  # set by the job layer when the gate itself kept failing; never by `decide`


@dataclass(frozen=True, slots=True)
class FileFacts:
    """What ffprobe and ffmpeg measured on the rendered file. None for a value means "not measured", which
    fails its check."""

    width: int
    height: int
    has_video: bool
    has_audio: bool
    duration_s: float | None  # whole file, end card included
    size_bytes: int | None
    longest_black_s: float | None  # longest black stretch inside the clip part (blackdetect)
    longest_freeze_s: float | None  # longest frozen stretch inside the clip part (freezedetect)


@dataclass(frozen=True, slots=True)
class VisualFindings:
    """The P5 vision verdict on frames of the rendered file."""

    other_watermark: bool
    platform_logos: bool
    menu_or_loading: bool
    pause_or_brb_screen: bool
    empty_facecam: bool
    readable_subtitles: bool
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ClipFacts:
    """The fields of one `clips` row the gate reads, plus the measurements of its file."""

    id: str
    creator_id: str
    profile: str  # `clips.gate_profile`: "hourly" or "express"
    lane: str  # `clips.lane`: "spike" or "hourly"
    source_platform: str
    source_id: str
    start_s: float  # position in the original video or stream
    end_s: float
    duration_s: float | None  # clip length without the end card
    score: float | None
    music_ratio: float | None  # None: the detector could not run
    spoiler: bool | None
    safety_flags: Mapping[str, object] | None  # the P2 object; None: P2 gave none
    hook: str | None
    title: str | None
    caption: str | None
    hashtags: tuple[str, ...]
    topic: str | None
    transcript_text: str  # the words of the clip joined by spaces
    word_spans: tuple[tuple[float, float], ...]  # (start, end) of every subtitle word, clip time
    created_at: datetime
    file: FileFacts | None  # None: there is no file to measure
    music_frames: tuple[bool, ...] | None = None  # the detector's per-second flags, when known
    subtitle_drift_s: float | None = None  # measured drift, when a second transcription exists
    is_compilation: bool = False
    commentary: str | None = None  # P2's optional on-screen remark


@dataclass(frozen=True, slots=True)
class CreatorFacts:
    """The creator of the clip. `clips_today` counts this creator's other clips that were posted today
    (UTC) or are approved and waiting; `prior_clips` counts their other clips ever approved or posted."""

    id: str
    tier: str
    clips_today: int = 0
    prior_clips: int = 0


@dataclass(frozen=True, slots=True)
class RecentClip:
    """Another clip, for the duplicate checks. `decided_at` is when it was approved or posted (its creation
    time when neither is known)."""

    id: str
    creator_id: str
    status: str
    source_platform: str
    source_id: str
    start_s: float
    end_s: float
    topic: str | None
    transcript_text: str
    decided_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    """One gate profile (`gate.hourly` or `gate.express`)."""

    name: str
    min_len_s: float
    max_len_s: float
    max_music_ratio: float
    min_score: float
    topic_dedupe_h: float
    # Express only: which creators may post at once. Tokens: "preferred", "auto", "auto_no_music".
    tiers: tuple[str, ...] = ()
    # Hourly only: send risky clips to the YouTube pre-check.
    precheck_risky: bool = True


@dataclass(frozen=True, slots=True)
class GateSettings:
    """Every number and list the gate uses, read from the `settings` table (see `gate.profiles`)."""

    hourly: ProfileLimits
    express: ProfileLimits
    spoiler_block_until: datetime | None
    spoiler_terms: tuple[str, ...]
    reject_topics: tuple[str, ...]
    auto_creator_daily_cap: int
    strict_max_music_ratio: float  # music limit of a risky hourly clip when the pre-check cannot run
    music_min_run_frames: int  # flagged frames in a shorter run are ignored; 1 counts every frame
    max_black_s: float
    max_freeze_s: float
    freeze_noise: float  # freezedetect tolerance on the whole frame (docs/10)
    band_freeze_noise: float  # the same inside the gameplay band (tighter)
    band_black_pixel: float  # blackdetect pixel level inside the gameplay band (tighter)
    max_subtitle_drift_s: float
    source_pad_s: float
    max_file_mb: float
    width: int
    height: int
    duration_tolerance_s: float
    end_card_s: float
    topic_overlap_same_creator: float
    topic_overlap_any_creator: float
    transcript_overlap: float

    def profile(self, name: str) -> ProfileLimits:
        if name == HOURLY:
            return self.hourly
        if name == EXPRESS:
            return self.express
        raise ValueError(f"unknown gate profile {name!r}")


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one check. `codes` and `reasons` are parallel and empty when `ok`."""

    ok: bool
    value: object = None
    limit: object = None
    codes: tuple[ReasonCode, ...] = ()
    reasons: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        data: dict[str, object] = {"ok": self.ok, "value": self.value}
        if self.limit is not None:
            data["limit"] = self.limit
        if self.codes:
            data["codes"] = [code.value for code in self.codes]
            data["reasons"] = list(self.reasons)
        return data


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The verdict on one clip.

    `codes` and `reasons` are parallel: empty for an approval, the failed rules for a rejection,
    `precheck_required` for a hold. `profile` and `lane` are what to store on the clip: an express clip
    that only failed express-only rules is approved (or held) with profile and lane `hourly`, `demoted`
    true and the express failures in `express_codes`. `checks` holds every check of `profile`, in the order
    of docs/10; `express_checks` the express evaluation of a demoted clip.
    """

    verdict: Verdict
    profile: str
    lane: str
    codes: tuple[ReasonCode, ...]
    reasons: tuple[str, ...]
    checks: Mapping[str, CheckResult]
    precheck: PrecheckState
    demoted: bool = False
    express_codes: tuple[ReasonCode, ...] = ()
    express_checks: Mapping[str, CheckResult] = field(default_factory=dict)
    # The hourly failures of an express clip that could not fall back (empty otherwise).
    fallback_codes: tuple[ReasonCode, ...] = ()
    risk_reasons: tuple[str, ...] = ()  # why the clip is risky for copyright (empty: not risky)

    @property
    def approved(self) -> bool:
        return self.verdict is Verdict.APPROVE

    @property
    def reject_reason(self) -> str | None:
        """The first failed rule, as stored in `clips.reject_reason` (None unless rejected)."""
        return self.codes[0].value if self.verdict is Verdict.REJECT and self.codes else None

    def as_json(self) -> dict[str, object]:
        """The `clips.gate_result` document (docs/10): profile, passed, checks, plus the verdict detail."""
        data: dict[str, object] = {
            "profile": self.profile,
            "passed": self.approved,
            "verdict": self.verdict.value,
            "lane": self.lane,
            "codes": [code.value for code in self.codes],
            "reasons": list(self.reasons),
            "checks": {name: check.as_json() for name, check in self.checks.items()},
            "precheck": self.precheck.value,
            "risk": list(self.risk_reasons),
            "demoted": self.demoted,
        }
        if self.express_checks:
            data["express"] = {
                "passed": not self.express_codes,
                "codes": [code.value for code in self.express_codes],
                "checks": {name: check.as_json() for name, check in self.express_checks.items()},
            }
        if self.fallback_codes:
            data["hourly_fallback"] = {"passed": False, "codes": [code.value for code in self.fallback_codes]}
        return data
