"""The delivery guard against Postgres: trip, pause the hourly lane, resume (docs/28 "Fix first")."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from guardian_support import IG, TT, creator, published, row, world
from guardian_support import now as guardian_now

from sixstars import config, db
from sixstars.api.main import create_app
from sixstars.learning import delivery
from sixstars.posting.store import DbStore
from sixstars.scoring import config as scoring_config

__all__ = ["world"]  # fixture

TOKEN = "integration-test-token-0123456789abcdef0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, world: None) -> Iterator[TestClient]:
    monkeypatch.setenv("CONTROL_API_TOKEN", TOKEN)
    config.env.cache_clear()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def guard_on() -> None:
    db.execute(
        "update scoring_config set value = jsonb_set(value, '{enabled}', 'true') where key = %s",
        (delivery.FLAG,),
    )
    scoring_config.invalidate()


def read(post_id: str) -> None:
    db.execute("update posts set stats_updated_at = now() where id = %s", (post_id,))


def test_flag_off_does_nothing(world: None) -> None:
    star = creator("Star")
    for i in range(5):
        read(published(star, IG, f"ig{i}", age=timedelta(hours=7 + i), views=0))
    scoring_config.invalidate()

    assert delivery.run(guardian_now()) == []
    assert row("platform_accounts", IG)["hourly_paused_until"] is None


def test_zero_views_pause_the_hourly_lane_mark_posts_and_alert(world: None) -> None:
    guard_on()
    star = creator("Star")
    ids = [published(star, IG, f"ig{i}", age=timedelta(hours=7 + i), views=0) for i in range(5)]
    for post_id in ids:
        read(post_id)

    verdicts = {v.account_id: v for v in delivery.run(guardian_now())}

    assert verdicts[IG].tripped
    account = row("platform_accounts", IG)
    assert account["hourly_paused_until"] is not None
    assert "median views" in account["hourly_pause_reason"]
    assert all(row("posts", post_id)["delivery_suspect"] for post_id in ids)
    assert db.fetch_one(
        "select 1 from events where type = 'delivery.guard_tripped' and entity_id = %s", (IG,)
    )
    assert db.fetch_one("select 1 from notify_outbox where title like 'Hourly lane paused%%'")
    loaded = {a.id: a for a in DbStore().load_accounts()}
    assert loaded[IG].hourly_paused_until is not None


def test_unread_posts_do_not_trip(world: None) -> None:
    guard_on()
    star = creator("Star")
    for i in range(5):
        published(star, IG, f"ig{i}", age=timedelta(hours=7 + i), views=None)

    verdicts = {v.account_id: v for v in delivery.run(guardian_now())}

    assert not verdicts[IG].tripped
    assert row("platform_accounts", IG)["hourly_paused_until"] is None


def test_tiktok_posts_without_an_id_trip(world: None) -> None:
    guard_on()
    star = creator("Star")
    for i in range(5):
        post_id = published(star, TT, f"tt{i}", age=timedelta(hours=7 + i))
        db.execute("update posts set platform_post_id = null where id = %s", (post_id,))

    verdicts = {v.account_id: v for v in delivery.run(guardian_now())}

    assert verdicts[TT].tripped


def test_resume_route_clears_the_pause(client: TestClient) -> None:
    guard_on()
    star = creator("Star")
    for i in range(5):
        read(published(star, IG, f"ig{i}", age=timedelta(hours=7 + i), views=0))
    delivery.run(guardian_now())

    answer = client.post("/v1/control/lane/resume", headers=AUTH, json={"text": f"/resume {IG}"})

    assert answer.status_code == 200
    assert answer.json()["changed"] is True
    assert "resumed" in answer.json()["text"]
    assert row("platform_accounts", IG)["hourly_paused_until"] is None
    usage = client.post("/v1/control/lane/resume", headers=AUTH, json={"text": "/resume"})
    assert "Usage" in usage.json()["text"]
