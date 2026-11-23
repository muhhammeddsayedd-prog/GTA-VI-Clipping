"""Implementation of `sixstars campaign …` (D118, PR 1): add, edit, activate, pause, resume, end, budget,
list, show, lookup and quotas. Each returns an exit code; every change goes through
`service.apply_change`, so the CLI can never save a campaign the checks refuse.
"""

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import typer

from sixstars.campaigns import lookup, rules, service, store
from sixstars.campaigns.models import Campaign, Status
from sixstars.clients._http import ClientError
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import YouTubeClient

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_USAGE: Final = 2
ACTOR: Final = "cli"


def parse_time(value: str | None) -> datetime | None:
    """ISO date or date-time; a naive value is read as UTC."""
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_quota_args(values: Sequence[str]) -> dict[str, str]:
    """`youtube=2-3` (repeatable) -> {"youtube": "2-3"}."""
    quota: dict[str, str] = {}
    for value in values:
        platform, sep, rng = value.partition("=")
        if not sep:
            raise ValueError(f"quota {value!r}: use <platform>=<min>-<max>")
        quota[platform.strip().lower()] = rng.strip()
    return quota


def caption_rules(
    tags: Sequence[str],
    mention: str | None,
    cta: str | None,
    banned: Sequence[str],
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rules = dict(base or {})
    if tags:
        rules["required_tags"] = ["#" + tag.strip().removeprefix("#") for tag in tags if tag.strip()]
    if mention is not None:
        rules["mention"] = mention
    if cta is not None:
        rules["cta"] = cta
    if banned:
        rules["banned_words"] = list(banned)
    return rules


def safe_stdout() -> None:
    """Never crash on a console that cannot show a character (an emoji tag on a Windows code page)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")


def _refuse(exc: service.CampaignError) -> int:
    typer.echo("Refused:", err=True)
    for problem in exc.problems:
        typer.echo(f"  - {problem}", err=True)
    return EXIT_REFUSED


def resolve_streamers(refs: Sequence[str]) -> list[str]:
    """Creator ids for `twitch:login` / `youtube:@handle` references, registering new ones; prints each
    account so the person can confirm it is the right streamer."""
    twitch, youtube = TwitchClient(), YouTubeClient()
    ids: list[str] = []
    for ref in refs:
        row, fetched = lookup.resolve(ref, twitch, youtube)
        where = "looked up" if fetched else "known"
        typer.echo(
            f"Streamer ({where}): {row['platform']}:{row['handle']} "
            f"({row.get('display_name') or '-'}, tier {row['tier']}, id {row['id']})"
        )
        ids.append(str(row["id"]))
    return ids


def save(slug: str, changes: dict[str, Any], create: bool, reason: str | None = None) -> int:
    try:
        campaign = service.apply_change(slug, changes, ACTOR, reason, create=create)
    except service.CampaignError as exc:
        return _refuse(exc)
    typer.echo(describe(campaign))
    return EXIT_OK


def changes_from_options(options: Mapping[str, Any], existing: Campaign | None) -> dict[str, Any]:
    """Turn CLI options (None = not given) into a change dict for `apply_change`."""
    changes: dict[str, Any] = {}
    simple = {
        "name": "name",
        "rate": "rate_per_1k",
        "min_payout": "min_payout",
        "max_payout": "max_payout",
        "currency": "currency",
        "budget": "budget_total",
        "budget_left": "budget_left",
        "profile": "render_profile",
        "priority": "priority",
        "submit_url": "submit_url",
        "notes": "notes",
    }
    for option, field in simple.items():
        if options.get(option) is not None:
            changes[field] = options[option]
    if options.get("budget") is not None and options.get("budget_left") is None and existing is None:
        changes["budget_left"] = options["budget"]
    if "budget_left" in changes:
        changes["budget_checked_at"] = datetime.now(UTC)
    if options.get("platforms"):
        changes["platforms"] = [p.strip().lower() for p in str(options["platforms"]).split(",") if p.strip()]
    if options.get("quota"):
        changes["quota"] = parse_quota_args(options["quota"])
    for option, field in (("starts", "starts_at"), ("ends", "ends_at")):
        if options.get(option) is not None:
            changes[field] = parse_time(options[option])
    if options.get("game") is not None:
        changes["required_game"] = None if options["game"].lower() == "any" else options["game"]
    tags, mention, cta, banned = (
        options.get("tag") or [],
        options.get("mention"),
        options.get("cta"),
        options.get("banned") or [],
    )
    if tags or mention is not None or cta is not None or banned:
        changes["caption_rules"] = caption_rules(
            tags, mention, cta, banned, existing.caption_rules if existing else None
        )
    if options.get("brief"):
        changes["brief_rules"] = {
            **(existing.brief_rules if existing else {}),
            **json.loads(options["brief"]),
        }
    if options.get("streamer"):
        changes["creator_ids"] = resolve_streamers(options["streamer"])
    return changes


def add(slug: str, options: Mapping[str, Any]) -> int:
    try:
        changes = changes_from_options(options, None)
    except (ValueError, lookup.StreamerNotFoundError, ClientError) as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    return save(slug, changes, create=True)


def edit(slug: str, options: Mapping[str, Any]) -> int:
    existing = store.load(slug)
    if existing is None:
        typer.echo(f"campaign {slug} does not exist", err=True)
        return EXIT_USAGE
    try:
        changes = changes_from_options(options, existing)
    except (ValueError, lookup.StreamerNotFoundError, ClientError) as exc:
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    if not changes:
        typer.echo("nothing to change", err=True)
        return EXIT_USAGE
    return save(slug, changes, create=False, reason=options.get("reason"))


def set_status(slug: str, status: Status, reason: str | None) -> int:
    try:
        campaign = service.set_status(slug, status, ACTOR, reason)
    except service.CampaignError as exc:
        return _refuse(exc)
    typer.echo(describe(campaign))
    return EXIT_OK


def budget(slug: str, amount: float) -> int:
    return save(
        slug,
        {"budget_left": amount, "budget_checked_at": datetime.now(UTC)},
        create=False,
        reason="budget update",
    )


def list_campaigns(status: str | None) -> int:
    campaigns = store.load_all([status] if status else None)
    if not campaigns:
        typer.echo("No campaigns.")
        return EXIT_OK
    for c in campaigns:
        quota = " ".join(f"{p}:{q.min}-{q.max}" for p, q in sorted(c.quota.items()))
        budget_text = "-" if c.budget_left is None else f"{c.budget_left:g}"
        typer.echo(
            f"{c.slug:<28} {c.status:<7} p{c.priority} {c.rate_per_1k:g}/1k left {budget_text:<8} {quota}"
        )
    return EXIT_OK


def show(slug: str) -> int:
    campaign = store.load(slug)
    if campaign is None:
        typer.echo(f"campaign {slug} does not exist", err=True)
        return EXIT_USAGE
    typer.echo(describe(campaign))
    for row in store.lane_quotas():
        if row["lane_key"] == campaign.lane_key:
            typer.echo(f"  lane {row['account_id']}: {row['min_day']}-{row['max_day']} a day")
    return EXIT_OK


def find(ref: str) -> int:
    try:
        streamer = lookup.find(ref, TwitchClient(), YouTubeClient())
    except (lookup.StreamerNotFoundError, ClientError) as exc:
        typer.echo(str(exc), err=True)
        return EXIT_REFUSED
    typer.echo(streamer.describe())
    if streamer.picture:
        typer.echo(f"  picture: {streamer.picture}")
    return EXIT_OK


def quotas(account: str | None) -> int:
    for row in store.lane_quotas(account):
        typer.echo(f"{row['account_id']:<16} {row['lane_key']:<36} {row['min_day']}-{row['max_day']}")
    return EXIT_OK


def describe(c: Campaign) -> str:
    quota = ", ".join(f"{p} {q.min}-{q.max}/day" for p, q in sorted(c.quota.items())) or "none"
    payout = f"{c.rate_per_1k:g} {c.currency}/1k, min {c.min_payout:g}" + (
        f", max {c.max_payout:g}" if c.max_payout is not None else ""
    )
    lines = [
        f"{c.slug} - {c.name} [{c.status}] v{c.version} priority {c.priority}",
        f"  platforms: {', '.join(c.platforms) or '-'}; quota: {quota}",
        f"  payout: {payout}; budget left: {'-' if c.budget_left is None else f'{c.budget_left:g}'}"
        + (f" of {c.budget_total:g}" if c.budget_total is not None else ""),
        f"  profile: {c.render_profile}; game: {c.required_game or 'any'}; streamers: {len(c.creator_ids)}",
        f"  window: {c.starts_at or '-'} -> {c.ends_at or '-'}",
    ]
    if c.caption_rules:
        lines.append(f"  caption rules: {json.dumps(dict(c.caption_rules), ensure_ascii=False)}")
    if c.brief_rules:
        lines.append(f"  brief rules: {json.dumps(dict(c.brief_rules), ensure_ascii=False)}")
    if c.submit_url:
        lines.append(f"  submit: {c.submit_url}")
    return "\n".join(lines)


# ── render profiles (PR 3) ───────────────────────────────────


def profile_add(
    profile_id: str,
    base: str,
    logo: str | None,
    pos: str | None,
    watermark: bool | None,
    disclosure: str | None,
    disclosure_s: float | None,
) -> int:
    """Copy `base` into a new profile and change what is given; a logo PNG is stored in the row."""
    from pathlib import Path

    from sixstars import db
    from sixstars.campaigns import profiles

    if pos is not None and pos not in profiles.LOGO_POSITIONS:
        typer.echo(f"--pos must be one of {', '.join(profiles.LOGO_POSITIONS)}", err=True)
        return EXIT_USAGE
    if not rules.SLUG_PATTERN.fullmatch(profile_id):
        typer.echo("profile id: use 2-63 lower-case letters, digits and dashes", err=True)
        return EXIT_USAGE
    png = None
    if logo is not None:
        try:
            png = Path(logo).read_bytes()
        except OSError as exc:
            typer.echo(f"cannot read {logo}: {exc}", err=True)
            return EXIT_USAGE
    with db.transaction():
        created = db.fetch_one(
            """
            insert into render_profiles (id, watermark, logo_pos, keep_overlay, disclosure_text, disclosure_s,
                                         description)
            select %s, coalesce(%s, watermark), coalesce(%s, logo_pos), keep_overlay,
                   coalesce(%s, disclosure_text), coalesce(%s, disclosure_s), 'copy of ' || id
            from render_profiles where id = %s
            on conflict (id) do nothing
            returning id
            """,
            (profile_id, watermark, pos, disclosure, disclosure_s, base),
        )
        if created is None:
            typer.echo(f"profile {profile_id} exists already, or {base} does not exist", err=True)
            return EXIT_USAGE
        if png is not None:
            try:
                profiles.save_logo(profile_id, png)
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=EXIT_USAGE) from exc
    return profile_list()


def profile_list() -> int:
    from sixstars import db

    rows = db.fetch_all(
        "select id, watermark, logo_pos, logo_png is not null as has_logo, keep_overlay, disclosure_text, "
        "disclosure_s from render_profiles order by id"
    )
    for row in rows:
        logo = (
            f"logo {row['logo_pos']}" + ("" if row["has_logo"] else " (none uploaded)")
            if row["logo_pos"]
            else "-"
        )
        disclosure = (
            f'"{row["disclosure_text"]}" {float(row["disclosure_s"]):g}s' if row["disclosure_text"] else "-"
        )
        typer.echo(
            f"{row['id']:<28} watermark {'on ' if row['watermark'] else 'off'}  {logo:<26} "
            f"overlay {'kept' if row['keep_overlay'] else '-':<5} disclosure {disclosure}"
        )
    return EXIT_OK
