"""The gate checks of docs/10, one pure function per check. Each returns a `CheckResult` with the measured
value, the limit and, when it fails, one reason code and one sentence per broken rule.

Rules that are read strictly here (see the M07 progress notes):
- a value that is missing or was not measured fails its check, it is never assumed to be fine;
- limits are inclusive where docs/10 says "<=" or gives a range, and a duration "of 2 s or more" fails.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from sixstars.gate import text
from sixstars.gate.models import (
    EXPRESS,
    FORBIDDEN_TIERS,
    PRECHECK_NOT_RUN,
    PREFERRED_LIKE,
    TIER_AUTO,
    CheckResult,
    ClipFacts,
    CreatorFacts,
    FileFacts,
    GateSettings,
    PrecheckState,
    ProfileLimits,
    ReasonCode,
    RecentClip,
    VisualFindings,
)

EPS = 1e-9
BYTES_PER_MB = 1_000_000
# Statuses of another clip that make its topic or its words "already used".
TOPIC_STATUSES = ("approved", "posted", "precheck_pending", "removed")
# Statuses of another clip that never block a source range: it was thrown away, or has no final cut yet.
SOURCE_IGNORED_STATUSES = ("rejected", "rendering")
SOURCE_UNDECIDED_STATUS = "gating"

RISK_FIRST_CLIP = "first_clip_of_auto_creator"
RISK_MUSIC = "music"
RISK_COMPILATION = "compilation"
RISK_COPYRIGHTED_MEDIA = "copyrighted_media"

_Failures = list[tuple[ReasonCode, str]]


def _result(failures: _Failures, value: object = None, limit: object = None) -> CheckResult:
    return CheckResult(
        ok=not failures,
        value=value,
        limit=limit,
        codes=tuple(code for code, _ in failures),
        reasons=tuple(reason for _, reason in failures),
    )


def effective_music_ratio(
    ratio: float | None, frames: Sequence[bool] | None, min_run_frames: int
) -> float | None:
    """The music ratio the limits are compared with.

    None stays None (the detector did not run). With `min_run_frames` above 1 and the per-second flags
    known, flagged frames in a run shorter than that are left out; the result is never above `ratio`.
    """
    if ratio is None:
        return None
    if min_run_frames <= 1 or not frames:
        return ratio
    counted = 0
    run = 0
    for flagged in [*frames, False]:
        if flagged:
            run += 1
            continue
        if run >= min_run_frames:
            counted += run
        run = 0
    return min(ratio, counted / len(frames))


def check_creator(
    creator: CreatorFacts, limits: ProfileLimits, settings: GateSettings, music: float | None
) -> CheckResult:
    """D10 and the tier row of docs/10. A forbidden or unknown tier fails in every profile."""
    tier = creator.tier
    failures: _Failures = []
    limit: object = None
    if tier in FORBIDDEN_TIERS or tier not in (*PREFERRED_LIKE, TIER_AUTO):
        failures.append(
            (ReasonCode.CREATOR_FORBIDDEN, f"creator tier is {tier or 'unknown'!r}: never clipped (D10)")
        )
    elif limits.name == EXPRESS:
        limit = list(limits.tiers)
        allowed = (tier in PREFERRED_LIKE and "preferred" in limits.tiers) or (
            tier == TIER_AUTO and ("auto" in limits.tiers or ("auto_no_music" in limits.tiers and music == 0))
        )
        if not allowed:
            failures.append(
                (
                    ReasonCode.CREATOR_TIER_EXPRESS,
                    f"creator tier {tier!r} with music ratio {music} may not post at once "
                    f"(allowed: {', '.join(limits.tiers) or 'none'})",
                )
            )
    elif tier == TIER_AUTO:
        limit = settings.auto_creator_daily_cap
        if creator.clips_today >= settings.auto_creator_daily_cap:
            failures.append(
                (
                    ReasonCode.CREATOR_DAILY_CAP,
                    f"auto-tier creator already has {creator.clips_today} clips today "
                    f"(cap {settings.auto_creator_daily_cap})",
                )
            )
    return _result(failures, {"tier": tier, "clips_today": creator.clips_today}, limit)


def check_length(duration_s: float | None, limits: ProfileLimits) -> CheckResult:
    failures: _Failures = []
    if duration_s is None:
        failures.append((ReasonCode.LENGTH_UNKNOWN, "the clip has no length"))
    elif duration_s < limits.min_len_s:
        failures.append(
            (ReasonCode.TOO_SHORT, f"clip is {duration_s:g} s, the minimum is {limits.min_len_s:g} s")
        )
    elif duration_s > limits.max_len_s:
        failures.append(
            (ReasonCode.TOO_LONG, f"clip is {duration_s:g} s, the maximum is {limits.max_len_s:g} s")
        )
    return _result(failures, duration_s, [limits.min_len_s, limits.max_len_s])


def check_music(music: float | None, limits: ProfileLimits) -> CheckResult:
    """`music` is the effective ratio. Unknown is a failure, never "no music"."""
    failures: _Failures = []
    if music is None:
        failures.append((ReasonCode.MUSIC_UNKNOWN, "the music detector did not run: music ratio unknown"))
    elif music > limits.max_music_ratio:
        failures.append(
            (
                ReasonCode.MUSIC_OVER_LIMIT,
                f"music ratio {music:.4g} is above the {limits.name} limit {limits.max_music_ratio:g}",
            )
        )
    return _result(failures, None if music is None else round(music, 4), limits.max_music_ratio)


def check_spoiler(spoiler: bool | None, block_until: datetime | None, now: datetime) -> CheckResult:
    """D20: a story spoiler is blocked while `now` is before `gate.spoiler_block_until`. A missing flag
    counts as a spoiler."""
    failures: _Failures = []
    blocking = block_until is not None and now < block_until
    if blocking and spoiler is not False:
        what = "story spoiler" if spoiler else "spoiler flag missing"
        failures.append(
            (ReasonCode.SPOILER_BLOCKED, f"{what}: blocked until {block_until:%Y-%m-%d %H:%M} UTC")
        )
    return _result(failures, spoiler, block_until.isoformat() if block_until else None)


def _written_fields(clip: ClipFacts) -> list[tuple[str, str | None]]:
    fields = [("hook", clip.hook), ("title", clip.title), ("caption", clip.caption)]
    return [*fields, ("commentary", clip.commentary)]


def check_spoiler_text(clip: ClipFacts, terms: Sequence[str]) -> CheckResult:
    """D20: hook, title, caption, commentary and hashtags never contain a term of `gate.spoiler_terms`,
    whatever the date and whatever the model said."""
    hits = text.find_terms(_written_fields(clip), clip.hashtags, terms)
    failures: _Failures = (
        [(ReasonCode.SPOILER_IN_TEXT, "spoiler term in the written text: " + "; ".join(hits))] if hits else []
    )
    return _result(failures, hits)


def check_safety(flags: Mapping[str, object] | None) -> CheckResult:
    """D21: every P2 safety flag must be false. A missing object or a value that is not a boolean fails."""
    failures: _Failures = []
    if not isinstance(flags, Mapping):
        failures.append((ReasonCode.SAFETY_UNKNOWN, "the clip has no safety flags"))
        return _result(failures, None)
    flagged = sorted(str(name) for name, value in flags.items() if value is True)
    malformed = sorted(str(name) for name, value in flags.items() if not isinstance(value, bool))
    if flagged:
        failures.append((ReasonCode.SAFETY_FLAG, "safety flags set: " + ", ".join(flagged)))
    if malformed:
        failures.append(
            (ReasonCode.SAFETY_UNKNOWN, "safety flags without a yes or no: " + ", ".join(malformed))
        )
    return _result(failures, flagged)


def check_reject_topics(clip: ClipFacts, topics: Sequence[str]) -> CheckResult:
    """D21 by keyword: no `gate.reject_topics` entry in the topic, hook, title, caption, commentary or
    hashtags."""
    fields = [("topic", clip.topic), *_written_fields(clip)]
    hits = text.find_terms(fields, clip.hashtags, topics)
    failures: _Failures = (
        [(ReasonCode.REJECT_TOPIC, "rejected topic in the written text: " + "; ".join(hits))] if hits else []
    )
    return _result(failures, hits)


def _note(visual: VisualFindings) -> str:
    return f" ({visual.notes.strip()})" if visual.notes.strip() else ""


def check_watermark(visual: VisualFindings | None) -> CheckResult:
    """P5: no other account's watermark, no platform logo. No verdict means no approval."""
    failures: _Failures = []
    if visual is None:
        failures.append((ReasonCode.VISUAL_UNVERIFIED, "the visual check did not run"))
        return _result(failures, None)
    if visual.other_watermark:
        failures.append(
            (ReasonCode.WATERMARK, "another account's watermark is in the picture" + _note(visual))
        )
    if visual.platform_logos:
        failures.append(
            (ReasonCode.PLATFORM_LOGO, "a platform logo or follow screen is in the picture" + _note(visual))
        )
    return _result(
        failures, {"other_watermark": visual.other_watermark, "platform_logos": visual.platform_logos}
    )


