"""The audio half of the render graph (docs/09 section 6): profanity bleep, music ducking, keyword pop
sound, loudness normalisation and the silent tail under the end card. Pure string building.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from sixstars.factory.bleep import BLEEP_FREQUENCY_HZ, BLEEP_GAIN_DB, Span
from sixstars.factory.escape import render_filter

# docs/09 sections 6 and 7 (fixed by the spec, not settings).
SAMPLE_RATE = 48000
LOUDNORM_TRUE_PEAK_DB = -1.5
LOUDNORM_LRA = 11.0
SFX_GAIN_DB = -12.0

_CHANNEL_LAYOUT = "stereo"


@dataclass(frozen=True, slots=True)
class LoudnormMeasurement:
    """First-pass loudnorm measurements, fed back into the second pass of a normal render."""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float


@dataclass(frozen=True, slots=True)
class SfxPlan:
    """Keyword pop sound: which ffmpeg input holds it and when (seconds from clip start) it plays."""

    input_index: int
    times_s: tuple[float, ...]


def _seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def spans_expression(spans: Sequence[Span]) -> str:
    """An ffmpeg expression that is non-zero inside any of the spans: `between(t,a,b)+between(t,c,d)`."""
    if not spans:
        raise ValueError("no spans")
    return "+".join(f"between(t,{_seconds(span.start_s)},{_seconds(span.end_s)})" for span in spans)


def bleep_filters(spans: Sequence[Span], duration_s: float, source: str, out: str) -> list[str]:
    """Filter chains that mute `source` over the spans and mix in a 1 kHz tone for the same spans.

    `source` and `out` are link labels without brackets. With no spans the audio is passed through.
    """
    if not spans:
        return [f"[{source}]anull[{out}]"]
    inside = spans_expression(spans)
    mute = render_filter("volume", [("volume", "0"), ("enable", inside)])
    tone = ",".join(
        [
            render_filter(
                "sine",
                [
                    ("frequency", str(BLEEP_FREQUENCY_HZ)),
                    ("sample_rate", str(SAMPLE_RATE)),
                    ("duration", _seconds(duration_s)),
                ],
            ),
            render_filter("aformat", [("sample_fmts", "fltp"), ("channel_layouts", _CHANNEL_LAYOUT)]),
            render_filter("volume", [("volume", f"{BLEEP_GAIN_DB:g}dB")]),
            render_filter("volume", [("volume", "0"), ("enable", f"not({inside})")]),
        ]
    )
    mix = render_filter("amix", [("inputs", "2"), ("duration", "first"), ("normalize", "0")])
    return [
        f"[{source}]{mute}[{out}_muted]",
        f"{tone}[{out}_tone]",
        f"[{out}_muted][{out}_tone]{mix}[{out}]",
    ]


def loudnorm_filter(
    target_lufs: float, measured: LoudnormMeasurement | None = None, *, report: bool = False
) -> str:
    """`loudnorm` for one-pass use, for the measuring pass (`report`) or for the second pass (`measured`)."""
    options = [
        ("I", f"{target_lufs:g}"),
        ("TP", f"{LOUDNORM_TRUE_PEAK_DB:g}"),
        ("LRA", f"{LOUDNORM_LRA:g}"),
    ]
    if measured is not None:
        options += [
            ("measured_I", f"{measured.input_i:g}"),
            ("measured_TP", f"{measured.input_tp:g}"),
            ("measured_LRA", f"{measured.input_lra:g}"),
            ("measured_thresh", f"{measured.input_thresh:g}"),
            ("offset", f"{measured.target_offset:g}"),
            ("linear", "true"),
        ]
    if report:
        options.append(("print_format", "json"))
    return render_filter("loudnorm", options)


def peak_limiter_filter() -> str:
    """A limiter at the loudnorm true-peak target. In its linear mode (second pass) loudnorm lets short
    peaks through, and the keyword pops are such peaks; this keeps the finished audio under the target."""
    limit = 10 ** (LOUDNORM_TRUE_PEAK_DB / 20)
    return render_filter(
        "alimiter",
        [("limit", f"{limit:.3f}"), ("attack", "5"), ("release", "50"), ("level", "false")],
    )


def parse_loudnorm_report(stderr: str) -> LoudnormMeasurement | None:
    """Reads the JSON block `loudnorm=print_format=json` prints. None when absent or not finite (silence)."""
    end = stderr.rfind("}")
    start = stderr.rfind("{", 0, end)
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
        values = [
            float(data[key]) for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        ]
    except (ValueError, KeyError, TypeError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return LoudnormMeasurement(*values)


def build_audio_filters(
    *,
    source: str | None,
    duration_s: float,
    end_card_s: float,
    target_lufs: float,
    bleeps: Sequence[Span] = (),
    duck_spans: Sequence[Span] = (),
    duck_db: float = 0.0,
    sfx: SfxPlan | None = None,
    measured: LoudnormMeasurement | None = None,
    report: bool = False,
    out: str = "aout",
) -> list[str]:
    """Filter chains from the source audio to the final `[out]` link.

    Order: resample to 48 kHz stereo and force the exact clip length, duck music spans, bleep, mix keyword
    pops, loudnorm, then append `end_card_s` of silence. `source` is the input link label without brackets
    (for example `0:a`), or None when the source has no audio (silence is generated instead).
    """
    length = _seconds(duration_s)
    fmt = render_filter("aformat", [("sample_fmts", "fltp"), ("channel_layouts", _CHANNEL_LAYOUT)])
    chains: list[str] = []
    if source is None:
        silence = render_filter(
            "anullsrc",
            [("sample_rate", str(SAMPLE_RATE)), ("channel_layout", _CHANNEL_LAYOUT), ("duration", length)],
        )
        chains.append(f"{silence},{fmt}[a_src]")
    else:
        prepare = [
            render_filter("aresample", [("out_sample_rate", str(SAMPLE_RATE))]),
            fmt,
            render_filter("apad", [("whole_dur", length)]),
            render_filter("atrim", [("duration", length)]),
            "asetpts=PTS-STARTPTS",
        ]
        if duck_spans:
            prepare.append(
                render_filter(
                    "volume", [("volume", f"{duck_db:g}dB"), ("enable", spans_expression(duck_spans))]
                )
            )
        chains.append(f"[{source}]{','.join(prepare)}[a_src]")
    chains += bleep_filters(bleeps, duration_s, "a_src", "a_bleep")
    mixed = "a_bleep"
    if sfx is not None and sfx.times_s:
        chains += _sfx_filters(sfx, duration_s, mixed, "a_sfx")
        mixed = "a_sfx"
    tail = [
        loudnorm_filter(target_lufs, measured, report=report),
        render_filter("aresample", [("out_sample_rate", str(SAMPLE_RATE))]),
        fmt,
        *([] if report else [peak_limiter_filter()]),
        render_filter("apad", [("whole_dur", length)]),
        render_filter("atrim", [("duration", length)]),
        "asetpts=PTS-STARTPTS",
    ]
    if end_card_s <= 0:
        chains.append(f"[{mixed}]{','.join(tail)}[{out}]")
        return chains
    chains.append(f"[{mixed}]{','.join(tail)}[a_main]")
    silence = render_filter(
        "anullsrc",
        [
            ("sample_rate", str(SAMPLE_RATE)),
            ("channel_layout", _CHANNEL_LAYOUT),
            ("duration", _seconds(end_card_s)),
        ],
    )
    chains.append(f"{silence},{fmt}[a_end]")
    chains.append(f"[a_main][a_end]{render_filter('concat', [('n', '2'), ('v', '0'), ('a', '1')])}[{out}]")
    return chains


def _sfx_filters(sfx: SfxPlan, duration_s: float, source: str, out: str) -> list[str]:
    times = [t for t in sfx.times_s if 0 <= t < duration_s]
    if not times:
        return [f"[{source}]anull[{out}]"]
    fmt = render_filter("aformat", [("sample_fmts", "fltp"), ("channel_layouts", _CHANNEL_LAYOUT)])
    resample = render_filter("aresample", [("out_sample_rate", str(SAMPLE_RATE))])
    gain = render_filter("volume", [("volume", f"{SFX_GAIN_DB:g}dB")])
    labels = [f"{out}_p{i}" for i in range(len(times))]
    chains = [
        f"[{sfx.input_index}:a]{resample},{fmt},{gain},"
        f"{render_filter('asplit', [('outputs', str(len(times)))])}"
        + "".join(f"[{label}_in]" for label in labels)
    ]
    for label, time_s in zip(labels, times, strict=True):
        delay = render_filter("adelay", [("delays", str(round(time_s * 1000))), ("all", "1")])
        chains.append(f"[{label}_in]{delay}[{label}]")
    mix = render_filter("amix", [("inputs", str(len(times) + 1)), ("duration", "first"), ("normalize", "0")])
    chains.append(f"[{source}]" + "".join(f"[{label}]" for label in labels) + f"{mix}[{out}]")
    return chains
