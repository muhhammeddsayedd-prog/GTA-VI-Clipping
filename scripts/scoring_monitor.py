"""Rollback-trigger monitor for the scoring overhaul (docs/CHANGELOG-scoring.md).

    uv run python scripts/scoring_monitor.py <env> [--since ISO] [--minutes 30] [--before-minutes 180]

Compares the window since the change (`--since`, default `--minutes` ago) with the same-length average of
the `--before-minutes` before it, per hour:
  spikes -> confirmed -> clips -> rendered -> approved -> posted, error events, failed jobs,
  AI spend, and the share of new approvals that carry a safety, spoiler or copyright flag.
Prints one JSON object. Reads the database only. n8n failed executions are checked separately.
"""

import argparse
import json
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]

SQL = """
with w as (select %(a)s::timestamptz a, %(b)s::timestamptz b)
select
 (select count(*) from spikes, w where detected_at >= a and detected_at < b) spikes,
 (select count(*) from events, w where type = 'spike.confirmed'
   and created_at >= a and created_at < b) confirmed,
 (select count(*) from clips, w where created_at >= a and created_at < b) clips,
 (select count(*) from events, w where type = 'clip.rendered'
   and created_at >= a and created_at < b) rendered,
 (select count(*) from clips, w where approved_at >= a and approved_at < b) approved,
 (select count(*) from posts, w where published_at >= a and published_at < b and not dry_run) posted,
 (select count(*) from events, w where level = 'error' and created_at >= a and created_at < b) errors,
 (select count(*) from jobs, w where status = 'failed' and updated_at >= a and updated_at < b) failed_jobs,
 (select coalesce(sum(cost_usd), 0)::float from api_usage, w
   where provider ilike 'anthropic%%' and created_at >= a and created_at < b) ai_usd,
 (select count(*) from clips, w where approved_at >= a and approved_at < b
   and (spoiler
        or exists (select 1 from jsonb_each(coalesce(safety_flags, '{}'::jsonb)) f where f.value = 'true'::jsonb)
        or exists (select 1 from posts p where p.clip_id = clips.id
                   and (coalesce(p.claim_status, 'ok') not in ('ok', 'unknown') or p.removed_at is not null))
        )) flagged_approvals,
 (select count(*) from scoring_log, w where created_at >= a and created_at < b) scoring_log_rows,
 (select count(*) from scoring_log, w where stage = 'spike.confirm'
   and created_at >= a and created_at < b
   and (sub_scores ->> 'fast_path_overrode_reject')::boolean) fast_path_overrides,
 (select count(*) from scoring_log, w where stage = 'gate' and created_at >= a and created_at < b
   and sub_scores -> 'first_time' ->> 'score_points' is not null
   and (sub_scores -> 'first_time' ->> 'score_points')::float > 0) first_time_boosts
"""


def window(conn: psycopg.Connection[Any], a: datetime, b: datetime) -> dict[str, float]:
    cur = conn.execute(SQL, {"a": a, "b": b})
    assert cur.description is not None
    row = cur.fetchone()
    assert row is not None
    hours = max((b - a).total_seconds() / 3600, 1e-6)
    return {d.name: round(float(v) / hours, 2) for d, v in zip(cur.description, row, strict=True)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("env")
    parser.add_argument("--since")
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--before-minutes", type=float, default=180)
    args = parser.parse_args()
    env_file = ROOT / f".env.{args.env}"
    url = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in env_file.read_text().splitlines()
        if line.startswith("DATABASE_URL=")
    )
    now = datetime.now(UTC)
    since = datetime.fromisoformat(args.since) if args.since else now - timedelta(minutes=args.minutes)
    with psycopg.connect(url) as conn:
        after = window(conn, since, now)
        before = window(conn, since - timedelta(minutes=args.before_minutes), since)
    triggers = {
        "errors_up": after["errors"] + after["failed_jobs"] > before["errors"] + before["failed_jobs"] + 1,
        "approvals_5x": after["approved"] > 5 * max(before["approved"], 1.0),
        "flagged_over_20pct": after["approved"] > 0 and after["flagged_approvals"] / after["approved"] > 0.2,
        "ai_spend_3x": after["ai_usd"] > 3 * max(before["ai_usd"], 0.01),
    }
    print(
        json.dumps(
            {
                "since": since.isoformat(),
                "now": now.isoformat(),
                "per_hour_after": after,
                "per_hour_before": before,
                "triggers": triggers,
                "rollback": any(triggers.values()),
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
