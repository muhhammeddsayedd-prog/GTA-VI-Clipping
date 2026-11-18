"""YouTube client: API key lists, RSS parsing, OAuth token handling, resumable private upload and the CLI."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from typer.testing import CliRunner

from sixstars.cli import app
from sixstars.clients import youtube, youtube_oauth
from sixstars.clients._http import ApiError, NotConfiguredError
from sixstars.clients.youtube import YouTubeClient

runner = CliRunner()

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <title>Channel</title>
 <entry>
  <id>yt:video:abc123DEF45</id>
  <yt:videoId>abc123DEF45</yt:videoId>
  <yt:channelId>UCchan</yt:channelId>
  <title>GTA &amp; chaos</title>
  <published>2026-11-16T18:00:05+00:00</published>
  <updated>2026-11-16T19:00:00+00:00</updated>
 </entry>
 <entry>
  <yt:videoId>zzz999YYY88</yt:videoId>
  <yt:channelId>UCchan</yt:channelId>
  <title>Older</title>
  <published>2026-11-15T10:00:00+00:00</published>
 </entry>
</feed>"""

ACCESS = {"access_token": "at1", "expires_in": 3599, "token_type": "Bearer"}


def _oauth_client() -> YouTubeClient:
    return YouTubeClient(
        api_key="k", oauth_client_id="cid", oauth_client_secret="sec", oauth_refresh_token="rt"
    )


def test_unconfigured_client_reports_state() -> None:
    client = YouTubeClient()
    assert client.is_configured() is False and client.has_oauth() is False
    with pytest.raises(NotConfiguredError):
        client.videos_list(["a"])
    with pytest.raises(NotConfiguredError):
        client.whoami()


@respx.mock
def test_videos_list_chunks_ids_and_keeps_key_out_of_url() -> None:
    route = respx.get(f"{youtube.API_BASE}/videos").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"items": [{"id": video_id} for video_id in request.url.params["id"].split(",")]}
        )
    )
    ids = [f"v{index}" for index in range(60)]
    items = YouTubeClient(api_key="secret-key").videos_list([*ids, "v0"], parts=("status", "statistics"))
    assert [item["id"] for item in items] == ids
    assert route.call_count == 2
    request = route.calls[0].request
    assert request.headers["X-goog-api-key"] == "secret-key"
    assert "secret-key" not in str(request.url)
    assert request.url.params["part"] == "status,statistics"
    assert len(request.url.params["id"].split(",")) == 50


@respx.mock
def test_search_list_sends_the_documented_parameters_and_records_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, str, float | None]] = []
    monkeypatch.setattr(
        youtube.usage,
        "record",
        lambda provider, operation, **kw: recorded.append((provider, operation, kw.get("units"))),
    )
    item = {"id": {"kind": "youtube#video", "videoId": "abc"}, "snippet": {"title": "GTA 5 live"}}
    route = respx.get(f"{youtube.API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"items": [item], "nextPageToken": "more"})
    )
    after = datetime(2026, 11, 15, 12, 30, tzinfo=UTC)
    found = YouTubeClient(api_key="secret-key").search_list(
        "GTA 5|GTA V",
        event_type="live",
        order="viewCount",
        published_after=after,
        relevance_language="en",
        video_duration="long",
        max_results=500,
    )
    assert found == [item]
    assert route.call_count == 1  # one page only, whatever nextPageToken says
    request = route.calls[0].request
    assert dict(request.url.params) == {
        "part": "snippet",
        "type": "video",
        "q": "GTA 5|GTA V",
        "order": "viewCount",
        "maxResults": "50",
        "eventType": "live",
        "publishedAfter": "2026-11-15T12:30:00Z",
        "videoCategoryId": "20",
        "relevanceLanguage": "en",
        "videoDuration": "long",
    }
    assert "%7C" in str(request.url) and "|" not in str(request.url)  # the pipe must be URL-escaped
    assert request.headers["X-goog-api-key"] == "secret-key" and "secret-key" not in str(request.url)
    assert recorded == [("youtube", "search.list", 1)]


@respx.mock
def test_search_list_minimal_call_quota_error_and_bad_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(youtube.usage, "record", lambda provider, operation, **kw: recorded.append(operation))
    route = respx.get(f"{youtube.API_BASE}/search").mock(return_value=httpx.Response(200, json={}))
    client = YouTubeClient(api_key="k")
    assert client.search_list("GTA 6", video_category_id=None, max_results=10) == []
    assert dict(route.calls[0].request.url.params) == {
        "part": "snippet",
        "type": "video",
        "q": "GTA 6",
        "order": "relevance",
        "maxResults": "10",
    }
    quota = {
        "error": {
            "code": 403,
            "message": "The request cannot be completed because you have exceeded your quota.",
        }
    }
    route.mock(return_value=httpx.Response(403, json=quota))
    with pytest.raises(ApiError) as caught:
        client.search_list("GTA 6")
    assert caught.value.status == 403 and "exceeded your quota" in str(caught.value)
    assert recorded == ["search.list", "search.list"]  # a refused call still counts
    for bad in ({"event_type": "soon"}, {"order": "newest"}, {"video_duration": "tiny"}):
        with pytest.raises(ValueError):
            client.search_list("GTA 6", **bad)  # type: ignore[arg-type]
    with pytest.raises(NotConfiguredError):
        YouTubeClient().search_list("GTA 6")


