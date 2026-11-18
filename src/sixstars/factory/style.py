"""The editing style of a clip: the setting `factory.style` (docs/09 section 10, docs/27).

The look is modelled on short-form clips that reached millions of views (docs/27): captions of one or two
words in heavy capitals that pop in on the seam, the hook as a title bar over the first seconds,
hard punch-ins to a full-frame facecam on the loudest reactions, a step zoom on the gameplay every few
seconds, sound effects on every cut and on the payoff, and dense compressed audio.

A missing or malformed setting gives `StyleRules.classic()`, the look of the clip before this setting
existed, so a database without migration 0021 renders exactly as before.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

HookMode = Literal["box", "title_bar"]

_HOOK_MODES: tuple[HookMode, ...] = ("box", "title_bar")


@dataclass(frozen=True, slots=True)
class StyleRules:
    """The setting `factory.style`.

    * `caption_words` / `caption_chars`: the most words and characters shown at once (0 words: whole
      subtitle lines with the spoken word highlighted, the classic look).
    * `caption_font` / `caption_size` / `caption_caps`: caption font family (empty: `factory.font`), size
      on the 1080x1920 canvas, and whether captions are upper case.
    * `hook_mode`: `box` draws the hook as a subtitle box, `title_bar` as a TikTok-style bar; both
      show it for the first `factory.hook_seconds`.
    * `punchin_max` / `punchin_zoom` / `punchin_len_s` / `punchin_min_gap_s` / `punchin_db`: up to
      `punchin_max` hard cuts to a full-frame close-up (the facecam in layout A, the centre of the source in
      layout B), `punchin_len_s` long, where the audio is `punchin_db` dB louder than the clip's median, at
      least `punchin_min_gap_s` apart. 0 switches punch-ins off.
    * `zoomcut_every_s` / `zoomcut_scale`: the gameplay alternates between 100% and `zoomcut_scale` every
      `zoomcut_every_s` seconds (0: the classic slow zoom over the last 40% of the clip).
    * `sfx_gain_db`: level of every sound effect. `cut_sfx`: whoosh and impact on each punch-in.
      `payoff_fx`: emoji and bass hit at the payoff the clip writer names.
    * `compress`: a compressor before loudness normalisation, for the dense sound of the references.
    * `facecam_top`: with a facecam, draw it on top and the gameplay below (layout C, decision D112)
      instead of gameplay on top (layout A).
    * `captions`: burn in the spoken words (off: a title-only style, docs/27 section 5).
    * `title_whole_clip`: the title bar stays for the whole clip instead of `factory.hook_seconds`.
    """

    caption_words: int = 0
    caption_chars: int = 22
    caption_font: str = ""
    caption_size: int = 78
    caption_caps: bool = False
    hook_mode: HookMode = "box"
    punchin_max: int = 0
    punchin_zoom: float = 1.0
    punchin_len_s: float = 1.2
    punchin_min_gap_s: float = 2.5
    punchin_db: float = 8.0
    zoomcut_every_s: float = 0.0
    zoomcut_scale: float = 1.0
    sfx_gain_db: float = -12.0
    cut_sfx: bool = False
    payoff_fx: bool = False
    compress: bool = False
    facecam_top: bool = False
    captions: bool = True
    title_whole_clip: bool = False

    def __post_init__(self) -> None:
        if self.caption_words < 0 or self.caption_chars < 1:
            raise ValueError("caption_words must not be negative and caption_chars must be positive")
        if not 24 <= self.caption_size <= 200:
            raise ValueError("caption_size must be within 24..200")
        if self.hook_mode not in _HOOK_MODES:
            raise ValueError(f"hook_mode must be one of {_HOOK_MODES}")
        if self.punchin_max < 0 or self.punchin_len_s <= 0 or self.punchin_min_gap_s < 0:
            raise ValueError("punch-in counts and lengths must not be negative")
        if not 1.0 <= self.punchin_zoom <= 3.0 or not 1.0 <= self.zoomcut_scale <= 1.5:
            raise ValueError("zoom factors are out of range")
        if self.zoomcut_every_s < 0:
            raise ValueError("zoomcut_every_s must not be negative")

    @classmethod
    def classic(cls) -> "StyleRules":
        """The look before `factory.style` existed."""
        return cls()

    @property
    def chunked(self) -> bool:
        """Captions in short chunks instead of whole highlighted lines."""
        return self.caption_words > 0

    @property
    def punchins(self) -> bool:
        return self.punchin_max > 0 and self.punchin_zoom >= 1.0

    @property
    def zoomcuts(self) -> bool:
        return self.zoomcut_every_s > 0 and self.zoomcut_scale > 1.0

    @classmethod
    def from_setting(cls, raw: object) -> "StyleRules":
        """Build the rules from the JSON object of the setting. A missing or malformed value (or one that
        fails validation) gives the classic look; a missing key keeps its classic value."""
        if not isinstance(raw, Mapping):
            return cls.classic()
        base = cls.classic()

        def number(key: str, fallback: float) -> float:
            value = raw.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                return float(value)
            return fallback

        def integer(key: str, fallback: int) -> int:
            value = number(key, float(fallback))
            return int(value) if value.is_integer() else fallback

        def flag(key: str, fallback: bool) -> bool:
            value = raw.get(key)
            return value if isinstance(value, bool) else fallback

        def text(key: str, fallback: str) -> str:
            value = raw.get(key)
            return " ".join(value.split()) if isinstance(value, str) else fallback

        hook_mode = raw.get("hook_mode")
        try:
            return cls(
                caption_words=integer("caption_words", base.caption_words),
                caption_chars=integer("caption_chars", base.caption_chars),
                caption_font=text("caption_font", base.caption_font),
                caption_size=integer("caption_size", base.caption_size),
                caption_caps=flag("caption_caps", base.caption_caps),
                hook_mode=hook_mode if hook_mode in _HOOK_MODES else base.hook_mode,
                punchin_max=integer("punchin_max", base.punchin_max),
                punchin_zoom=number("punchin_zoom", base.punchin_zoom),
                punchin_len_s=number("punchin_len_s", base.punchin_len_s),
                punchin_min_gap_s=number("punchin_min_gap_s", base.punchin_min_gap_s),
                punchin_db=number("punchin_db", base.punchin_db),
                zoomcut_every_s=number("zoomcut_every_s", base.zoomcut_every_s),
                zoomcut_scale=number("zoomcut_scale", base.zoomcut_scale),
                sfx_gain_db=number("sfx_gain_db", base.sfx_gain_db),
                cut_sfx=flag("cut_sfx", base.cut_sfx),
                payoff_fx=flag("payoff_fx", base.payoff_fx),
                compress=flag("compress", base.compress),
                facecam_top=flag("facecam_top", base.facecam_top),
                captions=flag("captions", base.captions),
                title_whole_clip=flag("title_whole_clip", base.title_whole_clip),
            )
        except ValueError:
            return base


# The look of migration 0021 (docs/27). `config.OFFLINE_DEFAULTS` and the tests use it as the reference.
VIRAL_STYLE: dict[str, object] = {
    "caption_words": 2,
    "caption_chars": 14,
    "caption_font": "Anton",
    "caption_size": 150,
    "caption_caps": True,
    "hook_mode": "title_bar",
    "punchin_max": 4,
    "punchin_zoom": 1.25,
    "punchin_len_s": 1.2,
    "punchin_min_gap_s": 2.5,
    "punchin_db": 5,
    "zoomcut_every_s": 2.5,
    "zoomcut_scale": 1.08,
    "sfx_gain_db": -10,
    "cut_sfx": True,
    "payoff_fx": True,
    "compress": True,
}


# The styles of migration 0027 (development-plan/M18, docs/27 section 5). `config.OFFLINE_DEFAULTS`, the
# catalogue's fallback and the tests use them as the reference; the services read `factory.styles`.
CAM_TOP_VIRAL: dict[str, object] = {**VIRAL_STYLE, "facecam_top": True}
CAM_TOP_TITLE: dict[str, object] = {
    "caption_words": 0,
    "captions": False,
    "hook_mode": "title_bar",
    "title_whole_clip": True,
    "facecam_top": True,
    "punchin_max": 0,
    "zoomcut_every_s": 0,
    "cut_sfx": False,
    "payoff_fx": False,
    "compress": True,
    "sfx_gain_db": -10,
}
