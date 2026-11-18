"""Console media, the parts without a database: storage keys and batch signing, Twitch template filling,
YouTube thumbnail choice, frame-vs-source choice of the admin API, batching and the discovery helpers."""

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from sixstars import storage
from sixstars.api import admin_queries as q
from sixstars.clients import twitch
from sixstars.clients.fakes import FakeTwitchClient, FakeYouTubeClient, fake_clip
from sixstars.clients.twitch import TwitchClient, TwitchUser, fill_thumbnail
from sixstars.clients.youtube import CHANNEL_THUMB_PREFERENCE, pick_thumbnail
from sixstars.discovery import media
from sixstars.discovery.models import Candidate
from sixstars.discovery.store import thumbnail_rows
from sixstars.factory import thumbs
from sixstars.factory.tools import build_thumb_command

BASE = "https://proj.supabase.test"
WHEN = datetime(2026, 11, 18, 23, 30, tzinfo=UTC)


# ── storage ──────────────────────────────────────────────────


def test_thumb_key_sits_next_to_the_clip_key() -> None:
    assert storage.thumb_key("abc", WHEN, "staging") == "clips/thumbs/staging/2026/11/18/abc.jpg"
    assert storage.clip_key("abc", WHEN, "staging") == "clips/staging/2026/11/18/abc.mp4"
    assert storage.split_key(storage.thumb_key("abc", WHEN, "production")) == (
        "clips",
        "thumbs/production/2026/11/18/abc.jpg",
    )


def test_group_by_bucket_drops_duplicates_and_non_keys() -> None:
    grouped = storage.group_by_bucket(["clips/a.jpg", "clips/a.jpg", "other/b.jpg", "nokey", ""])
    assert grouped == {"clips": [("clips/a.jpg", "a.jpg")], "other": [("other/b.jpg", "b.jpg")]}


@respx.mock
def test_signed_urls_signs_a_page_in_one_request() -> None:
    route = respx.post(f"{BASE}/storage/v1/object/sign/clips").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "path": "thumbs/a.jpg",
                    "signedURL": "/object/sign/clips/thumbs/a.jpg?token=t1",
                    "error": None,
                },
                {"path": "thumbs/gone.jpg", "signedURL": None, "error": "Either the object does not exist"},
            ],
        )
    )
    store = storage.ClipStorage(supabase_url=BASE + "/", service_role_key="service-key")
    signed = store.signed_urls(["clips/thumbs/a.jpg", "clips/thumbs/gone.jpg", "clips/thumbs/a.jpg"], 3600)
    assert signed == {
        "clips/thumbs/a.jpg": f"{BASE}/storage/v1/object/sign/clips/thumbs/a.jpg?token=t1",
        "clips/thumbs/gone.jpg": None,
    }
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {
        "expiresIn": 3600,
        "paths": ["thumbs/a.jpg", "thumbs/gone.jpg"],
    }
    assert store.signed_urls([]) == {}


@respx.mock
def test_signed_urls_rejects_an_answer_that_is_not_a_list() -> None:
    respx.post(f"{BASE}/storage/v1/object/sign/clips").mock(return_value=httpx.Response(200, json={}))
    store = storage.ClipStorage(supabase_url=BASE, service_role_key="k")
    with pytest.raises(storage.StorageError):
        store.signed_urls(["clips/x.jpg"])


def test_fake_signed_urls() -> None:
    fake = storage.FakeClipStorage()
    fake.missing.add("clips/b.jpg")
    signed = fake.signed_urls(["clips/a.jpg", "clips/b.jpg"])
    assert signed["clips/b.jpg"] is None and "clips/a.jpg" in str(signed["clips/a.jpg"])
    assert fake.sign_batches == 1


# ── platform thumbnails ──────────────────────────────────────


@pytest.mark.parametrize(
    ("template", "size", "expected"),
    [
        (  # Get Videos: %{width}x%{height}, 320x180 only
            "https://static-cdn.jtvnw.net/cf_vods/x/thumb/thumb0-%{width}x%{height}.jpg",
            twitch.VIDEO_THUMB_SIZE,
            "https://static-cdn.jtvnw.net/cf_vods/x/thumb/thumb0-320x180.jpg",
        ),
        (  # Get Streams: {width}x{height}
            "https://static-cdn.jtvnw.net/previews-ttv/live_user_a-{width}x{height}.jpg",
            twitch.STREAM_THUMB_SIZE,
            "https://static-cdn.jtvnw.net/previews-ttv/live_user_a-640x360.jpg",
        ),
        ("https://x.test/${width}x${height}.jpg", (320, 180), "https://x.test/320x180.jpg"),
        ("https://clips-media-assets2.twitch.tv/a-preview-480x272.jpg", (1, 1), None),  # unchanged, below
        ("", (320, 180), None),
        ("   ", (320, 180), None),
    ],
)
def test_fill_thumbnail(template: str, size: tuple[int, int], expected: str | None) -> None:
    if template.startswith("https://clips-media"):
        expected = template  # a Get Clips URL has no placeholders
    assert fill_thumbnail(template, *size) == expected


