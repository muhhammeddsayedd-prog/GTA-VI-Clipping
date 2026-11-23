"""Campaign watcher rules (D117, PR 2): reserved live slots, re-tagging, preemption, game check, and the
clip tag (campaign hours for spikes). Pure, no database."""

from datetime import UTC, datetime

from sixstars.campaigns import tagging, watcher
from sixstars.campaigns.watcher import ActiveSession, LiveNow
from sixstars.clients.fakes import fake_stream


def live(creator: str, value: float = 1.0, campaign: str = "k1", ok: bool = True) -> LiveNow:
    return LiveNow(
        creator,
        campaign,
        "twitch",
        f"s-{creator}",
        creator,
        f"https://twitch.tv/{creator}",
        "Grand Theft Auto VI",
        "title",
        100,
        ok,
        value,
    )


def session(sid: str, creator: str, campaign: str | None = None, score: float | None = 50.0) -> ActiveSession:
    return ActiveSession(sid, creator, campaign, score)


def test_a_live_campaign_stream_takes_a_free_slot() -> None:
    out = watcher.plan([live("c1")], [], {"c1": "k1"}, max_slots=10, campaign_slots=4)
    assert [s.creator_id for s in out.start] == ["c1"]
    assert out.preempt == [] and out.overflow == []


def test_a_full_lane_releases_the_weakest_trending_session() -> None:
    sessions = [session(f"t{i}", f"trend{i}", score=float(60 + i)) for i in range(10)]
    out = watcher.plan([live("c1")], sessions, {"c1": "k1"}, max_slots=10, campaign_slots=4)
    assert out.preempt == ["t0"]
    assert [s.creator_id for s in out.start] == ["c1"]


def test_campaign_slots_are_capped_and_ranked_by_expected_value() -> None:
    streams = [live("a", 5), live("b", 50), live("c", 20)]
    out = watcher.plan(streams, [], {"a": "k1", "b": "k1", "c": "k1"}, max_slots=10, campaign_slots=2)
    assert [s.creator_id for s in out.start] == ["b", "c"]
    assert [s.creator_id for s in out.overflow] == ["a"]


def test_a_creator_recorded_under_trending_is_retagged_not_restarted() -> None:
    out = watcher.plan([live("c1")], [session("s1", "c1")], {"c1": "k1"}, max_slots=10, campaign_slots=4)
    assert out.retag == [("s1", "k1")]
    assert out.start == []


def test_wrong_game_gets_no_slot_and_drops_the_tag() -> None:
    out = watcher.plan([live("c1", ok=False)], [], {"c1": "k1"}, max_slots=10, campaign_slots=4)
    assert out.start == [] and out.overflow == []
    tagged = [session("s1", "c1", "k1")]
    out = watcher.plan([live("c1", ok=False)], tagged, {"c1": "k1"}, max_slots=10, campaign_slots=4)
    assert out.untag == ["s1"]


def test_an_ended_campaign_untags_its_sessions_and_a_new_winner_retags() -> None:
    sessions = [session("s1", "c1", "old"), session("s2", "c2", "k1")]
    out = watcher.plan([], sessions, {"c2": "k2"}, max_slots=10, campaign_slots=4)
    assert out.untag == ["s1"]
    assert out.retag == [("s2", "k2")]


def test_campaign_sessions_are_never_preempted() -> None:
    sessions = [session(f"k{i}", f"camp{i}", "k1") for i in range(10)]
    watched = {f"camp{i}": "k1" for i in range(10)} | {"new": "k1"}
    out = watcher.plan([live("new")], sessions, watched, max_slots=10, campaign_slots=20)
    assert out.preempt == []
    assert [s.creator_id for s in out.overflow] == ["new"]


def test_game_and_value_helpers() -> None:
    assert watcher.game_ok(None, "Just Chatting")
    assert watcher.game_ok("Grand Theft Auto VI", "grand theft auto vi")
    assert not watcher.game_ok("Grand Theft Auto VI", "Grand Theft Auto V")
    assert watcher.expected_value(1.2, 1000, 100) == 1.2 * 1000 * 100
    assert watcher.went_offline({"a": True, "b": False, "c": True}, {"c"}, ["a", "b", "c"]) == ["a"]