@respx.mock
def test_channels_list_and_quota_error() -> None:
    route = respx.get(f"{youtube.API_BASE}/channels").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "UC1"}]})
    )
    assert YouTubeClient(api_key="k").channels_list(["UC1"]) == [{"id": "UC1"}]
    assert route.calls[0].request.url.params["part"] == "snippet,statistics"
    route.mock(return_value=httpx.Response(403, json={"error": {"message": "quotaExceeded"}}))
    with pytest.raises(ApiError) as raised:
        YouTubeClient(api_key="k").channels_list(["UC1"])
    assert raised.value.status == 403 and "quotaExceeded" in raised.value.message


def test_parse_feed() -> None:
    entries = youtube.parse_feed(FEED)
    assert [entry.video_id for entry in entries] == ["abc123DEF45", "zzz999YYY88"]
    assert entries[0].title == "GTA & chaos" and entries[0].channel_id == "UCchan"
    assert entries[0].published_at.isoformat() == "2026-11-16T18:00:05+00:00"
    assert entries[1].updated_at is None


@respx.mock
def test_channel_feed_needs_no_key_and_handles_unknown_channel() -> None:
    route = respx.get(youtube.FEED_URL).mock(return_value=httpx.Response(200, text=FEED))
    client = YouTubeClient()
    assert len(client.channel_feed("UCchan")) == 2
    assert route.calls[0].request.url.params["channel_id"] == "UCchan"
    route.mock(return_value=httpx.Response(404))
    assert client.channel_feed("UCnope") == []


@respx.mock
def test_whoami_refreshes_token_once_and_caches_it() -> None:
    token = respx.post(youtube.TOKEN_URL).mock(return_value=httpx.Response(200, json=ACCESS))
    channels = respx.get(f"{youtube.API_BASE}/channels").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "UCme", "snippet": {"title": "6 Stars"}}]})
    )
    client = _oauth_client()
    assert client.whoami() == youtube.YouTubeChannel(id="UCme", title="6 Stars")
    client.whoami()
    assert token.call_count == 1
    form = parse_qs(token.calls[0].request.content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["rt"]
    request = channels.calls[0].request
    assert request.headers["Authorization"] == "Bearer at1" and request.url.params["mine"] == "true"


@respx.mock
def test_oauth_request_retries_once_after_401() -> None:
    token = respx.post(youtube.TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json=ACCESS),
            httpx.Response(200, json={**ACCESS, "access_token": "at2"}),
        ]
    )
    videos = respx.get(f"{youtube.API_BASE}/videos").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"items": []})]
    )
    assert _oauth_client().get_status("gone") is None
    assert token.call_count == 2
    assert videos.calls[1].request.headers["Authorization"] == "Bearer at2"


@respx.mock
def test_upload_private_uses_resumable_protocol(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0123456789")
    respx.post(youtube.TOKEN_URL).mock(return_value=httpx.Response(200, json=ACCESS))
    session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=xyz"
    start = respx.post(youtube.UPLOAD_URL).mock(
        return_value=httpx.Response(200, headers={"Location": session})
    )
    put = respx.put(session).mock(return_value=httpx.Response(201, json={"id": "newvid"}))

    assert _oauth_client().upload_private(clip, "T" * 150, "desc") == "newvid"

    request = start.calls[0].request
    assert request.url.params["uploadType"] == "resumable" and request.url.params["part"] == "snippet,status"
    assert request.headers["X-Upload-Content-Length"] == "10"
    body = httpx.Response(200, content=request.content).json()
    assert body["status"]["privacyStatus"] == "private"
    assert len(body["snippet"]["title"]) == 100 and body["snippet"]["categoryId"] == "20"
    uploaded = put.calls[0].request
    assert uploaded.content == b"0123456789" and uploaded.headers["Content-Length"] == "10"


@respx.mock
def test_upload_put_is_retried_with_the_whole_file(tmp_path: Path, sleeps: list[float]) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"abc")
    respx.post(youtube.TOKEN_URL).mock(return_value=httpx.Response(200, json=ACCESS))
    session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=1"
    respx.post(youtube.UPLOAD_URL).mock(return_value=httpx.Response(200, headers={"Location": session}))
    put = respx.put(session).mock(side_effect=[httpx.Response(503), httpx.Response(201, json={"id": "v"})])
    assert _oauth_client().upload_private(clip, "t", "d") == "v"
    assert [call.request.content for call in put.calls] == [b"abc", b"abc"]


