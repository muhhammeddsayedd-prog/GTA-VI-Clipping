"""Twitch Helix client: token caching, refresh on 401, pagination and the missing-keys case."""

from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
import respx

from sixstars import config
from sixstars.clients import twitch
from sixstars.clients._http import ApiError, NotConfiguredError
from sixstars.clients.twitch import TwitchClient

TOKEN = {"access_token": "tok1", "expires_in": 5_000_000, "token_type": "bearer"}


def _client() -> TwitchClient:
    return TwitchClient(client_id="cid", client_secret="shh")


def _stream(index: int) -> dict[str, object]:
    return {
        "id": f"s{index}",
        "user_id": f"u{index}",
        "user_login": f"login{index}",
        "user_name": f"Name{index}",
        "game_id": "32982",
        "game_name": "Grand Theft Auto V",
        "type": "live",
        "title": "t",
        "tags": None,
        "viewer_count": 1000 - index,
        "started_at": "2026-11-17T10:00:00Z",
        "language": "en",
        "thumbnail_url": "x",
    }


def test_unconfigured_does_not_crash_and_reports_it() -> None:
    assert twitch.is_configured() is False
    client = TwitchClient()
    assert client.is_configured() is False
    with pytest.raises(NotConfiguredError):
        client.get_game_id("Grand Theft Auto V")


def test_is_configured_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "shh")
    config.env.cache_clear()
    assert twitch.is_configured() is True
    assert TwitchClient().is_configured() is True


@respx.mock
def test_token_is_fetched_once_and_sent_with_client_id() -> None:
    token_route = respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    games = respx.get(f"{twitch.API_BASE}/games").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "32982", "name": "Grand Theft Auto V"}]})
    )
    client = _client()
    assert client.get_game_id("Grand Theft Auto V") == "32982"
    assert client.get_game_id("Grand Theft Auto V") == "32982"
    assert token_route.call_count == 1
    body = token_route.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in body and "client_id=cid" in body
    request = games.calls[0].request
    assert request.headers["Client-Id"] == "cid" and request.headers["Authorization"] == "Bearer tok1"
    assert request.url.params["name"] == "Grand Theft Auto V"


@respx.mock
def test_unknown_game_returns_none() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    respx.get(f"{twitch.API_BASE}/games").mock(return_value=httpx.Response(200, json={"data": []}))
    assert _client().get_game_id("Nope") is None


@respx.mock
def test_token_near_expiry_is_refreshed() -> None:
    short = {"access_token": "short", "expires_in": 200}
    token_route = respx.post(twitch.TOKEN_URL).mock(
        side_effect=[httpx.Response(200, json=short), httpx.Response(200, json=TOKEN)]
    )
    respx.get(f"{twitch.API_BASE}/games").mock(return_value=httpx.Response(200, json={"data": []}))
    client = _client()
    client.get_game_id("a")
    client.get_game_id("b")
    assert token_route.call_count == 2


@respx.mock
def test_401_refreshes_the_token_once() -> None:
    token_route = respx.post(twitch.TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json=TOKEN),
            httpx.Response(200, json={**TOKEN, "access_token": "tok2"}),
        ]
    )
    games = respx.get(f"{twitch.API_BASE}/games").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"data": [{"id": "1"}]})]
    )
    assert _client().get_game_id("x") == "1"
    assert token_route.call_count == 2
    assert games.calls[1].request.headers["Authorization"] == "Bearer tok2"


@respx.mock
def test_get_streams_paginates_and_dedupes() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    pages = [
        {"data": [_stream(i) for i in range(100)], "pagination": {"cursor": "c1"}},
        {"data": [_stream(99), *[_stream(i) for i in range(100, 150)]], "pagination": {}},
    ]
    route = respx.get(f"{twitch.API_BASE}/streams").mock(
        side_effect=[httpx.Response(200, json=page) for page in pages]
    )
    streams = _client().get_streams("32982", max_items=300)
    assert len(streams) == 150 and len({stream.id for stream in streams}) == 150
    assert route.call_count == 2
    first, second = (call.request.url.params for call in route.calls)
    assert first["game_id"] == "32982" and first["first"] == "100" and "after" not in first
    assert second["after"] == "c1"
    assert streams[0].viewer_count == 1000 and streams[0].tags == ()
    assert streams[0].started_at.tzinfo is not None


@respx.mock
def test_get_streams_stops_at_max_items() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    page = {"data": [_stream(i) for i in range(100)], "pagination": {"cursor": "c1"}}
    route = respx.get(f"{twitch.API_BASE}/streams").mock(return_value=httpx.Response(200, json=page))
    assert len(_client().get_streams("32982", max_items=50)) == 50
    assert route.call_count == 1 and route.calls[0].request.url.params["first"] == "50"


@respx.mock
def test_videos_and_users() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    video = {
        "id": "v1",
        "stream_id": "s1",
        "user_id": "u1",
        "user_login": "l",
        "user_name": "N",
        "title": "T",
        "description": "",
        "created_at": "2026-11-16T10:00:00Z",
        "published_at": "2026-11-16T10:00:00Z",
        "url": "https://www.twitch.tv/videos/1",
        "thumbnail_url": "",
        "view_count": 9,
        "language": "en",
        "type": "archive",
        "duration": "1h0m5s",
    }
    videos = respx.get(f"{twitch.API_BASE}/videos").mock(
        return_value=httpx.Response(200, json={"data": [video]})
    )
    users = respx.get(f"{twitch.API_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "u1", "login": "l", "description": "bio"}]})
    )
    client = _client()
    by_game = client.get_videos_by_game("32982")
    assert by_game[0].duration_s == 3605 and by_game[0].stream_id == "s1"
    params = videos.calls[0].request.url.params
    assert (params["period"], params["sort"], params["type"], params["first"]) == (
        "day",
        "views",
        "archive",
        "50",
    )
    client.get_videos_by_user("u1")
    assert videos.calls[1].request.url.params["user_id"] == "u1"
    assert videos.calls[1].request.url.params["first"] == "5"
    assert client.get_users(["u1", "u1"])[0].description == "bio"
    assert users.calls[0].request.url.params.get_list("id") == ["u1"]
    assert client.get_users([]) == []