def check_visual(
    visual: VisualFindings | None, file: FileFacts | None, settings: GateSettings
) -> CheckResult:
    """ffmpeg black and freeze detection plus the P5 findings on menus, pause screens, the facecam and the
    subtitles. A missing file or verdict is reported by the file and watermark checks."""
    failures: _Failures = []
    value: dict[str, object] = {}
    if file is not None:
        value["longest_black_s"] = file.longest_black_s
        value["longest_freeze_s"] = file.longest_freeze_s
        if file.longest_black_s is None or file.longest_black_s >= settings.max_black_s - EPS:
            failures.append(
                (
                    ReasonCode.BLACK_FRAMES,
                    f"black picture for {file.longest_black_s} s (limit: below {settings.max_black_s:g})",
                )
            )
        if file.longest_freeze_s is None or file.longest_freeze_s >= settings.max_freeze_s - EPS:
            failures.append(
                (
                    ReasonCode.FROZEN_VIDEO,
                    f"frozen picture for {file.longest_freeze_s} s (limit: below {settings.max_freeze_s:g})",
                )
            )
    if visual is not None:
        findings = (
            (visual.menu_or_loading, ReasonCode.MENU_OR_LOADING, "menu or loading screen"),
            (visual.pause_or_brb_screen, ReasonCode.PAUSE_SCREEN, "pause or be-right-back screen"),
            (visual.empty_facecam, ReasonCode.EMPTY_FACECAM, "the facecam panel is empty"),
            (not visual.readable_subtitles, ReasonCode.UNREADABLE_SUBTITLES, "subtitles are not readable"),
        )
        for found, code, sentence in findings:
            value[code.value] = bool(found)
            if found:
                failures.append((code, sentence + _note(visual)))
    return _result(
        failures, value, {"max_black_s": settings.max_black_s, "max_freeze_s": settings.max_freeze_s}
    )


