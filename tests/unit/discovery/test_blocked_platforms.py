"""A platform that cannot be downloaded or recorded from this machine (fix-blocked-platform-queue), as
pure decisions: the blocked platform gets no new videos and cannot fill the backlog, one probe is queued
only when nothing of it is open, the videos kept waiting are bounded, live slots go to the platform that
can record, and the operator's platform weight."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sixstars.discovery import assign, blocked, scoring, tiers
from sixstars.discovery.blocked import BlockedVideo, Queue
from sixstars.discovery.models import Candidate, Creator, Scored
from sixstars.vod import health as vod_health

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
BOT_CHECK = "ERROR: [youtube] abc: Sign in to confirm you're not a bot."


def creator(name: str, platform: str) -> Creator:
    return Creator(
        id=f"id-{platform}-{name}",
        platform=platform,  # type: ignore[arg-type]
        channel_id=f"ch-{name}",
        handle=name,
        display_name=name,
        tier="auto",
        description_checked_at=NOW - timedelta(days=1),
    )


def item(name: str, platform: str, score: float, kind: str = "vod") -> Scored:
    owner = creator(name, platform)
    candidate = Candidate(
        platform=owner.platform,
        source_type=kind,  # type: ignore[arg-type]
        source_id=f"{kind}-{name}",
        url="https://example.test/x",
        title=name,
        channel_id=owner.channel_id,
        channel_handle=owner.handle,
        channel_name=name,
        origin="test",
        views=1000,
        viewers=1000 if kind == "live" else None,
    )
    return Scored(candidate, owner, score)


def pick(videos: list[Scored], max_total: int, limits: dict[str, int]) -> tuple[list[str], dict[str, int]]:
    held: dict[str, int] = {}
    picked = assign.pick_videos(
        videos,
        cutoff=50,
        max_total=max_total,
        max_per_creator=1,
        known=set(),
        allowed=lambda who: tiers.clip_allowed(who, True),
        platform_limit=limits,
        held=held,
    )
    return [video.candidate.source_id for video in picked], held


MIXED = [
    item("y1", "youtube", 95),
    item("y2", "youtube", 90),
    item("t1", "twitch", 80),
    item("y3", "youtube", 75),
    item("t2", "twitch", 70),
    item("t3", "twitch", 40),  # below the cutoff
]

# ── VOD queueing ─────────────────────────────────────────────


def test_a_blocked_platform_gets_no_videos_and_the_working_one_gets_the_whole_share() -> None:
    picked, held = pick(MIXED, 2, {"youtube": 0})
    assert picked == ["vod-t1", "vod-t2"]  # not one slot of the run went to YouTube
    assert held == {"youtube": 3}


def test_without_a_block_the_ranking_alone_decides() -> None:
    assert pick(MIXED, 2, {}) == (["vod-y1", "vod-y2"], {})


def test_a_probe_is_one_video_of_the_blocked_platform() -> None:
    picked, held = pick(MIXED, 6, {"youtube": 1})
    assert picked == ["vod-y1", "vod-t1", "vod-t2"]
    assert held == {"youtube": 2}


def test_a_backlog_full_of_blocked_videos_does_not_stop_the_working_platform() -> None:
    # The staging server: 24 YouTube videos wait out the block, `discovery.max_vod_backlog` is 24.
    queues = {"youtube": Queue(waiting=0, blocked=24)}
    assert blocked.vod_room(6, 24, queues) == 6
    # Videos that really wait for the lane still count, whatever their platform.
    assert blocked.vod_room(6, 24, {"youtube": Queue(blocked=24), "twitch": Queue(waiting=20)}) == 4
    assert blocked.vod_room(6, 24, {"twitch": Queue(waiting=30)}) == 0
    assert blocked.vod_room(6, 24, {}) == 6


def test_the_waiting_videos_are_the_probe_and_one_is_queued_only_when_none_is_open() -> None:
    assert blocked.vod_limits({"youtube"}, {"youtube": Queue(blocked=3)}) == {"youtube": 0}
    assert blocked.vod_limits({"youtube"}, {"youtube": Queue(processing=1)}) == {"youtube": 0}
    assert blocked.vod_limits({"youtube"}, {"youtube": Queue(waiting=1)}) == {"youtube": 0}
    assert blocked.vod_limits({"youtube"}, {"twitch": Queue(waiting=5)}) == {"youtube": 1}
    assert blocked.vod_limits(set(), {"youtube": Queue(blocked=3)}) == {}  # no block: no cap at all


def test_only_the_newest_blocked_videos_are_kept_waiting() -> None:
    waiting = [BlockedVideo(f"y{n}", "youtube", NOW - timedelta(hours=n)) for n in range(5)]
    waiting += [BlockedVideo(f"t{n}", "twitch", NOW - timedelta(hours=n)) for n in range(5)]
    dropped = blocked.overflow(waiting, {"youtube"}, keep=2)
    assert [video.id for video in dropped] == ["y4", "y3", "y2"]  # oldest first; Twitch is not blocked
    assert blocked.overflow(waiting, {"youtube"}, keep=5) == []
    assert len(blocked.overflow(waiting, {"youtube", "twitch"}, keep=0)) == 10


def test_the_note_of_the_pass_names_the_platform_and_the_numbers() -> None:
    note = blocked.vod_note("youtube", NOW - timedelta(hours=3), 5, 0, Queue(blocked=12))
    assert note == (
        "youtube downloads are blocked since 2026-11-17 09:00 UTC: 5 video(s) not queued, "
        "12 waiting for the VOD lane's retry"
    )
    assert "1 queued as a probe" in blocked.vod_note("youtube", NOW, 4, 1, Queue())


# ── the VOD lane's record ────────────────────────────────────


def test_the_vod_record_counts_refusals_and_is_cleared_by_a_download() -> None:
    state = vod_health.note_blocked({}, "youtube", NOW, BOT_CHECK, "no proxy")
    state = vod_health.note_blocked(state, "youtube", NOW + timedelta(minutes=30), BOT_CHECK * 50, "no proxy")
    entry = state["youtube"]
    assert (entry["blocks"], entry["since"]) == (2, NOW.isoformat())
    assert entry["last_blocked_at"] == (NOW + timedelta(minutes=30)).isoformat()
    assert len(entry["error"]) == vod_health.ERROR_CHARS and entry["access"] == "no proxy"
    assert vod_health.note_ok(state, "twitch") == state
    assert vod_health.note_ok(state, "youtube") == {}


def test_a_block_that_is_not_refreshed_stops_counting() -> None:
    state = vod_health.note_blocked({}, "youtube", NOW, BOT_CHECK, None)
    assert vod_health.blocked(state, NOW + timedelta(minutes=119), 7200) == {"youtube": NOW}
    assert vod_health.blocked(state, NOW + timedelta(minutes=121), 7200) == {}
    assert vod_health.blocked({"youtube": {"since": "nonsense"}}, NOW, 7200) == {}
    assert vod_health.blocked({}, NOW, 7200) == {}


# ── live slots ───────────────────────────────────────────────

LIVE = [
    item("y1", "youtube", 95, "live"),
    item("y2", "youtube", 90, "live"),
    item("t1", "twitch", 80, "live"),
    item("t2", "twitch", 70, "live"),
]


def slots(limits: dict[str, int], max_slots: int = 2) -> assign.SlotPlan:
    return assign.plan_slots(
        [],
        LIVE,
        cutoff=55,
        release_after_s=1200,
        max_slots=max_slots,
        min_viewers=50,
        now=NOW,
        allowed=lambda who: tiers.clip_allowed(who, True),
        platform_limit=limits,
    )


def test_live_slots_go_to_the_platform_that_can_record() -> None:
    plan = slots({"youtube": 0})
    assert [stream.candidate.source_id for stream in plan.assign] == ["live-t1", "live-t2"]
    assert plan.held == {"youtube": 2}
    assert [stream.candidate.source_id for stream in slots({}).assign] == ["live-y1", "live-y2"]


def test_a_platform_after_its_back_off_gets_one_slot_as_the_probe() -> None:
    plan = slots({"youtube": 1}, max_slots=3)
    assert [stream.candidate.source_id for stream in plan.assign] == ["live-y1", "live-t1", "live-t2"]


def test_when_every_platform_is_blocked_no_slot_is_assigned_and_the_summary_says_so() -> None:
    limits = {"twitch": 0, "youtube": 0}
    plan = slots(limits)
    assert plan.assign == [] and plan.held == {"youtube": 2, "twitch": 2}
    assert blocked.live_notes(limits, plan.held, {"youtube", "twitch"}) == [
        "no live slot assigned: every platform with streams is blocked (twitch, youtube)"
    ]
    assert blocked.live_notes({"youtube": 0}, {"youtube": 2}, {"youtube", "twitch"}) == [
        "youtube live recording is blocked: 2 stream(s) got no slot"
    ]
    assert blocked.live_notes({"youtube": 0}, {}, set()) == []


def test_live_limits_follow_the_live_lanes_back_off() -> None:
    state: dict[str, dict[str, Any]] = {
        "youtube": {"blocks": 3, "skip_until": (NOW + timedelta(minutes=10)).isoformat(), "access": "direct"},
        "twitch": {"blocks": 1, "skip_until": (NOW - timedelta(minutes=1)).isoformat(), "access": None},
        "kick": {"blocks": 0},
    }
    assert blocked.live_limits(state, NOW) == {"twitch": 1, "youtube": 0}
    # the owner changed how YouTube is reached: the back-off is over, one stream probes
    assert blocked.live_limits(state, NOW, lambda platform: "proxy") == {"twitch": 1, "youtube": 1}
    assert blocked.live_limits({}, NOW) == {}


# ── platform weight ──────────────────────────────────────────


def test_the_platform_weight_multiplies_the_score_and_is_noted() -> None:
    parts: dict[str, Any] = {}
    assert scoring.apply_platform_weight(60.0, parts, "twitch", {"twitch": 1.25}) == 75.0
    assert parts == {"platform_weight": 1.25}
    untouched: dict[str, Any] = {}
    assert scoring.apply_platform_weight(60.0, untouched, "youtube", {"twitch": 1.25}) == 60.0
    assert scoring.apply_platform_weight(60.0, untouched, "youtube", {}) == 60.0
    assert untouched == {}  # no weight: nothing changes, nothing is noted
    assert scoring.apply_platform_weight(90.0, {}, "twitch", {"twitch": 2}) == 100.0  # stays inside 0..100
    assert scoring.apply_platform_weight(90.0, {}, "twitch", {"twitch": 0}) == 0.0


def test_a_weight_lets_the_operator_favour_a_platform_in_the_ranking() -> None:
    weights = {"twitch": 1.3}
    weighted = [
        Scored(
            video.candidate,
            video.creator,
            scoring.apply_platform_weight(video.score, {}, video.candidate.platform, weights),
        )
        for video in MIXED
    ]
    # Twitch 80 -> 100 (capped) and 70 -> 91 now rank around YouTube's 95 and 90.
    assert pick(weighted, 3, {})[0] == ["vod-t1", "vod-y1", "vod-t2"]