@respx.mock
def test_followers_total_and_denied() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    route = respx.get(f"{twitch.API_BASE}/channels/followers").mock(
        return_value=httpx.Response(200, json={"total": 1234, "data": []})
    )
    client = _client()
    assert client.get_followers_total("u1") == 1234
    route.mock(return_value=httpx.Response(401))
    calls_before = route.call_count
    assert client.get_followers_total("u1") is None
    assert client.get_followers_total("u2") is None
    # One try, one more after a token refresh, then the denial is remembered.
    assert route.call_count == calls_before + 2


@respx.mock
def test_api_error_is_raised_for_bad_request() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    respx.get(f"{twitch.API_BASE}/videos").mock(
        return_value=httpx.Response(404, json={"message": "game not found"})
    )
    with pytest.raises(ApiError) as raised:
        _client().get_videos_by_game("0")
    assert raised.value.status == 404


@pytest.mark.parametrize(
    ("text", "seconds"), [("3m21s", 201), ("1h0m5s", 3605), ("45s", 45), ("", 0), ("x", 0)]
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert twitch.parse_duration(text) == seconds


def _clip(index: int, **changes: object) -> dict[str, object]:
    # the shape of a real Get Clips row (2026-11-17), viewer and broadcaster names changed
    return {
        "id": f"Slug{index}",
        "url": f"https://www.twitch.tv/streamer/clip/Slug{index}",
        "embed_url": f"https://clips.twitch.tv/embed?clip=Slug{index}",
        "broadcaster_id": "b1",
        "broadcaster_name": "Streamer",
        "creator_id": "v1",
        "creator_name": "SomeViewer",
        "video_id": "2372168353",
        "game_id": "32982",
        "language": "en",
        "title": "he did not have a parachute",
        "view_count": 1800 - index,
        "created_at": "2026-11-17T10:00:00Z",
        "thumbnail_url": "https://example.test/t.jpg",
        "duration": 45.1,
        "vod_offset": 8506,
        "is_featured": False,
        **changes,
    }


@respx.mock
def test_get_clips_sends_the_window_and_paginates() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    pages = [
        {"data": [_clip(i) for i in range(100)], "pagination": {"cursor": "c1"}},
        {"data": [_clip(99), *[_clip(i) for i in range(100, 130)]], "pagination": {}},
    ]
    route = respx.get(f"{twitch.API_BASE}/clips").mock(
        side_effect=[httpx.Response(200, json=page) for page in pages]
    )
    start = datetime(2026, 11, 16, 12, 0, 30, tzinfo=UTC)
    end = datetime(2026, 11, 17, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    clips = _client().get_clips("32982", start, end, max_items=150)

    assert len(clips) == 130 and len({clip.id for clip in clips}) == 130
    first, second = (call.request.url.params for call in route.calls)
    assert first["game_id"] == "32982" and first["first"] == "100" and "after" not in first
    assert first["started_at"] == "2026-11-16T12:00:30Z"
    assert first["ended_at"] == "2026-11-17T12:00:00Z"  # sent in UTC
    assert "is_featured" not in first  # omitted: featured and other clips alike
    assert second["after"] == "c1" and second["first"] == "50"
    top = clips[0]
    assert (top.id, top.view_count, top.duration_s) == ("Slug0", 1800, 45.1)
    assert (top.broadcaster_id, top.broadcaster_name, top.creator_name) == ("b1", "Streamer", "SomeViewer")
    assert (top.video_id, top.vod_offset_s) == ("2372168353", 8506.0)
    assert top.created_at == datetime(2026, 11, 17, 10, 0, tzinfo=UTC) and top.language == "en"
    assert top.url.endswith("/clip/Slug0") and top.is_featured is False


@respx.mock
def test_get_clips_without_end_and_with_the_featured_filter() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    rows = [_clip(1, video_id="", vod_offset=None, is_featured=True), _clip(2, vod_offset=None)]
    route = respx.get(f"{twitch.API_BASE}/clips").mock(
        return_value=httpx.Response(200, json={"data": rows, "pagination": {}})
    )
    clips = _client().get_clips("32982", datetime(2026, 11, 16, tzinfo=UTC), is_featured=True, max_items=20)
    params = route.calls[0].request.url.params
    assert "ended_at" not in params and params["is_featured"] == "true" and params["first"] == "20"
    # the broadcast's video is gone, or its offset is not written yet: no position in the broadcast
    assert (clips[0].video_id, clips[0].vod_offset_s, clips[0].is_featured) == (None, None, True)
    assert (clips[1].video_id, clips[1].vod_offset_s) == ("2372168353", None)


@respx.mock
def test_get_clips_empty_and_unknown_game() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    route = respx.get(f"{twitch.API_BASE}/clips").mock(
        side_effect=[
            httpx.Response(200, json={"data": [], "pagination": {}}),
            httpx.Response(404, json={"error": "Not Found", "status": 404, "message": "game not found"}),
        ]
    )
    start = datetime(2026, 11, 16, tzinfo=UTC)
    assert _client().get_clips("32982", start) == []
    with pytest.raises(ApiError):
        _client().get_clips("0", start)
    assert route.call_count == 2
