"""Apify client: actor input mapping, item normalisation, run polling and cost recording."""

from typing import Any

import httpx
import pytest
import respx

from sixstars.clients import apify
from sixstars.clients._http import ApiError, NotConfiguredError
from sixstars.clients.apify import ApifyClient, ApifyRunFailed

# Shape of real rows returned by streamers/youtube-scraper on 2026-11-17 (text shortened).
VIDEO_ROW: dict[str, Any] = {
    "title": "TOP 250 UNLUCKIEST MOMENTS IN GTA 5!",
    "type": "video",
    "id": "hMfoGSJHq7A",
    "url": "https://www.youtube.com/watch?v=hMfoGSJHq7A",
    "viewCount": 34311,
    "date": "2026-11-13T17:00:34.000Z",
    "likes": 562,
    "channelName": "Red Arcade",
    "channelId": "UCHZZo1h1cI1vg4I9g2RqOUQ",
    "numberOfSubscribers": 5020000,
    "duration": "01:02:10",
    "commentsCount": 17,
    "input": "GTA 5 funny moments",
}
LIVE_ROW: dict[str, Any] = {
    "title": "The Last Drive in GTA 5 RP",
    "type": "stream",
    "streamType": "live",
    "id": "CrwpQTTIzLQ",
    "url": "https://www.youtube.com/watch?v=CrwpQTTIzLQ",
    "viewCount": 598,
    "date": "2026-11-16T22:36:22.000Z",
    "likes": 475,
    "channelName": "Just Havoc",
    "channelId": "UCcISgmobjhJQzMqXMnnzgeQ",
    "numberOfSubscribers": 265000,
    "duration": None,
    "commentsCount": 0,
}
ERROR_ROW: dict[str, Any] = {
    "url": "https://www.youtube.com/results?search_query=GTA+5&sp=EgQQAU",
    "error": "NO_VIDEOS",
    "input": "GTA 5",
    "note": "No videos found on the page.",
}
RUNS_URL = f"{apify.API_BASE}/acts/streamers~youtube-scraper/runs"


def _run(status: str, **extra: Any) -> dict[str, Any]:
    return {"data": {"id": "run1", "status": status, "defaultDatasetId": "ds1", **extra}}


def _client() -> ApifyClient:
    return ApifyClient(token="apify-token", youtube_actor="streamers/youtube-scraper")


def test_search_input_for_finished_videos() -> None:
    payload = apify.build_youtube_search_input(["GTA 6", "GTA VI"], False, 48, 50, sort="views")
    assert payload == {
        "searchQueries": ["GTA 6", "GTA VI"],
        "maxResults": 50,
        "maxResultsShorts": 0,
        "maxResultStreams": 0,
        "dateFilter": "week",
        "sortingOrder": "views",
    }


def test_search_input_for_live_streams() -> None:
    payload = apify.build_youtube_search_input(["GTA 6"], True, 24, 30)
    assert payload == {
        "searchQueries": ["GTA 6"],
        "maxResults": 0,
        "maxResultsShorts": 0,
        "maxResultStreams": 30,
        "isLive": True,
    }


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (None, None),
        (1, "hour"),
        (2, "today"),
        (24, "today"),
        (25, "week"),
        (168, "week"),
        (169, "month"),
        (2000, "year"),
    ],
)
def test_date_filter_for(hours: int | None, expected: str | None) -> None:
    assert apify.date_filter_for(hours) == expected


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("01:02:10", 3730), ("10:47", 647), ("59", 59), (None, None), ("", None), ("1:x", None)],
)
def test_parse_clock(value: str | None, seconds: int | None) -> None:
    assert apify.parse_clock(value) == seconds


def test_normalise_video_live_and_error_rows() -> None:
    video = apify.normalise_youtube_item(VIDEO_ROW)
    assert video is not None
    assert (video.video_id, video.channel_id, video.channel_name) == (
        "hMfoGSJHq7A",
        "UCHZZo1h1cI1vg4I9g2RqOUQ",
        "Red Arcade",
    )
    assert video.is_live is False and video.views == 34311 and video.viewers is None
    assert video.duration_s == 3730 and video.likes == 562 and video.comments == 17
    assert video.subscribers == 5020000
    assert video.published_at is not None and video.published_at.isoformat() == "2026-11-13T17:00:34+00:00"

    live = apify.normalise_youtube_item(LIVE_ROW)
    assert live is not None
    assert live.is_live is True and live.viewers == 598 and live.views is None and live.duration_s is None

    assert apify.normalise_youtube_item(ERROR_ROW) is None
    assert apify.normalise_youtube_item({"title": "no id"}) is None


