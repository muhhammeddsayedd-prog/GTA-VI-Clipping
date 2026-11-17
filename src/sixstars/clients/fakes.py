"""Test doubles for every external client in this package, so unit and integration tests run without network
access. Implemented alongside the real clients in M02.

Each fake subclasses its real client (so it can be passed wherever the real one is expected), never
touches the network, the database or the environment, returns deterministic canned data, and records
every call in `calls` as `(method_name, arguments)`. Canned data is held in public attributes that a test
may replace before the call.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sixstars.clients.anthropic_client import AnthropicClient, Effort
from sixstars.clients.apify import ApifyClient, ApifyRun, YouTubeItem, normalise_youtube_item
from sixstars.clients.transcribe import Mode, Transcriber, Transcript, Word
from sixstars.clients.twitch import TwitchClient, TwitchClip, TwitchStream, TwitchUser, TwitchVideo
from sixstars.clients.upload_post import (
    NO_DELETE_PLATFORMS,
    NotSupported,
    PlatformResult,
    UploadPostClient,
    UploadStatus,
)
from sixstars.clients.youtube import FeedEntry, YouTubeChannel, YouTubeClient, YouTubeVideoStatus

Call = tuple[str, dict[str, Any]]

FIXED_TIME = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
FAKE_GAME_ID = "32982"


class _Recorder:
    """Call log shared by the fakes."""

    calls: list[Call]

    def _record(self, method: str, **arguments: Any) -> None:
        self.calls.append((method, arguments))

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        """Arguments of every recorded call to `method`, oldest first."""
        return [arguments for name, arguments in self.calls if name == method]


# ── Twitch ───────────────────────────────────────────────────


def fake_stream(index: int, game_id: str = FAKE_GAME_ID) -> TwitchStream:
    return TwitchStream(
        id=f"stream{index}",
        user_id=f"user{index}",
        user_login=f"streamer{index}",
        user_name=f"Streamer{index}",
        game_id=game_id,
        game_name="Grand Theft Auto V",
        title=f"GTA stream {index}",
        viewer_count=10_000 - index * 100,
        started_at=FIXED_TIME,
        language="en",
        tags=("English",),
        thumbnail_url=f"https://example.test/thumb/{index}-{{width}}x{{height}}.jpg",
    )


def fake_video(index: int, user_id: str | None = None) -> TwitchVideo:
    owner = user_id or f"user{index}"
    return TwitchVideo(
        id=f"video{index}",
        stream_id=f"stream{index}",
        user_id=owner,
        user_login=owner,
        user_name=owner.capitalize(),
        title=f"GTA VOD {index}",
        description="",
        created_at=FIXED_TIME,
        published_at=FIXED_TIME,
        url=f"https://www.twitch.tv/videos/{index}",
        thumbnail_url="",
        view_count=50_000 - index * 1000,
        language="en",
        type="archive",
        duration_s=7200,
    )


def fake_clip(index: int, game_id: str = FAKE_GAME_ID, **changes: Any) -> TwitchClip:
    """A 30-second English viewer clip of `user<index>`'s broadcast `video<index>`, made at `FIXED_TIME`."""
    clip = TwitchClip(
        id=f"FakeClipSlug{index}",
        url=f"https://www.twitch.tv/streamer{index}/clip/FakeClipSlug{index}",
        broadcaster_id=f"user{index}",
        broadcaster_name=f"Streamer{index}",
        creator_id=f"viewer{index}",
        creator_name=f"Viewer{index}",
        video_id=f"video{index}",
        game_id=game_id,
        language="en",
        title=f"GTA clip {index}",
        view_count=1000 - index * 100,
        created_at=FIXED_TIME,
        thumbnail_url=f"https://example.test/clip/{index}.jpg",
        duration_s=30.0,
        vod_offset_s=600.0 + index * 100,
        is_featured=False,
    )
    return dataclasses.replace(clip, **changes)


