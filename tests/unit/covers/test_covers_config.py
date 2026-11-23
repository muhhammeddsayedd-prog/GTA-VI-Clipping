"""Cover engine config (migration 0039): parsing, safety limits and per-lane lookups (docs/29a section 8)."""

from sixstars.covers.config import DEFAULT_WEIGHTS, CoverConfig, lane_key
from sixstars.storage import ClipStorage, cover_public_key, cover_work_key


def test_defaults_are_off_and_safe() -> None:
    config = CoverConfig.from_values({})
    assert not config.enabled and config.mode_for("normal") == "off"
    assert not config.runs_for("instagram", "normal") and not config.brand_mark_for("normal")
    assert config.weights == DEFAULT_WEIGHTS and config.qa_pass_score == 7.0 and config.exploration == 0.2


def test_parsing_and_lane_lookups() -> None:
    config = CoverConfig.from_values(
        {
            "cover.enabled": True,
            "cover.platforms": ["instagram", "myspace"],
            "cover.lane_modes": {"normal": "full", "campaign:tomo": "off", "spike": "bogus"},
            "cover.brand_mark": {"normal": True},
            "cover.weights": {"expression": 0.5, "gameplay": "x"},
            "cover.deadline_s": {"live": 45, "normal": -1},
        },
        version=42,
    )
    assert config.version == 42 and config.platforms == ("instagram",)
    assert config.mode_for("normal") == "full"
    assert config.mode_for("campaign:tomo") == "off"
    assert config.mode_for("spike") == "frame_only"  # an invalid value falls to the wildcard
    assert config.mode_for("campaign:other") == "frame_only"
    assert config.runs_for("instagram", "normal") and not config.runs_for("youtube", "normal")
    assert config.brand_mark_for("normal") and not config.brand_mark_for("campaign:other")
    assert config.weights["expression"] == 0.5 and config.weights["gameplay"] == 0.20
    assert config.deadline_for("spike") == 45 and config.deadline_for("hourly") == 300


def test_limits_cannot_be_loosened() -> None:
    config = CoverConfig.from_values({"cover.exploration": 0.05, "cover.qa_pass_score": 99})
    assert config.exploration == 0.2 and config.qa_pass_score == 10.0
    assert CoverConfig.from_values({"cover.exploration": True}).exploration == 0.2


def test_lane_key() -> None:
    assert lane_key("hourly") == "normal" and lane_key("spike") == "spike"
    assert lane_key("hourly", "tomo") == "campaign:tomo"


def test_storage_keys_and_public_url() -> None:
    assert cover_work_key("c1", "top_1.jpg") == "covers-work/c1/top_1.jpg"
    assert cover_public_key("abc", app_env="staging") == "covers-public/staging/abc.jpg"
    storage = ClipStorage(supabase_url="https://x.supabase.co", service_role_key="k")
    assert storage.public_url("covers-public/staging/a b.jpg") == (
        "https://x.supabase.co/storage/v1/object/public/covers-public/staging/a%20b.jpg"
    )
