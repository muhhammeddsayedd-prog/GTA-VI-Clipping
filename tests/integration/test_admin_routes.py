"""The admin console read API (`GET /v1/admin/*`, docs/26) against Postgres: every route answers 200 with
the contract's keys, lists paginate and count, unknown ids answer 404. The console's own token is used."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from posting_support import TT, YT, add_clip, add_creator, add_post, add_spike, production, video
from psycopg.types.json import Jsonb

from sixstars import config, db, events
from sixstars.api import routes_admin
from sixstars.api.main import create_app
from sixstars.clients._http import ApiError as ClientApiError
from sixstars.storage import FakeClipStorage

__all__ = ["production", "video"]  # fixtures

DASHBOARD = "integration-dashboard-token-0123456789abcdef0123"
AUTH = {"Authorization": f"Bearer {DASHBOARD}"}
MISSING = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", DASHBOARD)
    config.env.cache_clear()
    monkeypatch.setattr(routes_admin, "storage_factory", FakeClipStorage)
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    row = db.fetch_one(sql, params)
    assert row is not None
    return row


@pytest.fixture
def world(production: None, video: Path) -> dict[str, str]:
    """A creator with a live session, a video, a spike, a clip with a file, posts, a takedown, a job, a
    candidate, account stats and events."""
    now = datetime.now(UTC)
    creator = add_creator("AdminStreamer", platform="twitch")
    session = one(
        """
        insert into live_sessions (creator_id, platform, source_id, channel_login, url, status, started_at)
        values (%s, 'twitch', 'stream-1', 'adminstreamer', 'https://twitch.tv/adminstreamer', 'recording',
                now())
        returning id::text as id
        """,
        (creator,),
    )["id"]
    video_id = one(
        """
        insert into videos (creator_id, platform, source_id, url, title, status, live_session_id)
        values (%s, 'twitch', 'vod-1', 'https://twitch.tv/videos/1', 'Long stream', 'done', %s)
        returning id::text as id
        """,
        (creator, session),
    )["id"]
    db.execute(
        "insert into chat_files (platform, source_id, video_id, messages_count) "
        "values ('twitch', 'vod-1', %s, 9)",
        (video_id,),
    )
    spike = add_spike(creator, now - timedelta(minutes=10))
    db.execute(
        "update spikes set live_session_id = %s, video_id = %s, chat_sample = '[\"lol\"]', "
        "status = 'clipped' where id = %s",
        (session, video_id, spike),
    )
    clip = add_clip(creator, video, lane="spike", spike_id=spike, approved_at=now - timedelta(minutes=5))
    db.execute(
        "update clips set series_no = 142, hook = 'He jumped', storage_path = %s, "
        "transcript = '[]', created_at = now() - interval '8 minutes' where id = %s",
        (f"clips/test/2026/11/20/{clip}.mp4", clip),
    )
    db.execute("update spikes set clip_id = %s, spike_to_post_s = 95 where id = %s", (clip, spike))
    other = add_clip(creator, None, lane="hourly", status="rejected")
    db.execute("update clips set reject_reason = 'low_score' where id = %s", (other,))
    post = add_post(clip, TT, now - timedelta(minutes=2), lane="spike")
    db.execute(
        "update posts set post_url = 'https://www.tiktok.com/@6starsclips/video/777' where id = %s", (post,)
    )
    add_post(clip, YT, now - timedelta(minutes=1), lane="spike", status="remove_failed")
    takedown = one(
        """
        insert into takedowns
          (source, sender, subject, is_takedown, creator_id, matched_post_ids, status, actions)
        values ('email', 'a@b.test', 'Remove my clip', true, %s, array[%s::uuid], 'actioned',
                '{"strike": true}')
        returning id::text as id
        """,
        (creator, post),
    )["id"]
    job = one(
        "insert into jobs (queue, type, payload) values ('normal', 'vod.process', %s) returning id",
        (Jsonb({"video_id": video_id}),),
    )["id"]
    db.execute(
        "insert into candidates (creator_id, source_type, platform, source_id, url, trend_score, picked) "
        "values (%s, 'live', 'twitch', 'stream-1', 'https://twitch.tv/adminstreamer', 2.5, true)",
        (creator,),
    )
    db.execute(
        "insert into account_stats (account_id, captured_at, followers, views_total) values "
        "(%s, now() - interval '30 hours', 100, 1000), (%s, now(), 120, 1500)",
        (TT, TT),
    )
    db.execute("insert into notify_outbox (severity, title) values ('warn', 'Something')")
    db.execute(
        "insert into api_usage (provider, operation, cost_usd, units) "
        "values ('anthropic', 'clip_writer', 0.25, 1)"
    )
    db.heartbeat("trigger:hourly", {"slot_at": "x"})
    db.heartbeat("worker-express-host-1")
    event_id = events.emit("clip.rendered", entity_type="clip", entity_id=clip, message="rendered")
    events.emit("clip.scored", entity_type="clip", entity_id=clip, message="scored")
    events.emit("health.incident", "warn", entity_type="incident", entity_id="queue_low", message="Queue low")
    events.emit("setting.changed", "warn", entity_type="setting", entity_id="posting.min_gap_s")
    return {
        "creator": creator,
        "session": session,
        "video": video_id,
        "spike": spike,
        "clip": clip,
        "post": post,
        "takedown": takedown,
        "job": str(job),
        "event": str(event_id),
    }


def get(client: TestClient, path: str, status: int = 200) -> Any:
    response = client.get(f"/v1/admin/{path}", headers=AUTH)
    assert response.status_code == status, response.text
    return response.json()


def assert_list(body: dict[str, Any]) -> list[dict[str, Any]]:
    assert set(body) >= {"items", "next_cursor", "counts"}
    assert "all" in body["counts"]
    items: list[dict[str, Any]] = body["items"]
    return items


# ── single views ──────────────────────────────────────────────────────────────────────────────────────────


def test_overview(client: TestClient, world: dict[str, str]) -> None:
    body = get(client, "overview")
    assert set(body) == {
        "now", "env", "posting_mode", "stop_reason", "launch_at", "dry_run", "posts_24h", "queue", "express",
        "spend", "accounts", "up_next", "recent_events", "health", "growth",
    }  # fmt: skip
    assert len(body["posts_24h"]["by_hour"]) == 24
    assert body["posts_24h"]["published"] == 1
    assert body["express"]["p50_s"] == 95
    assert {line["provider"] for line in body["spend"]["by_provider"]} >= {
        "anthropic",
        "transcription",
        "apify",
    }
    assert body["spend"]["total_usd"] == 0.25
    tiktok = next(a for a in body["accounts"] if a["id"] == TT)
    assert set(tiktok) >= {"cap_24h", "spike_reserve", "used_24h", "spike_reserve_remaining", "last_post_at"}
    assert [item["clip_id"] for item in body["up_next"]] == [world["clip"]]
    assert all(e["type"] != "clip.scored" for e in body["recent_events"])  # a noisy type
    assert [i["key"] for i in body["health"]["open_incidents"]] == ["queue_low"]
    assert body["health"]["triggers_total"] == 4
    growth = next(a for a in body["growth"]["accounts"] if a["account_id"] == TT)
    assert growth == {"account_id": TT, "platform": "tiktok", "followers": 120, "delta_24h": 20}
    assert set(body["growth"]["ypp"]) == {
        "subscribers",
        "subscribers_target",
        "shorts_views_90d",
        "views_target",
    }


@pytest.mark.parametrize("window", ["24h", "7d", "30d"])
def test_pipeline(client: TestClient, world: dict[str, str], window: str) -> None:
    body = get(client, f"pipeline?window={window}")
    assert body["window"] == window
    assert set(body["nodes"]) == {"discovery", "live", "vod", "spikes", "render", "gate", "queue", "posts"}
    assert body["nodes"]["discovery"] == {"candidates": 1, "picked": 1}
    assert [row["tier"] for row in body["funnel"]] == ["S", "A", "B"]
    assert body["funnel"][0]["posted"] == 1
    assert body["rejects"] == [{"reason": "low_score", "n": 1}]
    assert [s["stage"] for s in body["express_stages"]] == ["detect", "queue", "render", "gate", "post"]
    get(client, "pipeline?window=1y", 422)


def test_queue(client: TestClient, world: dict[str, str]) -> None:
    body = get(client, "queue")
    assert set(body) == {"ranked", "slots", "caps", "decisions", "next_slot_at"}
    assert [item["clip_id"] for item in body["ranked"]["spike"]] == [world["clip"]]
    item = body["ranked"]["spike"][0]
    assert item["series_no"] == 142 and item["creator"]["handle"] == "AdminStreamer"
    assert item["expires_at"] is not None
    for row in body["slots"]:
        assert len(row["hours"]) == 24
        assert {h["state"] for h in row["hours"]} <= {
            "posted", "failed", "skipped", "planned", "not_allowed", "empty",
        }  # fmt: skip
    cap = next(c for c in body["caps"] if c["account_id"] == TT)
    assert cap["used_24h"] == 1


def test_accounts(client: TestClient, world: dict[str, str]) -> None:
    items = get(client, "accounts")["items"]
    assert {a["id"] for a in items} >= {TT, YT}
    body = get(client, f"accounts/{TT}")
    assert body["caps"]["account_id"] == TT
    assert [s["followers"] for s in body["stats"]] == [100, 120]
    assert set(body) >= {"caps", "stats", "views_by_day", "posts", "events"}
    assert body["posts"][0]["clip_hook"] == "He jumped"
    get(client, "accounts/nope", 404)


def test_discovery_guardian_health_usage_settings(client: TestClient, world: dict[str, str]) -> None:
    discovery = get(client, "discovery")
    assert set(discovery) == {"runs", "source_health", "apify_last_run", "quotas"}
    assert set(discovery["quotas"]) == {
        "youtube_units_today", "youtube_units_limit", "search_calls_today", "search_calls_limit",
        "apify_today_usd", "apify_daily_budget_usd", "apify_month_usd", "apify_monthly_budget_usd",
    }  # fmt: skip
    guardian = get(client, "guardian")
    assert guardian["strikes"]["count"] == 1
    assert guardian["strikes"]["items"][0]["takedown_id"] == world["takedown"]
    assert len(guardian["manual_removals"]) == 1
    assert guardian["takedown_counts"] == {"open": 0, "actioned": 1, "partial": 0, "ignored": 0}
    health = get(client, "health")
    assert set(health) == {"heartbeats", "triggers", "open_incidents", "incidents", "jobs"}
    hourly = next(t for t in health["triggers"] if t["name"] == "hourly")
    assert hourly["source"] == "n8n" and hourly["stale"] is False
    assert health["jobs"]["queued"] == 1
    usage = get(client, "usage?range=month")
    assert usage["totals"] == [{"provider": "anthropic", "usd": 0.25}]
    assert set(usage["budgets"]) == {
        "anthropic",
        "transcribe",
        "apify_daily",
        "apify_monthly",
        "proxy",
        "month_ceiling",
    }
    assert usage["operations"][0]["calls"] == 1
    items = get(client, "settings")["items"]
    paused = next(s for s in items if s["key"] == "vod.paused_until")
    assert paused["group"] == "vod" and paused["written_by_code"] is True
    gap = next(s for s in items if s["key"] == "posting.min_gap_s")
    assert gap["written_by_code"] is False
    history = assert_list(get(client, "settings/history"))
    assert [e["type"] for e in history] == ["setting.changed"]


# ── lists and details ─────────────────────────────────────────────────────────────────────────────────────

LISTS = [
    ("live-sessions", "session", "live-sessions/{id}", {"spikes", "clips", "creator"}),
    ("videos", "video", "videos/{id}", {"clips", "skip_reason"}),
    ("spikes", "spike", "spikes/{id}", {"clip_series_no", "window_start_wall"}),
    ("clips", "clip", "clips/{id}", {"has_file", "series_no"}),
    ("posts", "post", "posts/{id}", {"clip_hook", "claim_status"}),
    ("creators", "creator", "creators/{id}", {"clips_posted", "tier"}),
    ("takedowns", "takedown", "takedowns/{id}", {"creator", "matched_post_ids"}),
    ("jobs", "job", "jobs/{id}", {"max_attempts", "dedupe_key"}),
    ("events", "event", "events/{id}", {"entity_type", "data"}),
]


@pytest.mark.parametrize(("path", "key", "detail_path", "fields"), LISTS)
def test_lists_and_details(
    client: TestClient, world: dict[str, str], path: str, key: str, detail_path: str, fields: set[str]
) -> None:
    items = assert_list(get(client, path))
    ids = [str(item["id"]) for item in items]
    assert world[key] in ids
    assert fields <= set(items[0])
    detail = get(client, detail_path.format(id=world[key]))
    assert str(detail["id"]) == world[key]
    get(client, detail_path.format(id=MISSING if key not in ("job", "event") else "999999"), 404)
    get(client, detail_path.format(id="not-an-id"), 404)


def test_large_columns_only_on_details(client: TestClient, world: dict[str, str]) -> None:
    clip = get(client, "clips")["items"][0]
    assert "transcript" not in clip
    detail = get(client, f"clips/{world['clip']}")
    assert detail["transcript"] == []
    assert detail["posts"] and detail["events"]
    assert detail["trace"]["spike"]["id"] == world["spike"]
    assert detail["trace"]["live_session"]["id"] == world["session"]
    assert detail["trace"]["video"]["id"] == world["video"]
    assert {p["id"] for p in detail["trace"]["posts"]} >= {world["post"]}
    assert "chat_sample" not in get(client, "spikes")["items"][0]
    assert get(client, f"spikes/{world['spike']}")["chat_sample"] == ["lol"]
    assert "payload" not in get(client, "jobs")["items"][0]
    assert get(client, f"jobs/{world['job']}")["payload"] == {"video_id": world["video"]}


def test_detail_extras(client: TestClient, world: dict[str, str]) -> None:
    video_body = get(client, f"videos/{world['video']}")
    assert video_body["chat_file"]["messages_count"] == 9
    assert [j["id"] for j in video_body["jobs"]] == [int(world["job"])]
    assert video_body["trace"]["live_session"]["id"] == world["session"]
    session = get(client, f"live-sessions/{world['session']}")
    assert session["spikes_list"][0]["id"] == world["spike"]
    assert set(session["spikes"]) == {"S", "A", "B"}
    assert session["clips"] == 1
    creator = get(client, f"creators/{world['creator']}")
    assert creator["counts"]["posts"] == 2 and len(creator["clips"]) == 2
    takedown = get(client, f"takedowns/{world['takedown']}")
    assert [p["id"] for p in takedown["matched_posts"]] == [world["post"]]
    event = get(client, f"events/{world['event']}")
    assert {e["type"] for e in event["related"]} == {"clip.scored"}


def test_list_filters_counts_and_pages(client: TestClient, world: dict[str, str]) -> None:
    body = get(client, "clips?status=approved")
    assert [item["id"] for item in body["items"]] == [world["clip"]]
    assert body["counts"]["approved"] == 1 and body["counts"]["rejected"] == 1 and body["counts"]["all"] == 2
    lane = get(client, "clips?lane=hourly")
    assert lane["counts"]["all"] == 1
    assert get(client, "clips?q=jumped")["counts"]["all"] == 2  # both clips share the seeded title
    assert get(client, f"clips?creator_id={world['creator']}")["counts"]["all"] == 2
    get(client, "clips?status=bogus", 422)
    get(client, "clips?creator_id=nope", 422)
    first = get(client, "clips?limit=1")
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = get(client, f"clips?limit=1&cursor={first['next_cursor']}")
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert first["items"][0]["id"] != second["items"][0]["id"]
    get(client, "clips?cursor=garbage", 422)
    assert get(client, "posts?account_id=tiktok_main")["counts"]["all"] == 1
    assert get(client, "candidates?picked=true")["counts"] == {"live": 1, "vod": 0, "all": 1}
    alerts = get(client, "alerts?delivered=false")
    assert alerts["counts"]["warn"] == 1 and alerts["items"][0]["delivered"] is False
    jobs = get(client, "jobs?queue=normal")
    assert jobs["meta"]["by_queue"]["normal"]["queued"] == 1
    assert get(client, "events?type_prefix=clip.")["counts"]["all"] == 2
    assert "blocked" in get(client, "live-sessions")["meta"]
    assert "source_health" in get(client, "videos")["meta"]


def test_clip_media(client: TestClient, world: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    body = get(client, f"clips/{world['clip']}/media")
    assert body["reason"] == "ok" and body["url"].startswith("https://fake.supabase.test/")
    assert body["expires_at"] is not None

    class Gone(FakeClipStorage):
        def signed_url(self, key: str, expires_in_s: int = 3600) -> str:
            raise ClientApiError("supabase_storage", "POST /object/sign", 400, "Object not found")

    monkeypatch.setattr(routes_admin, "storage_factory", Gone)
    assert get(client, f"clips/{world['clip']}/media") == {
        "url": None,
        "expires_at": None,
        "reason": "deleted",
    }
    db.execute("update clips set storage_path = null where id = %s", (world["clip"],))
    assert get(client, f"clips/{world['clip']}/media")["reason"] == "no_file"
    get(client, f"clips/{MISSING}/media", 404)


def test_console_media_fields(
    client: TestClient, world: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClipStorage()
    monkeypatch.setattr(routes_admin, "storage_factory", lambda: fake)
    thumb = f"clips/thumbs/test/2026/11/20/{world['clip']}.jpg"
    db.execute("update clips set thumb_path = %s where id = %s", (thumb, world["clip"]))
    db.execute(
        "update creators set avatar_url = 'https://p.test/a.png', avatar_checked_at = now() where id = %s",
        (world["creator"],),
    )
    db.execute(
        "update videos set thumbnail_url = 'https://t.test/vod-320x180.jpg' where id = %s", (world["video"],)
    )
    db.execute(
        "update live_sessions set thumbnail_url = 'https://t.test/live-640x360.jpg' where id = %s",
        (world["session"],),
    )

    clips = get(client, "clips")["items"]
    assert fake.sign_batches == 1, "a page is signed in one Storage request"
    main = next(c for c in clips if c["id"] == world["clip"])
    assert main["thumb_kind"] == "frame" and main["thumb_url"].startswith("https://fake.supabase.test/")
    assert main["creator"]["avatar_url"] == "https://p.test/a.png"
    assert all("_thumb_path" not in c and "_source_thumb" not in c for c in clips)
    assert {"thumb_url", "thumb_kind"} <= set(get(client, f"clips/{world['clip']}"))

    posts = get(client, "posts")["items"]
    assert all(p["clip_thumb_url"] == main["thumb_url"] for p in posts if p["clip_id"] == world["clip"])
    assert get(client, f"posts/{world['post']}")["clip_thumb_url"].startswith("https://fake.supabase.test/")
    queue = get(client, "queue")["ranked"]["spike"][0]
    assert queue["thumb_kind"] == "frame" and queue["creator"]["avatar_url"] == "https://p.test/a.png"
    assert get(client, "overview")["up_next"][0]["thumb_url"].startswith("https://fake.supabase.test/")

    creators = get(client, "creators")["items"]
    assert next(c for c in creators if c["id"] == world["creator"])["avatar_url"] == "https://p.test/a.png"
    assert get(client, f"creators/{world['creator']}")["avatar_url"] == "https://p.test/a.png"
    assert get(client, "videos")["items"][0]["thumbnail_url"] == "https://t.test/vod-320x180.jpg"
    assert get(client, f"videos/{world['video']}")["thumbnail_url"] == "https://t.test/vod-320x180.jpg"
    assert get(client, "live-sessions")["items"][0]["thumbnail_url"] == "https://t.test/live-640x360.jpg"
    assert (
        get(client, f"live-sessions/{world['session']}")["thumbnail_url"] == "https://t.test/live-640x360.jpg"
    )

    # Storage down: the list still answers, with the source thumbnail (the spike's video).
    fake.error = RuntimeError("storage down")
    main = next(c for c in get(client, "clips")["items"] if c["id"] == world["clip"])
    assert (main["thumb_url"], main["thumb_kind"]) == ("https://t.test/vod-320x180.jpg", "source")
    # No frame and no source: null.
    fake.error = None
    db.execute("update clips set thumb_path = null where id = %s", (world["clip"],))
    db.execute("update videos set thumbnail_url = null where id = %s", (world["video"],))
    main = next(c for c in get(client, "clips")["items"] if c["id"] == world["clip"])
    assert (main["thumb_url"], main["thumb_kind"]) == (None, None)


def test_search(client: TestClient, world: dict[str, str]) -> None:
    def kinds(q: str) -> list[tuple[str, str]]:
        return [(r["kind"], r["href"]) for r in get(client, f"search?q={q}")["results"]]

    assert ("clip", f"/clips/{world['clip']}") in kinds("%23142")
    assert ("clip", f"/clips/{world['clip']}") in kinds("142")
    assert ("creator", f"/creators/{world['creator']}") in kinds("adminstr")
    assert ("post", f"/posts/{world['post']}") in kinds("video/777")
    assert ("setting", "/settings?key=posting.min_gap_s") in kinds("posting.min_g")
    assert ("spike", f"/spikes/{world['spike']}") in kinds(world["spike"][:8])
    assert get(client, "search?q=")["results"] == []
    assert len(get(client, "search?q=a")["results"]) <= 20


def test_the_statement_timeout_answers_504(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars.api import admin_queries

    monkeypatch.setattr(admin_queries, "settings_rows", lambda: db.fetch_all("select pg_sleep(1)"))
    db.execute("update settings set value = '50' where key = 'admin.statement_timeout_ms'")
    from sixstars import settings

    settings.invalidate()
    assert get(client, "settings", 504)["code"] == "timeout"


def test_the_trigger_routes_refuse_the_console_token(client: TestClient) -> None:
    assert client.post("/v1/posting/hourly", headers=AUTH).status_code == 401
