"""Implementation of the `sixstars chat` commands (M03): `dump` prints a live chat for a number of seconds,
`replay` downloads a whole chat replay to a JSON Lines file. Both are read-only smoke-test tools and work
without a database when `PROXY_URL` is not set.
"""

import asyncio
import contextlib
import re
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import typer

from sixstars.chat import twitch_irc, twitch_replay, youtube_innertube, youtube_live, youtube_replay
from sixstars.chat.models import ChatError, ChatMessage, ReplayChat, write_jsonl
from sixstars.chat.net import cookies_file, proxy_for
from sixstars.config import MissingConfigError

EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2
EXIT_NO_CHAT: Final = 3

_TWITCH_VOD_RE: Final = re.compile(r"^v?\d+$")


def _safe_stdout() -> None:
    """Never crash on a console that cannot show a character (emoji on a Windows code page)."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")


def format_message(message: ChatMessage) -> str:
    """One display line: UTC time, kind, flags, user and text."""
    clock = datetime.fromtimestamp(message.t, UTC).strftime("%H:%M:%S")
    flags = ("B" if message.is_broadcaster else "") + ("M" if message.is_mod else "")
    tag = f"{message.kind}{'/' + flags if flags else ''}"
    return f"{clock} [{tag}] {message.username}: {message.text}"


def summary_line(kinds: Counter[str], users: int, seconds: float) -> str:
    """The closing line of `chat dump`: totals, distinct users and the chat rate per 10 seconds."""
    total = kinds.total()
    per_kind = ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items())) or "none"
    rate = kinds["chat"] / seconds * 10 if seconds > 0 else 0.0
    return (
        f"{total} messages in {seconds:.0f} s ({per_kind}); {users} users; {rate:.1f} chat messages per 10 s"
    )


async def _dump(
    messages: AsyncIterator[ChatMessage], seconds: float, since: float | None
) -> tuple[Counter[str], int]:
    """Print messages for `seconds` seconds. Messages older than `since` (backlog) are not counted."""
    kinds: Counter[str] = Counter()
    users: set[str] = set()

    async def consume() -> None:
        async for message in messages:
            backlog = since is not None and message.t < since
            typer.echo(("(earlier) " if backlog else "") + format_message(message))
            if not backlog:
                kinds[message.kind] += 1
                users.add(message.user_id)

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(consume(), timeout=seconds)
    return kinds, len(users)


def dump(source: str, seconds: float) -> int:
    """Run `sixstars chat dump`. Returns the process exit code."""
    _safe_stdout()
    try:
        if youtube_innertube.is_youtube_reference(source):
            stream = youtube_live.stream(
                source, proxy=proxy_for("youtube", "chat"), cookies_file=cookies_file(), max_reconnects=3
            )
            since: float | None = time.time()
        else:
            stream = twitch_irc.stream(source, proxy=proxy_for("twitch", "chat"), max_reconnects=3)
            since = None
        started = time.monotonic()
        kinds, users = asyncio.run(_dump(stream, seconds, since))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    except MissingConfigError as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    except ChatError as exc:
        typer.echo(f"chat failed: {exc}", err=True)
        return EXIT_ERROR
    typer.echo(summary_line(kinds, users, min(seconds, time.monotonic() - started)), err=True)
    return 0


def _download(source: str) -> ReplayChat | None:
    if not youtube_innertube.is_youtube_reference(source) and (
        _TWITCH_VOD_RE.match(source.strip()) or "twitch.tv/" in source
    ):
        return twitch_replay.download(source, proxy=proxy_for("twitch", "chat"))
    return youtube_replay.download(source, proxy=proxy_for("youtube", "chat"), cookies_file=cookies_file())


def replay(source: str, output: Path) -> int:
    """Run `sixstars chat replay`. Returns the process exit code."""
    started = time.monotonic()
    try:
        chat = _download(source)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    except MissingConfigError as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    except ChatError as exc:
        typer.echo(f"chat replay failed: {exc}", err=True)
        return EXIT_ERROR
    if chat is None:
        typer.echo("this video has no chat replay", err=True)
        return EXIT_NO_CHAT
    output.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(output, chat.messages)
    kinds = Counter(message.kind for message in chat.messages)
    per_kind = ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items())) or "none"
    duration = f"{chat.duration_s:.0f} s" if chat.duration_s is not None else "unknown"
    typer.echo(
        f"{chat.platform} {chat.source_id}: {chat.message_count} messages ({per_kind}), "
        f"video length {duration}, downloaded in {time.monotonic() - started:.1f} s -> {output}"
    )
    return 0
