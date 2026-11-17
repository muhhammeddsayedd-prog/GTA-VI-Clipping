"""Live slot planning and VOD picking as pure functions: keep, release after the grace time below the
cutoff, one source per creator, fill to the maximum, forbidden and unchecked creators."""

from datetime import UTC, datetime, timedelta

from sixstars.discovery import assign, tiers
from sixstars.discovery.assign import Session
from sixstars.discovery.models import Candidate, Creator, Scored

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
CHECKED = NOW - timedelta(days=1)


def creator(name: str, platform: str = "youtube", tier: str = "auto", checked: bool = True) -> Creator:
    return Creator(
        id=f"id-{platform}-{name}",
        platform=platform,  # type: ignore[arg-type]
        channel_id=f"ch-{name}",
        handle=name,
        display_name=name,
        tier=tier,  # type: ignore[arg-type]
        description_checked_at=CHECKED if checked else None,
    )


def live(name: str, score: float, viewers: int | None = 1000, who: Creator | None = None) -> Scored:
    owner = who or creator(name)
    candidate = Candidate(
        platform=owner.platform,
        source_type="live",
        source_id=f"live-{owner.platform}-{name}",
        url="https://example.test/live",
        title=f"{name} live",
        channel_id=owner.channel_id,
        channel_handle=owner.handle,
        channel_name=name,
        origin="test",
        viewers=viewers,
    )
    return Scored(candidate, owner, score)


def video(name: str, score: float, who: Creator | None = None, number: int = 1) -> Scored:
    owner = who or creator(name)
    candidate = Candidate(
        platform=owner.platform,
        source_type="vod",
        source_id=f"vod-{name}-{number}",
        url="https://example.test/vod",
        title=f"{name} video {number}",
        channel_id=owner.channel_id,
        channel_handle=owner.handle,
        channel_name=name,
        origin="test",
        views=1000,
    )
    return Scored(candidate, owner, score)


def session(
    item: Scored, since: datetime | None = None, status: str = "recording", tier: str = "auto"
) -> Session:
    return Session(
        id=f"s-{item.candidate.source_id}",
        creator_id=item.creator.id,
        platform=item.candidate.platform,
        source_id=item.candidate.source_id,
        status=status,
        below_cutoff_since=since,
        identity=assign.identity(item.creator),
        creator_tier=tier,
    )


def plan(
    sessions: list[Session], candidates: list[Scored], max_slots: int = 3, strict: bool = True
) -> assign.SlotPlan:
    return assign.plan_slots(
        sessions,
        candidates,
        cutoff=55,
        release_after_s=1200,
        max_slots=max_slots,
        min_viewers=50,
        now=NOW,
        allowed=lambda who: tiers.clip_allowed(who, strict),
    )


# ── live slots ───────────────────────────────────────────────


def test_fills_free_slots_with_the_best_streams() -> None:
    result = plan([], [live("a", 60), live("b", 90), live("c", 70), live("d", 80)])
    assert [item.candidate.channel_name for item in result.assign] == ["b", "d", "c"]
    assert result.keep == [] and result.release == []


def test_streams_below_the_cutoff_get_no_slot() -> None:
    assert [i.candidate.channel_name for i in plan([], [live("a", 54.9), live("b", 55)]).assign] == ["b"]


def test_keeps_a_session_that_is_still_above_the_cutoff() -> None:
    current = live("a", 70)
    result = plan([session(current, since=NOW - timedelta(minutes=5))], [current, live("b", 90)], max_slots=1)
    assert result.keep == [assign.Keep(f"s-{current.candidate.source_id}", 70, None)]
    assert result.assign == [] and result.release == []


def test_first_drop_below_the_cutoff_starts_the_clock() -> None:
    current = live("a", 40)
    result = plan([session(current)], [current])
    assert result.keep == [assign.Keep(f"s-{current.candidate.source_id}", 40, NOW)]
    assert result.release == []


def test_session_below_cutoff_for_19_minutes_is_kept() -> None:
    current = live("a", 40)
    since = NOW - timedelta(minutes=19)
    result = plan([session(current, since=since)], [current])
    assert result.keep == [assign.Keep(f"s-{current.candidate.source_id}", 40, since)]


def test_session_below_cutoff_for_20_minutes_is_released_and_its_slot_refilled() -> None:
    current = live("a", 40)
    result = plan(
        [session(current, since=NOW - timedelta(minutes=20))], [current, live("b", 80)], max_slots=1
    )
    assert result.release == [assign.Release(f"s-{current.candidate.source_id}", "below_cutoff")]
    assert [item.candidate.channel_name for item in result.assign] == ["b"]


def test_stream_that_is_no_longer_seen_counts_as_below_the_cutoff() -> None:
    gone = live("a", 80)
    first = plan([session(gone)], [])
    assert first.keep == [assign.Keep(f"s-{gone.candidate.source_id}", None, NOW)]
    later = plan([session(gone, since=NOW - timedelta(minutes=25))], [])
    assert [release.reason for release in later.release] == ["below_cutoff"]


