"""Cover engine C5 (docs/29a stage 4): the compositor's geometry and the templates. The real render needs
Playwright's Chromium and is skipped without it."""

from pathlib import Path

import pytest

from sixstars.covers import compose


def test_face_crop_puts_the_face_where_the_layout_wants_it() -> None:
    face = (300, 100, 120, 150)
    x, y, w, h = compose.face_crop(face, 1280, 720, 1080, 620, 0.42, 0.45)
    assert w / h == pytest.approx(1080 / 620, rel=0.02)
    assert x >= 0 and y >= 0 and x + w <= 1280 and y + h <= 720
    assert 150 / h == pytest.approx(0.42, abs=0.02)
    assert (y + 0.45 * h) == pytest.approx(100 + 75, abs=2)
    assert abs((x + w / 2) - 360) <= 2


def test_face_crop_stays_inside_a_small_image() -> None:
    x, y, w, h = compose.face_crop((10, 10, 300, 300), 480, 360, 9, 16, 0.27, 0.42)
    assert x >= 0 and y >= 0 and x + w <= 480 and y + h <= 360 and w / h == pytest.approx(9 / 16, rel=0.03)


def test_cover_crop() -> None:
    assert compose.cover_crop(1920, 1080, 1080, 540) == (0, 60, 1920, 960)
    assert compose.cover_crop(1920, 1080, 1080, 1920) == (656, 0, 608, 1080)
    x, _y, w, _h = compose.cover_crop(1920, 1080, 1080, 1920, focus_x=0.0)
    assert x == 0 and w == 608


def test_sentence_case_and_layout_choice() -> None:
    assert compose.sentence_case("FIRST CAR STOLEN") == "First car stolen"
    assert compose.sentence_case("WHAT THE F*CK") == "What the f*ck"
    assert compose.choose_layout("face_led", 300) == "split"
    assert compose.choose_layout("face_led", 480) == "face_led"
    assert compose.choose_layout("moment_led", None) == "moment_led"


def test_templates_and_assets_exist() -> None:
    for layout in ("split", "face_led", "moment_led", "tiktok_native"):
        html = (compose.TEMPLATES / f"{layout}.html").read_text(encoding="utf-8")
        assert f'data-layout="{layout}"' in html and "cover.js" in html and "cover.css" in html
    root = Path(__file__).resolve().parents[3] / "assets"
    assert (root / "fonts" / "Anton-Regular.ttf").is_file() and (
        root / "fonts" / "Inter-Variable.ttf"
    ).is_file()
    assert (root / "covers" / "brand" / "vi-mark.svg").is_file()
    assert set(compose.LINE_Y) == {"split", "face_led", "moment_led", "tiktok_native"}
    top, bottom = compose.PREVIEW_BOX[1], compose.PREVIEW_BOX[3]
    assert bottom - top == 1440 and compose.PREVIEW_BOX[2] == 1080


def test_render_with_chromium(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    from PIL import Image

    face = tmp_path / "face.jpg"
    game = tmp_path / "game.jpg"
    Image.new("RGB", (640, 480), (120, 90, 80)).save(face)
    Image.new("RGB", (1280, 720), (40, 120, 60)).save(game)
    try:
        renderer = compose.Renderer()
    except playwright.Error:
        pytest.skip("Chromium for Playwright is not installed")
    try:
        spec = compose.CoverSpec("split", face, game, (260, 120, 120, 150), ("SIX STARS?!",), "STARS", True)
        result = compose.compose(spec, tmp_path, render=renderer)
    finally:
        renderer.close()
    with Image.open(result.jpeg) as image:
        assert image.size == (1080, 1920)
    with Image.open(result.preview) as preview:
        assert preview.size == (1080, 1440)
    assert result.fits and result.font_size >= 96 and 110 <= result.cap_height <= 170
    assert result.jpeg.stat().st_size < 2_000_000
