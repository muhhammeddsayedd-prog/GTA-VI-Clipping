"""AI cost plan B1: the shadow job stores both answers in ai_shadow (migration 0041), on its own budget."""

from types import SimpleNamespace
from typing import Any

from sixstars import db, settings, usage
from sixstars.ai import calls, shadow
from sixstars.clients.anthropic_client import RawResult


class RawClient:
    def complete_raw_json(self, *args: Any, **kwargs: Any) -> RawResult:
        usage.record(shadow.PROVIDER, args[0], model=args[4], cost_usd=0.01, ref_id=kwargs.get("ref_id"))
        return RawResult({"title": "A cop crashes"}, 0.01, None, 1200)


def test_the_job_stores_the_answer_and_spends_the_shadow_budget(database: None) -> None:
    seeded = {
        row["key"]: row["value"]
        for row in db.fetch_all("select key, value from settings where key like 'ai.shadow%'")
    }
    assert seeded["ai.shadow.p2_clip_writer"] == ["anthropic:claude-haiku-5-5"]
    assert seeded["ai.shadow_rate"] == 0.2 and seeded["ai.shadow_daily_budget_usd"] == 2
    payload = {
        "prompt_id": calls.P6,  # a text prompt with simple values
        "ref_id": "creator-1",
        "models": ["claude-haiku-5-5"],
        "values": {"name": "x", "description": "y", "titles": "(none)"},
        "schema": {"type": "object"},
        "primary_model": "claude-haiku-5-5",
        "primary_output": {"tier": "auto"},
        "frames": None,
    }
    shadow.handle(SimpleNamespace(id=1, payload=payload), client=RawClient())  # type: ignore[arg-type]
    row = db.fetch_one("select * from ai_shadow")
    assert (
        row is not None and row["valid_json"] is True and row["shadow_output"] == {"title": "A cop crashes"}
    )
    assert row["primary_output"] == {"tier": "auto"} and row["latency_ms"] == 1200
    assert usage.spent_today("anthropic") == 0.0  # the production budget never sees shadow spend
    assert usage.spent_today(shadow.PROVIDER) > 0
    settings.set("ai.shadow_daily_budget_usd", 0)
    shadow.handle(SimpleNamespace(id=2, payload=payload), client=RawClient())  # type: ignore[arg-type]
    assert len(db.fetch_all("select 1 from ai_shadow")) == 1
