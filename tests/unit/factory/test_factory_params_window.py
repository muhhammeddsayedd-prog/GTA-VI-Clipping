"""FactoryParams (settings mapping) and the window / trim / timing maths."""

import dataclasses
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from sixstars.factory.params import FactoryParams
from sixstars.factory.window import (
    ClipWindow,
    frame_count,
    padded_window,
    resolve_trim,
    snap_duration,
    total_duration_s,
)

DOCS = Path(__file__).resolve().parents[3] / "docs" / "16-settings-reference.md"


def test_fields_match_the_documented_factory_settings_one_to_one() -> None:
    documented = re.findall(r"^\| `(factory\.[a-z_0-9]+)` \|", DOCS.read_text(encoding="utf-8"), flags=re.M)
    assert documented, "no factory.* rows found in docs/16"
    # `factory.cut_snap` is a rule of the cut, not of the look: `FactoryConfig.snap` reads it.
    documented.remove("factory.cut_snap")
    # `factory.style` is one JSON setting read by `FactoryConfig.style` (`StyleRules`), docs/27.
    documented.remove("factory.style")
    # The design styles (development-plan/M18): `FactoryConfig.catalog` and `FactoryConfig.director`.
    documented.remove("factory.styles")
    documented.remove("factory.style_director")
    # Story cuts (development-plan/M18 Phase C): `FactoryConfig.story`.
    documented.remove("factory.story")
    # The checks before the clip writer (AI cost plan A5a): read by `factory.precheck`.
    documented.remove("factory.pre_p2_checks")
    documented.remove("factory.pre_p2_throttle")
    documented.remove("factory.pre_p2_queue_min")
    assert list(FactoryParams.setting_keys()) == documented


def test_from_settings_reads_documented_defaults(params: FactoryParams) -> None:
    assert (params.width, params.height, params.fps, params.crf) == (1080, 1920, 30, 21)
    assert params.loudness_lufs == -14.0 and isinstance(params.loudness_lufs, float)
    assert params.accent_color == "#FFD400" and params.font == "Montserrat Black"
    assert params.bleep is True and params.use_nvenc is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        params.width = 720  # type: ignore[misc]