def check_subtitle_sync(clip: ClipFacts, settings: GateSettings) -> CheckResult:
    """Subtitle timing. The subtitles are drawn from the stored transcript, so what can be checked on the
    row is that every word lies inside the clip (within the drift limit); a measured drift, when a second
    transcription provides one, must be within the limit too."""
    limit = settings.max_subtitle_drift_s
    failures: _Failures = []
    if not clip.word_spans:
        failures.append((ReasonCode.SUBTITLE_SYNC, "the clip has no subtitle words"))
    else:
        bad = 0
        for start, end in clip.word_spans:
            outside_end = clip.duration_s is not None and end > clip.duration_s + limit + EPS
            if end < start or start < -limit - EPS or outside_end:
                bad += 1
        if bad:
            failures.append((ReasonCode.SUBTITLE_SYNC, f"{bad} subtitle words lie outside the clip"))
    drift = clip.subtitle_drift_s
    if drift is not None and abs(drift) > limit + EPS:
        failures.append((ReasonCode.SUBTITLE_SYNC, f"subtitle drift {abs(drift):.2f} s is above {limit:g} s"))
    return _result(failures[:1], {"drift_s": drift, "words": len(clip.word_spans)}, limit)


def check_score(score: float | None, limits: ProfileLimits) -> CheckResult:
    failures: _Failures = []
    if score is None:
        failures.append((ReasonCode.SCORE_UNKNOWN, "the clip has no score"))
    elif score < limits.min_score:
        failures.append(
            (
                ReasonCode.SCORE_TOO_LOW,
                f"score {score:g} is below the {limits.name} minimum {limits.min_score:g}",
            )
        )
    return _result(failures, score, limits.min_score)


