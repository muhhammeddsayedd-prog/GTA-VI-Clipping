"""YouTube's own caption track as a transcript, fetched with yt-dlp without downloading any media.
Implemented in M08.

A caption track of a five-hour stream is a few MB and arrives in seconds, with a time for every word. The
lane uses it twice: to show the confirmation prompt what was said around a chat spike, and as the
transcript of the fallback for videos without a usable chat. By default the track in the language spoken
is taken (`.*-orig`: automatic captions, any language; the prompts read it as it is), else an English
track. Tracks are fetched in that order because YouTube often refuses machine-translated tracks with
"429 Too Many Requests", which ends the yt-dlp run; what was written before that is still used. A video
without captions gives None.

The track is asked for in YouTube's `json3` format: a list of events, each with a start time and words
(`segs`) carrying their own offset. Creator-written captions have one text per event without word times;
their words are spread evenly over the event.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sixstars.ai.calls import TranscriptChunk
from sixstars.logging import get_logger
from sixstars.vod import ytdlp
from sixstars.vod.ytdlp import Access, Runner, YtDlpError

FORMAT: Final = "json3"
# yt-dlp names the automatic track in the spoken language `<code>-orig`.
ORIGINAL_SUFFIX: Final = "-orig"
# A caption word is shown for at most this long when nothing says where it ends.
MAX_WORD_S: Final = 1.5


@dataclass(frozen=True, slots=True)
class Word:
    """One word with its position in the video (seconds from the start)."""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class Track:
    """A transcript of (part of) a video. `source` says where it came from: `captions`, `local` or
    `hosted`."""

    language: str
    words: tuple[Word, ...]
    source: str = "captions"

    @property
    def end_s(self) -> float:
        return self.words[-1].end_s if self.words else 0.0


def _event_words(event: Mapping[str, Any]) -> list[tuple[str, float]]:
    """`(word, start in seconds)` for one json3 event."""
    start_ms = event.get("tStartMs")
    segs = event.get("segs")
    if not isinstance(start_ms, int | float) or not isinstance(segs, list):
        return []
    duration_ms = event.get("dDurationMs")
    timed = any(isinstance(seg, Mapping) and "tOffsetMs" in seg for seg in segs)
    found: list[tuple[str, float]] = []
    for seg in segs:
        if not isinstance(seg, Mapping):
            continue
        offset_ms = seg.get("tOffsetMs", 0)
        base = (float(start_ms) + (float(offset_ms) if isinstance(offset_ms, int | float) else 0.0)) / 1000.0
        found.extend((piece, base) for piece in str(seg.get("utf8", "")).split())
    if not timed and len(found) > 1 and isinstance(duration_ms, int | float) and duration_ms > 0:
        step = float(duration_ms) / 1000.0 / len(found)
        found = [(text, float(start_ms) / 1000.0 + step * index) for index, (text, _) in enumerate(found)]
    return found


def parse_json3(data: Mapping[str, Any]) -> list[Word]:
    """The words of a json3 caption file in time order. A word ends where the next one starts, at most
    `MAX_WORD_S` later. Pure."""
    events = data.get("events")
    if not isinstance(events, list):
        return []
    starts: list[tuple[str, float]] = []
    for event in events:
        if isinstance(event, Mapping):
            starts.extend(_event_words(event))
    starts.sort(key=lambda item: item[1])
    words: list[Word] = []
    for index, (text, start) in enumerate(starts):
        following = starts[index + 1][1] if index + 1 < len(starts) else start + MAX_WORD_S
        words.append(Word(text, start, max(start, min(following, start + MAX_WORD_S))))
    return words


def chunk_words(words: Sequence[Word], chunk_s: float) -> list[TranscriptChunk]:
    """The transcript in blocks of `chunk_s` seconds on the video's clock (a word belongs to the block it
    starts in). Blocks without words are left out. Pure."""
    if chunk_s <= 0:
        raise ValueError("chunk_s must be greater than 0")
    blocks: dict[int, list[Word]] = {}
    for word in words:
        blocks.setdefault(int(word.start_s // chunk_s), []).append(word)
    return [
        TranscriptChunk(index * chunk_s, (index + 1) * chunk_s, " ".join(word.text for word in block))
        for index, block in sorted(blocks.items())
    ]


def text_between(words: Sequence[Word], start_s: float, end_s: float, max_chars: int = 0) -> str:
    """What was said from `start_s` to `end_s`; with `max_chars`, cut at a word boundary. Pure."""
    text = " ".join(word.text for word in words if start_s <= word.start_s < end_s)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " …"
    return text


def build_arguments(url: str, out_dir: Path, name: str, langs: Sequence[str]) -> list[str]:
    """The yt-dlp arguments that write the caption tracks of `langs` (no media) as `name.<lang>.json3`."""
    return [
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        ",".join(langs),
        "--sub-format",
        FORMAT,
        "--output",
        str(out_dir / f"{name}.%(ext)s"),
        url,
    ]


def read_track(out_dir: Path, name: str, langs: Sequence[str]) -> Track | None:
    """The first caption file that exists and has words, in the order of `langs`. An entry of `langs` is
    a language code or a pattern for one (`.*-orig` is the automatic track in the language spoken)."""
    prefix, suffix = f"{name}.", f".{FORMAT}"
    found = {
        path.name[len(prefix) : -len(suffix)]: path
        for path in sorted(out_dir.glob(f"{name}.*{suffix}"))
        if path.is_file()
    }
    for wanted in langs:
        for lang, path in found.items():
            if not re.fullmatch(wanted, lang):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            words = parse_json3(data) if isinstance(data, Mapping) else []
            if words:
                return Track(language=lang.removesuffix(ORIGINAL_SUFFIX), words=tuple(words))
    return None


def fetch(
    url: str,
    out_dir: Path,
    *,
    access: Access,
    langs: Sequence[str],
    timeout_s: float,
    name: str = "captions",
    runner: Runner | None = None,
) -> Track | None:
    """Download the caption track of a video; None when it has none in `langs`.

    Raises `ytdlp.DownloadBlockedError` when the platform refuses this machine, `ytdlp.VideoUnavailableError`
    when the video is gone, `ytdlp.YtDlpError` otherwise. The files are left in `out_dir` (scratch).
    """
    if not langs:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        ytdlp.run(
            build_arguments(url, out_dir, name, langs),
            access=access,
            timeout_s=timeout_s,
            what="captions",
            runner=runner,
        )
    except YtDlpError as exc:
        # The video itself answered, only a caption file did not (YouTube often rate-limits these):
        # that says nothing about video downloads, so it is "no captions", not a block.
        if "subtitles" not in ytdlp.normalise(exc.detail):
            raise
    track = read_track(out_dir, name, langs)
    get_logger("vod").info(
        "captions_fetched",
        found=track is not None,
        language=track.language if track else None,
        words=len(track.words) if track else 0,
    )
    return track