def test_pick_thumbnail_prefers_high_then_medium() -> None:
    def item(*sizes: str) -> dict[str, Any]:
        return {"snippet": {"thumbnails": {size: {"url": f"https://i.test/{size}.jpg"} for size in sizes}}}

    assert pick_thumbnail(item("default", "medium", "high", "maxres")) == "https://i.test/high.jpg"
    assert pick_thumbnail(item("default", "medium")) == "https://i.test/medium.jpg"
    assert pick_thumbnail(item("default")) == "https://i.test/default.jpg"
    assert pick_thumbnail(item("default", "high"), CHANNEL_THUMB_PREFERENCE) == "https://i.test/high.jpg"
    assert pick_thumbnail({"snippet": {}}) is None
    assert pick_thumbnail({}) is None
    assert pick_thumbnail({"snippet": {"thumbnails": {"high": {"url": ""}}}}) is None


TOKEN = {"access_token": "tok1", "expires_in": 5_000_000, "token_type": "bearer"}


@respx.mock
def test_twitch_by_id_lookups_batch_100_ids() -> None:
    respx.post(twitch.TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
    videos = respx.get(f"{twitch.API_BASE}/videos").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "v1", "user_id": "u1", "thumbnail_url": "https://t.test/v1-%{width}x%{height}.jpg"}
                ]
            },
        )
    )
    client = TwitchClient(client_id="cid", client_secret="shh")
    found = client.get_videos_by_id([f"v{i}" for i in range(150)] + ["v1", ""])
    assert [video.id for video in found] == ["v1", "v1"]  # one item per answered page
    assert videos.call_count == 2
    assert len(videos.calls[0].request.url.params.get_list("id")) == 100
    assert len(videos.calls[1].request.url.params.get_list("id")) == 50
    clips = respx.get(f"{twitch.API_BASE}/clips").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "c1", "broadcaster_id": "u1", "thumbnail_url": "https://t.test/c1.jpg"}]},
        )
    )
    assert [clip.thumbnail_url for clip in client.get_clips_by_id(["c1"])] == ["https://t.test/c1.jpg"]
    assert clips.calls.last.request.url.params.get_list("id") == ["c1"]


# ── admin API: frame or source ───────────────────────────────


def test_choose_thumb() -> None:
    assert q.choose_thumb("https://signed", "https://source") == ("https://signed", "frame")
    assert q.choose_thumb(None, "https://source") == ("https://source", "source")
    assert q.choose_thumb(None, None) == (None, None)
    assert q.choose_thumb("", "") == (None, None)


def test_resolve_thumbs_replaces_the_private_columns_everywhere() -> None:
    body: dict[str, Any] = {
        "items": [
            {"id": "c1", q.THUMB_PATH: "clips/thumbs/a.jpg", q.SOURCE_THUMB: "https://src/a.jpg"},
            {"id": "c2", q.THUMB_PATH: "clips/thumbs/gone.jpg", q.SOURCE_THUMB: "https://src/b.jpg"},
            {"id": "c3", q.THUMB_PATH: None, q.SOURCE_THUMB: None},
        ],
        "detail": {
            "posts": [{"id": "p1", q.CLIP_THUMB_PATH: "clips/thumbs/a.jpg", q.CLIP_SOURCE_THUMB: None}],
        },
        "creator": {"id": "x", "avatar_url": None},
    }
    assert q.thumb_keys(body) == ["clips/thumbs/a.jpg", "clips/thumbs/gone.jpg"]
    q.resolve_thumbs(body, {"clips/thumbs/a.jpg": "https://signed/a", "clips/thumbs/gone.jpg": None})
    assert body["items"] == [
        {"id": "c1", "thumb_url": "https://signed/a", "thumb_kind": "frame"},
        {"id": "c2", "thumb_url": "https://src/b.jpg", "thumb_kind": "source"},
        {"id": "c3", "thumb_url": None, "thumb_kind": None},
    ]
    assert body["detail"]["posts"] == [{"id": "p1", "clip_thumb_url": "https://signed/a"}]
    assert body["creator"] == {"id": "x", "avatar_url": None}


def test_resolve_thumbs_falls_back_when_signing_failed() -> None:
    rows = [{q.THUMB_PATH: "clips/thumbs/a.jpg", q.SOURCE_THUMB: "https://src/a.jpg"}]
    q.resolve_thumbs(rows, {})
    assert rows == [{"thumb_url": "https://src/a.jpg", "thumb_kind": "source"}]