def check_duplicate_topic(
    clip: ClipFacts,
    recent: Sequence[RecentClip],
    limits: ProfileLimits,
    settings: GateSettings,
    now: datetime,
) -> CheckResult:
    """No clip about the same thing approved or posted inside the profile's window.

    "The same thing" is: an equal topic; topics of the same creator sharing at least
    `gate.duplicate.topic_overlap_same_creator` of the shorter one's words; topics of any creator with a
    Jaccard word overlap of at least `topic_overlap_any_creator`; or transcripts sharing at least
    `transcript_overlap` of the shorter one's three-word sequences (the same speech from another source).
    """
    window = timedelta(hours=limits.topic_dedupe_h)
    mine = text.topic_tokens(clip.topic)
    failures: _Failures = []
    matches: list[str] = []
    for other in recent:
        if other.id == clip.id or other.status not in TOPIC_STATUSES or now - other.decided_at > window:
            continue
        theirs = text.topic_tokens(other.topic)
        same_creator = other.creator_id == clip.creator_id
        topic_hit = (
            text.same_topic(clip.topic, other.topic)
            or (
                same_creator
                and text.overlap_coefficient(mine, theirs) >= settings.topic_overlap_same_creator - EPS > 0
            )
            or text.jaccard(mine, theirs) >= settings.topic_overlap_any_creator - EPS > 0
        )
        if topic_hit:
            matches.append(other.id)
            if not any(code is ReasonCode.DUPLICATE_TOPIC for code, _ in failures):
                failures.append(
                    (
                        ReasonCode.DUPLICATE_TOPIC,
                        f"topic {clip.topic!r} repeats {other.topic!r} of clip {other.id} "
                        f"within {limits.topic_dedupe_h:g} h",
                    )
                )
            continue
        overlap = text.transcript_overlap(clip.transcript_text, other.transcript_text)
        if overlap >= settings.transcript_overlap - EPS > 0:
            matches.append(other.id)
            if not any(code is ReasonCode.DUPLICATE_TRANSCRIPT for code, _ in failures):
                failures.append(
                    (
                        ReasonCode.DUPLICATE_TRANSCRIPT,
                        f"{overlap:.0%} of the words repeat clip {other.id}",
                    )
                )
    return _result(failures, matches, limits.topic_dedupe_h)


def _older(other: RecentClip, clip: ClipFacts) -> bool:
    return (other.created_at, other.id) < (clip.created_at, clip.id)


def check_source_range(clip: ClipFacts, recent: Sequence[RecentClip], settings: GateSettings) -> CheckResult:
    """No other clip of the same video within `gate.duplicate.source_pad_s` of this clip's range, at any
    age. Rejected and unfinished clips do not count; of two clips still in the gate the older one wins."""
    pad = settings.source_pad_s
    matches: list[str] = []
    for other in recent:
        if other.id == clip.id or other.status in SOURCE_IGNORED_STATUSES:
            continue
        if other.status == SOURCE_UNDECIDED_STATUS and not _older(other, clip):
            continue
        if (other.source_platform, other.source_id) != (clip.source_platform, clip.source_id):
            continue
        if other.end_s + pad >= clip.start_s - EPS and other.start_s - pad <= clip.end_s + EPS:
            matches.append(other.id)
    failures: _Failures = []
    if matches:
        failures.append(
            (
                ReasonCode.DUPLICATE_SOURCE_RANGE,
                f"clip {matches[0]} covers the same part of {clip.source_platform} {clip.source_id} "
                f"(within {pad:g} s)",
            )
        )
    return _result(failures, matches, pad)


