"""Shared fixtures for the clip factory tests: the documented default settings, the lines of a short test
clip and the tools the real-render tests need (ffmpeg, a font file, a synthetic source video)."""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from sixstars.factory.lines import Line, clip_lines, group_lines, words_from_transcript
from sixstars.factory.params import FactoryParams
from sixstars.factory.window import ClipWindow

# Defaults from docs/16-settings-reference.md, section "Clip factory".
DEFAULT_SETTINGS: dict[str, object] = {
    "factory.width": 1080,
    "factory.height": 1920,
    "factory.fps": 30,
    "factory.crf": 21,
    "factory.preset_normal": "veryfast",
    "factory.preset_express": "superfast",
    "factory.use_nvenc": False,
    "factory.loudness_lufs": -14,
    "factory.safe_top_px": 220,
    "factory.safe_bottom_px": 420,
    "factory.accent_color": "#FFD400",
    "factory.font": "Montserrat Black",
    "factory.hook_seconds": 3.0,
    "factory.end_card_s": 1.5,
    "factory.watermark": "@6starsclips",
    "factory.series_label": "6 Stars Moments",
    "factory.bleep": True,
    "factory.music_duck_db": -18,
}

# A 10-second transcript: nine lines, one profanity ("fucking", 4.9 to 5.4 s).
TRANSCRIPT_WORDS: list[tuple[str, float, float]] = [
    ("He", 0.2, 0.4),
    ("did", 0.4, 0.6),
    ("NOT", 0.6, 1.0),
    ("expect", 1.0, 1.4),
    ("that!", 1.4, 1.8),
    ("No", 2.4, 2.6),
    ("way,", 2.6, 2.9),
    ("fifty", 3.0, 3.4),
    ("thousand", 3.4, 3.9),
    ("dollars", 3.9, 4.4),
    ("this", 4.7, 4.9),
    ("fucking", 4.9, 5.4),
    ("cop", 5.4, 5.7),
    ("just", 5.7, 5.9),
    ("crashed", 5.9, 6.4),
    ("into", 6.5, 6.7),
    ("the", 6.7, 6.8),
    ("helicopter", 6.8, 7.5),
    ("and", 7.9, 8.0),
    ("exploded", 8.0, 8.6),
    ("everywhere", 8.7, 9.4),
]
KEYWORDS: dict[int, list[str]] = {0: ["NOT"], 2: ["no way"], 6: ["helicopter"], 7: ["exploded"]}
PROFANITY_TERMS = frozenset({"fuck", "fucking", "shit", "ass", "son of a bitch"})

_FONT_CANDIDATES = (
    ("Arial Black", "C:/Windows/Fonts/ariblk.ttf"),
    ("Arial", "C:/Windows/Fonts/arialbd.ttf"),
    ("DejaVu Sans", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("Liberation Sans", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("Arial Black", "/System/Library/Fonts/Supplemental/Arial Black.ttf"),
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "ffmpeg: renders real video with ffmpeg (skipped when it is missing)")


def make_params(**overrides: object) -> FactoryParams:
    """`FactoryParams` with the documented defaults; keyword names are field names (`font="Arial"`)."""
    values = dict(DEFAULT_SETTINGS)
    values.update({f"factory.{name}": value for name, value in overrides.items()})
    return FactoryParams.from_settings(values)


@pytest.fixture
def params() -> FactoryParams:
    return make_params()


@pytest.fixture
def params_factory() -> Callable[..., FactoryParams]:
    return make_params


@pytest.fixture
def transcript() -> dict[str, object]:
    return {"language": "en", "words": [{"w": w, "s": s, "e": e} for w, s, e in TRANSCRIPT_WORDS]}


@pytest.fixture
def clip_lines_10s(transcript: dict[str, object]) -> list[Line]:
    return clip_lines(group_lines(words_from_transcript(transcript)), ClipWindow(0.0, 10.0))


@pytest.fixture
def keywords() -> dict[int, list[str]]:
    return {index: list(words) for index, words in KEYWORDS.items()}


@pytest.fixture
def profanity_terms() -> frozenset[str]:
    return PROFANITY_TERMS


@pytest.fixture(scope="session")
def ffmpeg_tools() -> tuple[str, str]:
    """Paths of ffmpeg and ffprobe; skips the test when either is missing."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    return ffmpeg, ffprobe


@pytest.fixture(scope="session")
def test_font() -> tuple[str, Path]:
    """A heavy font that exists on this machine: (family name for libass, file for drawtext)."""
    for name, location in _FONT_CANDIDATES:
        if Path(location).is_file():
            return name, Path(location)
    pytest.skip("no known test font installed")


@pytest.fixture(scope="session")
def synthetic_source(ffmpeg_tools: tuple[str, str], tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 10-second 1920x1080 30 fps test pattern with a 440 Hz tone, in a directory with a space."""
    path = tmp_path_factory.mktemp("factory source") / "source 10s.mp4"
    command = [
        ffmpeg_tools[0],
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:rate=30:duration=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=10",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path
