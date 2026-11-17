"""Property-style tests with seeded random histories (standard library only).

Invariants: the cap is never exceeded in any rolling 24 h window, the minimum gap is never violated, a spike
is never blocked by the cap while spike reserve remains, nothing is allowed under emergency stop, nothing
may be sent in dry mode, and staging never sends a post that could be public.
"""

import random
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from posting_helpers import (
    BOTH,
    LAUNCH_AT,
    T0,
    TIKTOK_HOURS,
    hourly_clip,
    make_params,
    post,
    production_accounts,
    small_account,
    spike_clip,
    staging_accounts,
)

from sixstars.posting.caps import cap_state
from sixstars.posting.models import Account, Action, Clip, Decision, Lane, Platform, PostRecord, Reason
from sixstars.posting.params import PostingParams
from sixstars.posting.precedence import decide_hourly_account, decide_spike, demoted_rank, plan_spikes
from sixstars.posting.selection import plan_hourly_slot
from sixstars.posting.slots import fire_at, slot_start

DAY = timedelta(hours=24)
HOUR = timedelta(hours=1)
IG = "instagram_main"
SEEDS = range(40)
STATUSES = ["pending", "published", "failed", "skipped", "removed", "remove_failed"]
PRIVACY = [
    None,
    "",
    "public",
    "unlisted",
    "private",
    "SELF_ONLY",
    "PUBLIC_TO_EVERYONE",
    "FOLLOWER_OF_CREATOR",
]


# ── checks on a finished posting history ──────────────────────────────────────────────────────────────────
def max_in_any_window(times: list[datetime], window: timedelta) -> int:
    """The largest number of `times` inside any closed interval of length `window`."""
    ordered = sorted(times)
    best = 0
    for i, end in enumerate(ordered):
        best = max(best, sum(1 for t in ordered[: i + 1] if end - t <= window))
    return best


def min_gap(times: list[datetime]) -> timedelta | None:
    ordered = sorted(times)
    return min((b - a for a, b in zip(ordered, ordered[1:], strict=False)), default=None)


def assert_account_invariants(account: Account, posts: list[PostRecord], params: PostingParams) -> None:
    own = [p for p in posts if p.account_id == account.id]
    times = [p.at for p in own]
    cap = min(account.cap_24h, params.platform_max_24h[account.platform])
    gap = timedelta(seconds=max(account.min_gap_s, params.min_gap_s))
    assert max_in_any_window(times, DAY) <= cap, f"{account.id} exceeded its cap"
    smallest = min_gap(times)
    assert smallest is None or smallest >= gap, f"{account.id} violated the minimum gap"
    spike_times = [p.at for p in own if p.lane is Lane.SPIKE]
    assert max_in_any_window(spike_times, HOUR) <= params.spike_max_per_hour
    assert len({p.clip_id for p in own}) == len(own), f"{account.id} got a clip twice"
    assert all(p.lane in account.lanes for p in own)
    if not account.enabled:
        assert not own


# ── one account, random rules, the engine builds the history ──────────────────────────────────────────────
def simulate_one_account(seed: int) -> tuple[Counter[Reason], int]:
    rng = random.Random(seed)
    cap = rng.randint(1, 8)
    account = small_account(cap=cap, reserve=rng.randint(0, cap), gap=rng.choice([0, 60, 900, 1800, 3600]))
    params = make_params(
        min_gap_s=rng.choice([0, 900]),
        spike_max_per_hour=rng.randint(1, 3),
        skip_hourly_after_spike_s=rng.choice([0, 1800]),
    )
    now = T0
    history: list[PostRecord] = []
    seen: Counter[Reason] = Counter()
    for step in range(700):
        now += timedelta(seconds=rng.choice([1, 30, 300, 899, 900, 901, 1800, 3600, rng.randint(1, 7200)]))
        before = cap_state(account, history, now, params)
        assert before.free >= before.reserve_left  # the hourly lane never ate into the spike reserve
        if rng.random() < 0.5:
            slot = slot_start(now)
            decision = decide_hourly_account(account, history, slot, now, params)
            if decision.action is Action.ALLOW:
                history.append(post(IG, now, Lane.HOURLY, clip_id=f"h{step}", slot_at=slot))
        else:
            decision = decide_spike(spike_clip(f"s{step}", at=now), account, history, now, params)
            if decision.code is Reason.AT_CAP:
                # A spike is blocked by the cap only when spikes themselves used up the whole reserve.
                assert before.reserve_left == 0
                assert before.spikes_24h >= account.spike_reserve
            if decision.action is Action.ALLOW:
                history.append(post(IG, now, Lane.SPIKE, clip_id=f"s{step}"))
        seen[decision.code] += 1
    assert_account_invariants(account, history, params)
    return seen, len(history)


