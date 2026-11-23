"""Stage 5 of the cover engine (docs/29a section 7): the QA gate. Cheap rule checks first, then one vision
check; a cover posts only when both pass.

Rule checks (`rule_problems`):
- ocr: the cover shrunk to 15% (162 px wide, the grid size) must read back as the line (similarity >= 0.85);
- text_outside_safe_band: the line (or the TikTok box) lies inside x 60-960, y 400-1520;
- foreign_text: no letters outside the Latin alphabet anywhere on the cover (a localised "WASTED" screen,
  chat in another script: the audience is English-only);
- face: a face in the safe band, its height in the layout's range;
- exposure: the pictures (not the type) have a mean luma of 70-200 in the safe band and < 2% clipped pixels;
- duplicate: perceptual hash at least 12 bits from each of the account's last 9 covers, and the same layout +
  line style not three times in a row;
- the line's banned words, story reveals and outcome words again (`hookline.text_problems`).

Vision check: P13 (`ai.calls.review_cover`) scores readable, faithful, natural, clear and safe (0-2 each);
pass = total >= `cover.qa_pass_score` and no hard fail (faithful or safe at 0).
"""

import difflib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sixstars.covers import hookline, ocr

OCR_SCALE = 0.15
OCR_MATCH = 0.85
SAFE_BAND = (60, 400, 960, 1520)  # x0, y0, x1, y1 on the 1080x1920 canvas
# Defaults of `cover.exposure` (the setting wins; see covers/config.py).
LUMA_RANGE = (60.0, 200.0)
CLIPPED_MAX = 0.10
PHASH_MIN_DISTANCE = 12
RECENT_COVERS = 9
# Face height on the cover (px) per layout: split = 35-75% of the 620 px panel, face-led = 40-55% of the
# 1120 px safe band (with a little tolerance), moment-led = the inset (300-380 px tall).
FACE_RANGE_PX: dict[str, tuple[float, float]] = {
    "split": (0.35 * 620, 0.75 * 620),
    "tiktok_native": (0.35 * 620, 0.75 * 620),
    "face_led": (0.36 * 1120, 0.60 * 1120),
    "moment_led": (90.0, 300.0),
}

# Where the face is looked for on the cover (the detector misses a face that is a small part of a tall
# image): the facecam panel, the whole face-led canvas, the moment-led inset.
FACE_REGION: dict[str, tuple[int, int, int, int]] = {
    "split": (0, 380, 1080, 1000),
    "tiktok_native": (0, 380, 1080, 1000),
    "face_led": (0, 0, 1080, 1920),
    "moment_led": (0, 1100, 540, 1560),
}


# ── Pure helpers ──────────────────────────────────────────────


def similarity(a: str, b: str) -> float:
    """Text similarity (0-1) with case, spaces and punctuation ignored. Pure."""
    left, right = ocr.normalise(a), ocr.normalise(b)
    if not left and not right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def has_foreign_script(text: str) -> bool:
    """Any letter that is not Latin (Cyrillic, Greek, CJK, Arabic...). Pure."""
    return any(ch.isalpha() and not unicodedata.name(ch, "").startswith("LATIN") for ch in text)


def inside(rect: Sequence[int], band: tuple[int, int, int, int] = SAFE_BAND) -> bool:
    """Is the (x, y, w, h) rectangle inside the band? Pure."""
    x, y, w, h = rect
    x0, y0, x1, y1 = band
    return x >= x0 and y >= y0 and x + w <= x1 and y + h <= y1


def phash_bits(gray32: Any) -> int:
    """The 64-bit perceptual hash of a 32x32 grey image (DCT, top-left 8x8 against its median)."""
    import cv2
    import numpy as np

    dct = cv2.dct(np.float32(gray32))
    block = dct[:8, :8].flatten()
    median = float(np.median(block[1:]))
    bits = 0
    for value in block:
        bits = (bits << 1) | int(value > median)
    return bits


def hamming(a: int, b: int) -> int:
    """Differing bits between two hashes. Pure."""
    return bin(a ^ b).count("1")


def three_in_a_row(recent: Sequence[tuple[str | None, str | None]], new: tuple[str, str]) -> bool:
    """Would `new` (layout, line style) be the third same one in a row (recent = newest first)? Pure."""
    return len(recent) >= 2 and all(item == new for item in recent[:2])


def exposure(
    luma: float, clipped: float, limits: tuple[float, float, float] = (*LUMA_RANGE, CLIPPED_MAX)
) -> list[str]:
    """Exposure problems of the pictures in the safe band; `limits` = (luma min, luma max, clipped max),
    from `cover.exposure`. Pure."""
    low, high, clipped_max = limits
    problems = []
    if not low <= luma <= high:
        problems.append(f"exposure:luma={round(luma)}")
    if clipped >= clipped_max:
        problems.append(f"exposure:clipped={clipped:.3f}")
    return problems


