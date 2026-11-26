"""M20 L5: the console's episode routes (`GET /v1/admin/episodes*`) against Postgres."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sixstars import config, db
from sixstars.api import routes_admin
from sixstars.api.main import create_app
from sixstars.storage import FakeClipStorage

DASHBOARD = "integration-dashboard-token-0123456789abcdef0123"
AUTH = {"Authorization": f"Bearer {DASHBOARD}"}
MISSING = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, test_database_url: str) -> Iterator[TestClient]:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", DASHBOARD)
    config.env.cache_clear()
    monkeypatch.setattr(routes_admin, "storage_factory", FakeClipStorage)
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client
    db.execute("delete from episodes where episode_date >= '2032-01-01'")
    db.execute(r"delete from clips where creator_id in (select id from creators where handle like 'lfa\_%%')")
    db.execute(r"delete from creators where handle like 'lfa\_%%'")


def get(client: TestClient, path: str, status: int = 200) -> Any:
    response = client.get(f"/v1/admin/{path}", headers=AUTH)
    assert response.status_code == status, response.text
    return response.json()


def make_episode(with_file: bool) -> str:
    db.execute(
        "insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h) "
        "values ('youtube_main', 'youtube', 'default', '@t', '{hourly}', 16) on conflict (id) do nothing"
    )
    ep = db.fetch_one(
        "insert into episodes (episode_date, status, account_id, title, storage_path, thumb_path, chapters) "
        "values ('2032-02-02', %s, 'youtube_main', 'TOP 2', %s, %s, '[{\"start_s\": 0, \"label\": \"#2\"}]') "
        "returning id::text as id",
        (
            "ready" if with_file else "fetching",
            "episodes/test/2032/02/02/x.mp4" if with_file else None,
            "episodes/test/2032/02/02/x.jpg" if with_file else None,
        ),
    )
    assert ep is not None
    for pos, status in ((1, "rendered"), (2, "rendered"), (100003, "dropped")):
        cr = db.fetch_one(
            "insert into creators (platform, channel_id, handle) values ('twitch', %s, %s) "
            "returning id::text",
            (uuid.uuid4().hex, f"lfa_{uuid.uuid4().hex[:6]}"),
        )
        assert cr is not None
        clip = db.fetch_one(
            """
            insert into clips (creator_id, source_platform, source_id, source_url, source_type, lane, start_s,
                               end_s, status, title, score)
            values (%s, 'twitch', %s, 'https://www.twitch.tv/videos/1', 'vod', 'hourly', 10, 30, 'approved',
                    'A moment', 8)
            returning id::text
            """,
            (cr["id"], uuid.uuid4().hex),
        )
        assert clip is not None
        db.execute(
            "insert into episode_items (episode_id, position, clip_id, creator_id, est_s, status, "
            "duration_s) "
            "values (%s, %s, %s, %s, 30, %s, 31.5)",
            (ep["id"], pos, clip["id"], cr["id"], status),
        )
    return str(ep["id"])


def test_list_and_detail(client: TestClient) -> None:
    episode_id = make_episode(with_file=True)
    items = get(client, "episodes")["items"]
    row = next(i for i in items if i["id"] == episode_id)
    assert row["items"] == 3 and row["rendered"] == 2 and row["dropped"] == 1 and row["has_file"] is True
    body = get(client, f"episodes/{episode_id}")
    assert [m["position"] for m in body["moments"]] == [2, 1, 100003]  # playback order, dropped last
    assert body["moments"][0]["creator_handle"].startswith("lfa_")
    assert body["chapters"][0]["label"] == "#2"
    get(client, f"episodes/{MISSING}", 404)
    get(client, "episodes/nope", 404)


def test_media(client: TestClient) -> None:
    ready = make_episode(with_file=True)
    body = get(client, f"episodes/{ready}/media")
    assert body["video"]["reason"] == "ok" and body["video"]["url"]
    assert body["thumb"]["reason"] == "ok"
    db.execute("update episodes set storage_path = null, thumb_path = null where id = %s", (ready,))
    assert get(client, f"episodes/{ready}/media")["video"]["reason"] == "no_file"
    get(client, f"episodes/{MISSING}/media", 404)