def test_live_from_twitch_maps_streams_to_campaigns() -> None:
    who = watcher.Watched(
        "c1", "twitch", "user0", "lucky", "k1", "lucky", 0, 1.0, 100.0, "Grand Theft Auto VI"
    )
    stream = fake_stream(0)
    found = watcher.live_from_twitch([stream, fake_stream(1)], {"user0": who})
    assert len(found) == 1
    assert found[0].creator_id == "c1" and found[0].game_ok is (stream.game_name == "Grand Theft Auto VI")


def test_pick_campaigns_keeps_the_best_priority_per_creator() -> None:
    rows = [
        {
            "creator_id": "c1",
            "platform": "twitch",
            "channel_id": "u1",
            "handle": "x",
            "campaign_id": "hi",
            "slug": "hi",
            "priority": 2,
            "rate_per_1k": 1,
            "budget_left": None,
            "required_game": None,
        },
        {
            "creator_id": "c1",
            "platform": "twitch",
            "channel_id": "u1",
            "handle": "x",
            "campaign_id": "lo",
            "slug": "lo",
            "priority": 0,
            "rate_per_1k": 1,
            "budget_left": 5,
            "required_game": None,
        },
    ]
    assert [w.campaign_id for w in watcher.pick_campaigns(rows)] == ["hi"]


# ── clip tag ─────────────────────────────────────────────────

HOURS = frozenset(range(8, 22))
ROWS = [
    {"id": "k-high", "slug": "high", "render_profile": "clean-disclose"},
    {"id": "k-low", "slug": "low", "render_profile": "brand-disclose"},
]


def test_the_highest_priority_campaign_wins_the_clip() -> None:
    tag = tagging.pick(ROWS, "hourly", 3, HOURS)
    assert tag == tagging.Tag("k-high", "high", "clean-disclose")


def test_a_night_spike_stays_normal_but_a_night_hourly_clip_is_tagged() -> None:
    assert tagging.pick(ROWS, "spike", 23, HOURS) is None
    assert tagging.pick(ROWS, "spike", 8, HOURS) is not None
    assert tagging.pick(ROWS, "hourly", 23, HOURS) is not None
    assert tagging.pick([], "hourly", 12, HOURS) is None


def test_cairo_hours_by_default() -> None:
    assert tagging.DEFAULT_HOURS["default"][0] == 8 and tagging.DEFAULT_HOURS["default"][-1] == 21
    assert datetime(2026, 11, 23, 21, 59, tzinfo=UTC).hour in tagging.DEFAULT_HOURS["default"]


# ── viewer clips and guardian ────────────────────────────────


def test_campaign_viewer_clips_go_first_and_merge_once() -> None:
    from sixstars.clients.fakes import fake_clip
    from sixstars.viewer_clips import select

    game = [fake_clip(0), fake_clip(1)]
    extra = [fake_clip(1), fake_clip(2)]
    merged = select.merge_clips(game, extra)
    assert [clip.id for clip in merged] == [game[0].id, game[1].id, extra[1].id]
    ranked = [select.Ranked(clip, rank=float(10 - i)) for i, clip in enumerate(merged)]
    target = merged[2].broadcaster_id
    ordered = select.campaign_first(ranked, {target})
    assert ordered[0].clip.broadcaster_id == target
    assert select.campaign_first(ranked, set()) == ranked


def test_the_watcher_matters_only_while_a_campaign_is_active() -> None:
    from sixstars.guardian.health import campaign_watcher_stale

    assert not campaign_watcher_stale(None, 0, 300)
    assert campaign_watcher_stale(None, 1, 300)
    assert campaign_watcher_stale(301, 1, 300)
    assert not campaign_watcher_stale(30, 2, 300)
