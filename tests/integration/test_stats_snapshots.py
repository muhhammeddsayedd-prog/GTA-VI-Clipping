"""Fixed-age snapshots in the stats pass, against Postgres (docs/28 Part A, L1)."""

from collections.abc import Iterator
from datetime import timedelta
from itertools import count

import pytest
from guardian_support import IG, TT, UploadPost, YouTube, creator, now, published, row, world

from sixstars import db
from sixstars.scoring import config as scoring_config
from sixstars.stats import collector

__all__ = ["world"]  # fixture


@pytest.fixture(autouse=True)
def flags_reset() -> Iterator[None]:
    """Flags written by a test are switched back off (fixtures don't restore scoring_config)."""
    yield
    db.execute(
        "update scoring_config set value = jsonb_set(value, '{enabled}', 'false') where key = any(%s)",
        (["learning.delivery_guard", "learning.snapshots"],),
    )
    scoring_config.invalidate()


def snapshots_on() -> None:
    db.execute(
        "update scoring_config set value = jsonb_set(value, '{enabled}', 'true') "
        "where key = 'learning.snapshots'"
    )
    scoring_config.invalidate()


def snapshot_rows() -> list[dict]:  # type: ignore[type-arg]
    return db.fetch_all("select * from post_metrics_snapshots order by target_age_h, post_id")


def test_flag_off_takes_no_snapshot(world: None) -> None:
    star = creator("Star")
    published(star, IG, "ig1", age=timedelta(hours=1.2))
    scoring_config.invalidate()

    collector.collect(UploadPost(), YouTube(), now())

    assert snapshot_rows() == []


def test_due_readings_are_stored_once_and_refresh_the_totals(world: None) -> None:
    snapshots_on()
    star = creator("Star")
    young = published(star, IG, "ig1", age=timedelta(hours=1.2))
    older = published(star, TT, "tt1", age=timedelta(hours=3.3))
    published(star, IG, "ig2", age=timedelta(hours=2.0))  # between windows: no reading

    result = collector.collect(UploadPost(), YouTube(), now())

    rows = snapshot_rows()
    assert [(r["post_id"].hex, float(r["target_age_h"])) for r in rows] == [
        (young.replace("-", ""), 1.0),
        (older.replace("-", ""), 3.0),
    ]
    assert all(r["views"] == 1000 and r["source"] == "upload_post" for r in rows)
    assert rows[0]["completion_rate"] is None  # the fake answer has no such field: null, not 0
    assert result.snapshots == 2
    assert row("posts", young)["views"] == 1000

    collector.collect(UploadPost(), YouTube(), now())
    assert len(snapshot_rows()) == 2


def test_readings_left_at_the_end_of_the_budget_go_to_a_worker_job(world: None) -> None:
    snapshots_on()
    star = creator("Star")
    for i in range(3):
        published(star, IG, f"ig{i}", age=timedelta(hours=1.1 + i * 0.1))
    ticks = count()
    clock = lambda: float(next(ticks)) * 10  # noqa: E731 - each call moves 10 s on

    result = collector.collect(UploadPost(), YouTube(), now(), clock, budget_s=25)

    assert result.snapshots_left > 0
    assert result.handed_to_job
    job = db.fetch_one("select type, dedupe_key from jobs where type = 'stats.collect'")
    assert job is not None and job["dedupe_key"] == "stats.collect"


def test_the_job_does_not_queue_itself(world: None) -> None:
    snapshots_on()
    star = creator("Star")
    for i in range(3):
        published(star, IG, f"ig{i}", age=timedelta(hours=1.1 + i * 0.1))
    ticks = count()

    result = collector.collect(
        UploadPost(), YouTube(), now(), lambda: float(next(ticks)) * 10, budget_s=25, from_job=True
    )

    assert result.snapshots_left > 0
    assert not result.handed_to_job
    assert db.fetch_one("select 1 from jobs where type = 'stats.collect'") is None
