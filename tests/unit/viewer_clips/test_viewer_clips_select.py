"""The pure rules of the Twitch Clips source: which clips fit, the rank and its parts, the picking limits
and the same-moment rule. No database, no network."""

from datetime import timedelta

import pytest

from sixstars.clients.fakes import FIXED_TIME, fake_clip
from sixstars.clients.twitch import TwitchClip
from sixstars.viewer_clips import select
from sixstars.viewer_clips.config import ClipsConfig

NOW = FIXED_TIME + timedelta(hours=4)
BLOCKLIST = ("giveaway",)


def config(**changes: float) -> ClipsConfig:
    values: dict[str, float] = {
        "lookback_h": 24,
        "read_per_pass": 100,
        "max_queued_per_pass": 3,
        "max_per_creator_per_pass": 1,
        "min_views": 10,
        "daily_cap": 48,
        "max_backlog": 6,
        "min_age_min": 15,
        "rate_floor_h": 1,
        "min_len_s": 20,
        "max_len_s": 60,
        "source_pad_s": 10,
        **changes,
    }
    return ClipsConfig(enabled=True, render_without_moment=False, **values)  # type: ignore[arg-type]


def allow(clip: TwitchClip) -> str | None:
    return None


def ranked(*clips: TwitchClip) -> list[select.Ranked]:
    return select.rank_all(clips, NOW, config(), BLOCKLIST, 0.85)[0]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({}, None),
        ({"duration_s": 60.0}, None),
        ({"duration_s": 75.0}, None),  # longer than the gate allows: the factory trims it
        ({"duration_s": 19.9}, select.TOO_SHORT),
        ({"view_count": 9}, select.LOW_VIEWS),
        ({"view_count": 10}, None),
        ({"created_at": NOW - timedelta(minutes=14)}, select.TOO_FRESH),
        ({"created_at": NOW - timedelta(minutes=15)}, None),
        ({"title": "Big GIVEAWAY tonight"}, select.TITLE_BLOCKLIST),
        ({"title": "gta 6 leak?"}, select.TITLE_BLOCKLIST),  # "leak" is always blocked
        ({"url": ""}, select.NO_URL),
    ],
)
def test_which_clips_fit(changes: dict[str, object], reason: str | None) -> None:
    assert select.unfit_reason(fake_clip(0, **changes), NOW, config(), BLOCKLIST) == reason


def test_rank_is_views_per_hour_with_the_language_factor_and_keeps_its_parts() -> None:
    english = select.rank_clip(fake_clip(0, view_count=800), NOW, config(), 0.85)
    assert english.rank == 200.0
    assert english.parts == {
        "views": 800,
        "age_h": 4.0,
        "views_per_hour": 200.0,
        "language": "en",
        "language_factor": 1.0,
        "rank": 200.0,
    }
    spanish = select.rank_clip(fake_clip(0, view_count=800, language="es"), NOW, config(), 0.85)
    assert spanish.rank == 170.0 and spanish.parts["language_factor"] == 0.85
    # Twitch always reports the broadcast language: "other" is not taken as English
    other = select.rank_clip(fake_clip(0, view_count=800, language="other"), NOW, config(), 0.85)
    assert other.rank == 170.0 and select.language_of(other.clip) is None


def test_a_very_young_clip_is_rated_over_the_floor_not_over_its_minutes() -> None:
    young = fake_clip(0, view_count=60, created_at=NOW - timedelta(minutes=20))
    assert select.rank_clip(young, NOW, config(), 0.85).rank == 60.0
    assert select.rank_clip(young, NOW, config(rate_floor_h=2), 0.85).rank == 30.0


def test_rank_all_orders_by_rank_counts_the_dropped_and_ignores_a_repeated_id() -> None:
    clips = [
        fake_clip(0, view_count=400),
        fake_clip(1, view_count=900, language="pt"),  # 225 x 0.85 = 191.25
        fake_clip(2, view_count=800),
        fake_clip(2, view_count=800),
        fake_clip(3, duration_s=8.0),
        fake_clip(4, view_count=3),
        fake_clip(5, view_count=2),
    ]
    kept, skipped = select.rank_all(clips, NOW, config(), BLOCKLIST, 0.85)
    assert [item.clip.id for item in kept] == ["FakeClipSlug2", "FakeClipSlug1", "FakeClipSlug0"]
    assert skipped == {select.TOO_SHORT: 1, select.LOW_VIEWS: 2}


@pytest.mark.parametrize(
    ("recent", "waiting", "expected"),
    [(0, 0, 3), (46, 0, 2), (48, 0, 0), (60, 0, 0), (0, 4, 2), (0, 6, 0), (47, 5, 1)],
)
def test_room_is_the_tightest_of_the_three_limits(recent: int, waiting: int, expected: int) -> None:
    assert select.room(config(), recent, waiting) == expected


def test_shortlist_leaves_out_clips_seen_before() -> None:
    items = ranked(fake_clip(0), fake_clip(1), fake_clip(2))
    short = select.shortlist(items, {"FakeClipSlug0"}, 1)
    assert [item.clip.id for item in short] == ["FakeClipSlug1"]


