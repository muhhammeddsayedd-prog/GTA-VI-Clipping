"""The posting gate: the checks that come before any cap or precedence rule, for both lanes.

Order (the first failing check wins):
1. `control.emergency_stop` true: nothing is allowed, not even a dry-run record.
2. The account is disabled.
3. The account is not in the lane.
4. The account is paused (`paused_until` in the future, or exactly now).
5. A TikTok or YouTube account without a valid privacy value (the upload fields could not be built).
6. Dry mode: `launch.dry_run` true or `now < launch.launch_at`. The decision is `DRY_RUN`, never `ALLOW`.
7. Staging safety for real posts: TikTok must be `SELF_ONLY`, YouTube must be `private`, Instagram needs
   `posting.staging_allow_instagram`.
"""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from sixstars.posting.models import Account, Action, Decision, Lane, Platform, Reason, ensure_utc
from sixstars.posting.params import PostingParams

# Upload-Post field values (docs/14): API constants, not thresholds.
VALID_PRIVACY: dict[Platform, frozenset[str]] = {
    Platform.TIKTOK: frozenset(
        {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}
    ),
    Platform.YOUTUBE: frozenset({"public", "unlisted", "private"}),
}
PRIVATE_PRIVACY: dict[Platform, str] = {Platform.TIKTOK: "SELF_ONLY", Platform.YOUTUBE: "private"}


class Mode(StrEnum):
    STOPPED = "stopped"
    DRY = "dry"
    LIVE = "live"


def posting_mode(now: datetime, params: PostingParams) -> tuple[Mode, Reason]:
    """The global mode at `now` and why. At exactly `launch_at` posting is live."""
    now = ensure_utc(now, "now")
    if params.emergency_stop:
        return Mode.STOPPED, Reason.EMERGENCY_STOP
    if params.dry_run:
        return Mode.DRY, Reason.DRY_RUN_SETTING
    if now < params.launch_at:
        return Mode.DRY, Reason.BEFORE_LAUNCH
    return Mode.LIVE, Reason.OK


def is_dry(now: datetime, params: PostingParams) -> bool:
    """True when decisions at `now` are dry runs (also true under emergency stop: nothing is sent)."""
    return posting_mode(now, params)[0] is not Mode.LIVE


def privacy_problem(account: Account) -> str | None:
    """Why the account's privacy value cannot be sent, or None."""
    valid = VALID_PRIVACY.get(account.platform)
    if valid is not None and account.privacy not in valid:
        platform = account.platform.value
        return f"{account.id} has privacy {account.privacy!r}; {platform} needs one of {sorted(valid)}"
    return None


def staging_problem(account: Account, params: PostingParams) -> tuple[Reason, str] | None:
    """Why a real post to `account` is refused in staging, or None (always None in production)."""
    if params.app_env != "staging":
        return None
    private = PRIVATE_PRIVACY.get(account.platform)
    if private is not None:
        if account.privacy != private:
            return (
                Reason.STAGING_PRIVACY_NOT_PRIVATE,
                f"staging may only post to {account.platform.value} with privacy {private!r}; "
                f"{account.id} has {account.privacy!r}",
            )
        return None
    if account.platform is Platform.INSTAGRAM and params.staging_allow_instagram:
        return None
    return (
        Reason.STAGING_INSTAGRAM_NOT_ALLOWED,
        f"{account.platform.value} cannot post privately, so staging does not post to {account.id} "
        "unless posting.staging_allow_instagram is true",
    )


def check_gate(account: Account, lane: Lane, now: datetime, params: PostingParams) -> Decision:
    """Runs the gate for one account and lane.

    Returns a `SKIP` decision, or a passing decision: `ALLOW` (code `OK`) in live mode, `DRY_RUN` (code
    `DRY_RUN_SETTING` or `BEFORE_LAUNCH`) in dry mode. The lane rules run only after a passing decision.
    """
    now = ensure_utc(now, "now")
    mode, mode_reason = posting_mode(now, params)
    dry = mode is not Mode.LIVE

    def skip(code: Reason, reason: str) -> Decision:
        return Decision(Action.SKIP, code, reason, account.id, lane, dry_run=dry)

    if mode is Mode.STOPPED:
        return skip(Reason.EMERGENCY_STOP, "control.emergency_stop is true: nothing posts")
    if not account.enabled:
        return skip(Reason.ACCOUNT_DISABLED, f"{account.id} is disabled")
    if lane not in account.lanes:
        lanes = sorted(item.value for item in account.lanes)
        return skip(Reason.LANE_NOT_ALLOWED, f"{account.id} does not take {lane.value} posts (lanes {lanes})")
    if account.paused_until is not None and account.paused_until >= now:
        return skip(Reason.ACCOUNT_PAUSED, f"{account.id} is paused until {account.paused_until.isoformat()}")
    problem = privacy_problem(account)
    if problem is not None:
        return skip(Reason.PRIVACY_INVALID, problem)
    if mode is Mode.DRY:
        why = (
            "launch.dry_run is true"
            if mode_reason is Reason.DRY_RUN_SETTING
            else f"before launch.launch_at {params.launch_at.isoformat()}"
        )
        return Decision(
            Action.DRY_RUN, mode_reason, f"dry run ({why}): nothing is sent", account.id, lane, dry_run=True
        )
    staging = staging_problem(account, params)
    if staging is not None:
        return skip(*staging)
    return Decision(Action.ALLOW, Reason.OK, "all posting rules passed", account.id, lane)


def duplicate_channels(accounts: Iterable[Account]) -> frozenset[str]:
    """Ids of enabled accounts that share a platform and Upload-Post profile with another enabled account.

    Two such rows post to the same real social account, so their caps and gaps would add up. The planners
    refuse all of them (`DUPLICATE_CHANNEL`) until one row is disabled.
    """
    by_channel: dict[tuple[Platform, str], list[str]] = {}
    for account in accounts:
        if account.enabled:
            by_channel.setdefault(account.channel, []).append(account.id)
    return frozenset(i for ids in by_channel.values() if len(ids) > 1 for i in ids)