def check_file(clip: ClipFacts, settings: GateSettings) -> CheckResult:
    """The rendered file: it exists, is `width` x `height` with audio, is as long as the clip plus the end
    card (within the tolerance) and is not above `max_file_mb` megabytes (10^6 bytes)."""
    file = clip.file
    limit = {
        "width": settings.width,
        "height": settings.height,
        "max_file_mb": settings.max_file_mb,
        "duration_tolerance_s": settings.duration_tolerance_s,
    }
    if file is None or file.size_bytes == 0:
        return _result([(ReasonCode.FILE_MISSING, "the rendered file is missing or empty")], None, limit)
    failures: _Failures = []
    if not file.has_video or (file.width, file.height) != (settings.width, settings.height):
        failures.append(
            (
                ReasonCode.FILE_RESOLUTION,
                f"picture is {file.width}x{file.height}, expected {settings.width}x{settings.height}",
            )
        )
    if not file.has_audio:
        failures.append((ReasonCode.FILE_NO_AUDIO, "the file has no audio"))
    if file.size_bytes is None or file.size_bytes > settings.max_file_mb * BYTES_PER_MB:
        failures.append(
            (
                ReasonCode.FILE_TOO_LARGE,
                f"file size {file.size_bytes} bytes (limit {settings.max_file_mb:g} MB)",
            )
        )
    expected = None if clip.duration_s is None else clip.duration_s + settings.end_card_s
    if file.duration_s is None or (
        expected is not None and abs(file.duration_s - expected) > settings.duration_tolerance_s + EPS
    ):
        failures.append(
            (ReasonCode.FILE_DURATION, f"file lasts {file.duration_s} s, expected about {expected} s")
        )
    value = {
        "width": file.width,
        "height": file.height,
        "has_audio": file.has_audio,
        "duration_s": file.duration_s,
        "size_bytes": file.size_bytes,
    }
    return _result(failures, value, limit)


def risk_reasons(clip: ClipFacts, creator: CreatorFacts, music: float | None) -> tuple[str, ...]:
    """Why a clip is risky for a copyright claim (docs/10 section 3); empty when it is not."""
    reasons: list[str] = []
    if creator.tier == TIER_AUTO and creator.prior_clips == 0:
        reasons.append(RISK_FIRST_CLIP)
    if music is None or music > 0:
        reasons.append(RISK_MUSIC)
    if clip.is_compilation:
        reasons.append(RISK_COMPILATION)
    if isinstance(clip.safety_flags, Mapping) and clip.safety_flags.get("copyrighted_media") is True:
        reasons.append(RISK_COPYRIGHTED_MEDIA)
    return tuple(reasons)


def check_precheck(
    clip: ClipFacts,
    creator: CreatorFacts,
    limits: ProfileLimits,
    settings: GateSettings,
    music: float | None,
    state: PrecheckState,
) -> tuple[CheckResult, tuple[str, ...]]:
    """The YouTube claim pre-check and its replacement (docs/10 section 3, decision D103).

    - A pre-check that ran and found a claim, or gave no answer, fails for every clip.
    - Express clips are not pre-checked. Hourly clips that are not risky need none.
    - A risky hourly clip passes after a passed pre-check, and is held (`precheck_required`) while the
      pre-check can run.
    - When the pre-check cannot run (no OAuth token, switched off, daily quota used) the stricter music
      rule applies: the clip fails with `music_strict` when its music ratio is above
      `gate.strict_max_music_ratio`. An unknown ratio already fails the music check.
    """
    failures: _Failures = []
    risks: tuple[str, ...] = ()
    limit: object = None
    if state is PrecheckState.CLAIMED:
        failures.append((ReasonCode.PRECHECK_CLAIM, "the YouTube pre-check found a claim, block or removal"))
    elif state is PrecheckState.INCONCLUSIVE:
        failures.append((ReasonCode.PRECHECK_INCONCLUSIVE, "the YouTube pre-check gave no answer"))
    elif limits.name != EXPRESS:
        risks = risk_reasons(clip, creator, music)
        effective = state
        if state is PrecheckState.AVAILABLE and not limits.precheck_risky:
            effective = PrecheckState.DISABLED
        if risks and effective is PrecheckState.AVAILABLE:
            failures.append(
                (
                    ReasonCode.PRECHECK_REQUIRED,
                    "risky clip waits for the YouTube pre-check: " + ", ".join(risks),
                )
            )
        elif risks and effective in PRECHECK_NOT_RUN:
            strict = min(settings.strict_max_music_ratio, limits.max_music_ratio)
            limit = strict
            if music is not None and strict < music <= limits.max_music_ratio:
                failures.append(
                    (
                        ReasonCode.MUSIC_STRICT,
                        f"music ratio {music:.4g} is above the strict limit {strict:g} that applies "
                        f"because the YouTube pre-check is {effective.value}",
                    )
                )
    return _result(failures, {"state": state.value, "risk": list(risks)}, limit), risks