def test_unconfigured() -> None:
    client = ApifyClient()
    assert client.is_configured() is False
    with pytest.raises(NotConfiguredError):
        client.start_run("a/b", {})


@respx.mock
def test_youtube_search_runs_polls_and_normalises(sleeps: list[float]) -> None:
    start = respx.post(RUNS_URL).mock(return_value=httpx.Response(201, json=_run("READY")))
    finished = _run(
        "SUCCEEDED",
        usageTotalUsd=0.001,
        chargedEventCounts={"result": 2},
        pricingInfo={"pricingPerEvent": {"actorChargeEvents": {"result": {"eventPriceUsd": 0.003}}}},
    )
    poll = respx.get(f"{apify.API_BASE}/actor-runs/run1").mock(
        side_effect=[httpx.Response(200, json=_run("RUNNING")), httpx.Response(200, json=finished)]
    )
    items = respx.get(f"{apify.API_BASE}/datasets/ds1/items").mock(
        return_value=httpx.Response(200, json=[VIDEO_ROW, ERROR_ROW, VIDEO_ROW])
    )

    found = _client().youtube_search(["GTA 5 funny moments"], uploaded_within_hours=48, max_results=5)

    assert [item.video_id for item in found] == ["hMfoGSJHq7A"]
    request = start.calls[0].request
    assert request.headers["Authorization"] == "Bearer apify-token"
    assert "apify-token" not in str(request.url)
    assert request.url.params["maxTotalChargeUsd"] == "1.0"
    sent = httpx.Response(200, content=request.content).json()
    assert sent["maxResults"] == 5 and sent["dateFilter"] == "week"
    assert poll.call_count == 2 and sleeps == [apify.POLL_INTERVAL_S]
    assert items.calls[0].request.url.params["clean"] == "true"


def test_run_cost_prefers_charged_events() -> None:
    data = {
        "id": "r",
        "status": "SUCCEEDED",
        "usageTotalUsd": 0.001,
        "chargedEventCounts": {"result": 5, "date-filter": 0},
        "pricingInfo": {"pricingPerEvent": {"actorChargeEvents": {"result": {"eventPriceUsd": 0.003}}}},
    }
    assert apify._run(data).cost_usd == pytest.approx(0.015)
    assert apify._run({"id": "r", "status": "SUCCEEDED", "usageTotalUsd": 0.2}).cost_usd == 0.2


@respx.mock
def test_failed_run_raises() -> None:
    respx.post(RUNS_URL).mock(return_value=httpx.Response(201, json=_run("READY")))
    respx.get(f"{apify.API_BASE}/actor-runs/run1").mock(return_value=httpx.Response(200, json=_run("FAILED")))
    with pytest.raises(ApifyRunFailed) as raised:
        _client().youtube_search(["x"])
    assert raised.value.status == "FAILED"


@respx.mock
def test_run_still_running_after_timeout_raises(sleeps: list[float]) -> None:
    respx.get(f"{apify.API_BASE}/actor-runs/run1").mock(
        return_value=httpx.Response(200, json=_run("RUNNING"))
    )
    with pytest.raises(ApifyRunFailed) as raised:
        _client().wait_for_run("run1", timeout_s=12, poll_s=5)
    assert raised.value.status == "STILL-RUNNING" and len(sleeps) == 3


@respx.mock
def test_start_run_is_not_retried_on_server_error(sleeps: list[float]) -> None:
    route = respx.post(RUNS_URL).mock(return_value=httpx.Response(502))
    with pytest.raises(ApiError):
        _client().start_run("streamers/youtube-scraper", {})
    assert route.call_count == 1


@respx.mock
def test_get_items_passes_limit() -> None:
    route = respx.get(f"{apify.API_BASE}/datasets/ds9/items").mock(
        return_value=httpx.Response(200, json=[{"a": 1}])
    )
    assert _client().get_items("ds9", limit=7) == [{"a": 1}]
    assert route.calls[0].request.url.params["limit"] == "7"
