"""The `sixstars live` commands: `status`, `probe` and `cut`.

`probe` is the check to run on a server whose access to a platform is unknown: it records one stream for
a few seconds with exactly the commands of the live lane (same proxy and cookies rules), reads its chat,
cuts a window from the buffer and says how the attempt is classified. It writes nothing to the database
and removes its files.
"""

import asyncio
import contextlib
import json
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from sixstars import chat, db
from sixstars.chat.models import ChatError, Platform
from sixstars.chat.net import cookies_file, proxy_for
from sixstars.config import env
from sixstars.live import buffer, health, store
from sixstars.live.config import LiveConfig
from sixstars.live.recorder import Recorder, stream_url

PROBE_SEGMENT_S = 10.0
PROBE_CUT_S = 12.0


def platform_of(source: str) -> Platform:
    """`youtube` for a YouTube URL or video id with a URL shape, else `twitch` (a login or a Twitch URL)."""
    lowered = source.lower()
    return "youtube" if "youtube.com" in lowered or "youtu.be" in lowered else "twitch"


def twitch_login(source: str) -> str:
    """The channel login from a Twitch URL or a bare login."""
    return source.rstrip("/").rsplit("/", 1)[-1].lstrip("@").lower()


async def _count_chat(platform: Platform, channel: str, seconds: float) -> tuple[int, str]:
    """Messages read from the live chat in `seconds`, and the error text when it could not be read."""
    count = 0
    stream = chat.open_live(platform, channel, max_reconnects=1)
    try:
        async with asyncio.timeout(seconds):
            async for _ in stream:
                count += 1
    except TimeoutError:
        pass
    except ChatError as exc:
        return count, str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 - the probe reports every failure as text
        return count, f"{type(exc).__name__}: {exc}"[:300]
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()
    return count, ""


def probe(source: str, seconds: float, *, keep: bool = False, with_chat: bool = True) -> int:
    """Record `source` for `seconds`, read its chat, cut a window and print a JSON report.

    Exit code 0 when stream data was recorded, 3 when the platform refused this machine, 1 otherwise.
    """
    platform = platform_of(source)
    login = twitch_login(source) if platform == "twitch" else ""
    url = stream_url(platform, login, "", source)
    cfg = LiveConfig.offline()
    directory = Path(tempfile.mkdtemp(prefix="probe-", dir=_scratch_root()))
    recorder = Recorder(
        url,
        platform,
        directory,
        segment_s=PROBE_SEGMENT_S,
        max_height=cfg.max_height,
        start_timeout_s=cfg.start_timeout_s,
        stall_timeout_s=cfg.stall_timeout_s,
        proxy=proxy_for(platform),
        cookies_file=cookies_file() if platform == "youtube" else None,
    )
    report: dict[str, Any] = {
        "platform": platform,
        "url": url,
        "proxy": proxy_for(platform) is not None,
        "cookies": platform == "youtube" and cookies_file() is not None,
        "result": "no_data",
    }
    started = time.time()
    recorder.start()
    chat_result: tuple[int, str] = (0, "")
    try:
        channel = (login if platform == "twitch" else source) if with_chat else None
        chat_result = asyncio.run(_poll(recorder, platform, channel, started + seconds, report))
        _describe_buffer(directory, report)
    finally:
        recorder.stop(cfg.stop_timeout_s)
        if not keep:
            shutil.rmtree(directory, ignore_errors=True)
    report["chat_messages"], report["chat_error"] = chat_result
    report["kept"] = str(directory) if keep else None
    failure_info = report.get("failure")
    if report.get("bytes"):
        report["result"] = "recorded"
    elif isinstance(failure_info, dict):
        report["result"] = str(failure_info["kind"])
    typer.echo(json.dumps(report, indent=2, ensure_ascii=True))
    if report["result"] == "recorded":
        return 0
    return 3 if report["result"] == "blocked" else 1


async def _poll(
    recorder: Recorder, platform: Platform, channel: str | None, deadline: float, report: dict[str, Any]
) -> tuple[int, str]:
    """Watch the recorder until `deadline` while the chat (when `channel` is given) is counted."""
    started = time.time()
    counter = (
        asyncio.create_task(_count_chat(platform, channel, max(deadline - started, 1.0)))
        if channel is not None
        else None
    )
    first_data: float | None = None
    failure = None
    while time.time() < deadline:
        status = await asyncio.to_thread(recorder.poll)
        if status.has_data and first_data is None:
            first_data = time.time() - started
        if status.failure is not None:
            failure = status.failure
            break
        await asyncio.sleep(1.0)
    report["first_data_s"] = round(first_data, 1) if first_data is not None else None
    report["failure"] = {"kind": failure.kind, "detail": failure.detail} if failure else None
    if counter is None:
        return 0, "not read"
    if failure is not None:
        counter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await counter
        return 0, "not read: the recording failed first"
    return await counter


