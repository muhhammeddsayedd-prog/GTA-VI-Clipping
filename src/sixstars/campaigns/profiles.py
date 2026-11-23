"""Render profiles (D118, PR 3): the look of a clip, picked by its lane. `brand` is today's look (the
"6 Stars Clips" watermark); a campaign clip gets its campaign's profile: watermark on or off, the
campaign's logo in a corner, and a "Paid partnership" line over the first seconds. Profiles are rows of
`render_profiles`, so a new brief is a new row, not code. A profile's logo PNG is kept in the row
(`logo_png`) and written to a cache file under `$DATA_DIR/logos` when a render needs it.

`apply` is pure: it turns a style's rules into the campaign version of them. Campaign renders also never
use the follow line (a Normal-lane test) or the POV crop (which cuts the streamer's face cam away).
"""

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from sixstars import db
from sixstars.config import env
from sixstars.factory.style import StyleRules

BRAND: Final = "brand"
LOGO_DIR: Final = "logos"
LOGO_POSITIONS: Final = ("bottom_right", "bottom_left", "top_right", "top_left")


@dataclass(frozen=True)
class RenderProfile:
    id: str
    watermark: bool = True
    logo_pos: str | None = None
    logo_sha: str | None = None  # sha256 of `logo_png`, None without a logo
    keep_overlay: bool = False
    disclosure_text: str | None = None
    disclosure_s: float | None = None

    @property
    def is_brand(self) -> bool:
        return self.id == BRAND


BRAND_PROFILE: Final = RenderProfile(BRAND)


def apply(rules: StyleRules, profile: RenderProfile, logo_path: Path | None = None) -> StyleRules:
    """The style's rules in this profile's look. `brand` changes nothing."""
    if profile.is_brand:
        return rules
    return replace(
        rules,
        watermark=rules.watermark and profile.watermark,
        follow_overlay_s=0.0,
        follow_overlay_text="",
        frame="auto" if rules.frame == "pov" else rules.frame,
        disclosure_text=(profile.disclosure_text or "") if profile.disclosure_s else "",
        disclosure_s=float(profile.disclosure_s or 0.0),
        campaign_logo=str(logo_path) if logo_path is not None and profile.logo_pos else "",
        campaign_logo_pos=profile.logo_pos or "",
    )


# ── database ─────────────────────────────────────────────────


def _from_row(row: dict[str, Any]) -> RenderProfile:
    return RenderProfile(
        id=str(row["id"]),
        watermark=bool(row["watermark"]),
        logo_pos=row["logo_pos"],
        logo_sha=row["logo_sha"],
        keep_overlay=bool(row["keep_overlay"]),
        disclosure_text=row["disclosure_text"],
        disclosure_s=None if row["disclosure_s"] is None else float(row["disclosure_s"]),
    )


def load(profile_id: str | None) -> RenderProfile:
    """A profile by id; `brand` when unset or unknown."""
    if not profile_id or profile_id == BRAND:
        return BRAND_PROFILE
    row = db.fetch_one(
        "select id, watermark, logo_pos, keep_overlay, disclosure_text, disclosure_s, "
        "case when logo_png is null then null else encode(sha256(logo_png), 'hex') end as logo_sha "
        "from render_profiles where id = %s",
        (profile_id,),
    )
    return BRAND_PROFILE if row is None else _from_row(row)


def for_clip(clip_id: str) -> RenderProfile:
    """The profile of a clip: its campaign's, or `brand` for a Normal clip."""
    row = db.fetch_one("select campaign_id, render_profile from clips where id = %s", (clip_id,))
    if row is None or row["campaign_id"] is None:
        return BRAND_PROFILE
    return load(row["render_profile"])


def logo_file(profile: RenderProfile) -> Path | None:
    """The profile's logo as a local PNG (written once per content hash), or None without a logo."""
    if profile.logo_sha is None or profile.logo_pos is None:
        return None
    folder = Path(env().DATA_DIR) / LOGO_DIR
    path = folder / f"{profile.logo_sha}.png"
    if path.exists():
        return path
    row = db.fetch_one("select logo_png from render_profiles where id = %s", (profile.id,))
    if row is None or row["logo_png"] is None:
        return None
    data = bytes(row["logo_png"])
    if hashlib.sha256(data).hexdigest() != profile.logo_sha:
        return None  # changed since it was read: the next render picks up the new one
    folder.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(path)
    return path


def styled(clip_id: str, rules: StyleRules) -> StyleRules:
    """A style's rules for this clip: unchanged for a Normal clip, in its campaign's look otherwise."""
    profile = for_clip(clip_id)
    return apply(rules, profile, logo_file(profile))


def save_logo(profile_id: str, png: bytes) -> str:
    """Store a PNG as the profile's logo; returns its sha256. Raises ValueError for a non-PNG file."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("the logo must be a PNG file")
    digest = hashlib.sha256(png).hexdigest()
    done = db.execute(
        "update render_profiles set logo_png = %s, logo_path = %s, updated_at = now() where id = %s",
        (png, f"db:{digest}", profile_id),
    )
    if not done:
        raise ValueError(f"render profile {profile_id} does not exist")
    return digest
