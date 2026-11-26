"""M20 go-live: generated packaging (P14), the AI thumbnail and the OpenAI image client."""

import base64
import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from sixstars.ai.schemas import EpisodePackage
from sixstars.clients import openai_images
from sixstars.clients.fakes import FakeOpenAIImagesClient
from sixstars.clients.openai_images import ImageRefusedError, OpenAIImagesClient
from sixstars.longform import chapters as ch
from sixstars.longform import package as pkg
from sixstars.longform import thumb as thumbs
from sixstars.longform.config import ThumbSpec

DAY = date(2026, 11, 25)
MOMENTS = [
    ch.Moment(3, "Alpha", "car flips twice", "https://twitch.tv/alpha", 30.0),
    ch.Moment(2, "Bravo", "cop chase ends in pool", "https://twitch.tv/bravo", 28.0),
    ch.Moment(1, "Alpha", "jet lands on the beach", "https://twitch.tv/alpha", 35.0),
]


def jpeg(w: int = 64, h: int = 36) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 80, 120)).save(out, "JPEG")
    return out.getvalue()


# ── packaging ────────────────────────────────────────────────


def test_clean_enforces_limits_and_hashtag_rules() -> None:
    reply = EpisodePackage(
        title="  " + "Very Long Title " * 10,
        intro="Hi.",
        hashtags=["gta 6", "#Fun!", "#extra", "#more"],
        tags=["#GTA 6", "a" * 40, "gta 6", "Funny, Moments"],
        thumb_text="he didn't see it coming at all",
        thumb_scene="A jet lands on a beach at sunset.",
    )
    out = pkg.clean(reply, n=3, day=DAY, featured=None, creators=["Alpha", "Bravo"])
    assert len(out.title) <= pkg.TITLE_MAX and not out.title.endswith(" ")
    assert out.hashtags[0] == "#GTA6" and len(out.hashtags) == 3
    assert len({h.lower() for h in out.hashtags}) == 3
    assert all(" " not in h for h in out.hashtags)
    assert "gta 6" in out.tags and all(len(t) <= pkg.TAG_MAX and "#" not in t for t in out.tags)
    assert sum(len(t) + 1 for t in out.tags) <= pkg.TAGS_TOTAL_MAX
    assert out.thumb_text == "HE DIDN'T SEE" and out.source == "ai"


def test_creator_day_hashtag_names_the_streamer() -> None:
    out = pkg.clean(EpisodePackage(title="Kai Cenat's Wildest GTA 6 Moments"), n=9, day=DAY,
                    featured="Kai Cenat", creators=["Kai Cenat"])  # fmt: skip
    assert out.hashtags[:2] == ("#GTA6", "#KaiCenat")


def test_writer_failure_falls_back_to_the_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pkg, "recent_titles", lambda day: [])

    def boom(*a: Any, **k: Any) -> EpisodePackage:
        raise RuntimeError("api down")

    monkeypatch.setattr(pkg.calls, "write_episode_package", boom)
    out = pkg.write("e1", MOMENTS, day=DAY, fmt="mix", featured=None, length_s=100)
    assert out.source == "template" and out.title.startswith("TOP 3 ") and out.thumb_text == "TOP 3"