def _scratch_root() -> str | None:
    # Not under `<DATA_DIR>/live`: the manager's janitor deletes the segments of every directory there
    # that does not belong to a running session.
    root = env().DATA_DIR / "live-probe"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None  # no data volume here: the system temp directory
    return str(root)


def _describe_buffer(directory: Path, report: dict[str, Any]) -> None:
    segments = buffer.scan(directory)
    size = sum(segment.size for segment in segments)
    report["segments"] = len(segments)
    report["bytes"] = size
    if not segments:
        return
    now = time.time()
    span = max(now - segments[0].start, 1.0)
    report["mbit_per_s"] = round(size * 8 / span / 1e6, 2)
    try:
        cut = buffer.extract(
            directory, now - PROBE_CUT_S - 4, now - 4, buffer.cut_path(directory, "probe"), anchor_span_s=300
        )
        report["cut"] = {"seconds": round(cut.duration_s, 2), "bytes": cut.size_bytes}
    except buffer.BufferCutError as exc:
        report["cut"] = {"error": str(exc)[:300]}


def cut(session: str, start: str, end: str, output: str) -> int:
    """Cut a wall-clock window (ISO 8601 times, UTC when no zone is given) from a session's buffer."""
    directory = Path(session)
    if not directory.is_dir():
        directory = store.buffer_dir(session)
    try:
        bounds = [_parse_time(value) for value in (start, end)]
    except ValueError as exc:
        typer.echo(f"bad time: {exc}", err=True)
        return 2
    out_path = Path(output) if output else buffer.cut_path(directory, f"manual-{int(time.time())}")
    try:
        result = buffer.extract(directory, bounds[0], bounds[1], out_path, anchor_span_s=300)
    except buffer.BufferCutError as exc:
        typer.echo(f"cut failed: {exc}", err=True)
        return 1
    typer.echo(
        json.dumps(
            {
                "path": str(result.path),
                "seconds": round(result.duration_s, 2),
                "bytes": result.size_bytes,
                "file_start": datetime.fromtimestamp(result.file_start, UTC).isoformat(),
                "window_offset_s": round(result.offset(bounds[0]), 2),
                "segments": list(result.segments),
            },
            indent=2,
        )
    )
    return 0


def _parse_time(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).timestamp()


def status(as_json: bool = False) -> int:
    """Print the live sessions, the buffers on disk and the health of the live sources."""
    cfg = LiveConfig.load()
    sessions = db.fetch_all(
        """
        select s.id::text as id, s.platform, s.status, c.handle, s.reconnects, s.started_at, s.ended_at,
               jsonb_array_length(s.clipped_ranges) as clips
        from live_sessions s join creators c on c.id = s.creator_id
        where s.status = any(%s) or s.updated_at > now() - interval '6 hours'
        order by s.updated_at desc limit 30
        """,
        (list(store.MANAGED_STATUSES),),
    )
    heartbeat = db.fetch_one(
        "select extract(epoch from now() - last_seen) as age_s, info from heartbeats "
        "where name = 'live-manager'"
    )
    root = store.live_root()
    buffers = {
        path.name: buffer.dir_bytes(path)
        for path in (root.iterdir() if root.is_dir() else [])
        if path.is_dir()
    }
    report = {
        "max_slots": cfg.max_slots,
        "heartbeat_age_s": round(float(heartbeat["age_s"]), 1) if heartbeat else None,
        "manager": heartbeat["info"] if heartbeat else None,
        "source_health": health.load(),
        "sessions": [
            {**row, "started_at": _iso(row["started_at"]), "ended_at": _iso(row["ended_at"])}
            for row in sessions
        ],
        "buffer_bytes": buffers,
    }
    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
        return 0
    typer.echo(f"live.max_slots {cfg.max_slots} · heartbeat age {report['heartbeat_age_s']} s")
    for name, entry in health.load().items():
        typer.echo(f"source {name}: blocked x{entry.get('blocks')} until {entry.get('skip_until')}")
    for row in sessions:
        size = buffers.get(row["id"], 0) / 1e6
        typer.echo(
            f"{row['status']:<14} {row['platform']:<8} {row['handle']:<24} reconnects {row['reconnects']} "
            f"clips {row['clips']} buffer {size:.0f} MB  {row['id']}"
        )
    if not sessions:
        typer.echo("no live sessions")
    return 0


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
