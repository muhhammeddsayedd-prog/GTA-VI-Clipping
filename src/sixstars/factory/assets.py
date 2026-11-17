"""Where the factory finds its bundled files: fonts, the keyword pop sound and the profanity list.

The files live in the repository's `assets/` directory (copied to `/app/assets` in the Docker image). The
directory is the one named by `SIXSTARS_ASSETS_DIR`, else `assets/` next to `src/`, else `assets/` in the
working directory.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sixstars.factory.bleep import ProfanityMatcher, load_profanity_list
from sixstars.factory.params import FontFiles

ASSETS_ENV = "SIXSTARS_ASSETS_DIR"
# Bundled font files by the family name used in `factory.font` (lower case).
FONT_FILES = {"montserrat black": "Montserrat-Black.ttf"}
SFX_POP = "pop.wav"
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

    def profanity(self) -> ProfanityMatcher:
        """The profanity matcher (empty when the list file is missing)."""
        if self.profanity_path is None:
            return ProfanityMatcher(frozenset())
        return _matcher(self.profanity_path)


@lru_cache(maxsize=4)
def _matcher(path: Path) -> ProfanityMatcher:
    return ProfanityMatcher(load_profanity_list(path))


def load_assets(font_name: str, root: Path | None = None) -> FactoryAssets:
    """Resolve the assets for the font named by `factory.font`.

    The bundled fonts directory is always given to libass when it exists. `drawtext` gets the font file
    when the font is one of the bundled ones; any other font name is looked up in the system fonts.
    """
    base = root or assets_dir()
    fonts_dir = base / "fonts"
    file_name = FONT_FILES.get(" ".join(font_name.lower().split()))
    font_file = fonts_dir / file_name if file_name else None
    if font_file is not None and not font_file.is_file():
        font_file = None
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
    )