class FakeTwitchClient(_Recorder, TwitchClient):
    """Twitch without the network: three streams and three VODs of one game, and the viewer clips a test
    puts in `clips`."""

    def __init__(self, configured: bool = True) -> None:
        self.calls = []
        self.configured = configured
        self.game_ids: dict[str, str] = {"Grand Theft Auto V": FAKE_GAME_ID, "Grand Theft Auto VI": "999999"}
        self.streams: list[TwitchStream] = [fake_stream(index) for index in range(3)]
        self.videos: list[TwitchVideo] = [fake_video(index) for index in range(3)]
        self.clips: list[TwitchClip] = []  # none by default: a test adds `fake_clip(...)` rows
        self.followers: dict[str, int | None] = {}

    def is_configured(self) -> bool:
        return self.configured

    def get_game_id(self, name: str) -> str | None:
        self._record("get_game_id", name=name)
        return self.game_ids.get(name)

    def get_streams(self, game_id: str, max_items: int = 300) -> list[TwitchStream]:
        self._record("get_streams", game_id=game_id, max_items=max_items)
        return [stream for stream in self.streams if stream.game_id == game_id][:max_items]

    def get_videos_by_game(
        self,
        game_id: str,
        period: str = "day",
        sort: str = "views",
        type: str = "archive",  # noqa: A002
        first: int = 50,
    ) -> list[TwitchVideo]:
        self._record("get_videos_by_game", game_id=game_id, period=period, sort=sort, type=type, first=first)
        return [video for video in self.videos if video.type == type][:first]

    def get_videos_by_user(self, user_id: str, type: str = "archive", first: int = 5) -> list[TwitchVideo]:  # noqa: A002
        self._record("get_videos_by_user", user_id=user_id, type=type, first=first)
        return [video for video in self.videos if video.user_id == user_id and video.type == type][:first]

    def get_clips(
        self,
        game_id: str,
        started_at: datetime,
        ended_at: datetime | None = None,
        max_items: int = 100,
        is_featured: bool | None = None,
    ) -> list[TwitchClip]:
        """Clips of the game made inside the window, most viewed first, like the real endpoint."""
        self._record(
            "get_clips",
            game_id=game_id,
            started_at=started_at,
            ended_at=ended_at,
            max_items=max_items,
            is_featured=is_featured,
        )
        found = [
            clip
            for clip in self.clips
            if clip.game_id == game_id
            and clip.created_at >= started_at
            and (ended_at is None or clip.created_at <= ended_at)
            and (is_featured is None or clip.is_featured == is_featured)
        ]
        return sorted(found, key=lambda clip: -clip.view_count)[:max_items]

    def get_users(self, ids: Sequence[str]) -> list[TwitchUser]:
        self._record("get_users", ids=list(ids))
        return [
            TwitchUser(
                id=user_id,
                login=user_id,
                display_name=user_id.capitalize(),
                description=f"Bio of {user_id}",
                profile_image_url=f"https://example.test/avatar/{user_id}.png",
                broadcaster_type="partner",
                created_at=FIXED_TIME,
            )
            for user_id in dict.fromkeys(ids)
        ]

    def get_followers_total(self, broadcaster_id: str) -> int | None:
        self._record("get_followers_total", broadcaster_id=broadcaster_id)
        return self.followers.get(broadcaster_id, 100_000)


# ── YouTube ──────────────────────────────────────────────────


