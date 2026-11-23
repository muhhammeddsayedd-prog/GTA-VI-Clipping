"""`GET /v1/admin/covers` (cover engine C8): an account's latest covers for the console grid."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from gate_support import add_clip, add_creator

from sixstars import config, db
from sixstars.api import routes_admin
from sixstars.api.main import create_app
from sixstars.covers import report, store
from sixstars.storage import FakeClipStorage

DASHBOARD = "integration-dashboard-token-0123456789abcdef0123"
AUTH = {"Authorization": f"Bearer {DASHBOARD}"}
MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0044_cover_engine.sql"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, test_database_url: str) -> Iterator[TestClient]:
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
    monkeypatch.setenv("DASHBOARD_API_TOKEN", DASHBOARD)
    config.env.cache_clear()
    monkeypatch.setattr(routes_admin, "storage_factory", FakeClipStorage)
    monkeypatch.setattr(report.clip_storage, "ClipStorage", FakeClipStorage)
    db.execute(
        "insert into platform_accounts (id, platform, upload_post_user, handle, lanes, cap_24h) "
        "values ('instagram_main', 'instagram', 'default', '@t', '{hourly}', 10) on conflict (id) do nothing"
    )
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_grid_and_filters(client: TestClient) -> None:
    clip_id = add_clip(add_creator(handle="GridStreamer"), None)
    for status, path in (("applied", "covers-work/c/a.jpg"), ("failed", None), ("fallback_default", None)):
        store.insert(
            store.NewCover(
                clip_id=clip_id,
                lane="normal",
                platform="instagram",
                arm="split|quote",
                config_version=1,
                account_id="instagram_main",
                status=status,  # type: ignore[arg-type]
                storage_path=path,
            )
        )
    body = client.get("/v1/admin/covers", params={"account": "instagram_main"}, headers=AUTH).json()
    assert (
        len(body["covers"]) == 3
        and {"account_id": "instagram_main", "platform": "instagram"} in body["accounts"]
    )
    applied = next(c for c in body["covers"] if c["status"] == "applied")
    assert applied["aspect"] == "3:4" and "a_3x4.jpg" in (applied["preview_url"] or "")
    failed = client.get(
        "/v1/admin/covers", params={"account": "instagram_main", "filter": "failed"}, headers=AUTH
    )
    assert [c["status"] for c in failed.json()["covers"]] == ["failed"]
    fallbacks = client.get(
        "/v1/admin/covers", params={"account": "instagram_main", "filter": "fallbacks"}, headers=AUTH
    )
    assert [c["status"] for c in fallbacks.json()["covers"]] == ["fallback_default"]
    assert client.get("/v1/admin/covers", params={"filter": "bogus"}, headers=AUTH).status_code == 422
