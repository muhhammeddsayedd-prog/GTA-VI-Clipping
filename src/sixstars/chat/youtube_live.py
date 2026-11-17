"""Live YouTube chat reader for streams in progress, yielding normalised `ChatMessage` objects. Implemented
in M03.

It polls YouTube's own web chat endpoint (see `youtube_innertube`), natively async, so no thread and no
queue are needed. The kit planned `chat-downloader` here; that package no longer works against YouTube.
"""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Final

import httpx

from sixstars.chat import youtube_innertube as innertube
from sixstars.chat.models import ChatConnectionError, ChatMessage, ChatSourceError
from sixstars.chat.net import backoff_delay
from sixstars.logging import get_logger

# Polling constants (not runtime settings: `docs/16-settings-reference.md` has no keys for them).
POLL_MAX_S: Final = 5.0  # YouTube suggests 10 s between polls; we poll at least this often
POLL_MIN_S: Final = 1.0
SETUP_ATTEMPTS: Final = 3  # tries to open the chat before the first message batch arrives
SEEN_IDS_KEPT: Final = 5000


class _SeenIds:
    """Remembers the most recent message ids, so the backlog sent again after a reconnect is dropped."""

    def __init__(self, capacity: int) -> None:
        self._order: deque[str] = deque()
        self._ids: set[str] = set()
        self._capacity = capacity

    def add(self, message_id: str) -> bool:
        """Record `message_id`. Returns False when it was already known."""
        if message_id in self._ids:
            return False
        self._ids.add(message_id)
        self._order.append(message_id)
        if len(self._order) > self._capacity:
            self._ids.discard(self._order.popleft())
        return True


class _EndedError(Exception):
    """The stream's chat is over (the stream ended)."""


class _State:
    """Progress shared between `stream` and its sessions."""

    def __init__(self) -> None:
        self.established = False  # a chat batch has been received at least once
        self.failures = 0  # failed attempts since the chat last made progress


async def _session(
    http: httpx.AsyncClient,
    video_id: str,
    seen: _SeenIds,
    state: _State,
    skip_backlog: bool,
    poll_max_s: float,
    sleep: Callable[[float], Awaitable[None]],
) -> AsyncIterator[ChatMessage]:
    """Open the chat from the watch page and yield new messages until an error or the end of the chat.

    Raises `_EndedError` when the chat is over and `ChatSourceError` on any failure.
    """
    page = await innertube.fetch_watch_page(http, video_id)
    if page.continuation is None or page.is_replay:
        detail = page.chat_notice or page.unplayable_reason or "the video has no live chat"
        raise _EndedError("the stream has ended" if page.is_replay else detail)
    continuation: str | None = page.continuation
    on_all_chat = False
    first_batch = True
    while continuation is not None:
        chat = await innertube.post_chat(http, page, innertube.LIVE_ENDPOINT, continuation)
        if chat is None:
            raise _EndedError("the chat returned no data")
        if not on_all_chat:
            # The watch page opens "Top chat", which hides messages; move to "Live chat" (all messages).
            on_all_chat = True
            if chat.all_chat_continuation is not None:
                continuation = chat.all_chat_continuation
                continue
        state.established = True
        if not first_batch:
            state.failures = 0
        for action in chat.actions:
            for message_id, message in innertube.action_to_messages(action):
                if seen.add(message_id) and not (first_batch and skip_backlog):
                    # A message we had not seen proves the chat is moving again after a reconnect.
                    state.failures = 0
                    yield message
        first_batch = False
        continuation = chat.continuation
        if continuation is not None:
            suggested = chat.timeout_ms / 1000 if chat.timeout_ms else poll_max_s
            await sleep(max(POLL_MIN_S, min(suggested, poll_max_s)))
    raise _EndedError("the chat has no continuation")


async def stream(
    video: str,
    *,
    proxy: str | None = None,
    cookies_file: Path | None = None,
    include_backlog: bool = True,
    max_reconnects: int | None = None,
    poll_max_s: float = POLL_MAX_S,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncGenerator[ChatMessage, None]:
    """Yield the live chat of a YouTube stream (video id or URL) as `ChatMessage` objects.

    `t` is the message's own timestamp in unix seconds. When the chat opens, YouTube sends the most recent
    messages (about 75, already in the past, with their true timestamps); they are yielded first unless
    `include_backlog` is False. Messages arrive in batches, one poll every `poll_max_s` seconds at most.

    The iterator ends normally when the stream ends. If the chat cannot be opened at all (no live chat, not
    live, blocked), `ChatSourceError` is raised after a few attempts. Once the chat is open, a
    failure reconnects after 1, 2, 5, 10, then 30 seconds; with `max_reconnects` set, that many consecutive
    failed attempts raise `ChatConnectionError`.
    """
    log = get_logger("chat")
    video_id = innertube.extract_video_id(video)
    seen = _SeenIds(SEEN_IDS_KEPT)
    state = _State()
    async with innertube.new_client(proxy, cookies_file, transport) as http:
        while True:
            skip_backlog = not include_backlog and not state.established
            try:
                async for message in _session(http, video_id, seen, state, skip_backlog, poll_max_s, sleep):
                    yield message
            except _EndedError as exc:
                if not state.established:
                    raise ChatSourceError(f"no live chat for YouTube video {video_id}: {exc}") from None
                log.info("youtube_live_chat_ended", video_id=video_id, reason=str(exc))
                return
            except ChatSourceError as exc:
                reason = str(exc)
            else:  # pragma: no cover - `_session` always ends by raising
                return
            failures = state.failures
            if not state.established and failures + 1 >= SETUP_ATTEMPTS:
                raise ChatSourceError(f"cannot open live chat for YouTube video {video_id}: {reason}")
            if state.established and max_reconnects is not None and failures >= max_reconnects:
                log.error("youtube_live_gave_up", video_id=video_id, reason=reason, failures=failures)
                raise ChatConnectionError(
                    f"YouTube chat for {video_id} lost after {failures} reconnects: {reason}"
                )
            delay = backoff_delay(failures)
            state.failures = failures + 1
            log.warning(
                "youtube_live_reconnecting",
                video_id=video_id,
                reason=reason,
                delay_s=delay,
                attempt=state.failures,
            )
            await sleep(delay)
