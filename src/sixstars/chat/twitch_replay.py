"""Twitch VOD chat replay download through a TwitchDownloaderCLI wrapper, parsed into normalised
`ChatMessage` objects. Implemented in M03.

Verified against TwitchDownloaderCLI 1.56.5 (2026-07): the command is
`TwitchDownloaderCLI chatdownload -u <vod id> -o <file>.json`; emote and badge images are only embedded
with `-E`, which is off by default, so the file stays small. The JSON has `video` (`length`, `title`,
`game`) and `comments[]` with `content_offset_seconds` (whole seconds), `commenter` (`_id`, `name`,
`display_name`) and `message` (`body`, `bits_spent`, `user_badges[]._id`). No API keys are needed.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from sixstars.chat.models import ChatMessage, ChatSourceError, Kind, ReplayChat, sort_messages
from sixstars.chat.twitch_irc import USERNOTICE_KINDS
from sixstars.config import env
from sixstars.logging import get_logger

# Process constants (not runtime settings: `docs/16-settings-reference.md` has no keys for them).
DOWNLOAD_TIMEOUT_S: Final = 900.0
DOWNLOAD_ATTEMPTS: Final = 3
RETRY_BACKOFF_S: Final[tuple[float, ...]] = (2.0, 8.0)
# The CLI default is 4 threads; its help warns that large values may cause IP rate limiting.
DEFAULT_THREADS: Final = 8
_STDERR_TAIL_CHARS: Final = 500

_VOD_ID_RE: Final = re.compile(r"(?:twitch\.tv/(?:[^/]+/v(?:ideo)?|videos)/|^v?)(\d+)(?:[/?#].*)?$")

# The CLI is run as `runner(command, env, timeout_s)` and returns (exit code, stderr text).
Runner = Callable[[Sequence[str], Mapping[str, str], float], tuple[int, str]]


def normalise_vod_id(vod: str) -> str:
    """Return the numeric VOD id from an id (`123`, `v123`) or a VOD URL. Raises `ValueError` if invalid."""
    match = _VOD_ID_RE.search(vod.strip())
    if match is None:
        raise ValueError(f"not a Twitch VOD id or URL: {vod!r}")
    return match.group(1)


def build_command(cli_path: Path, vod_id: str, out_path: Path, threads: int, temp_dir: Path) -> list[str]:
    """Return the TwitchDownloaderCLI command line that writes the chat of `vod_id` to `out_path`."""
    return [
        str(cli_path),
        "chatdownload",
        "--id",
        vod_id,
        "--output",
        str(out_path),
        "--threads",
        str(threads),
        "--temp-path",
        str(temp_dir),
        "--collision",
        "Overwrite",
        "--banner",
        "false",
        "--log-level",
        "Error",
    ]


def _run_subprocess(
    command: Sequence[str], environment: Mapping[str, str], timeout_s: float
) -> tuple[int, str]:
    """Run the CLI and return (exit code, stderr). A timeout kills the process and returns code -1."""
    try:
        done = subprocess.run(
            list(command),
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout_s:.0f} s"
    except OSError as exc:
        return -2, f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stderr.decode("utf-8", "replace")


def _comment_kind(message: Mapping[str, Any]) -> Kind:
    notice = message.get("user_notice_params")
    msg_id = notice.get("msg-id") or notice.get("msg_id") if isinstance(notice, Mapping) else None
    if msg_id:
        return USERNOTICE_KINDS.get(str(msg_id), "notice")
    bits = message.get("bits_spent")
    if isinstance(bits, int) and bits > 0:
        return "donation"
    return "chat"


def _comment_to_message(comment: Mapping[str, Any]) -> ChatMessage | None:
    """Convert one `comments[]` entry. Entries with no offset or no commenter (deleted accounts) give None."""
    offset = comment.get("content_offset_seconds")
    commenter = comment.get("commenter")
    message = comment.get("message")
    if isinstance(offset, bool) or not isinstance(offset, int | float):
        return None
    if not isinstance(commenter, Mapping) or not isinstance(message, Mapping):
        return None
    login = str(commenter.get("name") or "")
    username = str(commenter.get("display_name") or login)
    user_id = str(commenter.get("_id") or login.lower() or username.lower())
    if not user_id:
        return None
    raw_badges = message.get("user_badges")
    badges = {
        str(badge.get("_id")) for badge in raw_badges or [] if isinstance(badge, Mapping)
    }  # fmt: skip
    return ChatMessage(
        platform="twitch",
        t=float(offset),
        user_id=user_id,
        username=username,
        text=str(message.get("body") or ""),
        kind=_comment_kind(message),
        is_mod="moderator" in badges,
        is_broadcaster="broadcaster" in badges,
    )


def parse_chat_json(data: Mapping[str, Any], source_id: str | None = None) -> ReplayChat:
    """Convert a decoded TwitchDownloaderCLI chat JSON document into a `ReplayChat` sorted by time."""
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ChatSourceError("Twitch chat file has no `comments` list")
    raw_video = data.get("video")
    video: Mapping[str, Any] = raw_video if isinstance(raw_video, Mapping) else {}
    messages = [
        message
        for message in (_comment_to_message(comment) for comment in comments if isinstance(comment, Mapping))
        if message is not None
    ]
    length = video.get("length")
    if isinstance(length, bool) or not isinstance(length, int | float) or length <= 0:
        length = video.get("end")
    duration = float(length) if isinstance(length, int | float) and not isinstance(length, bool) else None
    title = video.get("title")
    game = video.get("game")
    return ReplayChat(
        platform="twitch",
        source_id=source_id or str(video.get("id") or ""),
        messages=sort_messages(messages),
        duration_s=duration,
        title=title if isinstance(title, str) else None,
        game=game if isinstance(game, str) else None,
    )


def parse_file(path: Path, source_id: str | None = None) -> ReplayChat:
    """Read and convert a TwitchDownloaderCLI chat JSON file. Raises `ChatSourceError` if it is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ChatSourceError(f"cannot read Twitch chat file {path.name}: {type(exc).__name__}") from exc
    if not isinstance(data, Mapping):
        raise ChatSourceError(f"Twitch chat file {path.name} is not a JSON object")
    return parse_chat_json(data, source_id)