def test_one_source_per_creator_across_platforms() -> None:
    on_twitch = live("SpeedyRP", 90, who=creator("SpeedyRP", platform="twitch"))
    on_youtube = live("Speedy RP", 80, who=creator("Speedy RP", platform="youtube"))
    result = plan([], [on_youtube, on_twitch, live("other", 60)])
    assert [item.candidate.platform for item in result.assign] == ["twitch", "youtube"]
    assert [item.candidate.channel_name for item in result.assign] == ["SpeedyRP", "other"]


def test_creator_with_a_kept_session_gets_no_second_slot() -> None:
    recording = live("SpeedyRP", 70, who=creator("SpeedyRP", platform="twitch"))
    same_person = live("speedyrp", 95, who=creator("speedyrp", platform="youtube"))
    result = plan([session(recording)], [recording, same_person])
    assert result.assign == []


def test_never_exceeds_max_slots_and_zero_assigns_nothing() -> None:
    current = live("a", 70)
    result = plan([session(current)], [current, live("b", 90), live("c", 80)], max_slots=2)
    assert [item.candidate.channel_name for item in result.assign] == ["b"]
    assert plan([], [live("b", 90)], max_slots=0).assign == []


def test_forbidden_creator_session_is_released_at_once() -> None:
    current = live("a", 90)
    result = plan([session(current, tier="restricted")], [current])
    assert result.release == [assign.Release(f"s-{current.candidate.source_id}", "creator_forbidden")]
    assert result.keep == []


def test_forbidden_and_unchecked_creators_get_no_slot() -> None:
    candidates = [
        live("blocked", 99, who=creator("blocked", tier="blocked")),
        live("restricted", 98, who=creator("restricted", tier="restricted")),
        live("unchecked", 97, who=creator("unchecked", checked=False)),
        live("fine", 60),
    ]
    assert [item.candidate.channel_name for item in plan([], candidates).assign] == ["fine"]
    relaxed = plan([], candidates, strict=False)
    assert [item.candidate.channel_name for item in relaxed.assign] == ["unchecked", "fine"]


def test_small_or_unknown_audience_gets_no_slot() -> None:
    candidates = [live("tiny", 90, viewers=49), live("hidden", 90, viewers=None), live("ok", 60, viewers=50)]
    assert [item.candidate.channel_name for item in plan([], candidates).assign] == ["ok"]


# ── videos ───────────────────────────────────────────────────


def pick(videos: list[Scored], **overrides: object) -> list[str]:
    arguments: dict[str, object] = {
        "cutoff": 50,
        "max_total": 6,
        "max_per_creator": 1,
        "known": set(),
        "allowed": lambda who: tiers.clip_allowed(who, True),
    }
    arguments.update(overrides)
    picked = assign.pick_videos(videos, **arguments)  # type: ignore[arg-type]
    return [item.candidate.source_id for item in picked]


def test_picks_best_videos_above_the_cutoff_up_to_the_maximum() -> None:
    videos = [video(name, score) for name, score in [("a", 60), ("b", 49), ("c", 80), ("d", 70)]]
    assert pick(videos) == ["vod-c-1", "vod-d-1", "vod-a-1"]
    assert pick(videos, max_total=2) == ["vod-c-1", "vod-d-1"]
    assert pick(videos, max_total=0) == []


def test_skips_videos_already_known() -> None:
    videos = [video("a", 60), video("b", 70)]
    assert pick(videos, known={("youtube", "vod-b-1")}) == ["vod-a-1"]


def test_caps_videos_per_creator() -> None:
    owner = creator("a")
    videos = [video("a", 90, who=owner, number=1), video("a", 80, who=owner, number=2), video("b", 60)]
    assert pick(videos) == ["vod-a-1", "vod-b-1"]
    assert pick(videos, max_per_creator=2) == ["vod-a-1", "vod-a-2", "vod-b-1"]


def test_forbidden_and_unchecked_creators_are_never_picked() -> None:
    videos = [
        video("blocked", 99, who=creator("blocked", tier="blocked")),
        video("restricted", 98, who=creator("restricted", tier="restricted")),
        video("unchecked", 97, who=creator("unchecked", checked=False)),
        video("preferred", 60, who=creator("preferred", tier="preferred")),
    ]
    assert pick(videos) == ["vod-preferred-1"]
    relaxed = pick(videos, allowed=lambda who: tiers.clip_allowed(who, False))
    assert relaxed == ["vod-unchecked-1", "vod-preferred-1"]


def test_low_hourly_queue_widens_the_run() -> None:
    assert assign.widen_for_low_queue(50, 6, approved_hourly=11, low_watermark=12) == (40, 12, True)
    assert assign.widen_for_low_queue(50, 6, approved_hourly=12, low_watermark=12) == (50, 6, False)