@respx.mock
def test_get_status_reads_restrictions_and_delete() -> None:
    respx.post(youtube.TOKEN_URL).mock(return_value=httpx.Response(200, json=ACCESS))
    item = {
        "id": "vid1",
        "status": {"uploadStatus": "processed", "privacyStatus": "private", "license": "youtube"},
        "contentDetails": {"licensedContent": True, "regionRestriction": {"blocked": ["DE", "FR"]}},
    }
    respx.get(f"{youtube.API_BASE}/videos").mock(return_value=httpx.Response(200, json={"items": [item]}))
    status = _oauth_client().get_status("vid1")
    assert status is not None
    assert status.region_blocked == ("DE", "FR") and status.region_allowed is None
    assert status.is_restricted is True and status.privacy_status == "private"

    clean = youtube.parse_video_status({"id": "v", "status": {"uploadStatus": "processed"}})
    assert clean.is_restricted is False
    rejected = youtube.parse_video_status({"id": "v", "status": {"uploadStatus": "rejected"}})
    assert rejected.is_restricted is True

    delete = respx.delete(f"{youtube.API_BASE}/videos").mock(
        side_effect=[httpx.Response(204), httpx.Response(404)]
    )
    client = _oauth_client()
    assert client.delete_video("vid1") is True
    assert client.delete_video("vid1") is False
    assert delete.calls[0].request.url.params["id"] == "vid1"


