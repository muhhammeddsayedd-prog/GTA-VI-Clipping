"""Seeded random clips through the gate decision: the invariants that must hold for every input."""

import functools
import random
from datetime import datetime, timedelta

from gate_helpers import (
    BLOCK_UNTIL,
    NOW,
    clean_visual,
    creator,
    good_clip,
    good_file,
    make_settings,
    other_clip,
)

from sixstars.gate.checks import effective_music_ratio
from sixstars.gate.decision import decide
from sixstars.gate.models import (
    FORBIDDEN_TIERS,
    PRECHECK_NOT_RUN,
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

CASES = 4000
FLAGS = ("politics", "gambling", "sponsored", "real_violence", "sexual", "personal_info", "hate")
Case = tuple[
    ClipFacts,
    CreatorFacts,
    tuple[RecentClip, ...],
    GateSettings,
    VisualFindings | None,
    PrecheckState,
    datetime,
]


def maybe(rng: random.Random, chance: float) -> bool:
    return rng.random() < chance


def random_case(rng: random.Random) -> Case:
    """Mostly good clips with a few random defects, so that approvals and every kind of rejection occur."""
    duration = rng.choice([14.9, 15.0, 19.9, 20.0, 30.0, 30.0, 30.0, 44.0, 45.0, 46.0, 60.0, 60.1, 75.0])
    music = rng.choice([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None, 0.03, 0.05, 0.1, 0.11, 0.4])
    flags: dict[str, object] | None = dict.fromkeys(FLAGS, False)
    if flags is not None and maybe(rng, 0.06):
        flags[rng.choice(FLAGS)] = True
    if maybe(rng, 0.02):
        flags = None
    frames = None
    if music is not None and maybe(rng, 0.5):
        count = round(music * 30)
        together = maybe(rng, 0.5)
        frames = tuple((i < count) if together else (i % 3 == 0 and i // 3 < count) for i in range(30))
    file = good_file(
        duration_s=duration + rng.choice([1.5, 1.5, 1.5, 1.5, 1.5, 0.2, 3.0]),
        width=rng.choice([1080] * 9 + [720]),
        has_audio=not maybe(rng, 0.02),
        size_bytes=rng.choice([20_000_000] * 8 + [100_000_000, 100_000_001]),
        longest_black_s=rng.choice([0.0] * 8 + [1.9, 2.0, None]),
        longest_freeze_s=rng.choice([0.0] * 8 + [1.9, 2.0, 5.0]),
    )
    express = maybe(rng, 0.5)
    clip = good_clip(
        profile="express" if express else "hourly",
        lane="spike" if express else "hourly",
        duration_s=duration,
        file=None if maybe(rng, 0.02) else file,
        score=rng.choice([None, 5, 6.9, 7, 7.5, 8, 8, 9, 9, 10]),
        music_ratio=music,
        music_frames=frames,
        spoiler=rng.choice([False] * 8 + [True, None]),
        safety_flags=flags,
        hook=rng.choice(["He did NOT expect that"] * 9 + ["The ending is wild"]),
        title=rng.choice(["Streamer jumps the bridge"] * 9 + ["Gambling stream"]),
        hashtags=rng.choice([("gta",)] * 9 + [("finalmission",)]),
        subtitle_drift_s=rng.choice([None, None, None, 0.1, 0.3, 0.5]),
        is_compilation=maybe(rng, 0.05),
    )
    who = creator(
        rng.choice(["preferred"] * 4 + ["auto"] * 4 + ["restricted", "blocked", "mystery"]),
        clips_today=rng.choice([0, 0, 0, 1, 3, 4, 9]),
        prior_clips=rng.choice([0, 1, 20]),
    )
    recent: list[RecentClip] = []
    if maybe(rng, 0.12):
        recent.append(
            other_clip(
                topic=clip.topic,
                decided_at=NOW - timedelta(hours=rng.choice([1, 5.9, 6.1, 47, 49])),
                status=rng.choice(["approved", "posted", "rejected"]),
            )
        )
    if maybe(rng, 0.08):
        recent.append(
            other_clip(
                id="clip-src",
                source_id=clip.source_id,
                start_s=clip.start_s + rng.choice([0, 20, 59, 61, 500]),
                end_s=clip.end_s + rng.choice([0, 20, 59, 61, 500]),
                status=rng.choice(["approved", "posted", "rejected", "gating"]),
            )
        )
    if maybe(rng, 0.04):
        recent.append(other_clip(id="clip-words", transcript_text=clip.transcript_text + " and more"))
    settings = make_settings(
        strict_max_music_ratio=rng.choice([0.0, 0.0, 0.05]),
        music_min_run_frames=rng.choice([1, 1, 2]),
    )
    visual: VisualFindings | None = clean_visual(
        other_watermark=maybe(rng, 0.03),
        platform_logos=maybe(rng, 0.03),
        menu_or_loading=maybe(rng, 0.02),
        pause_or_brb_screen=maybe(rng, 0.02),
        empty_facecam=maybe(rng, 0.02),
        readable_subtitles=not maybe(rng, 0.02),
    )
    if maybe(rng, 0.02):
        visual = None
    state = rng.choice(list(PrecheckState))
    now = rng.choice(
        [NOW, NOW, BLOCK_UNTIL - timedelta(seconds=1), BLOCK_UNTIL, BLOCK_UNTIL + timedelta(days=3)]
    )
    return clip, who, tuple(recent), settings, visual, state, now


@functools.cache
def all_cases() -> list[tuple[Case, GateDecision]]:
    rng = random.Random(20261118)
    cases = [random_case(rng) for _ in range(CASES)]
    return [(case, decide(*case)) for case in cases]


def test_the_random_cases_cover_every_verdict_and_code() -> None:
    decisions = [decision for _, decision in all_cases()]
    assert {decision.verdict for decision in decisions} == {Verdict.APPROVE, Verdict.REJECT, Verdict.HOLD}
    assert sum(decision.approved for decision in decisions) > 50
    assert sum(decision.demoted for decision in decisions) > 10
    seen = {code for decision in decisions for code in decision.codes}
    seen |= {code for decision in decisions for code in decision.express_codes}
    missing = set(ReasonCode) - seen - {ReasonCode.GATE_ERROR, ReasonCode.LENGTH_UNKNOWN}
    assert not missing, sorted(missing)


def test_nothing_unsafe_is_ever_approved_or_held() -> None:
    """A held clip is approved later by a passed pre-check alone, so it must be just as clean."""
    for (clip, who, _recent, settings, visual, state, now), decision in all_cases():
        if decision.verdict is Verdict.REJECT:
            continue
        assert who.tier in ("preferred", "auto"), "forbidden or unknown creator"
        assert who.tier not in FORBIDDEN_TIERS
        assert clip.music_ratio is not None, "unknown music ratio"
        assert isinstance(clip.safety_flags, dict) and not any(clip.safety_flags.values()), "safety flag"
        blocked = settings.spoiler_block_until is not None and now < settings.spoiler_block_until
        assert not (blocked and clip.spoiler is not False), "blocked spoiler"
        assert clip.hook != "The ending is wild" and clip.hashtags != ("finalmission",), "spoiler text"
        assert clip.title != "Gambling stream", "rejected topic"
        assert visual is not None and clean_visual() == visual, "visual finding"
        assert clip.file is not None and clip.score is not None
        assert state not in (PrecheckState.CLAIMED, PrecheckState.INCONCLUSIVE), "failed pre-check"


def test_an_approved_clip_satisfies_every_threshold_of_its_profile() -> None:
    for (clip, who, _recent, settings, _visual, state, _now), decision in all_cases():
        if not decision.approved:
            continue
        limits = settings.profile(decision.profile)
        music = effective_music_ratio(clip.music_ratio, clip.music_frames, settings.music_min_run_frames)
        assert clip.duration_s is not None and limits.min_len_s <= clip.duration_s <= limits.max_len_s
        assert clip.score is not None and clip.score >= limits.min_score
        assert music is not None and music <= limits.max_music_ratio
        assert clip.file is not None
        assert (clip.file.width, clip.file.height) == (settings.width, settings.height)
        assert clip.file.has_audio and clip.file.size_bytes is not None
        assert clip.file.size_bytes <= settings.max_file_mb * 1_000_000
        assert clip.file.longest_black_s is not None and clip.file.longest_black_s < settings.max_black_s
        assert clip.file.longest_freeze_s is not None and clip.file.longest_freeze_s < settings.max_freeze_s
        assert clip.file.duration_s is not None
        expected = clip.duration_s + settings.end_card_s
        assert abs(clip.file.duration_s - expected) <= settings.duration_tolerance_s + 1e-9
        assert clip.subtitle_drift_s is None or abs(clip.subtitle_drift_s) <= settings.max_subtitle_drift_s
        if who.tier == "auto" and decision.profile == "hourly":
            assert who.clips_today < settings.auto_creator_daily_cap
        if decision.profile == "express":
            assert clip.profile == "express" and decision.lane == "spike" and not decision.demoted
            assert music == 0
        else:
            assert decision.lane == "hourly"
            if music > 0:
                # Music in an approved hourly clip: a passed pre-check, or it fits the strict limit.
                assert state is PrecheckState.PASSED or state in PRECHECK_NOT_RUN
            if music > 0 and state in PRECHECK_NOT_RUN:
                assert music <= settings.strict_max_music_ratio
        assert all(check.ok for check in decision.checks.values())
        assert decision.codes == () and decision.reasons == ()


def test_decisions_are_well_formed_and_repeatable() -> None:
    for case, decision in all_cases()[:800]:
        assert decide(*case) == decision
        assert len(decision.codes) == len(decision.reasons)
        assert (decision.verdict is Verdict.APPROVE) == (not decision.codes)
        assert (decision.verdict is Verdict.HOLD) == (decision.codes == (ReasonCode.PRECHECK_REQUIRED,))
        if decision.verdict is Verdict.HOLD:
            assert decision.profile == "hourly" and case[5] is PrecheckState.AVAILABLE
            assert decision.risk_reasons
        if decision.verdict is Verdict.REJECT:
            assert decision.reject_reason == decision.codes[0].value
            assert ReasonCode.PRECHECK_REQUIRED not in decision.codes
            assert not decision.demoted
        if decision.demoted:
            assert case[0].profile == "express" and decision.express_codes
            assert (decision.profile, decision.lane) == ("hourly", "hourly")
        assert decision.as_json()["passed"] is decision.approved