def _child_environment(proxy: str | None) -> dict[str, str]:
    """Environment for the CLI: the current one, plus proxy variables (.NET honours them) when given."""
    environment = dict(os.environ)
    if proxy:
        environment["HTTPS_PROXY"] = proxy
        environment["HTTP_PROXY"] = proxy
        environment["ALL_PROXY"] = proxy
    return environment


def download(
    vod: str,
    out_path: Path | None = None,
    *,
    cli_path: Path | None = None,
    threads: int = DEFAULT_THREADS,
    proxy: str | None = None,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
    attempts: int = DOWNLOAD_ATTEMPTS,
    runner: Runner = _run_subprocess,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplayChat:
    """Download the chat replay of a Twitch VOD (id or URL) and return it parsed and sorted by time.

    The raw TwitchDownloaderCLI JSON is kept at `out_path` when one is given, else it is written to a
    temporary directory and deleted. `cli_path` defaults to the `TWITCH_DOWNLOADER_CLI` environment value.
    A failed or timed-out run is retried (up to `attempts` runs in total, with backoff); after that, and
    for an unreadable result, `ChatSourceError` is raised. A VOD that exists but has an empty chat gives a
    `ReplayChat` with no messages.
    """
    log = get_logger("chat")
    vod_id = normalise_vod_id(vod)
    cli = cli_path or env().TWITCH_DOWNLOADER_CLI
    environment = _child_environment(proxy)
    with tempfile.TemporaryDirectory(prefix="sixstars-twitch-chat-") as temp_name:
        temp_dir = Path(temp_name)
        target = out_path or temp_dir / f"{vod_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        command = build_command(cli, vod_id, target, threads, temp_dir)
        started = time.monotonic()
        error = ""
        for attempt in range(1, attempts + 1):
            code, stderr = runner(command, environment, timeout_s)
            if code == 0 and target.exists():
                chat = parse_file(target, vod_id)
                log.info(
                    "twitch_replay_downloaded",
                    vod_id=vod_id,
                    messages=chat.message_count,
                    duration_s=chat.duration_s,
                    elapsed_s=round(time.monotonic() - started, 1),
                    attempt=attempt,
                )
                return chat
            error = f"exit code {code}: {stderr.strip()[-_STDERR_TAIL_CHARS:]}"
            log.warning("twitch_replay_attempt_failed", vod_id=vod_id, attempt=attempt, error=error)
            if code == -2:  # the CLI could not be started at all: retrying cannot help
                break
            if attempt < attempts:
                sleep(RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)])
    raise ChatSourceError(f"TwitchDownloaderCLI failed for VOD {vod_id}: {error}")