class FakeYouTubeClient(_Recorder, YouTubeClient):
    """YouTube without the network. Uploads get ids `fakevid1`, `fakevid2`, ... and stay in `uploaded`."""

    def __init__(self, oauth: bool = True) -> None:
        self.calls = []
        self.oauth = oauth
        self.videos: dict[str, dict[str, Any]] = {}
        self.channels: dict[str, dict[str, Any]] = {}
        self.feeds: dict[str, list[FeedEntry]] = {}
        self.search_live: list[dict[str, Any]] = []  # `search_list` results with event_type "live"
        self.search_recent: list[dict[str, Any]] = []  # ... and for every other search
        self.search_error: Exception | None = None
        self.statuses: dict[str, YouTubeVideoStatus] = {}
        self.uploaded: dict[str, dict[str, Any]] = {}
        self.channel = YouTubeChannel(id="UCfake0000000000000000000", title="6 Stars Clips (fake)")

    def is_configured(self) -> bool:
        return True

    def has_oauth(self) -> bool:
        return self.oauth

    def videos_list(self, ids: Sequence[str], parts: Sequence[str] = ("snippet",)) -> list[dict[str, Any]]:
        self._record("videos_list", ids=list(ids), parts=list(parts))
        return [self.videos.get(video_id, _fake_video_item(video_id)) for video_id in dict.fromkeys(ids)]

    def channels_list(self, ids: Sequence[str], parts: Sequence[str] = ("snippet",)) -> list[dict[str, Any]]:
        self._record("channels_list", ids=list(ids), parts=list(parts))
        return [
            self.channels.get(channel_id, _fake_channel_item(channel_id)) for channel_id in dict.fromkeys(ids)
        ]

    def search_list(
        self,
        q: str,
        *,
        event_type: str | None = None,
        order: str = "relevance",
        published_after: datetime | None = None,
        video_category_id: str | None = "20",
        relevance_language: str | None = None,
        video_duration: str | None = None,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        self._record(
            "search_list",
            q=q,
            event_type=event_type,
            order=order,
            published_after=published_after,
            video_category_id=video_category_id,
            relevance_language=relevance_language,
            video_duration=video_duration,
            max_results=max_results,
        )
        if self.search_error is not None:
            raise self.search_error
        found = self.search_live if event_type == "live" else self.search_recent
        return list(found)[:max_results]

    def channel_feed(self, channel_id: str) -> list[FeedEntry]:
        self._record("channel_feed", channel_id=channel_id)
        default = [
            FeedEntry(
                video_id=f"{channel_id[-4:]}vid{index}",
                channel_id=channel_id,
                title=f"Video {index}",
                published_at=FIXED_TIME,
            )
            for index in range(2)
        ]
        return self.feeds.get(channel_id, default)

    def whoami(self) -> YouTubeChannel:
        self._record("whoami")
        return self.channel

    def upload_private(self, path: Path, title: str, description: str) -> str:
        self._record("upload_private", path=path, title=title, description=description)
        video_id = f"fakevid{len(self.uploaded) + 1}"
        self.uploaded[video_id] = {"path": path, "title": title, "description": description}
        return video_id

    def get_status(self, video_id: str) -> YouTubeVideoStatus | None:
        self._record("get_status", video_id=video_id)
        if video_id in self.statuses:
            return self.statuses[video_id]
        if video_id not in self.uploaded:
            return None
        return YouTubeVideoStatus(
            video_id=video_id,
            upload_status="processed",
            privacy_status="private",
            rejection_reason=None,
            failure_reason=None,
            license="youtube",
            licensed_content=False,
            region_blocked=(),
            region_allowed=None,
        )

    def delete_video(self, video_id: str) -> bool:
        self._record("delete_video", video_id=video_id)
        return self.uploaded.pop(video_id, None) is not None


def _fake_video_item(video_id: str) -> dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {
            "title": f"Video {video_id}",
            "channelId": "UCfakechannel",
            "channelTitle": "Fake Channel",
            "publishedAt": "2026-11-17T10:00:00Z",
            "liveBroadcastContent": "none",
        },
        "status": {"uploadStatus": "processed", "privacyStatus": "public", "embeddable": True},
        "contentDetails": {"duration": "PT2H", "licensedContent": False},
        "statistics": {"viewCount": "120000", "likeCount": "4000", "commentCount": "300"},
    }


