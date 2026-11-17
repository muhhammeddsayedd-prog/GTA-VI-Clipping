"""AI layer against Postgres: models and budget read from `settings`, spend read from `api_usage`, and a
spike verdict written to the `spikes` row."""

from typing import Any

import pytest

from sixstars import db, settings, usage
from sixstars.ai import calls, costs, spike_confirm
from sixstars.ai.calls import BudgetExceededError, ChatLine, SpikeContext
from sixstars.ai.schemas import SpikeVerdict
from sixstars.clients.fakes import FakeAnthropicClient

VERDICT: dict[str, Any] = {
    "is_moment": True,
    "confidence": 0.7,
    "moment_type": "chaos",
    "one_line": "Police chase ends in the canal",
    "noise_reason": None,
}


def spike_context(spike_id: str = "sp-1") -> SpikeContext:
    return SpikeContext(spike_id, "speedy", "twitch", "GTA RP", 4.2, 60, 40, chat=(ChatLine(0.0, "LMAO"),))


@pytest.fixture
def spike_id() -> Any:
    creator = db.fetch_one(
        "insert into creators (platform, channel_id, handle) values "
        "('twitch', 'm05-test', 'm05test') returning id"
    )
    assert creator is not None
    row = db.fetch_one(
        "insert into spikes (mode, creator_id, score_z, tier) values ('live', %s, 4.5, 'S') returning id",
        (creator["id"],),
    )
    assert row is not None
    yield str(row["id"])
    db.execute("delete from spikes where creator_id = %s", (creator["id"],))
    db.execute("delete from creators where id = %s", (creator["id"],))


def test_models_come_from_the_settings_table() -> None:
    fake = FakeAnthropicClient({calls.P1: VERDICT})
    calls.confirm_spikes([spike_context()], client=fake)
    assert fake.calls[0][1]["model"] == settings.get_str("ai.model_fast")
    settings.set("ai.model_fast", "claude-sonnet-5-5")
    calls.confirm_spikes([spike_context()], client=fake)
    assert fake.calls[1][1]["model"] == "claude-sonnet-5-5"
    assert settings.get_str("env.target_game") in fake.calls[1][1]["system"]


def test_budget_guard_uses_recorded_spend() -> None:
    fake = FakeAnthropicClient({calls.P1: VERDICT})
    settings.set("ai.daily_budget_usd", 1)
    usage.record(
        "anthropic", calls.P2, model="claude-sonnet-5-5", input_tokens=10, output_tokens=5, cost_usd=0.6
    )
    assert costs.today_spend() == pytest.approx(0.6)
    assert costs.budget_remaining() == pytest.approx(0.4)
    assert len(calls.confirm_spikes([spike_context()], client=fake)) == 1

    usage.record(
        "anthropic", calls.P2, model="claude-sonnet-5-5", input_tokens=10, output_tokens=5, cost_usd=0.5
    )
    assert costs.budget_remaining() == 0.0
    with pytest.raises(BudgetExceededError):
        calls.confirm_spikes([spike_context()], client=fake)
    assert len(calls.confirm_spikes([spike_context()], lane="express", client=fake)) == 1


def test_spend_by_operation_groups_rows() -> None:
    usage.record(
        "anthropic", calls.P2, model="claude-sonnet-5-5", input_tokens=1000, output_tokens=200, cost_usd=0.004
    )
    usage.record(
        "anthropic", calls.P2, model="claude-sonnet-5-5", input_tokens=500, output_tokens=100, cost_usd=0.002
    )
    usage.record(
        "anthropic", calls.P1, model="claude-haiku-5-5", input_tokens=300, output_tokens=50, cost_usd=0.0001
    )
    usage.record("openai", "transcribe", units=30, cost_usd=0.003)
    rows = costs.spend_by_operation()
    assert [(row.operation, row.calls) for row in rows] == [(calls.P2, 2), (calls.P1, 1)]
    assert rows[0].input_tokens == 1500 and rows[0].output_tokens == 300
    assert rows[0].cost_usd == pytest.approx(0.006)
    assert costs.today_spend("openai") == pytest.approx(0.003)


def test_apply_confirms_and_downgrades_a_provisional_s(spike_id: str) -> None:
    decision = spike_confirm.apply(spike_id, "S", SpikeVerdict.model_validate(VERDICT))
    assert (decision.tier, decision.status) == ("A", "confirmed")
    row = db.fetch_one(
        "select tier, status, claude_confidence, claude_reason from spikes where id = %s", (spike_id,)
    )
    assert row is not None
    assert (row["tier"], row["status"], float(row["claude_confidence"])) == ("A", "confirmed", 0.7)
    assert row["claude_reason"] == "chaos: Police chase ends in the canal"
    event = db.fetch_one("select type, entity_type, entity_id, data from events order by id desc limit 1")
    assert event is not None
    assert (event["type"], event["entity_type"], event["entity_id"]) == ("spike.confirmed", "spike", spike_id)
    assert event["data"]["provisional_tier"] == "S" and event["data"]["tier"] == "A"


def test_apply_rejects_noise(spike_id: str) -> None:
    noise = SpikeVerdict.model_validate(
        {**VERDICT, "is_moment": False, "noise_reason": "raid", "confidence": 0.1}
    )
    decision = spike_confirm.apply(spike_id, "S", noise)
    assert decision.status == "rejected"
    row = db.fetch_one("select tier, status, claude_reason from spikes where id = %s", (spike_id,))
    assert row is not None
    assert (row["tier"], row["status"], row["claude_reason"]) == ("rejected", "rejected", "raid")
    event = db.fetch_one("select type from events order by id desc limit 1")
    assert event is not None and event["type"] == "spike.rejected"