def test_creator_ref_carries_the_avatar() -> None:
    sql = q.creator_json()
    assert "'avatar_url', cr.avatar_url" in sql
    assert "'avatar_url', t.avatar_url" in q.creator_json("t", nullable=True)


# ── factory ──────────────────────────────────────────────────


def test_frame_time() -> None:
    assert thumbs.frame_time(30.0, 1.5) == 1.5
    assert thumbs.frame_time(1.0, 1.5) == 0.5  # a clip shorter than the frame time: its middle
    assert thumbs.frame_time(None, 1.5) == 1.5
    assert thumbs.frame_time(0.0, 1.5) == 1.5
    assert thumbs.frame_time(30.0, -2.0) == 0.0


def test_thumb_command_seeks_before_the_input() -> None:
    from pathlib import Path

    command = build_thumb_command(
        "https://x.test/a.mp4?token=t", Path("out.jpg"), time_s=1.5, width=540, qscale=5
    )
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-ss") + 1] == "1.500"
    assert "scale=540:-2" in command and command[command.index("-q:v") + 1] == "5"
    clamped = build_thumb_command("a.mp4", Path("o.jpg"), time_s=0, width=540, qscale=99)
    assert clamped[clamped.index("-q:v") + 1] == "31"


def test_redact_hides_the_signed_url_token() -> None:
    text = "thumbnail failed: https://p.test/object/sign/clips/a.mp4?token=eyJabc.def&x=1: 404"
    assert "eyJabc" not in thumbs.redact(text)
    assert "token=***&x=1" in thumbs.redact(text)


# ── discovery ────────────────────────────────────────────────


def test_batches_and_quota_units() -> None:
    assert list(media.batches(list(range(5)), 2)) == [[0, 1], [2, 3], [4]]
    assert list(media.batches([], 50)) == []
    assert list(media.batches([1], 0)) == [[1]]
    assert media.youtube_units(0) == 0
    assert media.youtube_units(1) == 1
    assert media.youtube_units(50) == 1
    assert media.youtube_units(51) == 2


def test_by_platform() -> None:
    rows = [
        {"platform": "twitch", "channel_id": "1"},
        {"platform": "youtube", "channel_id": "UC1"},
        {"platform": "twitch", "channel_id": "1"},
        {"platform": "twitch", "channel_id": "2"},
    ]
    assert media.by_platform(rows, "channel_id") == {"twitch": ["1", "2"], "youtube": ["UC1"]}


def test_avatar_answers() -> None:
    user = TwitchUser("1", "a", "A", "", "https://p.test/1.png", "", None)
    blank = TwitchUser("2", "b", "B", "", "", "", None)
    assert media.twitch_avatars([user, blank]) == {"1": "https://p.test/1.png", "2": None}
    items = FakeYouTubeClient().channels_list(["UCa"])
    assert media.youtube_avatars(items) == {"UCa": "https://yt.example.test/UCa/240.jpg"}
    assert media.youtube_avatars([{"id": "UCb", "snippet": {}}, {"snippet": {}}]) == {"UCb": None}


def _candidate(source_id: str, thumb: str | None, platform: str = "twitch") -> Candidate:
    return Candidate(
        platform=platform,  # type: ignore[arg-type]
        source_type="vod",
        source_id=source_id,
        url="u",
        title="t",
        channel_id="c",
        channel_handle="h",
        channel_name="n",
        origin="twitch",
        thumbnail_url=thumb,
    )


def test_thumbnail_rows_one_per_source() -> None:
    rows = thumbnail_rows([_candidate("a", "https://1"), _candidate("b", None), _candidate("a", "https://2")])
    assert rows == [{"platform": "twitch", "source_id": "a", "url": "https://2"}]


def test_twitch_candidates_fill_the_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from typing import cast

    from sixstars.discovery import sources
    from sixstars.discovery.config import DiscoveryConfig

    monkeypatch.setattr(sources, "twitch_game_id", lambda client, name, now: "32982")
    config = cast(
        DiscoveryConfig,
        SimpleNamespace(target_game="Grand Theft Auto V", twitch_max_streams=5, twitch_max_videos=5),
    )
    client = FakeTwitchClient()
    client.videos = [dataclasses.replace(video, stream_id=None) for video in client.videos]
    found = sources.twitch_candidates(client, config, WHEN) or []
    live = [c for c in found if c.source_type == "live"]
    vods = [c for c in found if c.source_type == "vod"]
    assert live[0].thumbnail_url == "https://example.test/thumb/0-640x360.jpg"
    assert vods[0].thumbnail_url == "https://example.test/vod/0-320x180.jpg"


def test_fake_clip_thumbnail_is_kept_as_is() -> None:
    assert fake_clip(1).thumbnail_url == "https://example.test/clip/1.jpg"
