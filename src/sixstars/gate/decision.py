"""The quality gate decision (docs/10): one pure function, no database, no network, no clock.

    decide(clip, creator, recent, settings, visual, precheck, now) -> GateDecision

The verdict is `approve`, `reject` or `hold` (wait for the YouTube pre-check). A rejection lists every
broken rule as a reason code with a sentence each, in the order of the docs/10 table; the first one is
`clips.reject_reason`. Nothing here is assumed: a missing score, music ratio, safety object, file or visual
verdict rejects.

Express fallback (docs/10 section 2): an express clip is first judged by the express profile. If it fails
and passes the whole hourly profile, it is approved (or held) as an hourly clip with `demoted` true and
lane `hourly`. If it fails the hourly profile too it is rejected with the express failures.

`control.emergency_stop` is not an input: docs/16 stops posting only, rendering and the gate continue, and
the posting engine refuses to post while the stop is set.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from sixstars.gate import checks
from sixstars.gate.models import (
    EXPRESS,
    HOURLY,
    LANE_HOURLY,
    PROFILES,
    CheckResult,
    ClipFacts,
    CreatorFacts,
    GateDecision,
    GateSettings,
    PrecheckState,
    ProfileLimits,
    ReasonCode,
    RecentClip,
    Verdict,
    VisualFindings,
)

Evaluation = tuple[dict[str, CheckResult], tuple[str, ...]]


def evaluate(
    limits: ProfileLimits,
    clip: ClipFacts,
    creator: CreatorFacts,
    recent: Sequence[RecentClip],
    settings: GateSettings,
    visual: VisualFindings | None,
    precheck: PrecheckState,
    now: datetime,
) -> Evaluation:
    """Every check of one profile, in the order of docs/10, and the clip's copyright risk reasons."""
    music = checks.effective_music_ratio(clip.music_ratio, clip.music_frames, settings.music_min_run_frames)
    precheck_result, risks = checks.check_precheck(clip, creator, limits, settings, music, precheck)
    results = {
        "creator": checks.check_creator(creator, limits, settings, music),
        "length": checks.check_length(clip.duration_s, limits),
        "music": checks.check_music(music, limits),
        "spoiler": checks.check_spoiler(clip.spoiler, settings.spoiler_block_until, now),
        "spoiler_text": checks.check_spoiler_text(clip, settings.spoiler_terms),
        "safety": checks.check_safety(clip.safety_flags),
        "reject_topics": checks.check_reject_topics(clip, settings.reject_topics),
        "watermark": checks.check_watermark(visual),
        "visual": checks.check_visual(visual, clip.file, settings),
        "subtitle_sync": checks.check_subtitle_sync(clip, settings),
        "score": checks.check_score(clip.score, limits),
        "duplicate_topic": checks.check_duplicate_topic(clip, recent, limits, settings, now),
        "source_range": checks.check_source_range(clip, recent, settings),
        "file": checks.check_file(clip, settings),
        "precheck": precheck_result,
    }
    return results, risks


def _failures(results: dict[str, CheckResult]) -> tuple[tuple[ReasonCode, ...], tuple[str, ...]]:
    """All failed rules of an evaluation. `precheck_required` only stands when nothing else failed: a clip
    that is rejected anyway is not sent to the pre-check."""
    pairs = [
        (code, reason)
        for check in results.values()
        for code, reason in zip(check.codes, check.reasons, strict=True)
    ]
    if any(code is not ReasonCode.PRECHECK_REQUIRED for code, _ in pairs):
        pairs = [pair for pair in pairs if pair[0] is not ReasonCode.PRECHECK_REQUIRED]
    return tuple(code for code, _ in pairs), tuple(reason for _, reason in pairs)


def _verdict(codes: tuple[ReasonCode, ...]) -> Verdict:
    if not codes:
        return Verdict.APPROVE
    if codes == (ReasonCode.PRECHECK_REQUIRED,):
        return Verdict.HOLD
    return Verdict.REJECT


def decide(
    clip: ClipFacts,
    creator: CreatorFacts,
    recent: Sequence[RecentClip],
    settings: GateSettings,
    visual: VisualFindings | None,
    precheck: PrecheckState,
    now: datetime,
) -> GateDecision:
    """Approve, reject or hold one rendered clip.

    - `clip`: the fields of its `clips` row and the measurements of its file.
    - `creator`: its creator's tier and clip counts.
    - `recent`: other clips, for the duplicate checks (the clip itself is ignored if listed).
    - `settings`: the gate settings.
    - `visual`: the P5 verdict on frames of the rendered file; None rejects (`visual_unverified`).
    - `precheck`: the state of the YouTube pre-check (see `PrecheckState`).
    - `now`: the current time, timezone-aware.

    The same inputs always give the same decision. Raises `ValueError` for a naive `now` or an unknown
    `clip.profile`.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (UTC)")
    if clip.profile not in PROFILES:
        raise ValueError(f"unknown gate profile {clip.profile!r}")
    now = now.astimezone(UTC)
    args = (clip, creator, recent, settings, visual, precheck, now)

    if clip.profile == HOURLY:
        results, risks = evaluate(settings.hourly, *args)
        codes, reasons = _failures(results)
        return GateDecision(
            verdict=_verdict(codes),
            profile=HOURLY,
            lane=clip.lane,
            codes=codes,
            reasons=reasons,
            checks=results,
            precheck=precheck,
            risk_reasons=risks,
        )

    express_results, _ = evaluate(settings.express, *args)
    express_codes, express_reasons = _failures(express_results)
    if not express_codes:
        return GateDecision(
            verdict=Verdict.APPROVE,
            profile=EXPRESS,
            lane=clip.lane,
            codes=(),
            reasons=(),
            checks=express_results,
            precheck=precheck,
        )
    hourly_results, risks = evaluate(settings.hourly, *args)
    hourly_codes, hourly_reasons = _failures(hourly_results)
    hourly_verdict = _verdict(hourly_codes)
    if hourly_verdict is Verdict.REJECT:
        return GateDecision(
            verdict=Verdict.REJECT,
            profile=EXPRESS,
            lane=clip.lane,
            codes=express_codes,
            reasons=express_reasons,
            checks=express_results,
            precheck=precheck,
            fallback_codes=hourly_codes,
        )
    return GateDecision(
        verdict=hourly_verdict,
        profile=HOURLY,
        lane=LANE_HOURLY,
        codes=hourly_codes,
        reasons=hourly_reasons,
        checks=hourly_results,
        precheck=precheck,
        demoted=True,
        express_codes=express_codes,
        express_checks=express_results,
        risk_reasons=risks,
    )
