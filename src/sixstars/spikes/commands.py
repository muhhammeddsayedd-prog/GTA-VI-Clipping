"""Implementation of `sixstars spikes scan` (M04): run replay detection on a chat file and print the spikes
as a table. A read-only tool for checking and tuning the engine; it never writes to the database.
"""

import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import typer

from sixstars.chat import twitch_replay
from sixstars.chat.models import ChatMessage, read_jsonl
from sixstars.spikes.buckets import tokenise
from sixstars.spikes.config import SETTING_KEYS, SpikeConfig, SpikeConfigError
from sixstars.spikes.detector import DetectedSpike, ReplayScan, analyse_replay

EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2
TOP_TOKENS: Final = 3
_SAMPLE_LINE_CHARS: Final = 90


def clock(seconds: float) -> str:
    """Seconds as `hh:mm:ss`."""
    whole = max(int(seconds), 0)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def load_chat(path: Path) -> list[ChatMessage]:
    """Read a chat file: `.jsonl` as written by `sixstars chat replay`, anything else as the JSON that
    TwitchDownloaderCLI writes."""
    if path.suffix.lower() == ".jsonl":
        return read_jsonl(path)
    return twitch_replay.parse_file(path).messages


def parse_overrides(pairs: Sequence[str]) -> dict[str, Any]:
    """Turn `key=json` options into a settings mapping. Raises `ValueError` on a bad pair."""
    overrides: dict[str, Any] = {}
    for pair in pairs:
        key, separator, raw = pair.partition("=")
        key = key.strip()
        if not separator or key not in SETTING_KEYS:
            raise ValueError(f"--set needs key=json with a spike setting key, got {pair!r}")
        try:
            overrides[key] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--set {key}: value is not valid JSON ({exc.msg})") from exc
    return overrides


def load_config(settings_file: Path | None, overrides: Mapping[str, Any]) -> SpikeConfig:
    """The config for a scan: from a JSON file of settings when one is given, else from the database."""
    if settings_file is None:
        return SpikeConfig.from_settings(overrides)
    values = json.loads(settings_file.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise SpikeConfigError("the settings file must hold one JSON object of setting keys")
    return SpikeConfig.from_mapping({**values, **overrides})


def top_tokens(spike: DetectedSpike, count: int = TOP_TOKENS) -> str:
    """The most common words in the chat sample from the peak onwards, as `WORD x n`."""
    words: Counter[str] = Counter()
    for entry in spike.chat_sample:
        if entry["t_rel"] >= 0:
            words.update(set(tokenise(str(entry["text"]))))
    return ", ".join(f"{word} x{n}" for word, n in words.most_common(count)) or "-"


def ranked(spikes: Sequence[DetectedSpike]) -> list[DetectedSpike]:
    """Best first: by score, then by message count."""
    return sorted(spikes, key=lambda spike: (spike.score_z, spike.msgs), reverse=True)


def format_table(spikes: Sequence[DetectedSpike]) -> list[str]:
    """The result table: rank, time, tier, score, messages, unique chatters, top tokens and window."""
    lines = [
        f"{'#':>3} {'time':8} {'tier':4} {'score':>6} {'msgs':>5} {'uniq':>5}  {'window':19}  top tokens"
    ]
    for rank, spike in enumerate(spikes, start=1):
        window = f"{clock(spike.window_start)}-{clock(spike.window_end)}"
        lines.append(
            f"{rank:>3} {clock(spike.peak_t):8} {spike.tier:4} {spike.score_z:>6.2f} {spike.msgs:>5} "
            f"{spike.uniq:>5}  {window:19}  {top_tokens(spike)}"
        )
    return lines


def format_details(rank: int, spike: DetectedSpike, sample: int) -> list[str]:
    """The feature breakdown of one spike and the first `sample` chat messages from the peak onwards."""
    parts = " ".join(f"{name}={value:+.2f}" for name, value in spike.parts.items())
    base = spike.baseline_mean
    lines = [
        f"[{rank}] {clock(spike.peak_t)} tier {spike.tier} score {spike.score_z:.2f} | {parts}",
        f"    msgs {spike.msgs} (baseline {base.get('msgs', 0):.1f}), uniq {spike.uniq} "
        f"(baseline {base.get('uniq', 0):.1f}), reactions {spike.reaction_count}, "
        f"clip requests {spike.clip_requests}, first bucket {clock(spike.first_t)}, "
        f"merged {len(spike.absorbed)}",
    ]
    shown = [entry for entry in spike.chat_sample if entry["t_rel"] >= 0][:sample]
    for entry in shown:
        text = " ".join(str(entry["text"]).split())[:_SAMPLE_LINE_CHARS]
        lines.append(f"    {entry['t_rel']:+5.1f}s {entry['user']}: {text}")
    return lines


def format_summary(path: Path, scan: ReplayScan, shown: int) -> str:
    """One line about the whole file."""
    tiers = Counter(spike.tier for spike in scan.spikes)
    per_tier = ", ".join(f"{tier}={tiers[tier]}" for tier in ("S", "A", "B"))
    return (
        f"{path.name}: {scan.counted_messages} counted messages, {scan.buckets} buckets, median "
        f"{scan.baseline_rate:g} msgs per bucket, {len(scan.bot_names)} bots; {len(scan.spikes)} spikes "
        f"({per_tier}), showing {shown}"
    )


def scan(
    chat_file: Path,
    *,
    top: int,
    details: bool,
    sample: int,
    settings_file: Path | None,
    set_pairs: Sequence[str],
    end_guard_s: float = 0.0,
) -> int:
    """Run `sixstars spikes scan`. Returns the process exit code."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    try:
        config = load_config(settings_file, parse_overrides(set_pairs))
        messages = load_chat(chat_file)
    except (ValueError, KeyError) as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    except OSError as exc:
        typer.echo(f"cannot read {exc.filename}: {exc.strerror}", err=True)
        return EXIT_ERROR
    result = analyse_replay(messages, config=config, end_guard_s=end_guard_s)
    best = ranked(result.spikes)[:top] if top > 0 else ranked(result.spikes)
    typer.echo(format_summary(chat_file, result, len(best)))
    for line in format_table(best):
        typer.echo(line)
    if details:
        for rank, spike in enumerate(best, start=1):
            typer.echo("")
            for line in format_details(rank, spike, sample):
                typer.echo(line)
    return 0
