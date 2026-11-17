"""The `ChatMessage` dataclass: the single normalised shape for a chat message from any platform, live or
replay. Implemented in M03.

Also holds `ReplayChat` (a whole downloaded chat replay with its metadata), the chat error types, and the
JSON Lines helpers used by the CLI and by the VOD lane to store a replay on disk.
"""

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast, get_args

Platform = Literal["twitch", "youtube"]
Kind = Literal["chat", "notice", "sub", "gift", "raid", "donation", "system"]

PLATFORMS: Final[tuple[str, ...]] = get_args(Platform)
KINDS: Final[tuple[str, ...]] = get_args(Kind)


class ChatError(RuntimeError):
    """Base class for chat ingestion failures."""


class ChatSourceError(ChatError):
    """The chat source could not be read: network failure, blocked request, missing video or bad data."""


class ChatConnectionError(ChatError):
    """A live chat connection could not be kept open within the allowed number of reconnects."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message in the shape the spike engine consumes.

    `t` is unix seconds (UTC wall clock) for live sources and seconds from the start of the video for
    replays. `user_id` is the platform's stable id, or the lowercase username when the platform gives none.
    """

    platform: Platform
    t: float
    user_id: str
    username: str
    text: str
    kind: Kind
    is_mod: bool = False
    is_broadcaster: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict with the field names as keys."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatMessage":
        """Build a message from a dict made by `to_dict`. Raises `ValueError` on a bad platform or kind."""
        platform = data["platform"]
        kind = data["kind"]
        if platform not in PLATFORMS:
            raise ValueError(f"unknown chat platform: {platform!r}")
        if kind not in KINDS:
            raise ValueError(f"unknown chat message kind: {kind!r}")
        return cls(
            platform=cast(Platform, platform),
            t=float(data["t"]),
            user_id=str(data["user_id"]),
            username=str(data["username"]),
            text=str(data["text"]),
            kind=cast(Kind, kind),
            is_mod=bool(data.get("is_mod", False)),
            is_broadcaster=bool(data.get("is_broadcaster", False)),
        )


@dataclass(frozen=True, slots=True)
class ReplayChat:
    """A whole chat replay: messages sorted by `t` (seconds from the start of the video) plus metadata.

    `duration_s` is the video length when the source reports it, else None. `title` and `game` are filled
    when the source reports them (Twitch does, YouTube gives the title only).
    """

    platform: Platform
    source_id: str
    messages: list[ChatMessage]
    duration_s: float | None = None
    title: str | None = None
    game: str | None = None

    @property
    def message_count(self) -> int:
        """Number of messages of every kind in the replay."""
        return len(self.messages)


def sort_messages(messages: Iterable[ChatMessage]) -> list[ChatMessage]:
    """Return the messages sorted by time; messages with the same time keep their original order."""
    return sorted(messages, key=lambda message: message.t)


def write_jsonl(path: Path, messages: Iterable[ChatMessage]) -> int:
    """Write one JSON object per message to `path` (UTF-8). Returns the number of messages written."""
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for message in messages:
            handle.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
            count += 1
    return count


def iter_jsonl(path: Path) -> Iterator[ChatMessage]:
    """Yield the messages of a file written by `write_jsonl`. Blank lines are skipped."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield ChatMessage.from_dict(json.loads(line))


def read_jsonl(path: Path) -> list[ChatMessage]:
    """Read a file written by `write_jsonl` into a list."""
    return list(iter_jsonl(path))
