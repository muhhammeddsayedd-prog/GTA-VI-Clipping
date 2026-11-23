# CLAUDE.md — Standing instructions for Claude Code

You are the main coding agent for **6 Stars Clips**, a fully automated GTA 6 clipping system. Read this file at the start of every session.

## Project in one paragraph

A Python system running on a Hetzner server finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat activity spikes (live and in replays), renders branded 9:16 clips with English subtitles, approves them through an automatic quality gate, and posts them to TikTok (2 accounts), Instagram Reels and YouTube Shorts through the Upload-Post API. Spike clips post immediately; a fresh clip posts every hour on every platform. n8n (on a separate VPS) runs schedules, Gmail takedown intake and Telegram notifications. Supabase (Postgres + Storage) holds all state. There is no human in the loop; Telegram is notifications only.

## How to work

1. **Work one milestone at a time** from `development-plan/`, in order (M00, M01, …). Read the whole milestone file before writing code. Each milestone file contains the context you need; the `docs/` folder has deeper reference if something is unclear.
2. **Do not start a milestone until the previous one's definition of done is met.** If a previous milestone left something unfinished, finish it first and note it.
3. **At the end of every milestone**, update `development-plan/PROGRESS.md` (create it in M00) with: milestone ID, date, what was built, deviations from the plan, open issues, and anything the next milestone must know.
4. **Never invent API details.** When a milestone uses an external API (Upload-Post, Twitch, YouTube, Apify, Anthropic, Groq), check the current official docs before coding the client. `docs/14-api-integrations.md` lists the docs URLs and the facts already verified. If the docs disagree with this kit, follow the docs and record the difference in PROGRESS.md.
5. **Ask before** anything irreversible on real accounts (posting publicly, deleting posts, changing production settings). In staging, posts go to private test accounts only.

## Non-negotiable rules

- Python 3.12, managed with `uv`. Package name `sixstars`, source in `src/sixstars/`.
- All configuration from environment variables (`.env`, see `config/.env.example`) plus the `settings` table in Postgres. **No hard-coded thresholds** — every number in the docs that is marked as a setting must be read from `settings`.
- Every external call goes through a client in `src/sixstars/clients/` with timeouts, retries (exponential backoff, max 3) and structured logging. Every client has a fake/mock for tests.
- All database access through `src/sixstars/db.py` (psycopg 3, connection pool). Use SQL, not an ORM. Migrations live in `supabase/migrations/` and are applied with the Supabase CLI.
- Background work goes through the `jobs` table (Postgres queue using `FOR UPDATE SKIP LOCKED`). No Celery, no Redis.
- Log as JSON (structlog). Every log line carries `env`, `service`, and where relevant `clip_id`, `spike_id`, `job_id`.
- Every important state change writes a row to `events`. Anything a human would want to know goes to Telegram through the n8n notify webhook (`sixstars.notify`).
- Respect `settings.emergency_stop` and `settings.dry_run` everywhere posting can happen.
- Times are UTC everywhere, stored as `timestamptz`.
- Type hints everywhere; `ruff` and `mypy --strict` on `src/` must pass; tests with `pytest`. CI runs all three.
- Secrets never in code, logs or the database.
- Keep functions small and testable. Pure logic (spike scoring, precedence rules, cap math, window maths) must have unit tests with no network.

## Environments

- `APP_ENV=staging`: GTA 6 content (`settings.target_game = "Grand Theft Auto VI"`; GTA 5 was removed everywhere on 2026-11-18), private test accounts, separate Supabase project, staging server.
- `APP_ENV=production`: GTA 6 content, real accounts, production Supabase project, production server.
- Branch `develop` deploys to staging; branch `main` deploys to production. Merge `develop` → `main` only after the staging acceptance tests pass.

## Key documents

| Need | Read |
|---|---|
| Why a decision was made | `docs/00-decisions.md` |
| What the product must do | `docs/01-prd.md` |
| How the parts fit | `docs/02-architecture.md` |
| Repo layout and standards | `docs/03-repo-and-stack.md` |
| Database | `docs/04-database.md`, `database/migrations/` |
| Any component | `docs/05` – `docs/12` |
| External APIs | `docs/14-api-integrations.md` |
| Prompts | `docs/15-claude-prompts.md` |
| Settings | `docs/16-settings-reference.md` |

## Brand

Name **6 Stars Clips**. Handles: `@6starsclips` (main), `@6starsclips.live` (TikTok spike account). English only, global audience. Branding is minimal: content first.