def test_engine_built_histories_never_break_cap_gap_or_reserve() -> None:
    seen: Counter[Reason] = Counter()
    posts = 0
    for seed in SEEDS:
        codes, count = simulate_one_account(seed)
        seen += codes
        posts += count

    # The runs must have exercised the rules, not just passed trivially.
    assert posts > 500
    for code in (
        Reason.OK,
        Reason.AT_CAP,
        Reason.RESERVED_FOR_SPIKE,
        Reason.MIN_GAP,
        Reason.MIN_GAP_WAIT,
        Reason.SPIKE_HOURLY_LIMIT,
        Reason.SPIKE_RECENT,
        Reason.SLOT_ALREADY_FILLED,
    ):
        assert seen[code] > 0, code


# ── arbitrary histories against an independent oracle ─────────────────────────────────────────────────────
def random_history(rng: random.Random, now: datetime, size: int) -> list[PostRecord]:
    accounts = ["instagram_main", "youtube_main", "tiktok_main"]
    history = []
    for i in range(size):
        age = rng.choice(
            [
                timedelta(seconds=rng.randint(0, 3 * 86400)),
                timedelta(seconds=rng.choice([899, 900, 901, 1799, 1800, 1801, 3599, 3600, 3601])),
                timedelta(seconds=rng.choice([86399, 86400, 86401])),
                -timedelta(seconds=rng.randint(1, 600)),
            ]
        )
        status = rng.choice(STATUSES)
        history.append(
            post(
                rng.choice(accounts) if rng.random() < 0.3 else IG,
                now - age,
                rng.choice([Lane.HOURLY, Lane.HOURLY, Lane.SPIKE]),
                clip_id=f"r{i}",
                status=status,
                dry_run=status == "skipped" and rng.random() < 0.7,
            )
        )
    return history


