"""Chat ingestion for Twitch and YouTube, live and replay, normalised into one `ChatMessage` model that the
spike engine consumes.

Entry points (M03):

- live: `open_live(platform, channel)` gives an async generator of `ChatMessage` (`t` = unix seconds);
- replay: `download_replay(platform, source_id)` gives a `ReplayChat` (`t` = seconds from the start of the
  video), or None when a YouTube video has no chat replay;
- filtering: `sixstars.chat.filters` (`FilterContext`, `is_counted`, `learn_bots`, `cap_per_user`).

The adapter modules (`twitch_irc`, `twitch_replay`, `youtube_live`, `youtube_replay`) can also be used
directly when a caller needs their extra parameters.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

from sixstars.chat import twitch_irc, twitch_replay, youtube_live, youtube_replay
from sixstars.chat.models import (
    ChatConnectionError,
    ChatError,
    ChatMessage,
    ChatSourceError,
    Kind,
    Platform,
    ReplayChat,
)
from sixstars.chat.net import cookies_file, proxy_for

__all__ = [
    "ChatConnectionError",
    "ChatError",
    "ChatMessage",
    "ChatSourceError",
    "Kind",
    "Platform",
    "ReplayChat",
    "download_replay",
    "open_live",
]


def open_live(
    platform: Platform, channel: str, *, max_reconnects: int | None = None
) -> AsyncGenerator[ChatMessage, None]:
    """Return the live chat of a stream as an async generator of `ChatMessage` (close it with `aclose()`).

    `channel` is a Twitch login, or a YouTube video id or URL of the live stream. The proxy (if any) and
    the YouTube cookies file come from the environment and settings (a cookies path that names no file
    means no cookies). Twitch: the iterator never ends by
    itself. YouTube: it ends when the stream ends and raises `ChatSourceError` if there is no live chat.
    """
    proxy = proxy_for(platform)
    if platform == "twitch":
        return twitch_irc.stream(channel, proxy=proxy, max_reconnects=max_reconnects)
    return youtube_live.stream(
        channel, proxy=proxy, cookies_file=cookies_file(), max_reconnects=max_reconnects
    )


def download_replay(platform: Platform, source_id: str, *, raw_path: Path | None = None) -> ReplayChat | None:
    """Download a whole chat replay (blocking; for job handlers).

    `source_id` is a Twitch VOD id or URL, or a YouTube video id or URL. Twitch always returns a
    `ReplayChat` (the raw TwitchDownloaderCLI JSON is kept at `raw_path` when given). YouTube returns None
    when the video has no chat replay. Failures raise `ChatSourceError`.
    """
    proxy = proxy_for(platform)
    if platform == "twitch":
        return twitch_replay.download(source_id, raw_path, proxy=proxy)
    return youtube_replay.download(source_id, proxy=proxy, cookies_file=cookies_file())
