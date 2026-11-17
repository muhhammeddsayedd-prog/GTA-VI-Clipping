"""`api_usage` recording, the daily budget guards, the posting guard and migration 0003, against Postgres."""

from pathlib import Path

import pytest

from sixstars import db, settings, usage
from sixstars.clients import anthropic_client, transcribe, upload_post

MIGRATION_0003 = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0003_intake_overrides.sql"


def test_record_writes_a_row() -> None:
    assert usage.record(
        "anthropic",
        "clip_writer",
        model="claude-sonnet-5-5",
        input_tokens=1200,
        output_tokens=300,
        cost_usd=0.0054,
        ref_id="clip-7",
    )
    assert usage.record("youtube", "videos.list", units=1)
    rows = db.fetch_all("select * from api_usage order by id")
    assert len(rows) == 2
    first = rows[0]
    assert (first["provider"], first["operation"], first["model"], first["ref_id"]) == (
        "anthropic",
        "clip_writer",
        "claude-sonnet-5-5",
        "clip-7",
    )
    assert (
        first["input_tokens"] == 1200 and first["output_tokens"] == 300 and float(first["cost_usd"]) == 0.0054
    )
    assert rows[1]["units"] == 1 and float(rows[1]["cost_usd"]) == 0.0


def test_record_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("database is down")

    monkeypatch.setattr(db, "execute", broken)
    assert usage.record("apify", "run", cost_usd=0.1) is False


def test_spent_today_sums_only_today_and_the_named_providers() -> None:
    usage.record("anthropic", "a", cost_usd=1.25)
    usage.record("anthropic", "b", cost_usd=0.75)
    usage.record("openai", "transcribe", cost_usd=0.5)
    db.execute(
        "insert into api_usage (provider, operation, cost_usd, created_at) "
        "values ('anthropic', 'old', 100, now() - interval '2 days')"
    )
    assert usage.spent_today("anthropic") == pytest.approx(2.0)
    assert usage.spent_today(("openai", "groq")) == pytest.approx(0.5)
    assert usage.spent_today("apify") == 0.0


def test_anthropic_budget_guard() -> None:
    settings.set("ai.daily_budget_usd", 5)
    assert anthropic_client.budget_ok("vod") is True
    usage.record("anthropic", "clip_writer", cost_usd=5.01)
    assert anthropic_client.budget_ok("vod") is False
    # The express (live spike) path is never blocked by the budget.
    assert anthropic_client.budget_ok("express") is True


def test_transcribe_budget_guard() -> None:
    settings.set("transcribe.daily_budget_usd", 1)
    assert transcribe.budget_ok() is True
    usage.record("openai", "transcribe", cost_usd=0.6)
    usage.record("groq", "transcribe", cost_usd=0.5)
    assert transcribe.budget_ok() is False


def test_posting_guard_follows_settings() -> None:
    settings.set("launch.dry_run", False)
    settings.set("control.emergency_stop", False)
    upload_post.default_posting_guard()
    settings.set("launch.dry_run", True)
    with pytest.raises(upload_post.PostingBlocked, match="dry_run"):
        upload_post.default_posting_guard()
    settings.set("launch.dry_run", False)
    settings.set("control.emergency_stop", True)
    with pytest.raises(upload_post.PostingBlocked, match="emergency_stop"):
        upload_post.default_posting_guard()


def test_upload_is_blocked_by_default_in_dry_run(tmp_path: Path) -> None:
    settings.set("launch.dry_run", True)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    client = upload_post.UploadPostClient(api_key="k")
    with pytest.raises(upload_post.PostingBlocked):
        client.upload_video("default", "tiktok", clip, {"title": "t"}, "post:1:tiktok_main", "req-1")


def test_hosted_provider_comes_from_settings() -> None:
    assert settings.get_str("transcribe.express_provider") == "openai"


def test_migration_0003_rewrites_handles_and_is_idempotent() -> None:
    settings.set("transcribe.express_provider", "groq")
    settings.set("posting.first_comment", "Follow @6starsclips for the best GTA 6 moments every hour")
    settings.set("factory.watermark", "@6starsclips")
    sql = MIGRATION_0003.read_text(encoding="utf-8")
    for _ in range(2):
        with db.transaction() as conn:
            conn.execute(sql.encode("utf-8"))
        settings.invalidate()
        assert settings.get_str("transcribe.express_provider") == "openai"
        assert settings.get_str("factory.watermark") == "@6starsclipping"
        assert (
            settings.get_str("posting.first_comment")
            == "Follow @6starsclipping for the best GTA 6 moments every hour"
        )
    # A value without the placeholder handle (the staging seed) is left alone.
    settings.set("posting.first_comment", "Follow for more (staging test)")
    with db.transaction() as conn:
        conn.execute(sql.encode("utf-8"))
    settings.invalidate()
    assert settings.get_str("posting.first_comment") == "Follow for more (staging test)"
