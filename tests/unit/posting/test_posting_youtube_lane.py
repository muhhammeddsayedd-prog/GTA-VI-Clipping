"""YouTube lane tuning (migration 0034): the hourly lane's own gap, the 12 YouTube hours and the seeds."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from posting_helpers import T0, acct, make_params, post

from sixstars.posting.caps import hourly_gap_ends_at
from sixstars.posting.models import Account, Action, Lane, Reason
from sixstars.posting.precedence import decide_hourly_account
from sixstars.posting.slots import fire_at, jitter_offset_s

ROOT = Path(__file__).resolve().parents[3]
YT = "youtube_main"
YT_HOURS = frozenset({0, 10, 13, 14, 16, 17, 18, 19, 20, 21, 22, 23})
HOURLY_GAP_S = 3000
# The longest an upload may take before `published_at` is stamped and the next slot must still be free.
MAX_UPLOAD_S = 240
MINUTE = timedelta(minutes=1)
SLOT = T0.replace(hour=13)  # 13:00 UTC, one of the YouTube hours
NOW = SLOT + 2 * MINUTE


def youtube(**changes: object) -> Account:
    """youtube_main as migration 0034 leaves it."""
    base: dict[str, object] = {
        "cap_24h": 16,
        "spike_reserve": 4,
        "hourly_hours_utc": YT_HOURS,
        "hourly_min_gap_s": HOURLY_GAP_S,
    }
    return acct(YT, **{**base, **changes})


# ── the hourly gap ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("ago_s", "code"),
    [
        (49 * 60, Reason.MIN_GAP),  # an hourly post 49 min ago blocks the slot
        (HOURLY_GAP_S - 1, Reason.MIN_GAP),
        (HOURLY_GAP_S, Reason.OK),  # the gap is met exactly
        (51 * 60, Reason.OK),
    ],
)
def test_the_hourly_gap_counts_from_the_last_hourly_post(ago_s: int, code: Reason) -> None:
    history = [post(YT, NOW - timedelta(seconds=ago_s), Lane.HOURLY, slot_at=SLOT - timedelta(hours=1))]

    decision = decide_hourly_account(youtube(), history, SLOT, NOW, make_params())

    assert decision.code is code


def test_a_spike_post_does_not_start_the_hourly_gap() -> None:
    # 35 min before the slot: outside R3 (posting.skip_hourly_after_spike_s, 30 min) and the 900 s global gap,
    # but inside the 50 min hourly gap, which must ignore it.
    history = [post(YT, SLOT - 35 * MINUTE, Lane.SPIKE)]

    decision = decide_hourly_account(youtube(), history, SLOT, NOW, make_params())

    assert (decision.action, decision.code) == (Action.ALLOW, Reason.OK)


def test_r3_and_the_global_gap_still_apply_after_a_spike() -> None:
    history = [post(YT, SLOT - 10 * MINUTE, Lane.SPIKE)]

    decision = decide_hourly_account(youtube(), history, SLOT, NOW, make_params())

    assert decision.code is Reason.SPIKE_RECENT


def test_without_an_hourly_gap_nothing_changes() -> None:
    history = [post(YT, NOW - 20 * MINUTE, Lane.HOURLY, slot_at=SLOT - timedelta(hours=1))]
    account = youtube(hourly_min_gap_s=None)

    assert hourly_gap_ends_at(account, history, dry=False) is None
    assert decide_hourly_account(account, history, SLOT, NOW, make_params()).code is Reason.OK


def test_the_hourly_gap_ignores_skipped_and_other_accounts_posts() -> None:
    history = [
        post(YT, NOW - 10 * MINUTE, Lane.HOURLY, status="skipped"),
        post("instagram_main", NOW - 10 * MINUTE, Lane.HOURLY),
    ]

    assert hourly_gap_ends_at(youtube(), history, dry=False) is None


def test_a_negative_hourly_gap_is_refused() -> None:
    with pytest.raises(ValueError, match="hourly_min_gap_s"):
        youtube(hourly_min_gap_s=-1)


def test_the_youtube_hours_are_the_only_hourly_hours() -> None:
    params = make_params()
    for hour in range(24):
        slot = SLOT.replace(hour=hour)
        decision = decide_hourly_account(youtube(), [], slot, slot + 2 * MINUTE, params)
        expected = Reason.OK if hour in YT_HOURS else Reason.OUTSIDE_HOURLY_HOURS
        assert decision.code is expected, hour


# ── the gap and the jitter fit together ───────────────────────────────────────────────────────────────────
def test_consecutive_hourly_slots_never_block_each_other() -> None:
    """Slots fire 0..posting.jitter_max_s past the hour; the post counts from its publish time, up to
    MAX_UPLOAD_S later. The next slot must still find the 3000 s gap met, for every pair of hours."""
    jitter = make_params().jitter_max_s
    assert 3600 - jitter - MAX_UPLOAD_S >= HOURLY_GAP_S  # the worst case, whatever the hash gives

    start = SLOT.replace(hour=0)
    for n in range(24 * 30):  # 30 days of consecutive slots
        slot = start + timedelta(hours=n)
        nxt = slot + timedelta(hours=1)
        published = fire_at(slot, jitter) + timedelta(seconds=MAX_UPLOAD_S)
        history = [post(YT, published, Lane.HOURLY, slot_at=slot)]
        account = youtube(hourly_hours_utc=None)  # every hour, the hardest case
        gap_end = hourly_gap_ends_at(account, history, dry=False)
        assert gap_end is not None and gap_end <= fire_at(nxt, jitter), slot
        assert 0 <= jitter_offset_s(nxt, jitter) <= jitter


def test_every_youtube_hour_fits_in_the_cap() -> None:
    account = youtube()
    assert account.hourly_hours_utc is not None
    assert len(account.hourly_hours_utc) == account.cap_24h - account.spike_reserve


# ── the seeds: listed hours equal the hourly capacity ─────────────────────────────────────────────────────
_ROW = re.compile(
    r"\(\s*'(?P<id>[^']+)',\s*'[^']+',\s*'[^']+',\s*'[^']+',\s*'\{(?P<lanes>[^}]*)\}',\s*(?P<cap>\d+),\s*"
    r"(?P<reserve>\d+),\s*\d+,\s*(?:'[^']*'|null),\s*(?:'\{(?P<hours>[^}]*)\}'|null),\s*(?P<enabled>true|false)"
    r"(?:,\s*(?P<gap>\d+|null))?\s*\)"
)


def seed_accounts(name: str) -> dict[str, dict[str, object]]:
    text = (ROOT / "supabase" / "seed" / name).read_text(encoding="utf-8")
    rows = {}
    for match in _ROW.finditer(text):
        hours = match["hours"]
        rows[match["id"]] = {
            "lanes": {lane.strip() for lane in match["lanes"].split(",") if lane.strip()},
            "cap": int(match["cap"]),
            "reserve": int(match["reserve"]),
            "hours": None if hours is None else {int(h) for h in hours.split(",")},
            "gap": None if match["gap"] in (None, "null") else int(match["gap"]),
        }
    return rows


@pytest.mark.parametrize("seed", ["production.sql", "staging.sql"])
def test_seeded_hours_equal_the_hourly_capacity(seed: str) -> None:
    rows = seed_accounts(seed)
    assert {"tiktok_main", "tiktok_live", "instagram_main", YT} <= set(rows)
    for account_id, row in rows.items():
        hours = row["hours"]
        if hours is None or "hourly" not in row["lanes"]:  # type: ignore[operator]
            continue
        assert isinstance(hours, set)
        assert len(hours) == row["cap"] - row["reserve"], account_id  # type: ignore[operator]


@pytest.mark.parametrize("seed", ["production.sql", "staging.sql"])
def test_the_seeds_carry_the_youtube_tuning(seed: str) -> None:
    row = seed_accounts(seed)[YT]
    assert (row["cap"], row["reserve"], row["hours"], row["gap"]) == (16, 4, set(YT_HOURS), HOURLY_GAP_S)
