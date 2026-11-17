"""What the modules of the VOD lane share: the replaceable collaborators and the two ways a video stops
early without having failed. Implemented in M08.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sixstars.chat.models import ReplayChat
from sixstars.clients.anthropic_client import AnthropicClient
from sixstars.clients.transcribe import Transcriber
from sixstars.vod.ytdlp import Runner

DeferKind = Literal[
    "download_blocked",
    "ai_budget",
    "transcribe_budget",
    "paused",
    "emergency_stop",
    "concurrency",
]
ChatDownloader = Callable[[str, str, Path | None], ReplayChat | None]


def utc_now() -> datetime:
    """The current time, timezone-aware, UTC."""
    return datetime.now(UTC)


@dataclass(slots=True)
class Deps:
    """Replaceable collaborators (tests pass fakes). None means the real one."""

    chat_downloader: ChatDownloader | None = None
    ai_client: AnthropicClient | None = None
    runner: Runner | None = None
    transcriber: Transcriber | None = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = utc_now
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


class DeferredError(Exception):
    """The video cannot go on right now and must be tried again at `run_after`. Not a failure: the video
    goes back to status `new` and the job is queued again without using up an attempt."""

    def __init__(self, kind: DeferKind, reason: str, run_after: datetime) -> None:
        super().__init__(reason)
        self.kind: DeferKind = kind
        self.reason = reason
        self.run_after = run_after


class VideoGoneError(Exception):
    """The video itself cannot be had (private, removed, members only): it fails for good."""