# ── Image measurements ────────────────────────────────────────


def phash_of(path: Path) -> int:
    """pHash of an image file."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        gray = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
        return phash_bits(np.asarray(gray, dtype=np.float32))


def _ocr_text(engine: Any, image: Any) -> str:
    import numpy as np

    result = engine(np.ascontiguousarray(np.asarray(image.convert("RGB"))[:, :, ::-1]))
    return " ".join(str(t) for t in (getattr(result, "txts", None) or ()))


def _parts_exposure(parts_dir: Path, layout: str) -> tuple[float, float] | None:
    """Mean luma and clipped share of the composed pictures as they sit in the safe band (no type)."""
    import numpy as np
    from PIL import Image

    canvas = Image.new("L", (1080, 1920), 0)
    placed = False
    positions = {
        "face": (0, 380) if layout in ("split", "tiktok_native") else (0, 0),
        "game": (0, 1000) if layout in ("split", "tiktok_native") else (0, 0),
    }
    for name, (x, y) in positions.items():
        path = parts_dir / f"{name}.png"
        if (
            path.is_file()
            and not (layout == "face_led" and name == "game")
            and not (layout == "moment_led" and name == "face")
        ):
            with Image.open(path) as part:
                canvas.paste(part.convert("L"), (x, y))
                placed = True
    if not placed:
        return None
    x0, y0, x1, y1 = SAFE_BAND
    band = np.asarray(canvas.crop((x0, y0, x1, y1)), dtype=np.uint8)
    return float(band.mean()), float(np.mean((band <= 5) | (band >= 250)))


def _face_on_cover(rgb: Any, layout: str, parts_dir: Path | None, landmarker: Any) -> Any:
    """The face as placed on the cover, in canvas pixels: found on the composed face picture before the type
    goes on (a line across the chin must not hide it), else in the layout's face region of the cover."""
    import numpy as np
    from PIL import Image

    placed = {
        "split": ("face.png", 0, 380, 1.0),
        "tiktok_native": ("face.png", 0, 380, 1.0),
        "face_led": ("face.png", 0, 0, 1.0),
        "moment_led": ("cutout.png", 64, 1920 - 400 - 340, 340 / 600),
    }.get(layout)
    if parts_dir is not None and placed is not None and (parts_dir / placed[0]).is_file():
        name, x0, y0, scale = placed
        with Image.open(parts_dir / name) as part:
            face = landmarker.face(np.asarray(part.convert("RGB")))
        if face.found and face.box is not None:
            fx, fy, fw, fh = face.box
            box = (round(x0 + fx * scale), round(y0 + fy * scale), round(fw * scale), round(fh * scale))
            return replace(face, box=box)
        return face
    region = FACE_REGION.get(layout, (0, 0, rgb.width, rgb.height))
    face = landmarker.face(np.asarray(rgb.crop(region)))
    if face.found and face.box is not None:
        fx, fy, fw, fh = face.box
        face = replace(face, box=(fx + region[0], fy + region[1], fw, fh))
    return face


@dataclass(frozen=True, slots=True)
class CoverFacts:
    """What the rules look at for one composed cover."""

    layout: str
    line: str  # "" for a plain cover
    line_style: str
    cover_path: Path
    preview_path: Path
    parts_dir: Path | None
    text_rect: Sequence[int] | None
    face_h_px: float | None
    face_rect: Sequence[int] | None
    small_text: str = ""
    full_text: str = ""
    luma: float | None = None
    clipped: float | None = None
    phash: int | None = None
    face_source: str = "none"


def measure(
    cover_path: Path,
    preview_path: Path,
    *,
    layout: str,
    line: str,
    line_style: str,
    text_rect: Sequence[int] | None,
    parts_dir: Path | None,
    engine: Any,
    landmarker: Any,
    placed_face: Sequence[int] | None = None,
) -> CoverFacts:
    """OCR (grid size and full size), the face on the cover, the pictures' exposure and the pHash."""
    from PIL import Image

    with Image.open(cover_path) as image:
        rgb = image.convert("RGB")
        small = rgb.resize(
            (round(rgb.width * OCR_SCALE), round(rgb.height * OCR_SCALE)), Image.Resampling.LANCZOS
        )
        small_text = _ocr_text(engine, small) if line else ""
        full_text = _ocr_text(engine, rgb)
        face = _face_on_cover(rgb, layout, parts_dir, landmarker)
    exposure_values = _parts_exposure(parts_dir, layout) if parts_dir is not None else None
    return CoverFacts(
        layout=layout,
        line=line,
        line_style=line_style,
        cover_path=cover_path,
        preview_path=preview_path,
        parts_dir=parts_dir,
        text_rect=text_rect,
        face_h_px=float(face.box[3])
        if face.found and face.box
        else (placed_face[3] if placed_face else None),
        face_rect=face.box if face.found else placed_face,
        face_source="detected" if face.found else ("placed" if placed_face else "none"),
        small_text=small_text,
        full_text=full_text,
        luma=None if exposure_values is None else exposure_values[0],
        clipped=None if exposure_values is None else exposure_values[1],
        phash=phash_of(preview_path),
    )


