"""Read-only checks against real Twitch and YouTube. Skipped unless `RUN_LIVE_TESTS=1`.

Sources come from environment variables, because what is live changes by the hour:

- `LIVE_TWITCH_LOGIN`: a channel that is live now (anonymous IRC, 45 seconds);
- `LIVE_TWITCH_VOD`: a VOD id (needs `LIVE_TWITCH_DOWNLOADER_CLI`, the path of TwitchDownloaderCLI);
- `LIVE_YOUTUBE_LIVE_URL`: a stream that is live now (45 seconds);
- `LIVE_YOUTUBE_REPLAY_URL`: a finished stream with chat replay;
- `LIVE_YOUTUBE_NO_CHAT_URL`: a normal upload (defaults to a well-known music video).

A test whose variable is missing is skipped.
"""

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from sixstars.chat import twitch_irc, twitch_replay, youtube_live, youtube_replay
from sixstars.chat.models import ChatMessage

pytestmark = pytest.mark.live

LISTEN_S = 45.0


def _source(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default or "")
    if not value:
        pytest.skip(f"{name} is not set")
    return value


async def _listen(stream: AsyncGenerator[ChatMessage, None], seconds: float) -> list[ChatMessage]:
    messages: list[ChatMessage] = []

    async def consume() -> None:
        async for message in stream:
            messages.append(message)

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(consume(), timeout=seconds)
    return messages


async def test_twitch_live_chat_delivers_messages() -> None:
    login = _source("LIVE_TWITCH_LOGIN")
    started = time.time()
    messages = await _listen(twitch_irc.stream(login, max_reconnects=2), LISTEN_S)
    assert messages, "no chat message in 45 seconds: is the channel live?"
    assert all(message.platform == "twitch" and message.user_id for message in messages)
    assert all(abs(message.t - started) < LISTEN_S + 60 for message in messages)


def test_twitch_replay_download() -> None:
    vod = _source("LIVE_TWITCH_VOD")
    cli = Path(_source("LIVE_TWITCH_DOWNLOADER_CLI"))
    chat = twitch_replay.download(vod, cli_path=cli)
    assert chat.message_count > 0
    assert chat.duration_s is not None and chat.duration_s > 0
    assert chat.messages[-1].t <= chat.duration_s + 60
    assert [message.t for message in chat.messages] == sorted(message.t for message in chat.messages)


async def test_youtube_live_chat_delivers_messages() -> None:
    url = _source("LIVE_YOUTUBE_LIVE_URL")
    messages = await _listen(youtube_live.stream(url, max_reconnects=2), LISTEN_S)
    # The backlog alone proves the whole path (watch page, view switch, polling, parsing).
    assert messages, "no chat message at all: is the stream live with chat enabled?"
    assert all(message.platform == "youtube" and message.t > 1_600_000_000 for message in messages)
    assert any(message.kind == "chat" for message in messages)


async def test_youtube_replay_fetch() -> None:
    url = _source("LIVE_YOUTUBE_REPLAY_URL")
    chat = await youtube_replay.fetch(url)
    assert chat is not None, "this video has no chat replay"
    assert chat.message_count > 0
    assert chat.duration_s is not None
    assert 0 <= chat.messages[0].t <= chat.messages[-1].t <= chat.duration_s + 60


async def test_youtube_video_without_chat_gives_none() -> None:
    url = _source("LIVE_YOUTUBE_NO_CHAT_URL", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert await youtube_replay.fetch(url) is None


def test_youtube_replay_ytdlp_fallback_agrees_with_the_primary_path() -> None:
    url = _source("LIVE_YOUTUBE_REPLAY_URL")
    primary = youtube_replay.download(url, use_fallback=False)
    fallback = youtube_replay.download_with_ytdlp(url)
    assert primary is not None and fallback is not None
    assert fallback.message_count == primary.message_count