def test_from_settings_ignores_other_keys_and_requires_every_key(
    params_factory: Callable[..., FactoryParams],
) -> None:
    values = {
        key: getattr(params_factory(), key.removeprefix("factory.")) for key in FactoryParams.setting_keys()
    }
    assert FactoryParams.from_settings({**values, "gate.min_score": 6}) == params_factory()
    del values["factory.font"]
    with pytest.raises(KeyError, match="factory.font"):
        FactoryParams.from_settings(values)


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("width", "1080", TypeError),
        ("width", True, TypeError),
        ("fps", 29.97, TypeError),
        ("use_nvenc", 1, TypeError),
        ("bleep", "true", TypeError),
        ("font", 5, TypeError),
        ("loudness_lufs", "-14", TypeError),
        ("width", 1081, ValueError),
        ("height", 0, ValueError),
        ("fps", 0, ValueError),
        ("crf", 52, ValueError),
        ("accent_color", "yellow", ValueError),
        ("font", "  ", ValueError),
        ("safe_top_px", -1, ValueError),
        ("safe_bottom_px", 1800, ValueError),
        ("hook_seconds", -1.0, ValueError),
        ("end_card_s", -0.5, ValueError),
    ],
)
def test_bad_settings_are_rejected(
    params_factory: Callable[..., FactoryParams], name: str, value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        params_factory(**{name: value})


def test_whole_floats_are_accepted_for_integer_settings(params_factory: Callable[..., FactoryParams]) -> None:
    assert params_factory(fps=30.0).fps == 30
    assert params_factory(hook_seconds=3).hook_seconds == 3.0


@pytest.mark.parametrize(("start", "end"), [(-1.0, 5.0), (5.0, 5.0), (6.0, 5.0), (0.0, float("inf"))])
def test_invalid_windows_are_rejected(start: float, end: float) -> None:
    with pytest.raises(ValueError):
        ClipWindow(start, end)


@pytest.mark.parametrize(
    ("window", "pad", "file_duration", "expected"),
    [
        ((30.0, 60.0), 15.0, None, (15.0, 75.0)),
        ((5.0, 40.0), 15.0, None, (0.0, 55.0)),
        ((30.0, 60.0), 15.0, 70.0, (15.0, 70.0)),
        ((30.0, 60.0), 0.0, 100.0, (30.0, 60.0)),
    ],
)
def test_padded_window(
    window: tuple[float, float], pad: float, file_duration: float | None, expected: tuple[float, float]
) -> None:
    result = padded_window(ClipWindow(*window), pad, file_duration)
    assert (result.start_s, result.end_s) == expected


TRIM_CASES = [
    # (proposed, writer_start, writer_end, floor, min, max, file) -> expected
    ("writer silent keeps the proposal", (15, 45), None, None, 20, 15, 60, 75, (15, 45)),
    ("start moved earlier", (15, 45), 10, None, 20, 15, 60, 75, (10, 45)),
    ("end moved later", (15, 45), None, 52, 20, 15, 60, 75, (15, 52)),
    ("start never after the spike floor", (15, 45), 30, None, 20, 15, 60, 75, (20, 45)),
    ("negative start clamps to zero", (15, 45), -4, None, 20, 15, 60, 75, (0, 45)),
    ("end clamps to the file", (15, 45), None, 90, 20, 15, 60, 75, (15, 75)),
    ("too long loses its tail", (0, 45), None, 74, 20, 15, 60, 75, (0, 60)),
    ("too short is extended at the end", (15, 45), 18, 25, 20, 15, 60, 75, (18, 33)),
    ("too short at the file end moves the start back", (60, 75), 68, 75, 70, 15, 60, 75, (60, 75)),
    ("end before start is repaired to the minimum", (15, 45), 20, 10, 20, 15, 60, 75, (20, 35)),
    ("file shorter than the minimum gives the whole file", (0, 8), None, None, 4, 15, 60, 8, (0, 8)),
    ("floor beyond the file is clamped", (0, 8), 7, None, 99, 5, 60, 8, (3, 8)),
]


@pytest.mark.parametrize(
    ("proposed", "writer_start", "writer_end", "floor", "min_len", "max_len", "file_duration", "expected"),
    [case[1:] for case in TRIM_CASES],
    ids=[case[0] for case in TRIM_CASES],
)
def test_resolve_trim(
    proposed: tuple[float, float],
    writer_start: float | None,
    writer_end: float | None,
    floor: float,
    min_len: float,
    max_len: float,
    file_duration: float,
    expected: tuple[float, float],
) -> None:
    result = resolve_trim(
        ClipWindow(*proposed),
        writer_start_s=writer_start,
        writer_end_s=writer_end,
        spike_floor_s=floor,
        min_len_s=min_len,
        max_len_s=max_len,
        file_duration_s=file_duration,
    )
    assert (result.start_s, result.end_s) == expected
    assert result.start_s <= min(floor, file_duration)
    assert result.duration_s <= max_len


@pytest.mark.parametrize(("min_len", "max_len", "file_duration"), [(0, 60, 75), (30, 20, 75), (15, 60, 0)])
def test_resolve_trim_rejects_bad_bounds(min_len: float, max_len: float, file_duration: float) -> None:
    with pytest.raises(ValueError):
        resolve_trim(
            ClipWindow(0, 30),
            writer_start_s=None,
            writer_end_s=None,
            spike_floor_s=10,
            min_len_s=min_len,
            max_len_s=max_len,
            file_duration_s=file_duration,
        )


@pytest.mark.parametrize(
    ("duration", "fps", "frames", "snapped"),
    [
        (10.0, 30, 300, 10.0),
        (10.016, 30, 300, 10.0),
        (10.02, 30, 301, 301 / 30),
        (0.001, 30, 1, 1 / 30),
        (1.5, 60, 90, 1.5),
    ],
)
def test_frame_snapping(duration: float, fps: int, frames: int, snapped: float) -> None:
    assert frame_count(duration, fps) == frames
    assert snap_duration(duration, fps) == pytest.approx(snapped)


@pytest.mark.parametrize(
    ("clip", "end_card", "expected"),
    [(10.0, 1.5, 11.5), (36.0, 1.5, 37.5), (10.0, 0.0, 10.0), (10.016, 1.49, 11.5)],
)
def test_total_duration_adds_the_end_card(clip: float, end_card: float, expected: float) -> None:
    assert total_duration_s(clip, end_card, 30) == pytest.approx(expected)
