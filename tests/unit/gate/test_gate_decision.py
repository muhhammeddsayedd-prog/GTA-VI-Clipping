"""The gate decision (docs/10) as a pure function: every rule, every boundary, every reason code."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from gate_helpers import (
    AVAILABLE,
    BLOCK_UNTIL,
    NOW,
    UNAVAILABLE,
    clean_visual,
    creator,
    express_clip,
    good_clip,
    good_file,
    make_settings,
    other_clip,
)

from sixstars.gate.decision import decide
from sixstars.gate.models import (
    ClipFacts,
    CreatorFacts,
    GateDecision,
    GateSettings,
    PrecheckState,
    ReasonCode,
    RecentClip,
    Verdict,
    VisualFindings,
)

R = ReasonCode
_VISUAL = clean_visual()


def run(
    clip: ClipFacts | None = None,
    who: CreatorFacts | None = None,
    recent: tuple[RecentClip, ...] = (),
    settings: GateSettings | None = None,
    visual: VisualFindings | None = _VISUAL,
    precheck: PrecheckState = UNAVAILABLE,
    now: datetime = NOW,
) -> GateDecision:
    return decide(
        clip or good_clip(), who or creator(), recent, settings or make_settings(), visual, precheck, now
    )


def codes(decision: GateDecision) -> set[str]:
    return {code.value for code in decision.codes}


# ── The happy path ───────────────────────────────────────────


def test_a_good_hourly_clip_is_approved_in_the_hourly_lane() -> None:
    decision = run()
    assert decision.verdict is Verdict.APPROVE
    assert (decision.profile, decision.lane, decision.demoted) == ("hourly", "hourly", False)
    assert decision.codes == () and decision.reasons == ()
    assert decision.reject_reason is None
    assert all(check.ok for check in decision.checks.values())


def test_a_good_express_clip_is_approved_in_the_spike_lane() -> None:
    decision = run(express_clip())
    assert decision.verdict is Verdict.APPROVE
    assert (decision.profile, decision.lane, decision.demoted) == ("express", "spike", False)


def test_a_clip_judged_by_the_hourly_profile_never_stays_in_the_spike_lane() -> None:
    """Only the express profile may send a clip to be posted at once."""
    decision = run(good_clip(profile="hourly", lane="spike", score=7))
    assert decision.approved and (decision.profile, decision.lane) == ("hourly", "hourly")


def test_the_result_document_has_the_shape_of_docs_10() -> None:
    document = run(good_clip(music_ratio=0.22)).as_json()
    assert document["profile"] == "hourly" and document["passed"] is False
    checks = document["checks"]
    assert isinstance(checks, dict)
    assert checks["music"]["ok"] is False
    assert checks["music"]["value"] == 0.22 and checks["music"]["limit"] == 0.1
    assert checks["length"] == {"ok": True, "value": 30.0, "limit": [20.0, 60.0]}
    assert list(checks) == [
        "creator",
        "length",
        "music",
        "spoiler",
        "spoiler_text",
        "safety",
        "reject_topics",
        "watermark",
        "visual",
        "subtitle_sync",
        "score",
        "duplicate_topic",
        "source_range",
        "file",
        "precheck",
    ]


def test_codes_and_reasons_are_parallel_and_readable() -> None:
    decision = run(good_clip(score=3, duration_s=5.0, music_ratio=0.5))
    assert len(decision.codes) == len(decision.reasons) == 3
    assert all(isinstance(reason, str) and len(reason) > 10 for reason in decision.reasons)


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="UTC"):
        run(now=datetime(2026, 11, 20, 12, 0))  # noqa: DTZ001


def test_an_unknown_profile_is_refused() -> None:
    with pytest.raises(ValueError, match="profile"):
        run(good_clip(profile="vip"))


def test_a_time_in_another_zone_is_compared_as_the_same_instant() -> None:
    cairo = timezone(timedelta(hours=2))
    just_before = (BLOCK_UNTIL - timedelta(seconds=1)).astimezone(cairo)
    assert R.SPOILER_BLOCKED in run(good_clip(spoiler=True), now=just_before).codes


# ── Length ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "duration", "expected"),
    [
        ("hourly", 19.99, R.TOO_SHORT),
        ("hourly", 20.0, None),
        ("hourly", 20.01, None),
        ("hourly", 59.99, None),
        ("hourly", 60.0, None),
        ("hourly", 60.01, R.TOO_LONG),
        ("express", 14.99, R.TOO_SHORT),
        ("express", 15.0, None),
        ("express", 45.0, None),
        ("express", 45.01, R.TOO_LONG),
        ("hourly", None, R.LENGTH_UNKNOWN),
    ],
)
def test_length_limits(profile: str, duration: float | None, expected: ReasonCode | None) -> None:
    file = good_file(duration_s=(duration or 30.0) + 1.5)
    if profile == "express":
        # Outside both profiles where it matters, so no demotion hides the express failure.
        clip = express_clip(duration_s=duration, file=file)
        decision = run(clip)
        if expected is R.TOO_LONG:
            assert decision.demoted and decision.express_codes == (R.TOO_LONG,)
            return
    else:
        decision = run(good_clip(duration_s=duration, file=file))
    assert decision.codes == ((expected,) if expected else ())


# ── Music ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ratio", "state", "expected"),
    [
        (0.0, UNAVAILABLE, ()),
        (None, UNAVAILABLE, (R.MUSIC_UNKNOWN,)),
        (None, AVAILABLE, (R.MUSIC_UNKNOWN,)),
        (None, PrecheckState.PASSED, (R.MUSIC_UNKNOWN,)),
        (0.11, UNAVAILABLE, (R.MUSIC_OVER_LIMIT,)),
        (0.11, PrecheckState.PASSED, (R.MUSIC_OVER_LIMIT,)),
        (0.1001, AVAILABLE, (R.MUSIC_OVER_LIMIT,)),
    ],
)
def test_hourly_music_limit(
    ratio: float | None, state: PrecheckState, expected: tuple[ReasonCode, ...]
) -> None:
    assert run(good_clip(music_ratio=ratio), precheck=state).codes == expected


@pytest.mark.parametrize("ratio", [0.0001, 0.03, 0.05, 0.1])
@pytest.mark.parametrize(
    "state", [PrecheckState.UNAVAILABLE, PrecheckState.DISABLED, PrecheckState.QUOTA_USED]
)
def test_stricter_music_rule_when_the_precheck_cannot_run(ratio: float, state: PrecheckState) -> None:
    """D103: music the hourly profile allows (up to 0.10) is still rejected without a pre-check."""
    decision = run(good_clip(music_ratio=ratio), precheck=state)
    assert decision.verdict is Verdict.REJECT
    assert decision.codes == (R.MUSIC_STRICT,)
    assert decision.checks["music"].ok  # the profile limit itself was met
    assert not decision.checks["precheck"].ok


@pytest.mark.parametrize(
    ("ratio", "strict", "expected"),
    [(0.04, 0.05, ()), (0.05, 0.05, ()), (0.0501, 0.05, (R.MUSIC_STRICT,))],
)
def test_strict_music_limit_boundary(ratio: float, strict: float, expected: tuple[ReasonCode, ...]) -> None:
    settings = make_settings(strict_max_music_ratio=strict)
    assert run(good_clip(music_ratio=ratio), settings=settings).codes == expected


def test_strict_limit_never_allows_more_than_the_profile() -> None:
    settings = make_settings(strict_max_music_ratio=0.5)
    assert run(good_clip(music_ratio=0.2), settings=settings).codes == (R.MUSIC_OVER_LIMIT,)


@pytest.mark.parametrize(("ratio", "ok"), [(0.0, True), (0.0001, False), (0.04, False)])
def test_express_music_must_be_zero(ratio: float, ok: bool) -> None:
    decision = run(express_clip(music_ratio=ratio), precheck=PrecheckState.PASSED)
    assert (decision.profile == "express") is ok
    if not ok:
        assert R.MUSIC_OVER_LIMIT in decision.express_codes


def test_unknown_music_is_rejected_in_the_express_profile_too() -> None:
    decision = run(express_clip(music_ratio=None))
    assert decision.verdict is Verdict.REJECT and R.MUSIC_UNKNOWN in decision.codes


def test_isolated_music_frames_count_by_default() -> None:
    frames = tuple(i == 12 for i in range(30))
    decision = run(good_clip(music_ratio=1 / 30, music_frames=frames))
    assert decision.codes == (R.MUSIC_STRICT,)


def test_isolated_music_frames_can_be_ignored_by_a_setting() -> None:
    settings = make_settings(music_min_run_frames=2)
    lone = tuple(i == 12 for i in range(30))
    pair = tuple(i in (12, 13) for i in range(30))
    assert run(good_clip(music_ratio=1 / 30, music_frames=lone), settings=settings).approved
    assert run(good_clip(music_ratio=2 / 30, music_frames=pair), settings=settings).codes == (R.MUSIC_STRICT,)
    # Without the per-second flags the stored ratio counts in full.
    assert run(good_clip(music_ratio=1 / 30), settings=settings).codes == (R.MUSIC_STRICT,)
    # The flags can lower the ratio, never hide an unknown one.
    assert run(good_clip(music_ratio=None, music_frames=lone), settings=settings).codes == (R.MUSIC_UNKNOWN,)


# ── Pre-check states ─────────────────────────────────────────


def test_a_risky_clip_is_held_when_the_precheck_can_run() -> None:
    decision = run(good_clip(music_ratio=0.05), precheck=AVAILABLE)
    assert decision.verdict is Verdict.HOLD
    assert decision.codes == (R.PRECHECK_REQUIRED,)
    assert decision.risk_reasons == ("music",)
    assert decision.reject_reason is None and not decision.approved


def test_a_clip_that_is_not_risky_needs_no_precheck() -> None:
    decision = run(precheck=AVAILABLE)
    assert decision.approved and decision.risk_reasons == ()


def test_a_risky_clip_is_approved_after_a_passed_precheck() -> None:
    assert run(good_clip(music_ratio=0.05), precheck=PrecheckState.PASSED).approved


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (PrecheckState.CLAIMED, R.PRECHECK_CLAIM),
        (PrecheckState.INCONCLUSIVE, R.PRECHECK_INCONCLUSIVE),
    ],
)
def test_a_failed_precheck_rejects(state: PrecheckState, code: ReasonCode) -> None:
    assert run(good_clip(music_ratio=0.05), precheck=state).codes == (code,)
    # Also for a clip that would not have been risky: a claim is a claim.
    assert run(precheck=state).codes == (code,)
    assert run(express_clip(), precheck=state).codes == (code,)


def test_first_clip_of_an_auto_creator_is_risky() -> None:
    who = creator("auto", prior_clips=0)
    held = run(who=who, precheck=AVAILABLE)
    assert held.verdict is Verdict.HOLD and held.risk_reasons == ("first_clip_of_auto_creator",)
    # Without a pre-check the stricter rule is about music only: no music, so it passes.
    assert run(who=who, precheck=UNAVAILABLE).approved
    assert run(who=creator("auto", prior_clips=1), precheck=AVAILABLE).approved
    assert run(who=creator("preferred", prior_clips=0), precheck=AVAILABLE).approved


def test_a_compilation_is_risky() -> None:
    held = run(good_clip(is_compilation=True), precheck=AVAILABLE)
    assert held.verdict is Verdict.HOLD and held.risk_reasons == ("compilation",)


def test_an_express_clip_is_never_held_for_the_precheck() -> None:
    decision = run(express_clip(), who=creator("preferred", prior_clips=0), precheck=AVAILABLE)
    assert decision.approved and decision.profile == "express"


def test_hourly_profile_with_precheck_switched_off_uses_the_strict_rule() -> None:
    settings = make_settings()
    settings = replace(settings, hourly=replace(settings.hourly, precheck_risky=False))
    decision = run(good_clip(music_ratio=0.05), settings=settings, precheck=AVAILABLE)
    assert decision.codes == (R.MUSIC_STRICT,)


def test_a_rejected_clip_is_not_held() -> None:
    decision = run(good_clip(music_ratio=0.05, score=2), precheck=AVAILABLE)
    assert decision.verdict is Verdict.REJECT and decision.codes == (R.SCORE_TOO_LOW,)


# ── Score ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "score", "ok"),
    [
        ("hourly", 6, False),
        ("hourly", 6.99, False),
        ("hourly", 7, True),
        ("hourly", 8, True),
        ("express", 7, False),
        ("express", 7.99, False),
        ("express", 8, True),
        ("express", 9, True),
    ],
)
def test_score_limits(profile: str, score: float, ok: bool) -> None:
    clip = good_clip(score=score) if profile == "hourly" else express_clip(score=score)
    decision = run(clip)
    if profile == "hourly":
        assert decision.codes == (() if ok else (R.SCORE_TOO_LOW,))
    elif ok:
        assert decision.profile == "express" and decision.approved
    elif score >= 7:
        assert decision.demoted and decision.express_codes == (R.SCORE_TOO_LOW,)
    else:
        assert decision.codes == (R.SCORE_TOO_LOW,)


def test_a_missing_score_is_rejected() -> None:
    assert run(good_clip(score=None)).codes == (R.SCORE_UNKNOWN,)


# ── Creator ──────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["restricted", "blocked", "", "vip"])
@pytest.mark.parametrize("profile", ["hourly", "express"])
def test_a_forbidden_or_unknown_creator_is_never_approved(tier: str, profile: str) -> None:
    clip = good_clip(score=10) if profile == "hourly" else express_clip(score=10)
    decision = run(clip, who=creator(tier), precheck=PrecheckState.PASSED)
    assert decision.verdict is Verdict.REJECT
    assert decision.codes == (R.CREATOR_FORBIDDEN,)
    assert not decision.demoted


@pytest.mark.parametrize(("today", "ok"), [(0, True), (3, True), (4, False), (5, False)])
def test_auto_creator_daily_cap(today: int, ok: bool) -> None:
    decision = run(who=creator("auto", clips_today=today))
    assert decision.codes == (() if ok else (R.CREATOR_DAILY_CAP,))


def test_the_daily_cap_does_not_apply_to_preferred_creators() -> None:
    assert run(who=creator("preferred", clips_today=50)).approved


def test_express_tiers() -> None:
    assert run(express_clip(), who=creator("preferred")).profile == "express"
    assert run(express_clip(), who=creator("auto")).profile == "express"
    settings = make_settings()
    preferred_only = replace(settings, express=replace(settings.express, tiers=("preferred",)))
    demoted = run(express_clip(), who=creator("auto"), settings=preferred_only)
    assert demoted.approved and demoted.demoted
    assert demoted.express_codes == (R.CREATOR_TIER_EXPRESS,)
    assert (demoted.profile, demoted.lane) == ("hourly", "hourly")


def test_a_campaign_creator_is_treated_like_a_preferred_one() -> None:
    """D117: tier `campaign` passes the creator check, has no daily cap and may post at once."""
    assert run(who=creator("campaign", clips_today=50)).approved
    settings = make_settings()
    preferred_only = replace(settings, express=replace(settings.express, tiers=("preferred",)))
    decision = run(express_clip(), who=creator("campaign"), settings=preferred_only)
    assert decision.approved and not decision.demoted
    assert decision.profile == "express"


def test_express_auto_creator_with_music_fails_the_tier_and_the_music_rule() -> None:
    decision = run(express_clip(music_ratio=0.05), who=creator("auto"), precheck=PrecheckState.PASSED)
    assert decision.demoted
    assert set(decision.express_codes) == {R.CREATOR_TIER_EXPRESS, R.MUSIC_OVER_LIMIT}


# ── Spoilers ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("offset_s", "blocked"),
    [(-86400, True), (-1, True), (0, False), (1, False), (86400, False)],
)
def test_spoiler_block_date_boundary(offset_s: int, blocked: bool) -> None:
    now = BLOCK_UNTIL + timedelta(seconds=offset_s)
    decision = run(good_clip(spoiler=True), now=now)
    assert decision.codes == ((R.SPOILER_BLOCKED,) if blocked else ())


def test_a_clip_that_is_not_a_spoiler_passes_before_the_date() -> None:
    assert run(good_clip(spoiler=False), now=BLOCK_UNTIL - timedelta(days=10)).approved


def test_an_unknown_spoiler_flag_counts_as_a_spoiler() -> None:
    assert run(good_clip(spoiler=None)).codes == (R.SPOILER_BLOCKED,)
    assert run(good_clip(spoiler=None), now=BLOCK_UNTIL).approved


def test_no_block_date_means_no_date_block() -> None:
    assert run(good_clip(spoiler=True), settings=make_settings(spoiler_block_until=None)).approved


@pytest.mark.parametrize(
    "field_values",
    [
        {"hook": "The FINAL mission is insane"},
        {"title": "speedyrp picks the secret final-mission option"},
        {"caption": "Wait for the ending..."},
        {"hashtags": ("gta", "finalmission")},
        {"hashtags": ("gta", "#Ending")},
        {"hashtags": ("Spoiler",)},
        {"title": "Huge PLOT   TWIST nobody saw coming"},
        {"commentary": "This is the final mission"},
    ],
)
def test_spoiler_terms_in_the_written_text(field_values: dict[str, Any]) -> None:
    for now in (NOW, BLOCK_UNTIL + timedelta(days=30)):  # never, also after the block date
        decision = run(good_clip(spoiler=False, **field_values), now=now)
        assert decision.codes == (R.SPOILER_IN_TEXT,)
        assert next(iter(field_values)) in decision.reasons[0]


@pytest.mark.parametrize(
    "field_values",
    [
        {"hook": "Unending chase, pending doom"},  # "ending" only inside other words
        {"title": "He is spending it all"},
        {"hashtags": ("trending", "gta")},
        {"caption": "Mission failed successfully"},
    ],
)
def test_spoiler_terms_match_whole_words_only(field_values: dict[str, Any]) -> None:
    assert run(good_clip(**field_values)).approved


def test_the_transcript_is_not_searched_for_spoiler_terms() -> None:
    assert run(good_clip(transcript_text="this is the final mission chat")).approved


def test_spoiler_terms_come_from_settings() -> None:
    settings = make_settings(spoiler_terms=("bridge",))
    assert run(settings=settings).codes == (R.SPOILER_IN_TEXT,)
    assert run(good_clip(hook="The final mission"), settings=make_settings(spoiler_terms=())).approved


# ── Safety and rejected topics ───────────────────────────────


@pytest.mark.parametrize(
    "flag",
    [
        "politics",
        "gambling",
        "sponsored",
        "real_violence",
        "sexual",
        "personal_info",
        "hate",
        "copyrighted_media",
        "something_new",
    ],
)
def test_any_safety_flag_rejects(flag: str) -> None:
    flags = {**(good_clip().safety_flags or {}), flag: True}
    for clip in (good_clip(safety_flags=flags, score=10), express_clip(safety_flags=flags, score=10)):
        decision = run(clip, precheck=PrecheckState.PASSED)
        assert decision.verdict is Verdict.REJECT and not decision.demoted
        assert decision.codes == (R.SAFETY_FLAG,)
        assert flag in decision.reasons[0]


def test_two_flags_are_one_code_naming_both() -> None:
    decision = run(good_clip(safety_flags={"gambling": True, "sponsored": True}))
    assert decision.codes == (R.SAFETY_FLAG,)
    assert "gambling" in decision.reasons[0] and "sponsored" in decision.reasons[0]


@pytest.mark.parametrize("flags", [None, {"gambling": "yes"}, {"gambling": None}, {"gambling": 1}])
def test_missing_or_malformed_safety_flags_reject(flags: Any) -> None:
    decision = run(good_clip(safety_flags=flags))
    assert decision.verdict is Verdict.REJECT
    assert codes(decision) <= {"safety_unknown", "safety_flag"} and decision.codes


def test_an_empty_flag_object_means_no_flag() -> None:
    assert run(good_clip(safety_flags={})).approved


@pytest.mark.parametrize(
    "field_values",
    [
        {"topic": "streamer-gambling-big-win"},
        {"title": "Real-world politics rant on stream"},
        {"hashtags": ("gambling",)},
        {"caption": "this sponsored segment went wrong"},
        {"hook": "SEXUAL content warning"},
        {"commentary": "pure gambling"},
    ],
)
def test_reject_topics_in_the_written_text(field_values: dict[str, Any]) -> None:
    assert run(good_clip(**field_values)).codes == (R.REJECT_TOPIC,)


def test_reject_topics_come_from_settings() -> None:
    assert run(settings=make_settings(reject_topics=("whole bridge",))).codes == (R.REJECT_TOPIC,)


# ── Visual (P5) and ffmpeg measurements ──────────────────────


@pytest.mark.parametrize(
    ("finding", "code"),
    [
        ({"other_watermark": True}, R.WATERMARK),
        ({"platform_logos": True}, R.PLATFORM_LOGO),
        ({"menu_or_loading": True}, R.MENU_OR_LOADING),
        ({"pause_or_brb_screen": True}, R.PAUSE_SCREEN),
        ({"empty_facecam": True}, R.EMPTY_FACECAM),
        ({"readable_subtitles": False}, R.UNREADABLE_SUBTITLES),
    ],
)
def test_each_visual_finding_rejects(finding: dict[str, bool], code: ReasonCode) -> None:
    for clip in (good_clip(), express_clip()):
        decision = run(clip, visual=clean_visual(**finding))
        assert decision.verdict is Verdict.REJECT and decision.codes == (code,)


def test_no_visual_verdict_means_no_approval() -> None:
    assert run(visual=None).codes == (R.VISUAL_UNVERIFIED,)
    assert run(express_clip(), visual=None).codes == (R.VISUAL_UNVERIFIED,)


def test_the_visual_note_is_part_of_the_reason() -> None:
    visual = clean_visual(other_watermark=True, notes="@otherchannel in the corner")
    assert "@otherchannel" in run(visual=visual).reasons[0]


@pytest.mark.parametrize(
    ("field_name", "value", "expected"),
    [
        ("longest_black_s", 1.99, ()),
        ("longest_black_s", 2.0, (R.BLACK_FRAMES,)),
        ("longest_black_s", 2.01, (R.BLACK_FRAMES,)),
        ("longest_black_s", None, (R.BLACK_FRAMES,)),
        ("longest_freeze_s", 1.99, ()),
        ("longest_freeze_s", 2.0, (R.FROZEN_VIDEO,)),
        ("longest_freeze_s", 2.5, (R.FROZEN_VIDEO,)),
        ("longest_freeze_s", None, (R.FROZEN_VIDEO,)),
    ],
)
def test_black_and_frozen_video(
    field_name: str, value: float | None, expected: tuple[ReasonCode, ...]
) -> None:
    assert run(good_clip(file=good_file(**{field_name: value}))).codes == expected


# ── Subtitles ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("drift", "expected"),
    [(None, ()), (0.0, ()), (0.3, ()), (0.31, (R.SUBTITLE_SYNC,)), (-0.31, (R.SUBTITLE_SYNC,))],
)
def test_measured_subtitle_drift(drift: float | None, expected: tuple[ReasonCode, ...]) -> None:
    assert run(good_clip(subtitle_drift_s=drift)).codes == expected


@pytest.mark.parametrize(
    ("spans", "ok"),
    [
        (((0.0, 0.4), (29.6, 30.0)), True),
        (((-0.3, 0.2), (29.9, 30.3)), True),
        (((-0.31, 0.2),), False),
        (((29.9, 30.31),), False),
        (((5.0, 4.0),), False),
        ((), False),
    ],
)
def test_subtitle_words_must_lie_inside_the_clip(spans: tuple[tuple[float, float], ...], ok: bool) -> None:
    assert run(good_clip(word_spans=spans)).codes == (() if ok else (R.SUBTITLE_SYNC,))


# ── File ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"width": 1080, "height": 1920}, ()),
        ({"width": 1920, "height": 1080}, (R.FILE_RESOLUTION,)),
        ({"width": 720, "height": 1280}, (R.FILE_RESOLUTION,)),
        ({"has_video": False}, (R.FILE_RESOLUTION,)),
        ({"has_audio": False}, (R.FILE_NO_AUDIO,)),
        ({"size_bytes": 100_000_000}, ()),
        ({"size_bytes": 100_000_001}, (R.FILE_TOO_LARGE,)),
        ({"size_bytes": 0}, (R.FILE_MISSING,)),
        ({"size_bytes": None}, (R.FILE_TOO_LARGE,)),
        ({"duration_s": 31.5}, ()),
        ({"duration_s": 30.5}, ()),
        ({"duration_s": 32.5}, ()),
        ({"duration_s": 30.49}, (R.FILE_DURATION,)),
        ({"duration_s": 32.51}, (R.FILE_DURATION,)),
        ({"duration_s": None}, (R.FILE_DURATION,)),
        ({"has_audio": False, "width": 100, "size_bytes": 200_000_000}, None),
    ],
)
def test_file_checks(changes: dict[str, Any], expected: tuple[ReasonCode, ...] | None) -> None:
    decision = run(good_clip(file=good_file(**changes)))
    if expected is None:
        assert decision.codes == (R.FILE_RESOLUTION, R.FILE_NO_AUDIO, R.FILE_TOO_LARGE)
    else:
        assert decision.codes == expected


def test_a_clip_without_a_file_is_rejected() -> None:
    assert run(good_clip(file=None)).codes == (R.FILE_MISSING,)


# ── Duplicates ───────────────────────────────────────────────


def test_an_unrelated_recent_clip_is_no_duplicate() -> None:
    assert run(recent=(other_clip(),)).approved


def test_the_clip_itself_is_not_its_own_duplicate() -> None:
    me = good_clip()
    same = other_clip(
        id=me.id,
        creator_id=me.creator_id,
        source_id=me.source_id,
        start_s=me.start_s,
        end_s=me.end_s,
        topic=me.topic,
        transcript_text=me.transcript_text,
    )
    assert run(me, recent=(same,)).approved


@pytest.mark.parametrize(
    ("profile", "age_h", "duplicate"),
    [
        ("hourly", 47.9, True),
        ("hourly", 48.0, True),
        ("hourly", 48.1, False),
        ("express", 5.9, True),
        ("express", 6.0, True),
        ("express", 6.1, False),
    ],
)
def test_same_topic_inside_the_profile_window(profile: str, age_h: float, duplicate: bool) -> None:
    clip = good_clip() if profile == "hourly" else express_clip()
    earlier = other_clip(topic=clip.topic, decided_at=NOW - timedelta(hours=age_h))
    decision = run(clip, recent=(earlier,))
    if profile == "hourly":
        assert decision.codes == ((R.DUPLICATE_TOPIC,) if duplicate else ())
    elif duplicate:
        assert decision.verdict is Verdict.REJECT and decision.codes == (R.DUPLICATE_TOPIC,)
    elif age_h <= 48:
        # Outside the express window but inside the hourly one: stays express.
        assert decision.approved and decision.profile == "express"


def test_topic_comparison_ignores_case_and_separators() -> None:
    earlier = other_clip(topic="Streamer_Jumps  whole-BRIDGE")
    assert run(recent=(earlier,)).codes == (R.DUPLICATE_TOPIC,)


def test_reworded_topic_of_the_same_creator_is_a_duplicate() -> None:
    """The two topics P2 wrote for one real moment (progress notes M05)."""
    clip = good_clip(topic="new-player-robs-man-on-ground")
    earlier = other_clip(creator_id=clip.creator_id, topic="player-caught-robbing-downed-player")
    assert run(clip, recent=(earlier,)).codes == (R.DUPLICATE_TOPIC,)


def test_loosely_similar_topics_of_different_creators_are_not_duplicates() -> None:
    clip = good_clip(topic="new-player-robs-man-on-ground")
    earlier = other_clip(topic="player-caught-robbing-downed-player")
    assert run(clip, recent=(earlier,)).approved


def test_nearly_identical_topics_of_different_creators_are_duplicates() -> None:
    clip = good_clip(topic="streamer-jumps-whole-bridge-bike")
    earlier = other_clip(topic="streamer-jumping-the-whole-bridge")
    assert run(clip, recent=(earlier,)).codes == (R.DUPLICATE_TOPIC,)


def test_different_topics_of_the_same_creator_are_not_duplicates() -> None:
    earlier = other_clip(creator_id="creator-1", topic="cop-chase-ends-in-river")
    assert run(recent=(earlier,)).approved


@pytest.mark.parametrize("status", ["rejected", "rendering", "gating", "expired"])
def test_only_approved_or_posted_clips_count_for_the_topic(status: str) -> None:
    earlier = other_clip(topic=good_clip().topic, status=status)
    assert run(recent=(earlier,)).approved


@pytest.mark.parametrize("status", ["approved", "posted", "precheck_pending", "removed"])
def test_statuses_that_count_for_the_topic(status: str) -> None:
    earlier = other_clip(topic=good_clip().topic, status=status)
    assert run(recent=(earlier,)).codes == (R.DUPLICATE_TOPIC,)


def test_same_words_from_another_source_are_a_duplicate() -> None:
    """The live stream and its replay have different source ids but the same speech."""
    clip = good_clip()
    earlier = other_clip(
        creator_id=clip.creator_id,
        topic="something-else-entirely",
        transcript_text="chat " + clip.transcript_text + " let us go",
    )
    assert run(clip, recent=(earlier,)).codes == (R.DUPLICATE_TRANSCRIPT,)
    old = replace(earlier, decided_at=NOW - timedelta(hours=49))
    assert run(clip, recent=(old,)).approved


def test_a_few_shared_words_are_not_a_transcript_duplicate() -> None:
    earlier = other_clip(transcript_text="no way he did that oh my god that was insane chat look at this")
    assert run(recent=(earlier,)).approved


@pytest.mark.parametrize(
    ("start", "end", "duplicate"),
    [
        (1000.0, 1030.0, True),  # the same range
        (1010.0, 1050.0, True),  # overlapping
        (900.0, 969.9, False),  # ends 30.1 s before this clip starts
        (900.0, 970.0, True),  # exactly 30 s before
        (1060.0, 1100.0, True),  # starts exactly 30 s after
        (1060.1, 1100.0, False),
        (0.0, 5000.0, True),  # contains it
    ],
)
def test_source_range_overlap(start: float, end: float, duplicate: bool) -> None:
    earlier = other_clip(source_id="video-1", start_s=start, end_s=end, decided_at=NOW - timedelta(days=30))
    decision = run(recent=(earlier,))
    assert decision.codes == ((R.DUPLICATE_SOURCE_RANGE,) if duplicate else ())


def test_source_range_needs_the_same_video_on_the_same_platform() -> None:
    assert run(recent=(other_clip(source_id="video-9", start_s=1000.0, end_s=1030.0),)).approved
    twitch = other_clip(source_id="video-1", source_platform="twitch", start_s=1000.0, end_s=1030.0)
    assert run(recent=(twitch,)).approved


@pytest.mark.parametrize(
    ("status", "older", "duplicate"),
    [
        ("rejected", True, False),
        ("rendering", True, False),
        ("gating", True, True),  # two clips of one moment in the gate: the older one wins
        ("gating", False, False),
        ("precheck_pending", False, True),
        ("approved", False, True),
        ("posted", False, True),
        ("removed", False, True),
        ("expired", False, True),
    ],
)
def test_source_range_statuses(status: str, older: bool, duplicate: bool) -> None:
    clip = good_clip()
    created = clip.created_at + timedelta(seconds=-5 if older else 5)
    earlier = other_clip(source_id="video-1", start_s=1005.0, end_s=1035.0, status=status, created_at=created)
    assert run(clip, recent=(earlier,)).codes == ((R.DUPLICATE_SOURCE_RANGE,) if duplicate else ())


# ── Combinations ─────────────────────────────────────────────


def test_a_clip_failing_three_rules_reports_all_three_in_order() -> None:
    clip = good_clip(duration_s=75.0, file=good_file(duration_s=76.5), score=4, spoiler=True)
    decision = run(clip)
    assert decision.codes == (R.TOO_LONG, R.SPOILER_BLOCKED, R.SCORE_TOO_LOW)
    assert decision.reject_reason == "too_long"


def test_everything_wrong_at_once() -> None:
    clip = good_clip(
        duration_s=5.0,
        music_ratio=None,
        spoiler=True,
        safety_flags={"gambling": True},
        hook="The ending",
        topic="gambling-stream",
        score=1,
        word_spans=(),
        file=good_file(width=640, has_audio=False, longest_black_s=3.0, longest_freeze_s=4.0),
    )
    earlier = other_clip(source_id="video-1", start_s=1000.0, end_s=1030.0, topic="gambling-stream")
    visual = clean_visual(other_watermark=True, platform_logos=True, menu_or_loading=True)
    decision = run(clip, who=creator("blocked"), recent=(earlier,), visual=visual)
    assert codes(decision) == {
        "creator_forbidden",
        "too_short",
        "music_unknown",
        "spoiler_blocked",
        "spoiler_in_text",
        "safety_flag",
        "reject_topic",
        "watermark",
        "platform_logo",
        "menu_or_loading",
        "black_frames",
        "frozen_video",
        "subtitle_sync",
        "score_too_low",
        "duplicate_topic",
        "duplicate_source_range",
        "file_resolution",
        "file_no_audio",
        "file_duration",
    }
    assert decision.reject_reason == "creator_forbidden"
    assert len(decision.reasons) == len(decision.codes)


# ── Express fallback (docs/10 section 2) ─────────────────────


def test_express_clip_failing_only_express_rules_moves_to_the_hourly_lane() -> None:
    decision = run(express_clip(score=7, duration_s=50.0, file=good_file(duration_s=51.5)))
    assert decision.verdict is Verdict.APPROVE
    assert decision.demoted and (decision.profile, decision.lane) == ("hourly", "hourly")
    assert decision.codes == ()
    assert decision.express_codes == (R.TOO_LONG, R.SCORE_TOO_LOW)
    assert not decision.express_checks["score"].ok and decision.checks["score"].ok
    document = decision.as_json()
    assert document["profile"] == "hourly" and document["demoted"] is True
    assert document["express"]["codes"] == ["too_long", "score_too_low"]  # type: ignore[index]


def test_express_clip_failing_the_hourly_profile_too_is_rejected() -> None:
    decision = run(express_clip(score=6))
    assert decision.verdict is Verdict.REJECT and not decision.demoted
    assert (decision.profile, decision.lane) == ("express", "spike")
    assert decision.codes == (R.SCORE_TOO_LOW,)
    assert decision.fallback_codes == (R.SCORE_TOO_LOW,)


def test_express_clip_between_15_and_20_seconds_cannot_fall_back() -> None:
    decision = run(express_clip(duration_s=17.0, score=7, file=good_file(duration_s=18.5)))
    assert decision.verdict is Verdict.REJECT and decision.codes == (R.SCORE_TOO_LOW,)


def test_a_demoted_clip_follows_the_hourly_precheck_rules() -> None:
    clip = express_clip(music_ratio=0.05)
    assert run(clip, precheck=AVAILABLE).verdict is Verdict.HOLD
    assert run(clip, precheck=AVAILABLE).demoted and run(clip, precheck=AVAILABLE).lane == "hourly"
    strict = run(clip, precheck=UNAVAILABLE)
    assert strict.verdict is Verdict.REJECT and strict.codes == (R.MUSIC_OVER_LIMIT,)
    assert strict.fallback_codes == (R.MUSIC_STRICT,)
    assert run(clip, precheck=PrecheckState.PASSED).approved


def test_a_demoted_clip_respects_the_hourly_daily_cap() -> None:
    decision = run(express_clip(score=7), who=creator("auto", clips_today=4))
    assert decision.verdict is Verdict.REJECT
    assert decision.codes == (R.SCORE_TOO_LOW,)
    assert decision.fallback_codes == (R.CREATOR_DAILY_CAP,)


def test_a_rejected_clip_also_reports_the_strict_music_rule() -> None:
    decision = run(good_clip(music_ratio=0.05, score=2), precheck=UNAVAILABLE)
    assert decision.codes == (R.SCORE_TOO_LOW, R.MUSIC_STRICT)


def test_utc_now_is_accepted() -> None:
    assert run(now=datetime(2026, 11, 20, 12, 0, tzinfo=UTC)).approved
