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

from sixstars.factory.escape import parse_hex_color

HookMode = Literal["box", "title_bar"]
Frame = Literal["auto", "headline_card", "pov", "montage"]
TitleSource = Literal["hook", "title_line", "headline", "question", "pov_line"]
TitleLook = Literal["bar", "question"]

_HOOK_MODES: tuple[HookMode, ...] = ("box", "title_bar")
_FRAMES: tuple[Frame, ...] = ("auto", "headline_card", "pov", "montage")
_TITLE_SOURCES: tuple[TitleSource, ...] = ("hook", "title_line", "headline", "question", "pov_line")
_TITLE_LOOKS: tuple[TitleLook, ...] = ("bar", "question")


MAX_FOLLOW_OVERLAY_S = 5.0  # the follow line covers at most the last 5 seconds


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
    * `frame`: `auto` draws layout A, B or C from the facecam and `facecam_top`; `headline_card` the
      news card (layout H: brand header, headline, the stream as the streamer had it); `pov` the
      gameplay alone in a tall crop with black bars and a centred line (layout P).
    * `title_source`: which of the clip writer's texts the title shows (the hook when that one is
      missing). `title_look`: `bar` (white bar) or `question` (big red capitals with a black outline
      at the top of the gameplay).
    * `watermark`: burn in `factory.watermark` (off for the headline card, whose header is the brand).
    * `caption_y`: the caption line's centre on the 1080x1920 canvas (0: the layout's own place).
    * `caption_active_color`: the spoken word of a caption chunk in this colour (empty: no highlight).
    * `show_title`: draw the title at all (off for the one-word caption style).
    * `frame` `montage` (Phase E, reference R10): gameplay above the facecam in a 3:4 letterbox with a
      soft fade between them; every other `montage_every_s` seconds the gameplay cuts to B-roll from
      the footage around the moment. `caption_tiers`: a chunk with a keyword shows its other words
      small above the keyword, which is big, italic and glowing in the accent colour.
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
    frame: Frame = "auto"
    title_source: TitleSource = "hook"
    title_look: TitleLook = "bar"
    watermark: bool = True
    caption_y: int = 0
    caption_active_color: str = ""
    show_title: bool = True
    montage_every_s: float = 0.0
    caption_tiers: bool = False
    # YouTube lane tuning C: a follow line over the last `follow_overlay_s` seconds of the clip (0: off). It
    # adds no time, so the clip still loops. The text names the handle, so a style that uses it is limited
    # to one account (`factory.styles` `accounts`).
    follow_overlay_s: float = 0.0
    follow_overlay_text: str = ""
    # D118 campaign looks (set by `sixstars.campaigns.profiles.apply`, never from factory.styles): a
    # disclosure line over the first `disclosure_s` seconds and the campaign's logo in a corner.
    disclosure_text: str = ""
    disclosure_s: float = 0.0
    campaign_logo: str = ""
    campaign_logo_pos: str = ""

    def __post_init__(self) -> None:
        if self.caption_words < 0 or self.caption_chars < 1:
            raise ValueError("caption_words must not be negative and caption_chars must be positive")
        if not 24 <= self.caption_size <= 200:
            raise ValueError("caption_size must be within 24..200")
        if self.hook_mode not in _HOOK_MODES:
            raise ValueError(f"hook_mode must be one of {_HOOK_MODES}")
        if (
            self.frame not in _FRAMES
            or self.title_source not in _TITLE_SOURCES
            or self.title_look not in _TITLE_LOOKS
        ):
            raise ValueError("frame, title_source or title_look is unknown")
        if self.caption_y and not 200 <= self.caption_y <= 1720:
            raise ValueError("caption_y must be 0 or within 200..1720")
        if self.montage_every_s < 0:
            raise ValueError("montage_every_s must not be negative")
        if not 0 <= self.follow_overlay_s <= MAX_FOLLOW_OVERLAY_S:
            raise ValueError(f"follow_overlay_s must be within 0..{MAX_FOLLOW_OVERLAY_S:g}")
        if self.caption_active_color:
            parse_hex_color(self.caption_active_color)
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
        frame = raw.get("frame")
        title_source = raw.get("title_source")
        title_look = raw.get("title_look")
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
                frame=frame if frame in _FRAMES else base.frame,
                title_source=title_source if title_source in _TITLE_SOURCES else base.title_source,
                title_look=title_look if title_look in _TITLE_LOOKS else base.title_look,
                watermark=flag("watermark", base.watermark),
                caption_y=integer("caption_y", base.caption_y),
                caption_active_color=text("caption_active_color", base.caption_active_color),
                show_title=flag("show_title", base.show_title),
                montage_every_s=number("montage_every_s", base.montage_every_s),
                caption_tiers=flag("caption_tiers", base.caption_tiers),
                follow_overlay_s=number("follow_overlay_s", base.follow_overlay_s),
                follow_overlay_text=text("follow_overlay_text", base.follow_overlay_text),
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
    "title_source": "title_line",
    "facecam_top": True,
    "punchin_max": 0,
    "zoomcut_every_s": 0,
    "cut_sfx": False,
    "payoff_fx": False,
    "compress": True,
    "sfx_gain_db": -10,
}
# Phase B (development-plan/M18): the calm styles of the references R05 (question), R08 (POV) and R09 (news
# card). None has spoken captions, punch-ins or zoom cuts; each shows one of the writer's texts for the
# whole clip.
_CALM: dict[str, object] = {
    "caption_words": 0,
    "captions": False,
    "hook_mode": "title_bar",
    "title_whole_clip": True,
    "punchin_max": 0,
    "zoomcut_every_s": 0,
    "cut_sfx": False,
    "payoff_fx": False,
    "compress": True,
    "sfx_gain_db": -10,
}
CAM_TOP_QUESTION: dict[str, object] = {
    **_CALM,
    "facecam_top": True,
    "title_source": "question",
    "title_look": "question",
}
HEADLINE_CARD: dict[str, object] = {
    **_CALM,
    "frame": "headline_card",
    "title_source": "headline",
    "watermark": False,
}
POV_FULLFRAME: dict[str, object] = {**_CALM, "frame": "pov", "title_source": "pov_line"}
# Phase C (development-plan/M18): the one-word captions of reference R06: one or two heavy words at a time on
# the bottom edge of the facecam, the spoken one in yellow; no title, calm cut. Suits the story cuts.
CAM_TOP_WORDCAP: dict[str, object] = {
    **_CALM,
    "facecam_top": True,
    "captions": True,
    "caption_words": 2,
    "caption_chars": 16,
    "caption_font": "Montserrat Black",
    "caption_size": 92,
    "caption_caps": True,
    "caption_y": 580,
    "caption_active_color": "#FFD400",
    "show_title": False,
}
# Phase E (development-plan/M18): the hype montage of reference R10. Gameplay over the streamer's face in a
# 3:4 letterbox, cuts to B-roll every other 3 s, two-tier kinetic words on the seam, a whoosh on each cut and
# the payoff emoji; no title. Needs a facecam and spare footage around the moment (`requires`).
HYPE_MONTAGE: dict[str, object] = {
    "frame": "montage",
    "montage_every_s": 3.0,
    "captions": True,
    "caption_tiers": True,
    "caption_words": 2,
    "caption_chars": 16,
    "caption_font": "Anton",
    "caption_size": 120,
    "caption_caps": True,
    "show_title": False,
    "hook_mode": "title_bar",
    "punchin_max": 0,
    "zoomcut_every_s": 0,
    "cut_sfx": True,
    "payoff_fx": True,
    "compress": True,
    "sfx_gain_db": -12,
}