def fake_search_item(
    video_id: str, channel_id: str = "UCfakechannel", title: str | None = None, live: bool = False
) -> dict[str, Any]:
    """A `search.list` result item in the documented shape."""
    return {
        "kind": "youtube#searchResult",
        "id": {"kind": "youtube#video", "videoId": video_id},
        "snippet": {
            "publishedAt": "2026-11-17T10:00:00Z",
            "channelId": channel_id,
            "title": title or f"Video {video_id}",
            "description": "",
            "channelTitle": f"Name {channel_id}",
            "liveBroadcastContent": "live" if live else "none",
        },
    }


def _fake_channel_item(channel_id: str) -> dict[str, Any]:
    return {
        "id": channel_id,
        "snippet": {"title": f"Channel {channel_id}", "description": "Fake channel", "country": "US"},
        "statistics": {"subscriberCount": "250000", "viewCount": "90000000", "videoCount": "800"},
    }


# ── Apify ────────────────────────────────────────────────────


def fake_youtube_item(index: int, live: bool = False) -> YouTubeItem:
    return YouTubeItem(
        video_id=f"{'live' if live else 'vod'}{index:07d}",
        channel_id=f"UCchannel{index}",
        channel_name=f"Creator {index}",
        title=f"GTA {'live' if live else 'video'} {index}",
        is_live=live,
        viewers=5000 - index * 100 if live else None,
        views=None if live else 200_000 - index * 1000,
        published_at=FIXED_TIME,
        duration_s=None if live else 3600,
        likes=1000,
        comments=None if live else 150,
        subscribers=100_000,
        url=f"https://www.youtube.com/watch?v={'live' if live else 'vod'}{index:07d}",
    )