def test_writer_gets_the_countdown(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(pkg, "recent_titles", lambda day: ["Old Title"])

    def fake(values: dict[str, object], **k: Any) -> EpisodePackage:
        seen.update(values)
        return EpisodePackage(title="TOP 3 Wildest GTA 6 Moments", intro="Three moments.")

    monkeypatch.setattr(pkg.calls, "write_episode_package", fake)
    out = pkg.write("e1", MOMENTS, day=DAY, fmt="creator", featured="Alpha", length_s=125)
    assert out.title == "TOP 3 Wildest GTA 6 Moments"
    assert "#1 Alpha: jet lands on the beach" in str(seen["moments"])
    assert seen["length"] == "2:05" and seen["recent_titles"] == "Old Title" and seen["format"] == "creator"


def test_description_has_intro_chapters_credits_and_hashtags_last() -> None:
    text = ch.description(MOMENTS, ch.chapters(MOMENTS, 5.0), DAY, "Footer", intro="Intro.",
                          hashtags=("#GTA6", "#A", "#B"))  # fmt: skip
    assert text.startswith("Intro.") and "00:00 #3 Alpha" in text and "#Shorts" not in text
    assert text.endswith("#GTA6 #A #B") and "https://twitch.tv/bravo" in text


# ── thumbnail ────────────────────────────────────────────────


def test_prompt_rules_per_format() -> None:
    mix = thumbs.build_prompt("STYLE", "mix", "a jet on a beach", None, False)
    assert "STYLE" in mix and "a jet on a beach" in mix and "no real person" in mix
    solo = thumbs.build_prompt("STYLE", "creator", "x", "Alpha", True)
    assert "Alpha" in solo and "profile picture" in solo


def test_headline_and_eyebrow() -> None:
    assert thumbs.headline_and_eyebrow("mix", 24, "", None) == ("TOP 24", "GTA 6 · STREAM MOMENTS")
    assert thumbs.headline_and_eyebrow("mix", 24, "PURE CHAOS", None) == ("PURE CHAOS", "TOP 24 · GTA 6")
    assert thumbs.headline_and_eyebrow("creator", 9, "", "Kai") == ("TOP 9", "KAI")


SPEC = ThumbSpec(size="1536x864")


def picture(client: FakeOpenAIImagesClient, avatar: bytes | None = None, spec: ThumbSpec = SPEC) -> Any:
    return thumbs.picture([jpeg()], avatar, fmt="creator" if avatar else "mix", scene="s", featured="A",
                          spec=spec, episode_id="e1", client=client)  # fmt: skip


def test_refusal_with_avatar_retries_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thumbs.usage, "spent_today", lambda provider: 0.0)
    client = FakeOpenAIImagesClient(errors=[ImageRefusedError("moderation_blocked")])
    _, source, _ = picture(client, avatar=jpeg())
    assert source == "ai_no_avatar"
    assert [c["images"] for c in client.calls_to("edit")] == [2, 1]


def test_failure_or_budget_or_switch_uses_the_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thumbs.usage, "spent_today", lambda provider: 0.0)
    assert picture(FakeOpenAIImagesClient(errors=[RuntimeError("down")]))[1] == "frame"
    assert picture(FakeOpenAIImagesClient(), spec=ThumbSpec(ai=False))[1] == "frame"
    monkeypatch.setattr(thumbs.usage, "spent_today", lambda provider: 9.0)
    client = FakeOpenAIImagesClient()
    assert picture(client)[1] == "frame" and client.calls_to("edit") == []


class FakeRender:
    def render_page(
        self, template: str, size: tuple[int, int], data: dict[str, Any], out_png: Path
    ) -> dict[str, Any]:
        assert template == "thumb_16x9.html" and size == (1280, 720) and data["headline"]
        Image.new("RGB", size, (10, 10, 10)).save(out_png, "PNG")
        return {"size": 104, "fits": True}


def test_make_writes_a_1280x720_jpeg_under_the_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(thumbs.usage, "spent_today", lambda provider: 0.0)
    monkeypatch.setattr(thumbs, "frames", lambda segs, n: [jpeg(1920, 1080)])
    result = thumbs.make("e1", [tmp_path / "s.mp4"], tmp_path, fmt="mix", n=24, thumb_text="", scene="s",
                         featured=None, avatar_url=None, spec=SPEC, client=FakeOpenAIImagesClient(),
                         render=FakeRender())  # type: ignore[arg-type]  # fmt: skip
    with Image.open(result.path) as img:
        assert img.size == (1280, 720) and img.format == "JPEG"
    assert result.path.stat().st_size <= SPEC.max_bytes and result.source == "ai"


# ── OpenAI image client ──────────────────────────────────────


def test_client_sends_multipart_and_records_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(openai_images.usage, "record", lambda *a, **k: recorded.append(k) or True)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        body = {
            "data": [{"b64_json": base64.b64encode(b"IMG").decode()}],
            "output_format": "jpeg",
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"image_tokens": 800},
                "output_tokens": 5000,
            },
        }
        return httpx.Response(200, json=body)

    client = OpenAIImagesClient(api_key="k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    got = client.edit([b"a", b"b"], "p", model="m", size="1536x864", quality="high", ref_id="e1")
    assert got.data == b"IMG" and seen["url"] == openai_images.EDITS_URL
    assert seen["body"].count(b'name="image[]"') == 2 and b'name="size"' in seen["body"]
    assert got.cost_usd == pytest.approx((800 * 8 + 200 * 5 + 5000 * 30) / 1e6)
    assert recorded and recorded[0]["cost_usd"] == got.cost_usd


def test_client_maps_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        error = {
            "error": {"message": "Request was rejected by the safety system", "code": "moderation_blocked"}
        }
        return httpx.Response(400, content=json.dumps(error))

    client = OpenAIImagesClient(api_key="k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ImageRefusedError):
        client.edit([b"a"], "p", model="m", size="1024x1024", quality="low")