def oracle(
    account: Account, history: list[PostRecord], now: datetime, params: PostingParams
) -> tuple[bool, bool, bool, bool, bool]:
    """(cap free, reserve allows hourly, spike rate ok, gap ok, no recent spike), all computed afresh."""
    real = [r for r in history if r.account_id == account.id and not r.dry_run and r.status != "skipped"]
    used = sum(1 for r in real if (now - r.at).total_seconds() <= 86400)
    spikes_day = sum(1 for r in real if r.lane is Lane.SPIKE and (now - r.at).total_seconds() <= 86400)
    spikes_hour = sum(1 for r in real if r.lane is Lane.SPIKE and (now - r.at).total_seconds() <= 3600)
    free = min(account.cap_24h, params.platform_max_24h[account.platform]) - used
    gap = max(account.min_gap_s, params.min_gap_s)
    gap_ok = all((now - r.at).total_seconds() >= gap for r in real)
    slot = slot_start(now)
    recent_spike = any(
        r.lane is Lane.SPIKE and (slot - r.at).total_seconds() < params.skip_hourly_after_spike_s
        for r in real
    )
    return (
        free > 0,
        free > max(0, account.spike_reserve - spikes_day),
        spikes_hour < params.spike_max_per_hour,
        gap_ok,
        not recent_spike,
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_a_post_is_allowed_exactly_when_an_independent_check_agrees(seed: int) -> None:
    rng = random.Random(1000 + seed)
    for _ in range(60):
        now = T0 + timedelta(seconds=rng.randint(0, 5 * 86400))
        cap = rng.randint(0, 12)
        account = small_account(cap=cap, reserve=rng.randint(0, 6), gap=rng.choice([0, 600, 900, 1200]))
        params = make_params(
            min_gap_s=rng.choice([0, 900]),
            spike_max_per_hour=rng.randint(0, 3),
            skip_hourly_after_spike_s=rng.choice([0, 900, 1800]),
        )
        history = random_history(rng, now, rng.randint(0, 14))
        cap_free, reserve_ok, rate_ok, gap_ok, no_recent_spike = oracle(account, history, now, params)

        hourly = decide_hourly_account(account, history, slot_start(now), now, params)
        spike = decide_spike(spike_clip("fresh", at=now), account, history, now, params)

        assert (hourly.action is Action.ALLOW) is (cap_free and reserve_ok and gap_ok and no_recent_spike)
        assert (spike.action is Action.ALLOW) is (cap_free and rate_ok and gap_ok)
        assert (spike.action is Action.DEFER) is (
            cap_free and rate_ok and not gap_ok and spike.defer_until is not None
        )
        if spike.defer_until is not None:
            assert now < spike.defer_until <= now + timedelta(seconds=params.spike_max_wait_s)
        assert (spike.code is Reason.AT_CAP) is (not cap_free)  # reserve or hourly posts never block a spike
        assert hourly.action in (Action.ALLOW, Action.SKIP)  # the hourly lane never waits


# ── emergency stop, dry mode, staging ─────────────────────────────────────────────────────────────────────
def random_accounts(rng: random.Random) -> list[Account]:
    base = list((staging_accounts() if rng.random() < 0.5 else production_accounts()).values())
    accounts = []
    for account in base:
        accounts.append(
            replace(
                account,
                enabled=rng.random() < 0.85,
                lanes=rng.choice([BOTH, frozenset({Lane.HOURLY}), frozenset({Lane.SPIKE}), frozenset()]),
                privacy=rng.choice(PRIVACY),
                cap_24h=rng.randint(0, 50),
                spike_reserve=rng.randint(0, 10),
                upload_post_user=rng.choice(["default", "default", "other"]),
                hourly_hours_utc=rng.choice([None, None, TIKTOK_HOURS, frozenset()]),
                paused_until=rng.choice([None, None, T0 + timedelta(hours=rng.randint(-5, 5))]),
            )
        )
    return accounts


def random_world(
    rng: random.Random,
) -> tuple[list[Account], list[PostRecord], list[Clip], list[Clip], datetime]:
    now = rng.choice([LAUNCH_AT - timedelta(seconds=rng.randint(1, 86400)), LAUNCH_AT, T0, T0 + HOUR * 30])
    now += timedelta(seconds=rng.randint(0, 3599))
    queue = [
        hourly_clip(f"h{i}", rng.uniform(1, 10), f"creator-{rng.randint(0, 3)}", at=now) for i in range(5)
    ]
    spikes = [spike_clip(f"s{i}", rng.uniform(1, 10), at=now) for i in range(rng.randint(0, 3))]
    return random_accounts(rng), random_history(rng, now, rng.randint(0, 20)), queue, spikes, now


def all_decisions(
    accounts: list[Account],
    history: list[PostRecord],
    queue: list[Clip],
    spikes: list[Clip],
    now: datetime,
    params: PostingParams,
) -> list[Decision]:
    slot = slot_start(now)
    decisions = list(
        plan_hourly_slot(accounts, history, queue, slot, now, params, spike_clips=spikes).decisions
    )
    for plan in plan_spikes(spikes, accounts, history, now, params):
        decisions.extend(plan.decisions)
    for account in accounts:
        decisions.append(decide_hourly_account(account, history, slot, now, params))
        decisions.extend(decide_spike(clip, account, history, now, params) for clip in spikes)
    return decisions


@pytest.mark.parametrize("seed", SEEDS)
def test_nothing_is_ever_allowed_under_emergency_stop(seed: int) -> None:
    rng = random.Random(2000 + seed)
    for _ in range(15):
        accounts, history, queue, spikes, now = random_world(rng)
        params = make_params(
            rng.choice(["staging", "production"]),
            emergency_stop=True,
            dry_run=rng.random() < 0.5,
            staging_allow_instagram=rng.random() < 0.5,
        )

        decisions = all_decisions(accounts, history, queue, spikes, now, params)

        assert decisions
        for decision in decisions:
            assert (decision.action, decision.code) == (Action.SKIP, Reason.EMERGENCY_STOP)
            assert not decision.may_send and not decision.would_post
        assert not any(plan.demote_to_hourly for plan in plan_spikes(spikes, accounts, history, now, params))
        slot_plan = plan_hourly_slot(accounts, history, queue, slot_start(now), now, params)
        assert slot_plan.posts == () and not slot_plan.slot_empty


@pytest.mark.parametrize("seed", SEEDS)
def test_nothing_may_be_sent_in_dry_mode(seed: int) -> None:
    rng = random.Random(3000 + seed)
    would_post = 0
    for _ in range(15):
        accounts, history, queue, spikes, now = random_world(rng)
        dry_setting = rng.random() < 0.5
        if not dry_setting:
            now = LAUNCH_AT - timedelta(seconds=rng.randint(1, 86400))  # before launch
            queue = [replace(c, approved_at=now - HOUR) for c in queue]
        params = make_params(rng.choice(["staging", "production"]), dry_run=dry_setting)

        for decision in all_decisions(accounts, history, queue, spikes, now, params):
            assert not decision.may_send
            assert decision.action is not Action.ALLOW
            assert decision.dry_run
            would_post += decision.would_post
    assert would_post > 0  # dry mode still makes decisions


@pytest.mark.parametrize("seed", SEEDS)
def test_live_decisions_respect_the_account_and_the_staging_rule(seed: int) -> None:
    rng = random.Random(4000 + seed)
    for _ in range(15):
        accounts, history, queue, spikes, now = random_world(rng)
        now = max(now, LAUNCH_AT)
        app_env = rng.choice(["staging", "production"])
        allow_instagram = rng.random() < 0.3
        params = make_params(app_env, staging_allow_instagram=allow_instagram)
        by_id = {a.id: a for a in accounts}

        for decision in all_decisions(accounts, history, queue, spikes, now, params):
            if not decision.may_send:
                continue
            account = by_id[decision.account_id]
            assert account.enabled
            assert decision.lane in account.lanes
            assert account.paused_until is None or account.paused_until < now
            assert not decision.dry_run
            if account.platform is Platform.TIKTOK:
                assert account.privacy in {"SELF_ONLY", "PUBLIC_TO_EVERYONE", "FOLLOWER_OF_CREATOR"}
            if account.platform is Platform.YOUTUBE:
                assert account.privacy in {"public", "unlisted", "private"}
            if app_env == "staging":
                if account.platform is Platform.TIKTOK:
                    assert account.privacy == "SELF_ONLY"
                elif account.platform is Platform.YOUTUBE:
                    assert account.privacy == "private"
                else:
                    assert allow_instagram


def test_the_live_random_worlds_do_allow_posts() -> None:
    """Guards the test above against passing because nothing was ever allowed."""
    allowed: Counter[tuple[str, Platform]] = Counter()
    for seed in SEEDS:
        rng = random.Random(4000 + seed)
        for _ in range(15):
            accounts, history, queue, spikes, now = random_world(rng)
            now = max(now, LAUNCH_AT)
            app_env = rng.choice(["staging", "production"])
            params = make_params(app_env, staging_allow_instagram=rng.random() < 0.3)
            by_id = {a.id: a for a in accounts}
            for decision in all_decisions(accounts, history, queue, spikes, now, params):
                if decision.may_send:
                    allowed[(app_env, by_id[decision.account_id].platform)] += 1
    for app_env in ("staging", "production"):
        for platform in Platform:
            assert allowed[(app_env, platform)] > 0, (app_env, platform)


# ── the whole system on the seeded accounts for two days ──────────────────────────────────────────────────
def simulate_system(
    seed: int, accounts: list[Account], params: PostingParams
) -> tuple[list[PostRecord], Counter[Reason]]:
    rng = random.Random(seed)
    start = T0.replace(hour=0)
    posts: list[PostRecord] = []
    queue: list[Clip] = []
    pending_spikes: list[Clip] = []
    creators: dict[str, str] = {}
    seen: Counter[Reason] = Counter()
    by_id = {a.id: a for a in accounts}

    def record(decision: Decision, now: datetime) -> None:
        assert decision.clip_id is not None
        posts.append(
            post(
                decision.account_id,
                now,
                decision.lane,
                clip_id=decision.clip_id,
                slot_at=decision.slot_at,
                creator_id=creators[decision.clip_id],
            )
        )

    for minute in range(2 * 24 * 60):
        now = start + timedelta(minutes=minute)
        slot = slot_start(now)
        if now.minute == 0:
            queue = [c for c in queue if c.approved_at is not None and now - c.approved_at < 8 * HOUR]
            for i in range(rng.randint(0, 2)):
                clip = hourly_clip(
                    f"h{minute}-{i}", rng.uniform(1, 10), f"creator-{rng.randint(0, 4)}", at=now
                )
                queue.append(clip)
                creators[clip.id] = clip.creator_id
        if rng.random() < 0.04:
            clip = spike_clip(f"s{minute}", rng.uniform(5, 10), f"creator-{rng.randint(0, 4)}", at=now)
            pending_spikes.append(clip)
            creators[clip.id] = clip.creator_id
        if pending_spikes:
            for plan in plan_spikes(pending_spikes, accounts, posts, now, params):
                for decision in plan.decisions:
                    seen[decision.code] += 1
                    if decision.code is Reason.AT_CAP:
                        state = cap_state(by_id[decision.account_id], posts, now, params)
                        assert state.reserve_left == 0, (
                            "a spike was blocked by the cap while reserve remained"
                        )
                    if decision.may_send:
                        record(decision, now)
                if plan.demote_to_hourly:
                    clip = next(c for c in pending_spikes if c.id == plan.clip_id)
                    pending_spikes.remove(clip)
                    queue.append(replace(clip, lane=Lane.HOURLY, rank_score=demoted_rank(clip.rank_score)))
        if now.replace(second=0) == fire_at(slot, params.jitter_max_s).replace(second=0):
            plan_h = plan_hourly_slot(accounts, posts, queue, slot, now, params, spike_clips=pending_spikes)
            for decision in plan_h.decisions:
                seen[decision.code] += 1
                if decision.may_send:
                    record(decision, now)
    return posts, seen


def assert_system_invariants(accounts: list[Account], posts: list[PostRecord], params: PostingParams) -> None:
    by_id = {a.id: a for a in accounts}
    for account in accounts:
        assert_account_invariants(account, posts, params)
        own = [p for p in posts if p.account_id == account.id]
        hourly = [p for p in own if p.lane is Lane.HOURLY]
        assert len({p.slot_at for p in hourly}) == len(hourly), "two hourly posts in one slot"
        for p in hourly:
            assert p.slot_at is not None and p.slot_at <= p.at < p.slot_at + HOUR
            if account.hourly_hours_utc is not None:
                assert p.slot_at.hour in account.hourly_hours_utc
            window_start = p.slot_at - timedelta(seconds=params.skip_hourly_after_spike_s)
            assert not any(s.lane is Lane.SPIKE and window_start < s.at <= p.at for s in own)  # R3
        for earlier, later in zip(hourly, hourly[1:], strict=False):
            assert earlier.creator_id != later.creator_id  # R9
    seen_on_platform: dict[tuple[str, Platform], str] = {}
    for p in posts:
        key = (p.clip_id, by_id[p.account_id].platform)
        assert seen_on_platform.setdefault(key, p.account_id) == p.account_id  # D42


@pytest.mark.parametrize("seed", range(6))
def test_two_days_on_the_production_seed_keep_every_rule(seed: int) -> None:
    accounts = list(production_accounts().values())
    params = make_params()

    posts, seen = simulate_system(seed, accounts, params)

    assert_system_invariants(accounts, posts, params)
    per_account = Counter(p.account_id for p in posts)
    assert per_account["tiktok_live"] == 0
    assert all(p.lane is Lane.HOURLY for p in posts if p.account_id == "tiktok_main")
    youtube_times = [p.at for p in posts if p.account_id == "youtube_main"]
    assert max_in_any_window(youtube_times, DAY) <= 30  # Upload-Post's limit (the seeded cap is 28)
    assert (
        per_account["instagram_main"] > 30
        and per_account["youtube_main"] > 30
        and per_account["tiktok_main"] > 10
    )
    assert seen[Reason.OK] > 0 and seen[Reason.MIN_GAP_WAIT] > 0


@pytest.mark.parametrize("seed", range(4))
def test_two_days_with_a_second_real_tiktok_account_and_tight_caps(seed: int) -> None:
    accounts = list(production_accounts().values())
    accounts = [
        replace(a, enabled=True, upload_post_user="live-profile", cap_24h=6, spike_reserve=6)
        if a.id == "tiktok_live"
        else replace(a, cap_24h=9, spike_reserve=3)
        if a.id != "tiktok_main"
        else a
        for a in accounts
    ]
    params = make_params(spike_max_per_hour=1)

    posts, seen = simulate_system(100 + seed, accounts, params)

    assert_system_invariants(accounts, posts, params)
    assert seen[Reason.AT_CAP] > 0 or seen[Reason.RESERVED_FOR_SPIKE] > 0
    assert seen[Reason.SPIKE_HOURLY_LIMIT] > 0
    assert any(p.account_id == "tiktok_live" for p in posts)
