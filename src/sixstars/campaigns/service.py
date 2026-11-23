"""The one write path of the campaign registry (D117). Every change, from the CLI, the admin console or a
Telegram command, goes through `apply_change`: it checks the change (`rules.validate`), saves it with a
new version number, rewrites the campaign's lane quotas, moves its streamers in or out of tier `campaign`,
writes a `campaign.changed` audit event and sends one Telegram line. The engine reads only saved, checked
campaigns.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sixstars import db, events, notify
from sixstars.campaigns import rules, store
from sixstars.campaigns.models import COLUMN_FIELDS, Campaign, PlatformQuota, Status
from sixstars.logging import get_logger

_log = get_logger("campaigns")

# Fields a change may set (everything on Campaign except identity and bookkeeping).
EDITABLE = frozenset({*COLUMN_FIELDS, "creator_ids"})


STOP_FIELDS = frozenset({"status", "end_reason"})


class CampaignError(Exception):
    """A change was refused; `problems` says why (shown to whoever asked)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _now() -> datetime:
    return datetime.now(UTC)


def _build(slug: str, old: Campaign | None, changes: Mapping[str, Any]) -> Campaign:
    unknown = set(changes) - EDITABLE
    if unknown:
        raise CampaignError([f"unknown field(s): {', '.join(sorted(unknown))}"])
    fields = dict(changes)
    if "quota" in fields and not isinstance(next(iter(fields["quota"].values()), None), PlatformQuota):
        try:
            fields["quota"] = rules.parse_quota(fields["quota"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CampaignError([f"quota is not valid: {exc}"]) from exc
    for name in ("platforms", "creator_ids"):
        if name in fields:
            fields[name] = tuple(dict.fromkeys(fields[name]))
    if old is None:
        missing = [name for name in ("name", "rate_per_1k") if name not in fields]
        if missing:
            raise CampaignError([f"a new campaign needs {', '.join(missing)}"])
        return Campaign(slug=slug, **fields)
    return replace(old, **fields)


def apply_change(
    slug: str, changes: Mapping[str, Any], actor: str, reason: str | None = None, *, create: bool = False
) -> Campaign:
    """Check and save one change. `create=True` makes a new campaign (always as a draft first, unless the
    change sets a status). Raises CampaignError with every problem when the change is refused."""
    now = _now()
    with db.transaction():
        old = store.load(slug, lock=True)
        if create and old is not None:
            raise CampaignError([f"campaign {slug} already exists"])
        if not create and old is None:
            raise CampaignError([f"campaign {slug} does not exist"])
        new = _build(slug, old, changes)
        if old is not None:
            error = rules.transition_error(old.status, new.status)
            if error:
                raise CampaignError([error])
        changed = rules.diff(old, new)
        # Pausing or ending must always work (the guardian relies on it), so a bare status change to
        # paused/ended skips the checks a changed world (a banned streamer, a lowered share) would fail.
        stopping = old is not None and new.status in ("paused", "ended") and set(changed) <= STOP_FIELDS
        problems = [] if stopping else rules.validate(new, store.context(new, now))
        if problems:
            raise CampaignError(problems)
        if old is not None and not changed:
            return old
        stored = store.save(new, old)
        ctx = store.context(stored, now)
        store.write_lane_rows(stored, ctx.accounts)
        store.refresh_base_rows()
        tier_moves = sync_tiers({*stored.creator_ids, *(old.creator_ids if old else ())}, stored.slug)
        events.emit(
            "campaign.changed",
            level="warn" if stored.status in ("paused", "ended") and changed.get("status") else "info",
            entity_type="campaign",
            entity_id=stored.id,
            message=f"campaign {stored.slug} v{stored.version} by {actor}: {', '.join(sorted(changed))}",
            data={
                "actor": actor,
                "reason": reason,
                "version": stored.version,
                "changes": changed,
                "tiers": tier_moves,
            },
            service="campaigns",
        )
    _log.info(
        "campaign_changed", slug=stored.slug, version=stored.version, actor=actor, fields=sorted(changed)
    )
    notify.send("info", f"Campaign {stored.slug}: {_summary(changed)}", _body(stored, actor, reason))
    return stored


def set_status(slug: str, status: Status, actor: str, reason: str | None = None) -> Campaign:
    changes: dict[str, Any] = {"status": status}
    if status == "ended":
        changes["end_reason"] = reason
    return apply_change(slug, changes, actor, reason)


def sync_tiers(creator_ids: set[str] | frozenset[str], slug: str) -> list[dict[str, str]]:
    """Move each streamer in or out of tier `campaign` to match whether any of their campaigns is active.
    Returns the moves made (for the audit event)."""
    moves: list[dict[str, str]] = []
    for row in store.creator_tiers(creator_ids):
        tier, before = rules.tier_for(row["tier"], row["tier_before_campaign"], bool(row["has_active"]))
        if tier == row["tier"] and before == row["tier_before_campaign"]:
            continue
        store.set_tier(row["id"], tier, before, slug)
        events.emit(
            "creator.tier_changed",
            entity_type="creator",
            entity_id=row["id"],
            message=f"{row['platform']} {row['handle']}: {row['tier']} -> {tier} (campaign {slug})",
            data={"old": row["tier"], "new": tier, "campaign": slug},
            service="campaigns",
        )
        moves.append({"creator": f"{row['platform']}:{row['handle']}", "old": row["tier"], "new": tier})
    return moves


def on_creator_forbidden(creator_id: str, reason: str) -> list[str]:
    """A streamer became restricted or blocked: pause every active campaign they are in (the guardian of
    D117). Returns the slugs paused. Never raises: a failure is logged and the caller carries on."""
    paused: list[str] = []
    for slug in store.active_campaigns_of_creator(creator_id):
        try:
            apply_change(slug, {"status": "paused"}, actor="system", reason=f"streamer forbidden: {reason}")
            paused.append(slug)
        except CampaignError as exc:
            _log.warning("campaign_pause_failed", slug=slug, problems=exc.problems)
    if paused:
        notify.send("warn", "Campaign paused: streamer forbids clipping", f"{', '.join(paused)}: {reason}")
    return paused


def _summary(changed: Mapping[str, list[Any]]) -> str:
    if "status" in changed:
        old, new = changed["status"]
        return f"{old or 'new'} -> {new}"
    return "edited " + ", ".join(sorted(changed))


def _body(c: Campaign, actor: str, reason: str | None) -> str:
    quota = ", ".join(f"{p} {q.min}-{q.max}/day" for p, q in sorted(c.quota.items()))
    lines = [f"{c.name} ({c.status}, v{c.version}) by {actor}", f"Quota: {quota or 'none'}"]
    if c.budget_left is not None:
        lines.append(f"Budget left: {c.budget_left:g} {c.currency}")
    if reason:
        lines.append(f"Reason: {reason}")
    return "\n".join(lines)