class FakeApifyClient(_Recorder, ApifyClient):
    """Apify without the network. Runs succeed at once; `datasets` maps a dataset id to its rows."""

    def __init__(self) -> None:
        self.calls = []
        self.runs: dict[str, ApifyRun] = {}
        self.datasets: dict[str, list[dict[str, Any]]] = {}
        self.search_results: list[YouTubeItem] | None = None

    def is_configured(self) -> bool:
        return True

    def start_run(
        self,
        actor_id: str,
        input: dict[str, Any],  # noqa: A002
        max_charge_usd: float = 1.0,
        timeout_s: int = 300,
    ) -> str:
        self._record("start_run", actor_id=actor_id, input=input, max_charge_usd=max_charge_usd)
        run_id = f"run{len(self.runs) + 1}"
        self.runs[run_id] = ApifyRun(
            id=run_id, status="SUCCEEDED", dataset_id=f"dataset-{run_id}", cost_usd=0.0
        )
        return run_id

    def get_run(self, run_id: str) -> ApifyRun:
        self._record("get_run", run_id=run_id)
        return self.runs[run_id]

    def wait_for_run(
        self, run_id: str, timeout_s: float = 360.0, poll_s: float = 5.0, operation: str = "run"
    ) -> ApifyRun:
        self._record("wait_for_run", run_id=run_id)
        return self.runs[run_id]

    def get_items(self, dataset_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        self._record("get_items", dataset_id=dataset_id, limit=limit)
        return self.datasets.get(dataset_id, [])[:limit]

    def youtube_search(
        self,
        terms: list[str],
        live: bool = False,
        uploaded_within_hours: int | None = None,
        max_results: int = 50,
        sort: str | None = None,
        max_charge_usd: float = 1.0,
    ) -> list[YouTubeItem]:
        self._record(
            "youtube_search",
            terms=list(terms),
            live=live,
            uploaded_within_hours=uploaded_within_hours,
            max_results=max_results,
            sort=sort,
        )
        if self.search_results is not None:
            return [item for item in self.search_results if item.is_live == live][:max_results]
        return [fake_youtube_item(index, live=live) for index in range(min(3, max_results))]


def youtube_items_from_rows(rows: Sequence[dict[str, Any]]) -> list[YouTubeItem]:
    """Normalise raw actor rows (for tests that load a recorded dataset fixture)."""
    return [item for item in (normalise_youtube_item(row) for row in rows) if item is not None]


# ── Anthropic ────────────────────────────────────────────────


class FakeAnthropicClient(_Recorder, AnthropicClient):
    """Claude without the network.

    `responses` maps a `prompt_id` to the reply: a dict (validated against the schema), a model
    instance, a list of those (one per call, the last one repeats) or an exception to raise. A prompt
    with no entry gets `default`, or an error when no default is set.
    """

    def __init__(self, responses: Mapping[str, Any] | None = None, default: Any = None) -> None:
        self.calls = []
        self.responses: dict[str, Any] = dict(responses or {})
        self.default = default
        self._served: dict[str, int] = {}

    def is_configured(self) -> bool:
        return True

    def complete_json[T: BaseModel](
        self,
        prompt_id: str,
        system: str,
        user_content: str | Sequence[dict[str, Any]],
        schema: type[T],
        model: str,
        max_tokens: int,
        images: Sequence[bytes] | None = None,
        cache_system: bool = True,
        effort: Effort | None = "low",
        ref_id: str | None = None,
    ) -> T:
        self._record(
            "complete_json",
            prompt_id=prompt_id,
            system=system,
            user_content=user_content,
            schema=schema,
            model=model,
            max_tokens=max_tokens,
            images=len(images or ()),
            ref_id=ref_id,
        )
        reply = self.responses.get(prompt_id, self.default)
        if isinstance(reply, list):
            index = self._served.get(prompt_id, 0)
            self._served[prompt_id] = index + 1
            reply = reply[min(index, len(reply) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        if reply is None:
            raise KeyError(f"FakeAnthropicClient has no response for prompt_id {prompt_id!r}")
        if isinstance(reply, schema):
            return reply
        if isinstance(reply, BaseModel):
            reply = reply.model_dump()
        return schema.model_validate(reply)


# ── Transcription ────────────────────────────────────────────

FAKE_WORDS = ("no", "way", "he", "actually", "landed", "that")


def fake_transcript(provider: str = "fake") -> Transcript:
    words = tuple(
        Word(w=word, s=round(index * 0.4, 2), e=round(index * 0.4 + 0.3, 2))
        for index, word in enumerate(FAKE_WORDS)
    )
    return Transcript(language="en", words=words, duration_s=5.0, provider=provider, model="fake")


class FakeTranscriber(_Recorder, Transcriber):
    """Transcription without a model or the network: every file gives the same six words."""

    def __init__(self, transcript: Transcript | None = None) -> None:
        self.calls = []
        self.transcript = transcript or fake_transcript()
        self.error: Exception | None = None

    def configured_providers(self) -> set[str]:
        return {"openai"}

    def transcribe(self, audio_path: Path, mode: Mode = "local", language: str | None = None) -> Transcript:
        self._record("transcribe", audio_path=audio_path, mode=mode, language=language)
        if self.error is not None:
            raise self.error
        return self.transcript


# ── Upload-Post ──────────────────────────────────────────────


class FakeUploadPostClient(_Recorder, UploadPostClient):
    """Upload-Post without the network.

    `upload_video` stores the upload and returns `pending`; `get_upload_status` then reports it
    completed with a deterministic post id and URL. Put an exception in `upload_error` to make the next
    upload fail, or a `PlatformResult` in `platform_results[platform]` to control the outcome. Sending
    the same idempotency key twice returns the first upload again without creating a second one.
    """

    def __init__(self) -> None:
        self.calls = []
        self.uploads: dict[str, dict[str, Any]] = {}
        self.deleted: list[tuple[str, str, str]] = []
        self.upload_error: Exception | None = None
        self.platform_results: dict[str, PlatformResult] = {}
        self.analytics: dict[str, Any] = {
            "tiktok": {"followers": 1200, "views": 45000},
            "instagram": {"followers": 800, "reach": 9000, "views": 20000},
            "youtube": {"followers": 300, "views": 15000},
        }
        self._by_key: dict[str, str] = {}

    def is_configured(self) -> bool:
        return True

    def upload_video(
        self,
        user: str,
        platform: str,
        video_path: Path,
        fields: Mapping[str, Any],
        idempotency_key: str,
        request_id: str,
    ) -> UploadStatus:
        self._record(
            "upload_video",
            user=user,
            platform=platform,
            video_path=video_path,
            fields=dict(fields),
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        if self.upload_error is not None:
            error, self.upload_error = self.upload_error, None
            raise error
        first = self._by_key.setdefault(idempotency_key, request_id)
        if first == request_id:
            self.uploads[request_id] = {
                "user": user,
                "platform": platform,
                "fields": dict(fields),
                "path": video_path,
            }
        return UploadStatus(request_id=first, status="pending", completed=0, total=1)

    def _result(self, request_id: str) -> PlatformResult:
        platform = str(self.uploads[request_id]["platform"])
        if platform in self.platform_results:
            return self.platform_results[platform]
        post_id = f"{platform}-{request_id}"
        return PlatformResult(
            platform=platform,
            success=True,
            status="completed",
            post_id=post_id,
            post_url=f"https://example.test/{platform}/{post_id}",
        )

    def get_upload_status(self, request_id: str) -> UploadStatus:
        self._record("get_upload_status", request_id=request_id)
        if request_id not in self.uploads:
            return UploadStatus(request_id=request_id, status="not_found")
        result = self._result(request_id)
        return UploadStatus(
            request_id=request_id,
            status="completed" if result.success else "failed",
            completed=1,
            total=1,
            results=(result,),
        )

    def wait_for_upload(
        self, request_id: str, timeout_s: float = 600.0, poll_s: float = 10.0
    ) -> UploadStatus:
        self._record("wait_for_upload", request_id=request_id, timeout_s=timeout_s)
        return self.get_upload_status(request_id)

    def list_profiles(self) -> list[dict[str, Any]]:
        self._record("list_profiles")
        return [{"username": "default", "social_accounts": {"tiktok": {}, "instagram": {}, "youtube": {}}}]

    def get_profile_analytics(self, user: str, platforms: Sequence[str]) -> dict[str, Any]:
        self._record("get_profile_analytics", user=user, platforms=list(platforms))
        return {platform: self.analytics.get(platform, {}) for platform in platforms}

    def get_post_analytics(self, request_id: str, platform: str | None = None) -> dict[str, Any]:
        self._record("get_post_analytics", request_id=request_id, platform=platform)
        if request_id not in self.uploads:
            return {"success": False, "platforms": {}}
        result = self._result(request_id)
        metrics = {"views": 1000, "likes": 50, "comments": 5, "shares": 2}
        return {
            "success": True,
            "post": {"request_id": request_id, "profile_username": self.uploads[request_id]["user"]},
            "platforms": {
                result.platform: {
                    "success": result.success,
                    "platform_post_id": result.post_id,
                    "post_url": result.post_url,
                    "post_metrics": metrics,
                }
            },
        }

    def get_post_analytics_by_platform_id(
        self, user: str, platform: str, platform_post_id: str
    ) -> dict[str, Any]:
        self._record(
            "get_post_analytics_by_platform_id",
            user=user,
            platform=platform,
            platform_post_id=platform_post_id,
        )
        return {
            "success": True,
            "post": {"platform_post_id": platform_post_id, "platform": platform, "profile_username": user},
            "post_metrics": {"views": 1000, "likes": 50, "comments": 5, "shares": 2},
        }

    def delete_post(self, user: str, platform: str, post_id: str) -> bool:
        self._record("delete_post", user=user, platform=platform, post_id=post_id)
        if platform in NO_DELETE_PLATFORMS:
            raise NotSupported(f"Upload-Post cannot delete {platform} posts")
        self.deleted.append((user, platform, post_id))
        return True
