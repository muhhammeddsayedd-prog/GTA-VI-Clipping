"""M20 go-live: mix / creator day rotation and the creator-day selection."""

from datetime import UTC, datetime, timedelta

from sixstars.longform.config import RankWeights
from sixstars.longform.rotation import format_for
from sixstars.longform.selector import Candidate, Limits, SoloLimits, select_creator_day

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
LIMITS = Limits(
    target_s=120,
    min_s=90,
    max_s=150,
    segment_min_s=15,
    segment_max_s=45,
    lead_s=3,
    tail_s=2,
    max_per_creator=2,
    max_creator_s=60,
    pool_hours=168,
)
W = RankWeights()


def cand(i: int, *, creator: str, score: float = 7.0, topic: str | None = None) -> Candidate:
    return Candidate(
        clip_id=f"c{i:02d}",
        creator_id=creator,
        creator_name=creator,
        title=f"moment {i}",
        score=score,
        clip_s=20.0,
        window_s=25.0,
        score_z=2.0,
        views=None,
        topic=topic,
        created_at=NOW - timedelta(hours=2),
    )


SOLO = SoloLimits(min_s=100, target_s=150, max_s=180, min_moments=4)


def test_rotation_alternates_after_the_last_built_episode() -> None:
    cycle = ("mix", "creator")
    assert format_for(None, cycle) == "mix"
    assert format_for("mix", cycle) == "creator"
    assert format_for("creator", cycle) == "mix"


def test_rotation_ignores_unknown_formats_and_bad_cycles() -> None:
    assert format_for("weird", ("mix", "creator")) == "mix"
    assert format_for("mix", ("creator",)) == "creator"
    assert format_for("mix", ()) == "mix"
    assert format_for("mix", ("bogus",)) == "mix"


def many(creator: str, n: int, start: int, score: float = 7.0, topic: str | None = None) -> list[Candidate]:
    return [cand(start + k, creator=creator, score=score, topic=topic) for k in range(n)]


def test_creator_with_the_most_moments_wins_and_fills_only_their_moments() -> None:
    pool = (
        many("alpha", 6, 0)
        + many("beta", 4, 20, score=9.5)
        + [cand(40, creator="g1"), cand(41, creator="g2")]
    )
    day = select_creator_day(pool, LIMITS, SOLO, W, NOW, frozenset())
    assert day is not None
    assert day.creator_id == "alpha"
    picks = day.selection.picks
    assert {p.candidate.creator_id for p in picks} == {"alpha"}
    assert SOLO.min_s <= day.selection.total_s <= SOLO.max_s
    assert [p.position for p in picks] == list(range(len(picks), 0, -1))


def test_cooling_creator_is_skipped() -> None:
    pool = many("alpha", 6, 0) + many("beta", 5, 20)
    day = select_creator_day(pool, LIMITS, SOLO, W, NOW, frozenset({"alpha"}))
    assert day is not None and day.creator_id == "beta"


def test_no_creator_with_enough_moments_returns_none() -> None:
    pool = many("alpha", 3, 0) + many("beta", 3, 20)
    assert select_creator_day(pool, LIMITS, SOLO, W, NOW, frozenset()) is None


def test_repeated_topics_do_not_count_toward_the_minimum() -> None:
    pool = many("alpha", 6, 0, topic="same")
    assert select_creator_day(pool, LIMITS, SOLO, W, NOW, frozenset()) is None
