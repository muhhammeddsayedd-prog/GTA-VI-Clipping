"""The four M13 routes through FastAPI's test client against Postgres. The platforms and prompt P7 are
fakes; nothing is sent anywhere (the notify webhook is not configured, so alerts stay in the outbox)."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from guardian_support import (
    TT,
    YT,
    Classifier,
    UploadPost,
    YouTube,
    creator,
    outbox,
    post_states,
    published,
    row,
    setting,
    takedown_rows,
    titles,
    verdict,
    world,
)

from sixstars import config, db
from sixstars.api.main import create_app
from sixstars.guardian import service

__all__ = ["world"]  # fixture

TOKEN = "integration-test-token-0123456789abcdef0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OUR_TIKTOK = "https://www.tiktok.com/@6starsclipping/video/7300000000000000001"


class Platforms:
    def __init__(self) -> None:
        self.upload_post = UploadPost()
        self.youtube = YouTube()
        self.classifier = Classifier(verdict(is_takedown=False))


@pytest.fixture
def platforms(monkeypatch: pytest.MonkeyPatch) -> Platforms:
    fakes = Platforms()
    monkeypatch.setattr(service, "clients", lambda: service.Clients(fakes.upload_post, fakes.youtube))
    monkeypatch.setattr(service, "classify", lambda *args: fakes.classifier(*args))
    return fakes


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, world: None) -> Iterator[TestClient]:
    monkeypatch.setenv("CONTROL_API_TOKEN", TOKEN)
    config.env.cache_clear()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def heartbeat_names() -> set[str]:
    return {r["name"] for r in db.fetch_all("select name from heartbeats")}


def test_the_routes_need_the_token(client: TestClient, platforms: Platforms) -> None:
    assert client.post("/v1/guardian/check").status_code == 401
    assert client.post("/v1/guardian/takedown", json={"message_id": "m"}).status_code == 401
    assert client.post("/v1/stats/collect").status_code == 401
    assert client.get("/v1/reports/daily").status_code == 401
    assert takedown_rows() == [] and heartbeat_names() == set()


def test_guardian_check_reports_a_problem_and_two_of_them_stop_posting(
    client: TestClient, platforms: Platforms
) -> None:
    one, two = creator("One"), creator("Two")
    published(one, YT, "ytAAAAAAAA1")
    published(two, YT, "ytBBBBBBBB1")
    for index in range(8):
        published(creator(f"Fine{index}"), YT, f"ytFINE{index:05d}")

    clean = client.post("/v1/guardian/check", headers=AUTH)
    assert clean.status_code == 200
    assert clean.json() == {
        "status": "ok",
        "checked": 10,
        "problems": [],
        "unverifiable": 0,
        "suspects": 0,
        "removed_by_owner": 0,
        "manual_removals_pending": 0,
        "errors": [],
        "mass_event": [],
        "emergency_stop": False,
        "strikes": 0,
    }
    assert heartbeat_names() == {"trigger:guardian"}

    platforms.youtube.missing = {"ytAAAAAAAA1", "ytBBBBBBBB1"}
    body = client.post("/v1/guardian/check", headers=AUTH).json()
    assert sorted(problem["creator"] for problem in body["problems"]) == ["One", "Two"]
    assert set(body["problems"][0]) == {
        "post_id",
        "platform",
        "account_id",
        "creator",
        "kind",
        "detail",
        "post_url",
    }
    assert body["emergency_stop"] is True and body["strikes"] == 2
    assert setting("control.emergency_stop") is True

    # `/v1/control/start` (the owner) clears it; the guardian does not set it again for the old strikes.
    assert client.post("/v1/control/start", headers=AUTH).json() == {"stopped": False, "changed": True}
    assert client.post("/v1/guardian/check", headers=AUTH).json()["emergency_stop"] is False
    assert setting("control.emergency_stop") is False


def test_takedown_route_end_to_end_and_idempotent(client: TestClient, platforms: Platforms) -> None:
    big = creator("BigStreamer")
    published(big, YT, "ytBIG000001")
    published(big, TT, "7300000000000000001", url=OUR_TIKTOK)
    platforms.classifier = Classifier(verdict(urls=[OUR_TIKTOK]))
    payload = {
        "from": "Big Streamer <big@streamer.test>",
        "subject": "Takedown request",
        "body_text": f"Please remove {OUR_TIKTOK}",
        "received_at": "2026-11-19T10:00:00.000Z",
        "message_id": "18c1f0a2b3c4d5e6",
    }

    first = client.post("/v1/guardian/takedown", headers=AUTH, json=payload)

    assert first.status_code == 200
    body = first.json()
    assert set(body) == {
        "takedown_id",
        "status",
        "is_takedown",
        "duplicate",
        "matched",
        "actions",
        "strike",
        "emergency_stop",
    }
    assert (body["is_takedown"], body["status"], body["duplicate"]) == (True, "partial", False)
    assert [action["action"] for action in body["actions"]] == ["block_creator", "remove_post", "remove_post"]
    assert row("creators", big)["tier"] == "blocked"
    assert (
        post_states()["ytBIG000001"][0] == "removed"
        and post_states()["7300000000000000001"][0] == "remove_failed"
    )
    assert platforms.classifier.seen == [
        ("Big Streamer <big@streamer.test>", "Takedown request", f"Please remove {OUR_TIKTOK}")
    ]
    alerts = outbox()
    assert [title for _, title, _ in alerts] == [
        "Takedown actioned: BigStreamer",
        "ACTION NEEDED: delete 1 post(s) by hand",
    ]

    second = client.post("/v1/guardian/takedown", headers=AUTH, json=payload).json()
    assert second["duplicate"] is True and second["takedown_id"] == body["takedown_id"]
    assert len(takedown_rows()) == 1 and outbox() == alerts and len(platforms.classifier.seen) == 1
    assert "trigger:guardian" not in heartbeat_names()  # an email is not the schedule


def test_takedown_route_with_spam_and_without_a_message_id(client: TestClient, platforms: Platforms) -> None:
    published(creator("BigStreamer"), YT, "ytBIG000001")
    spam: dict[str, Any] = {
        "from": "x@spam.test",
        "subject": "You won",
        "body_text": "Click https://evil.test/win",
    }

    first = client.post("/v1/guardian/takedown", headers=AUTH, json=spam).json()
    second = client.post("/v1/guardian/takedown", headers=AUTH, json=spam).json()

    assert (first["is_takedown"], first["status"], first["actions"]) == (False, "ignored", [])
    assert second["duplicate"] is True  # the id derived from the content made it idempotent
    assert takedown_rows()[0]["message_id"].startswith("sha256:")
    assert post_states()["ytBIG000001"][0] == "published" and outbox() == []


def test_takedown_route_with_an_oversized_email(client: TestClient, platforms: Platforms) -> None:
    big = creator("BigStreamer")
    published(big, TT, "7300000000000000001", url=OUR_TIKTOK)
    platforms.classifier = Classifier(verdict())
    within_limit = {"message_id": "big-1", "body_text": "legal text " * 20_000 + OUR_TIKTOK}
    over_limit = {"message_id": "big-2", "body_text": "x" * 300_000}

    accepted = client.post("/v1/guardian/takedown", headers=AUTH, json=within_limit)
    refused = client.post("/v1/guardian/takedown", headers=AUTH, json=over_limit)

    assert accepted.status_code == 200 and accepted.json()["status"] == "partial"
    assert len(platforms.classifier.seen[0][2]) == 20_000
    assert refused.status_code == 413 and len(takedown_rows()) == 1


def test_stats_collect_route(client: TestClient, platforms: Platforms) -> None:
    star = creator("Star")
    post_id = published(star, YT, "ytSTATS0001")
    response = client.post("/v1/stats/collect", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "accounts": 3,
        "posts": 1,
        "skipped": 0,
        "rate_limited": False,
        "errors": [],
    }
    assert row("posts", post_id)["views"] == 5000 and heartbeat_names() == {"trigger:stats"}
    assert client.post("/v1/stats/collect", headers=AUTH).json()["accounts"] == 0


def test_daily_report_route_in_both_formats(client: TestClient, platforms: Platforms) -> None:
    star = creator("Star")
    published(star, YT, "ytSTATS0001", views=1234, title="<b>Hostile</b> & title")
    as_json = client.get("/v1/reports/daily?format=json", headers=AUTH)
    assert as_json.status_code == 200
    assert {
        "day",
        "accounts",
        "views",
        "top_clips",
        "ypp",
        "posts",
        "spikes",
        "clips",
        "guardian",
        "spend",
    } <= set(as_json.json())
    as_text = client.get("/v1/reports/daily?format=telegram", headers=AUTH)
    assert as_text.status_code == 200 and set(as_text.json()) == {"text"}
    text = as_text.json()["text"]
    assert 0 < len(text) <= 4096 and "daily report" in text and "Guardian" in text
    assert "<b>Hostile</b>" not in text
    assert client.get("/v1/reports/daily?format=telegram&day=2026-11-18", headers=AUTH).status_code == 200
    assert client.get("/v1/reports/daily?format=pdf", headers=AUTH).status_code == 422
    assert titles() == [] and heartbeat_names() == set()  # reading a report changes nothing