def test_pick_respects_the_total_and_the_per_creator_limit() -> None:
    items = ranked(
        fake_clip(0, view_count=900),
        fake_clip(1, view_count=800, broadcaster_id="user0", video_id="other", vod_offset_s=5000.0),
        fake_clip(2, view_count=700),
        fake_clip(3, view_count=600),
        fake_clip(4, view_count=500),
    )
    chosen = select.pick(items, limit=3, max_per_creator=1, pad_s=10, verdict=allow, taken={})
    assert [item.clip.id for item in chosen.picks] == ["FakeClipSlug0", "FakeClipSlug2", "FakeClipSlug3"]
    assert chosen.reasons == {"FakeClipSlug1": select.PER_CREATOR_LIMIT, "FakeClipSlug4": select.NO_ROOM}
    assert chosen.counts() == {select.PER_CREATOR_LIMIT: 1, select.NO_ROOM: 1}
    two = select.pick(items, limit=3, max_per_creator=2, pad_s=10, verdict=allow, taken={})
    assert [item.clip.id for item in two.picks][:2] == ["FakeClipSlug0", "FakeClipSlug1"]


def test_pick_never_takes_a_refused_creator_and_the_next_clip_moves_up() -> None:
    def verdict(clip: TwitchClip) -> str | None:
        return {"user0": select.FORBIDDEN_CREATOR, "user1": select.CREATOR_UNCHECKED}.get(clip.broadcaster_id)

    items = ranked(fake_clip(0), fake_clip(1), fake_clip(2))
    chosen = select.pick(items, limit=2, max_per_creator=1, pad_s=10, verdict=verdict, taken={})
    assert [item.clip.id for item in chosen.picks] == ["FakeClipSlug2"]
    assert chosen.reasons == {
        "FakeClipSlug0": select.FORBIDDEN_CREATOR,
        "FakeClipSlug1": select.CREATOR_UNCHECKED,
    }


def test_two_viewer_clips_of_the_same_moment_give_one_pick() -> None:
    # two viewers clipped the same seconds of one broadcast; a third clip is far away in the same video
    first = fake_clip(0, view_count=900, broadcaster_id="a", video_id="v1", vod_offset_s=1000.0)
    again = fake_clip(1, view_count=800, broadcaster_id="b", video_id="v1", vod_offset_s=1035.0)
    later = fake_clip(2, view_count=700, broadcaster_id="c", video_id="v1", vod_offset_s=4000.0)
    chosen = select.pick(
        ranked(first, again, later), limit=3, max_per_creator=1, pad_s=10, verdict=allow, taken={}
    )
    assert [item.clip.id for item in chosen.picks] == ["FakeClipSlug0", "FakeClipSlug2"]
    assert chosen.reasons == {"FakeClipSlug1": select.SAME_MOMENT}


def test_a_moment_we_already_have_a_clip_of_is_not_picked_again() -> None:
    clip = fake_clip(0, video_id="v1", vod_offset_s=1000.0)  # 1000..1030 in the broadcast
    for taken, picked in (
        ({"v1": [(940.0, 989.0)]}, True),  # ends 11 s before it starts
        ({"v1": [(940.0, 991.0)]}, False),  # within the 10 s pad
        ({"v1": [(1041.0, 1080.0)]}, True),
        ({"v1": [(1039.0, 1080.0)]}, False),
        ({"other": [(1000.0, 1030.0)]}, True),  # another video
    ):
        chosen = select.pick(ranked(clip), limit=1, max_per_creator=1, pad_s=10, verdict=allow, taken=taken)
        assert bool(chosen.picks) is picked, taken


def test_a_clip_without_a_place_in_the_broadcast_is_not_compared() -> None:
    clip = fake_clip(0, video_id=None, vod_offset_s=None)
    assert select.clip_range(clip) is None
    chosen = select.pick(
        ranked(clip), limit=1, max_per_creator=1, pad_s=10, verdict=allow, taken={"video0": [(0.0, 9e9)]}
    )
    assert len(chosen.picks) == 1


def test_top_rows_expose_the_ranking_parts_and_why_a_clip_was_passed_over() -> None:
    items = ranked(fake_clip(0, view_count=900), fake_clip(1, view_count=800), fake_clip(2, view_count=700))
    rows = select.top_rows(
        items, {"FakeClipSlug0"}, {"FakeClipSlug2": select.NO_ROOM}, {"FakeClipSlug1"}, limit=10
    )
    assert [(row["picked"], row["skipped"]) for row in rows] == [
        (True, None),
        (False, select.ALREADY_SEEN),
        (False, select.NO_ROOM),
    ]
    top = rows[0]
    assert top["channel"] == "Streamer0" and top["views"] == 900 and top["views_per_hour"] == 225.0
    assert top["parts"]["language_factor"] == 1.0 and top["age_h"] == 4.0 and top["duration_s"] == 30.0
    assert len(select.top_rows(items, set(), {}, set(), limit=2)) == 2
