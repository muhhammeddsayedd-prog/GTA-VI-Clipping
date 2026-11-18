"""Where the factory finds its bundled files: fonts, the keyword pop sound and the profanity list.

The files live in the repository's `assets/` directory (copied to `/app/assets` in the Docker image). The
directory is the one named by `SIXSTARS_ASSETS_DIR`, else `assets/` next to `src/`, else `assets/` in the
working directory.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from sixstars.factory.bleep import ProfanityMatcher, load_profanity_list
from sixstars.factory.params import FontFiles

ASSETS_ENV = "SIXSTARS_ASSETS_DIR"
# Bundled font files by the family name used in `factory.font` (lower case).
FONT_FILES = {
    "montserrat black": "Montserrat-Black.ttf",
    "anton": "Anton-Regular.ttf",
    "tiktok sans": "TikTokSans-Variable.ttf",
}
# docs/27 title bar: TikTok Sans for the text; colour emoji from a system font (the Docker image installs
# Debian's fonts-noto-color-emoji; Windows has Segoe UI Emoji), else the bundled monochrome Noto Emoji.
TITLE_FONT = "tiktok sans"
COLOUR_EMOJI_FONTS = (
    Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
    Path("C:/Windows/Fonts/seguiemj.ttf"),
    Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
)
MONO_EMOJI_FONT = "NotoEmoji.ttf"
SFX_POP = "pop.wav"
# docs/27 edit sound effects by kind (`effects.SfxKind`); the pop is `SFX_POP`.
SFX_FILES = {"whoosh": "whoosh.wav", "impact": "impact.wav", "boom": "boom.wav"}
LOGO = "star.png"
PROFANITY_LIST = "profanity.txt"


def assets_dir() -> Path:
    """The assets directory (it may not exist: callers check the files they need)."""
    override = os.environ.get(ASSETS_ENV)
    if override:
        return Path(override)
    bundled = Path(__file__).resolve().parents[3] / "assets"
    return bundled if bundled.is_dir() else Path.cwd() / "assets"


@dataclass(frozen=True, slots=True)
class FactoryAssets:
    """Resolved asset paths. A missing optional file is None, so the render simply goes without it."""

    root: Path
    fonts: FontFiles
    font_file: Path | None
    sfx_pop: Path | None
    logo: Path | None
    profanity_path: Path | None
    sfx: Mapping[str, Path] = field(default_factory=dict)
    caption_font_file: Path | None = None
    title_font_file: Path | None = None
    emoji_font_file: Path | None = None
    emoji_colour: bool = False

    def profanity(self) -> ProfanityMatcher:
        """The profanity matcher (empty when the list file is missing)."""
        if self.profanity_path is None:
            return ProfanityMatcher(frozenset())
        return _matcher(self.profanity_path)


@lru_cache(maxsize=4)
def _matcher(path: Path) -> ProfanityMatcher:
    return ProfanityMatcher(load_profanity_list(path))


def _bundled_font(fonts_dir: Path, family: str) -> Path | None:
    file_name = FONT_FILES.get(" ".join(family.lower().split()))
    path = fonts_dir / file_name if file_name else None
    return path if path is not None and path.is_file() else None


def load_assets(font_name: str, root: Path | None = None, *, caption_font: str = "") -> FactoryAssets:
    """Resolve the assets for the font named by `factory.font` (and the caption font of `factory.style`,
    which is measured with its own file when it is bundled).

    The bundled fonts directory is always given to libass when it exists. `drawtext` gets the font file
    when the font is one of the bundled ones; any other font name is looked up in the system fonts.
    """
    base = root or assets_dir()
    fonts_dir = base / "fonts"
    font_file = _bundled_font(fonts_dir, font_name)
    caption_file = _bundled_font(fonts_dir, caption_font) if caption_font else font_file
    effects = {kind: base / "sfx" / name for kind, name in SFX_FILES.items()}
    colour = next((path for path in COLOUR_EMOJI_FONTS if path.is_file()), None)
    mono = fonts_dir / MONO_EMOJI_FONT
    sfx = base / "sfx" / SFX_POP
    profanity = base / PROFANITY_LIST
    logo = base / "brand" / LOGO
    return FactoryAssets(
        root=base,
        fonts=FontFiles(fonts_dir=fonts_dir if fonts_dir.is_dir() else None, font_file=font_file),
        font_file=font_file,
        sfx_pop=sfx if sfx.is_file() else None,
        logo=logo if logo.is_file() else None,
        profanity_path=profanity if profanity.is_file() else None,
        sfx={kind: path for kind, path in effects.items() if path.is_file()},
        caption_font_file=caption_file,
        title_font_file=_bundled_font(fonts_dir, TITLE_FONT),
        emoji_font_file=colour or (mono if mono.is_file() else None),
        emoji_colour=colour is not None,
    )
