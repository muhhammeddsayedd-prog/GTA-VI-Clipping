"""Typed parameters for the clip factory. Field names map one-to-one to the `factory.*` settings keys in
`docs/16-settings-reference.md`; nothing in the factory reads the settings table directly.
"""

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Self

from sixstars.factory.escape import parse_hex_color

SETTINGS_PREFIX = "factory."


@dataclass(frozen=True, slots=True)
class FactoryParams:
    """Every `factory.*` setting. `FactoryParams.width` is the setting `factory.width`, and so on."""

    width: int
    height: int
    fps: int
    crf: int
    preset_normal: str
    preset_express: str
    use_nvenc: bool
    loudness_lufs: float
    safe_top_px: int
    safe_bottom_px: int
    accent_color: str
    font: str
    hook_seconds: float
    end_card_s: float
    watermark: str
    series_label: str
    bleep: bool
    music_duck_db: float

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.width % 2 or self.height % 2:
            raise ValueError("width and height must be positive even numbers")
        if self.fps <= 0:
            raise ValueError("fps must be positive")
        if not 0 <= self.crf <= 51:
            raise ValueError("crf must be within 0..51")
        if self.safe_top_px < 0 or self.safe_bottom_px < 0:
            raise ValueError("safe zone values must not be negative")
        if self.safe_top_px + self.safe_bottom_px >= self.height:
            raise ValueError("safe zone leaves no room for text")
        if self.hook_seconds < 0 or self.end_card_s < 0:
            raise ValueError("hook_seconds and end_card_s must not be negative")
        if not self.font.strip() or not self.preset_normal.strip() or not self.preset_express.strip():
            raise ValueError("font and presets must not be empty")
        parse_hex_color(self.accent_color)

    @classmethod
    def setting_keys(cls) -> tuple[str, ...]:
        """The settings keys this dataclass is filled from, e.g. `factory.width`."""
        return tuple(SETTINGS_PREFIX + field.name for field in dataclasses.fields(cls))

    @classmethod
    def from_settings(cls, values: Mapping[str, object]) -> Self:
        """Builds the parameters from a mapping of settings keys (`factory.width`, ...) to JSON values."""
        kwargs: dict[str, object] = {}
        for field in dataclasses.fields(cls):
            key = SETTINGS_PREFIX + field.name
            if key not in values:
                raise KeyError(f"missing setting {key}")
            kwargs[field.name] = _coerce(key, values[key], field.type)
        return cls(**kwargs)  # type: ignore[arg-type]


def _coerce(key: str, value: object, target: object) -> object:
    name = target if isinstance(target, str) else getattr(target, "__name__", str(target))
    if name == "bool":
        if isinstance(value, bool):
            return value
    elif name == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    elif name == "float":
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    elif name == "str" and isinstance(value, str):
        return value
    raise TypeError(f"setting {key} must be {name}, got {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class FontFiles:
    """Where ffmpeg finds the font named by `factory.font`.

    `fonts_dir` is passed to libass (`ass=fontsdir=`); `font_file` is passed to `drawtext=fontfile=`. With
    both unset, libass and drawtext look the font name up in the system fonts. Pure paths are accepted so
    a graph for another operating system can be built and inspected.
    """

    fonts_dir: PurePath | None = None
    font_file: PurePath | None = None
