"""Checks before the clip writer (P2): reject a clip the gate is certain to reject, before it is paid for.

AI cost plan A5a (D117). In production (2026-11-18 to 11-22) 877 written clips were later rejected by the
gate for the creator's daily cap or tier and 815 for music over the limit, after P2 had been paid for each
of them. These checks run the gate's own functions (`gate.checks.check_creator` and `check_music`) with the
gate's own settings (`gate.profiles.load`, the same length adjustments), on facts known before P2:

- the creator's tier and today's clip count (`gate.store.creator_facts`, the same query the gate runs);
- the lowest music ratio any cut of the window could have (`lowest_music_ratio`): the gate measures the
  final cut, which lies inside the padded window, so if every possible cut is over the limit the gate
  rejects whatever the writer picks.

A clip is rejected here only when it would fail under every profile the gate may judge it with: the hourly
profile, and for a spike-lane clip also the express profile (the gate falls back from express to hourly).
Music that cannot be measured is never a reason here (the gate decides then). In the last
`DAY_END_MARGIN_S` before 00:00 UTC the daily cap is not checked, since the count resets before the gate runs.

Switch: `factory.pre_p2_checks` (bool, default off when missing).
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sixstars import lengths, settings
from sixstars.gate import checks, overhaul, profiles
from sixstars.gate import store as gate_store
from sixstars.gate.models import EXPRESS, HOURLY, CreatorFacts, GateSettings, ProfileLimits

SETTING = "factory.pre_p2_checks"
REASON_PREFIX = "pre_p2"
# Cut edges of the gate's own measurement may fall one frame off ours at each end.
EDGE_FRAMES = 2
DAY_END_MARGIN_S = 1800


def enabled() -> bool:
    """`factory.pre_p2_checks`; off for a database that does not have the setting."""
    return settings.get_bool(SETTING, False)


def gate_settings() -> GateSettings:
    """The gate's settings as they apply to a clip of this moment: the profiles, the adaptive-cut minimum
    length and the per-platform hourly lengths (as `gate.gate.run_gate` builds them; the score adjustment
    there does not matter to these checks)."""
    loaded = overhaul.adjust_lengths(profiles.load())
    return profiles.with_platform_lengths(loaded, settings.get(lengths.SETTING, None))


def profiles_to_pass(profile: str, gate: GateSettings) -> list[ProfileLimits]:
    """The profiles the gate may approve a clip under: hourly, or express then hourly. Pure."""
    if profile == HOURLY:
        return [gate.hourly]
    if profile == EXPRESS:
        return [gate.express, gate.hourly]
    raise ValueError(f"unknown gate profile {profile!r}")


def lowest_music_ratio(
    frames: Sequence[bool], min_cut_frames: int, min_run_frames: int, edge_frames: int = EDGE_FRAMES
) -> float:
    """The lowest music ratio the gate could measure on any cut of at least `min_cut_frames` frames inside
    a window whose music flags are `frames`. Each cut is scored the way the gate scores the final cut
    (`checks.effective_music_ratio`), less `edge_frames` music frames for the edges. Pure.

    A window shorter than the minimum cut is scored as a whole."""
    total = len(frames)
    if total == 0:
        return 0.0
    shortest = max(1, min(min_cut_frames, total))
    best = 1.0
    for length in range(shortest, total + 1):
        for start in range(0, total - length + 1):
            part = frames[start : start + length]
            ratio = checks.effective_music_ratio(sum(part) / length, part, min_run_frames)
            assert ratio is not None
            best = min(best, max(0.0, ratio - edge_frames / length))
            if best == 0.0:
                return 0.0
    return best


def near_day_end(now: datetime, margin_s: float = DAY_END_MARGIN_S) -> bool:
    """True in the last `margin_s` before 00:00 UTC. Pure."""
    current = now.astimezone(UTC)
    midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - current).total_seconds() < margin_s


def reject_reason(profile: str, creator: CreatorFacts, gate: GateSettings, music: float | None) -> str | None:
    """Why the gate is certain to reject the clip, or None when it might pass. Pure.

    `music` is the lowest ratio a cut could have (None: not measured, the music check is skipped)."""
    reasons: list[str] = []
    for limits in profiles_to_pass(profile, gate):
        results = [checks.check_creator(creator, limits, gate, music)]
        if music is not None:
            results.append(checks.check_music(music, limits))
        failed = [reason for result in results for reason in result.reasons]
        if not failed:
            return None
        reasons.append(f"{limits.name}: {'; '.join(failed)}")
    return " | ".join(reasons)


def check(
    profile: str,
    creator_id: str | None,
    clip_id: str,
    music_frames: Sequence[bool] | None,
    frame_s: float,
    now: datetime,
) -> str | None:
    """The reason to reject the clip before P2 (prefixed `pre_p2: `), or None. Reads the database."""
    if creator_id is None:
        return None
    gate = gate_settings()
    creator = gate_store.creator_facts(creator_id, clip_id, now)
    if near_day_end(now):
        creator = replace(creator, clips_today=0)
    music: float | None = None
    if music_frames is not None:
        shortest_s = min(limits.min_len_s for limits in profiles_to_pass(profile, gate))
        min_cut_frames = max(1, int(shortest_s / frame_s))
        music = lowest_music_ratio(music_frames, min_cut_frames, gate.music_min_run_frames)
    reason = reject_reason(profile, creator, gate, music)
    return f"{REASON_PREFIX}: {reason}" if reason else None