def rule_problems(
    facts: CoverFacts,
    *,
    recent_hashes: Sequence[int] = (),
    recent_arms: Sequence[tuple[str | None, str | None]] = (),
    exposure_limits: tuple[float, float, float] = (*LUMA_RANGE, CLIPPED_MAX),
) -> list[str]:
    """Every rule check that fails. Pure (all measurements are in `facts`)."""
    problems: list[str] = []
    if facts.line:
        if similarity(facts.small_text, facts.line) < OCR_MATCH:
            problems.append("ocr")
        if facts.text_rect is None or not inside(facts.text_rect):
            problems.append("text_outside_safe_band")
        problems += hookline.text_problems(facts.line)
        if len(hookline.words_of(facts.line)) > hookline.MAX_WORDS:
            problems.append("too_many_words")
    if has_foreign_script(facts.full_text):
        problems.append("foreign_text")
    low, high = FACE_RANGE_PX.get(facts.layout, (0.0, 1e9))
    if facts.face_h_px is None or facts.face_rect is None:
        problems.append("no_face")
    elif not low <= facts.face_h_px <= high:
        problems.append(f"face_size={round(facts.face_h_px)}")
    elif not inside(facts.face_rect):
        problems.append("face_outside_safe_band")
    if facts.luma is not None and facts.clipped is not None:
        problems += exposure(facts.luma, facts.clipped, exposure_limits)
    if facts.phash is not None:
        nearest = min((hamming(facts.phash, other) for other in recent_hashes[:RECENT_COVERS]), default=64)
        if nearest < PHASH_MIN_DISTANCE:
            problems.append(f"duplicate:phash={nearest}")
    if three_in_a_row(recent_arms, (facts.layout, facts.line_style)):
        problems.append("duplicate:arm")
    return problems


@dataclass(frozen=True, slots=True)
class QaResult:
    """The gate's verdict on one cover."""

    rule_problems: tuple[str, ...]
    vision: Mapping[str, Any] | None = None
    pass_score: float = 7.0
    extras: Mapping[str, Any] = field(default_factory=dict)

    @property
    def vision_ok(self) -> bool:
        if self.vision is None:
            return False
        return not self.vision.get("hard_fail") and float(self.vision.get("total", 0)) >= self.pass_score

    @property
    def passed(self) -> bool:
        return not self.rule_problems and self.vision_ok

    def as_json(self) -> dict[str, Any]:
        return {
            "rules": list(self.rule_problems),
            "vision": dict(self.vision) if self.vision is not None else None,
            "passed": self.passed,
            **dict(self.extras),
        }


def review(
    facts: CoverFacts,
    *,
    title: str,
    transcript: str,
    pass_score: float,
    recent_hashes: Sequence[int] = (),
    recent_arms: Sequence[tuple[str | None, str | None]] = (),
    ignore: Sequence[str] = (),
    exposure_limits: tuple[float, float, float] = (*LUMA_RANGE, CLIPPED_MAX),
    client: Any = None,
    clip_id: str | None = None,
) -> QaResult:
    """Rules first; the vision check only when they pass (it costs money). `ignore` drops rule names (a
    plain fallback cover skips the OCR rule)."""
    problems = [
        p
        for p in rule_problems(
            facts, recent_hashes=recent_hashes, recent_arms=recent_arms, exposure_limits=exposure_limits
        )
        if p.split(":")[0].split("=")[0] not in ignore
    ]
    if problems:
        return QaResult(tuple(problems), None, pass_score)
    from sixstars.ai import calls

    verdict = calls.review_cover(
        facts.preview_path.read_bytes(),
        facts.cover_path.read_bytes(),
        {
            "title": title or "(none)",
            "transcript": transcript or "(no speech)",
            "line": facts.line or "(none)",
        },
        client=client,
        clip_id=clip_id,
    )
    return QaResult((), verdict.model_dump(), pass_score)