def test_build_auth_url_and_pkce() -> None:
    verifier, challenge = youtube.new_pkce_pair()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge
    url = youtube.build_auth_url("cid", "http://127.0.0.1:8765", "st", challenge)
    query = parse_qs(urlparse(url).query)
    assert url.startswith(youtube.AUTH_URL)
    assert query["response_type"] == ["code"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765"] and query["state"] == ["st"]
    assert set(query["scope"][0].split()) == set(youtube.SCOPES)


@respx.mock
def test_exchange_code() -> None:
    route = respx.post(youtube.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r"})
    )
    tokens = youtube.exchange_code("cid", "sec", "code1", "verifier1", "http://127.0.0.1:1")
    assert tokens["refresh_token"] == "r"
    form = parse_qs(route.calls[0].request.content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code_verifier"] == ["verifier1"]


def test_parse_redirect() -> None:
    assert youtube_oauth.parse_redirect("/?state=s1&code=c1&scope=x", "s1") == "c1"
    with pytest.raises(youtube_oauth.OAuthFlowError, match="state mismatch"):
        youtube_oauth.parse_redirect("/?state=other&code=c1", "s1")
    with pytest.raises(youtube_oauth.OAuthFlowError, match="access_denied"):
        youtube_oauth.parse_redirect("/?error=access_denied&state=s1", "s1")
    with pytest.raises(youtube_oauth.OAuthFlowError, match="no authorization code"):
        youtube_oauth.parse_redirect("/?state=s1", "s1")


@respx.mock
def test_local_flow_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loopback server receives the redirect and the code is exchanged for tokens."""
    import threading

    respx.route(host="127.0.0.1").pass_through()
    respx.post(youtube.TOKEN_URL).mock(return_value=httpx.Response(200, json={"refresh_token": "r1"}))
    lines: list[str] = []

    def echo(line: str) -> None:
        lines.append(line)
        if line.startswith(youtube.AUTH_URL):
            query = parse_qs(urlparse(line).query)
            target = f"{query['redirect_uri'][0]}/?state={query['state'][0]}&code=the-code"
            threading.Thread(target=lambda: httpx.get(target, timeout=10), daemon=True).start()

    tokens = youtube_oauth.run_local_flow("cid", "sec", open_browser=False, timeout_s=15, echo=echo)
    assert tokens == {"refresh_token": "r1"}


def test_cli_whoami_without_oauth_exits_2() -> None:
    result = runner.invoke(app, ["youtube", "whoami"])
    assert result.exit_code == 2
    assert "YOUTUBE_OAUTH_REFRESH_TOKEN" in result.output


def test_cli_auth_without_client_exits_2() -> None:
    result = runner.invoke(app, ["youtube", "auth", "--no-browser"])
    assert result.exit_code == 2
    assert "YOUTUBE_OAUTH_CLIENT_ID" in result.output


@respx.mock
def test_cli_whoami_prints_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars import config

    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "sec")
    monkeypatch.setenv("YOUTUBE_OAUTH_REFRESH_TOKEN", "rt")
    config.env.cache_clear()
    respx.post(youtube.TOKEN_URL).mock(return_value=httpx.Response(200, json=ACCESS))
    respx.get(f"{youtube.API_BASE}/channels").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "UCme", "snippet": {"title": "6 Stars"}}]})
    )
    result = runner.invoke(app, ["youtube", "whoami"])
    assert result.exit_code == 0
    assert "6 Stars (UCme)" in result.output


def test_cli_auth_prints_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from sixstars import config

    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "sec")
    config.env.cache_clear()
    monkeypatch.setattr(youtube_oauth, "run_local_flow", lambda *args, **kwargs: {"refresh_token": "new-rt"})
    result = runner.invoke(app, ["youtube", "auth", "--no-browser"])
    assert result.exit_code == 0
    assert "YOUTUBE_OAUTH_REFRESH_TOKEN=new-rt" in result.output


# ── quota accounting: one unit per HTTP request, whatever the parts or ids ──


def _echo_items(request: httpx.Request) -> httpx.Response:
    ids = request.url.params["id"].split(",")
    return httpx.Response(200, json={"items": [{"id": item_id} for item_id in ids]})


def _record_usage(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, float | None]]:
    recorded: list[tuple[str, str, float | None]] = []
    monkeypatch.setattr(
        youtube.usage,
        "record",
        lambda provider, operation, units=None, **_: recorded.append((provider, operation, units)),
    )
    return recorded


@respx.mock
def test_120_channel_ids_are_3_requests_and_3_units(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_usage(monkeypatch)
    route = respx.get(f"{youtube.API_BASE}/channels").mock(side_effect=_echo_items)
    ids = [f"UC{index:022d}" for index in range(120)]
    items = YouTubeClient(api_key="k").channels_list(ids, parts=("snippet", "statistics", "brandingSettings"))
    assert len(items) == 120
    assert route.call_count == 3
    assert [len(call.request.url.params["id"].split(",")) for call in route.calls] == [50, 50, 20]
    # One row for the invocation, charged per request: not per id, not per part.
    assert recorded == [("youtube", "channels.list", 3)]


@respx.mock
def test_videos_list_costs_one_unit_per_request_whatever_the_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_usage(monkeypatch)
    respx.get(f"{youtube.API_BASE}/videos").mock(side_effect=_echo_items)
    client = YouTubeClient(api_key="k")
    client.videos_list(["a"], parts=youtube.DEFAULT_VIDEO_PARTS)  # five parts, one id
    client.videos_list([f"v{index}" for index in range(50)], parts=("snippet",))  # one part, fifty ids
    assert recorded == [("youtube", "videos.list", 1), ("youtube", "videos.list", 1)]


def test_documented_quota_costs() -> None:
    # https://developers.google.com/youtube/v3/determine_quota_cost (checked 2026-11-18): list methods cost
    # 1 unit; search.list and videos.insert have their own buckets of 100 calls, 1 per call.
    assert youtube.QUOTA_LIST == 1
    assert youtube.QUOTA_SEARCH == 1
    assert youtube.QUOTA_INSERT == 1
    assert youtube.QUOTA_DELETE == 50


@respx.mock
def test_get_statuses_batches_50_ids_per_oauth_request(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_usage(monkeypatch)
    respx.post(youtube.TOKEN_URL).mock(return_value=httpx.Response(200, json=ACCESS))
    route = respx.get(f"{youtube.API_BASE}/videos").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "items": [
                    {"id": video_id, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
                    for video_id in request.url.params["id"].split(",")
                    if video_id != "gone"
                ]
            },
        )
    )
    ids = [f"v{index}" for index in range(119)] + ["gone", "v0"]
    statuses = _oauth_client().get_statuses(ids)
    assert route.call_count == 3
    assert route.calls[0].request.headers["Authorization"] == "Bearer at1"
    assert route.calls[0].request.url.params["part"] == "status,contentDetails"
    assert len(statuses) == 119 and "gone" not in statuses
    assert statuses["v7"].upload_status == "processed"
    assert recorded == [("youtube", "videos.list", 3)]


@respx.mock
def test_get_statuses_records_the_requests_made_before_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_usage(monkeypatch)
    respx.post(youtube.TOKEN_URL).mock(return_value=httpx.Response(200, json=ACCESS))
    respx.get(f"{youtube.API_BASE}/videos").mock(
        side_effect=[httpx.Response(200, json={"items": []}), httpx.Response(403, json={})]
    )
    with pytest.raises(ApiError):
        _oauth_client().get_statuses([f"v{index}" for index in range(60)])
    assert recorded == [("youtube", "videos.list", 2)]
