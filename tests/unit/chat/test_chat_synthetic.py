"""The synthetic chat generators used by the filter and spike-engine tests."""

from collections import Counter

from sixstars.chat import synthetic
from sixstars.chat.filters import FilterContext, is_counted

CTX = FilterContext(warmup_s=0.0, ignore_users=frozenset())


def _per_bucket(messages: list[synthetic.ChatMessage]) -> Counter[int]:
    return Counter(int(message.t) // 10 for message in messages)


def test_generators_are_deterministic() -> None:
    assert synthetic.flat_chat(600, 5) == synthetic.flat_chat(600, 5)
    assert synthetic.flat_chat(600, 5, seed=1) != synthetic.flat_chat(600, 5, seed=2)
    assert synthetic.burst(100.0, 50, 20) == synthetic.burst(100.0, 50, 20)


def test_flat_chat_has_the_requested_rate_in_every_bucket() -> None:
    messages = synthetic.flat_chat(1200, 5)
    assert len(messages) == 600
    assert set(_per_bucket(messages).values()) == {5}
    assert [message.t for message in messages] == sorted(message.t for message in messages)
    assert messages[0].t >= 0 and messages[-1].t < 1200
    assert all(is_counted(message, CTX) for message in messages)
    assert len({message.user_id for message in messages}) <= 50


def test_flat_chat_supports_fractional_rates_start_and_platform() -> None:
    messages = synthetic.flat_chat(100, 0.5, start=1_700_000_000.0, platform="youtube")
    assert len(messages) == 5
    assert all(message.platform == "youtube" and message.t >= 1_700_000_000.0 for message in messages)


def test_burst_has_the_requested_messages_and_users() -> None:
    messages = synthetic.burst(1200.0, 120, 80)
    assert len(messages) == 120
    assert len({message.user_id for message in messages}) == 80
    assert all(1200.0 <= message.t < 1208.0 for message in messages)
    assert _per_bucket(messages) == {120: 120}
    few = synthetic.burst(1200.0, 120, 3)
    assert len({message.user_id for message in few}) == 3


def test_bot_flood_is_one_account_repeating_itself() -> None:
    messages = synthetic.bot_flood(0.0, 600.0)
    assert len(messages) == 300
    assert {message.username for message in messages} == {"dealsbot9000x"}
    assert len({message.text[:20] for message in messages}) == 1


def test_sub_train_has_no_chat_messages() -> None:
    messages = synthetic.sub_train(500.0, 200)
    assert len(messages) == 200
    assert Counter(message.kind for message in messages) == {"sub": 150, "gift": 50}
    assert not any(is_counted(message, CTX) for message in messages)
    assert all(500.0 <= message.t < 510.0 for message in messages)


def test_raid_is_a_notice_followed_by_greetings() -> None:
    messages = synthetic.raid(900.0, 40)
    assert (messages[0].kind, messages[0].t) == ("raid", 900.0)
    assert Counter(message.kind for message in messages) == {"raid": 1, "chat": 40}
    assert len({message.user_id for message in messages}) == 41


def test_merge_sorts_by_time() -> None:
    merged = synthetic.merge(
        synthetic.flat_chat(60, 5), synthetic.burst(30.0, 10, 10), synthetic.raid(5.0, 2)
    )
    assert len(merged) == 30 + 10 + 3
    assert [message.t for message in merged] == sorted(message.t for message in merged)
