"""YouTube chat replay download for finished streams and premieres, parsed into normalised `ChatMessage`
objects. Implemented in M03.

Primary path: YouTube's own web chat endpoint (see `youtube_innertube`), which pages through the whole
replay in seconds. Fallback path: `yt-dlp --write-subs --sub-langs live_chat`, which writes the same raw
actions to a `.live_chat.json` file (slower, but maintained by others, so it survives a page change that
breaks our parser). The kit planned `chat-downloader` here; that package no longer works against YouTube.
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any, Final

import httpx

from sixstars.chat import youtube_innertube as innertube
from sixstars.chat.models import ChatMessage, ChatSourceError, ReplayChat, sort_messages
from sixstars.chat.net import redact_proxy, usable_cookies
from sixstars.logging import get_logger

# Request constants (not runtime settings: `docs/16-settings-reference.md` has no keys for them).
REQUEST_ATTEMPTS: Final = 3
RETRY_BACKOFF_S: Final[tuple[float, ...]] = (1.0, 3.0)
DOWNLOAD_TIMEOUT_S: Final = 900.0
YTDLP_SOCKET_TIMEOUT_S: Final = 20
_STDERR_TAIL_CHARS: Final = 500

# yt-dlp is run as `runner(command, timeout_s)` and returns (exit code, stdout, stderr).
Runner = Callable[[Sequence[str], float], tuple[int, str, str]]


class VideoUnavailableError(ChatSourceError):
    """The video cannot be used for a chat replay: it is unplayable (private, removed) or still live."""


def messages_from_actions(actions: Iterable[Mapping[str, Any]]) -> list[ChatMessage]:
    """Convert raw replay actions to messages sorted by time, dropping repeated message ids."""
    seen: set[str] = set()
    messages: list[ChatMessage] = []
    for action in actions:
        for message_id, message in innertube.action_to_messages(action):
            if message_id not in seen:
                seen.add(message_id)
                messages.append(message)
    return sort_messages(messages)


def parse_actions_file(path: Path) -> list[ChatMessage]:
    """Read a JSON Lines file of raw replay actions (what yt-dlp writes as `<id>.live_chat.json`)."""
    actions: list[Mapping[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                action = json.loads(line)
            except ValueError as exc:
                raise ChatSourceError(f"chat file {path.name} has a line that is not JSON") from exc
            if isinstance(action, dict):
                actions.append(action)
    return messages_from_actions(actions)


async def _with_retries[T](
    call: Callable[[], Awaitable[T]], sleep: Callable[[float], Awaitable[None]], what: str, video_id: str
) -> T:
    """Run `call`, retrying a `ChatSourceError` up to `REQUEST_ATTEMPTS` runs in total with backoff."""
    log = get_logger("chat")
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            return await call()
        except ChatSourceError as exc:
            if attempt == REQUEST_ATTEMPTS:
                raise
            delay = RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)]
            log.warning("youtube_replay_retry", video_id=video_id, what=what, error=str(exc), delay_s=delay)
            await sleep(delay)
    raise AssertionError("unreachable")


async def fetch(
    video: str,
    *,
    proxy: str | None = None,
    cookies_file: Path | None = None,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ReplayChat | None:
    """Download the chat replay of a finished YouTube stream or premiere (video id or URL).

    Returns None when the video has no chat replay (a normal upload, chat was disabled, or the replay is
    not available, which also happens for a while right after a stream ends). Raises
    `VideoUnavailableError` when the video is unplayable or still live, and `ChatSourceError` when YouTube
    cannot be read (network, block, changed page) or `timeout_s` runs out.
    """
    log = get_logger("chat")
    video_id = innertube.extract_video_id(video)
    started = time.monotonic()
    actions: list[dict[str, Any]] = []
    requests = 0
    async with innertube.new_client(proxy, cookies_file, transport) as http:
        page = await _with_retries(
            partial(innertube.fetch_watch_page, http, video_id), sleep, "watch", video_id
        )
        if page.continuation is None:
            if page.unplayable_reason is not None:
                raise VideoUnavailableError(
                    f"YouTube video {video_id} is unplayable: {page.unplayable_reason}"
                )
            log.info("youtube_replay_no_chat", video_id=video_id, notice=page.chat_notice)
            return None
        if not page.is_replay:
            raise VideoUnavailableError(
                f"YouTube video {video_id} is still live: its chat is not a replay yet"
            )
        continuation: str | None = page.continuation
        on_all_chat = False
        while continuation is not None:
            if time.monotonic() - started > timeout_s:
                raise ChatSourceError(f"YouTube chat replay for {video_id} timed out after {timeout_s:.0f} s")
            chat = await _with_retries(
                partial(innertube.post_chat, http, page, innertube.REPLAY_ENDPOINT, continuation),
                sleep,
                "chat",
                video_id,
            )
            requests += 1
            if chat is None:
                break
            if not on_all_chat:
                # The watch page opens "Top chat replay", which hides messages; move to the full replay.
                on_all_chat = True
                if chat.all_chat_continuation is not None:
                    continuation = chat.all_chat_continuation
                    continue
            actions.extend(chat.actions)
            continuation = chat.continuation
    messages = messages_from_actions(actions)
    log.info(
        "youtube_replay_downloaded",
        video_id=video_id,
        messages=len(messages),
        duration_s=page.duration_s,
        requests=requests,
        elapsed_s=round(time.monotonic() - started, 1),
    )
    return ReplayChat(
        platform="youtube",
        source_id=video_id,
        messages=messages,
        duration_s=page.duration_s,
        title=page.title,
    )


def build_ytdlp_command(
    video_id: str, out_dir: Path, proxy: str | None, cookies_file: Path | None
) -> list[str]:
    """Return the yt-dlp command that writes the chat replay of `video_id` into `out_dir`."""
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--write-subs",
        "--sub-langs",
        "live_chat",
        "--no-progress",
        "--no-playlist",
        "--socket-timeout",
        str(YTDLP_SOCKET_TIMEOUT_S),
        "--retries",
        str(REQUEST_ATTEMPTS),
        "--no-simulate",
        "--print",
        "%(duration)s",
        "--output",
        str(out_dir / "%(id)s.%(ext)s"),
    ]
    if proxy:
        command += ["--proxy", proxy]
    cookies_file = usable_cookies(cookies_file)  # a path without a file means "no cookies"
    if cookies_file is not None:
        command += ["--cookies", str(cookies_file)]
    command.append(f"https://www.youtube.com/watch?v={video_id}")
    return command


def _run_subprocess(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    """Run yt-dlp and return (exit code, stdout, stderr). A timeout kills it and returns code -1."""
    try:
        done = subprocess.run(
            list(command), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout_s:.0f} s"
    except OSError as exc:
        return -2, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")


def download_with_ytdlp(
    video: str,
    *,
    proxy: str | None = None,
    cookies_file: Path | None = None,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
    runner: Runner = _run_subprocess,
) -> ReplayChat | None:
    """Download a chat replay with yt-dlp (the fallback path). Same result contract as `fetch`, except
    that an unplayable or still-live video is reported as `ChatSourceError` and `title` is not filled."""
    video_id = innertube.extract_video_id(video)
    with tempfile.TemporaryDirectory(prefix="sixstars-youtube-chat-") as temp_name:
        out_dir = Path(temp_name)
        code, stdout, stderr = runner(build_ytdlp_command(video_id, out_dir, proxy, cookies_file), timeout_s)
        if code != 0:
            tail = redact_proxy(stderr.strip(), proxy)[-_STDERR_TAIL_CHARS:]
            raise ChatSourceError(f"yt-dlp failed for YouTube video {video_id} (exit code {code}): {tail}")
        chat_file = out_dir / f"{video_id}.live_chat.json"
        if not chat_file.exists():
            return None
        messages = parse_actions_file(chat_file)
    first_line = stdout.strip().splitlines()[0] if stdout.strip() else ""
    try:
        duration: float | None = float(first_line)
    except ValueError:
        duration = None
    return ReplayChat(platform="youtube", source_id=video_id, messages=messages, duration_s=duration)


def download(
    video: str,
    *,
    proxy: str | None = None,
    cookies_file: Path | None = None,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
    use_fallback: bool = True,
) -> ReplayChat | None:
    """Blocking form of `fetch` for job handlers, with the yt-dlp fallback.

    Returns the replay, or None when the video has no chat replay (the VOD lane then falls back to
    transcripts). If the primary path fails with a `ChatSourceError` other than `VideoUnavailableError`,
    yt-dlp is tried once before the error is raised. Must not be called from a running event loop: use
    `fetch` there.
    """
    log = get_logger("chat")
    try:
        return asyncio.run(fetch(video, proxy=proxy, cookies_file=cookies_file, timeout_s=timeout_s))
    except VideoUnavailableError:
        raise
    except ChatSourceError as exc:
        if not use_fallback:
            raise
        log.warning("youtube_replay_fallback_ytdlp", video=innertube.extract_video_id(video), error=str(exc))
        return download_with_ytdlp(video, proxy=proxy, cookies_file=cookies_file, timeout_s=timeout_s)
