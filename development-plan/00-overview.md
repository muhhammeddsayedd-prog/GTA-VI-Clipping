# Development Plan — Overview

17 milestones, built in order by Claude Code. Each milestone file is self-contained: it repeats the context, rules, data contracts and acceptance tests it needs. `docs/` holds deeper reference if needed.

## How Claude Code should run a milestone
1. Read `CLAUDE.md`, `development-plan/PROGRESS.md` (from M00 on) and the milestone file in full.
2. Check the "Requires" list is done (look at PROGRESS.md and the repo).
3. Implement the "Build" section in order, writing tests alongside.
4. Run the milestone's acceptance tests. Fix until all pass.
5. Update PROGRESS.md (what was built, deviations, open issues, notes for the next milestone).
6. Commit on a branch `mNN-<slug>`, open a PR to `develop`, merge when CI is green (from M11 on, merging deploys to staging).

## Milestone map

| ID | Milestone | Requires | Launch-critical | Planned window (UTC) |
|---|---|---|---|---|
| M00 | Repo bootstrap, tooling, config, logging | — | yes | Mon 22:00 – 23:00 |
| M01 | Database, settings, job queue, events, notify outbox | M00 | yes | Mon 23:00 – Tue 00:30 |
| M02 | External API clients + fakes | M01 | yes | Tue 00:30 – 02:30 |
| M03 | Chat ingestion (live + replay adapters) | M02 | yes | Tue 02:30 – 04:00 |
| M04 | Chat-spike engine | M03 | yes | Tue 04:00 – 06:00 |
| M05 | AI layer (prompts, schemas, cost tracking) | M02 | yes | Tue 06:00 – 07:30 |
| M06 | Clip factory (render pipeline) | M05 | yes | Tue 07:30 – 11:30 |
| M07 | Quality gate + YouTube pre-check | M06 | yes | Tue 11:30 – 13:30 |
| M08 | VOD lane (chat-first + fallback) | M04, M07 | yes | Tue 13:30 – 15:30 |
| M09 | Discovery engine + creator tiers | M08 | yes | Tue 15:30 – 17:00 |
| M10 | Posting engine (caps, precedence, lanes, publisher) | M07 | yes | Tue 17:00 – 19:30 |
| M11 | Control API, Docker, CI/CD, staging deploy | M10 | yes | Tue 19:30 – 21:30 |
| M12 | n8n workflows + Telegram | M11 | yes | Tue 21:30 – 23:00 |
| M13 | Guardian, stats, daily report, failover | M12 | yes | Tue 23:00 – Wed 01:30 |
| M14 | Live lane + express path | M13 | **can join after launch** | Wed 01:30 – 05:00 |
| M15 | Staging acceptance (end-to-end) | M13 (+M14 if ready) | yes | Wed 05:00 – 07:00 |
| M16 | Production deploy, dry run, launch | M15 | yes | Wed 07:00 – 11:00 |

**This is a 37-hour schedule with no slack.** If time runs short, M14 moves after launch (the hourly lane and VOD lane carry launch day) and M15 runs without the live checks (A2–A4). Everything else is required at 11:00 UTC.

The owner should complete `docs/22-account-setup-guide.md` in parallel with M00–M02 so keys exist by M02's integration checks.

## Dependency graph

```
M00 → M01 → M02 → M03 → M04 ─┐
              └──→ M05 → M06 → M07 → M08 → M09
                              └────→ M10 → M11 → M12 → M13 → M14 → M15 → M16
```
(M08 needs M04 and M07; M10 needs M07.)

## Global conventions (repeated in every milestone)
- Python 3.12, `uv`, package `sixstars` in `src/sixstars/`.
- Settings from the `settings` table via `sixstars.settings.get("key")`; env from `sixstars.config.env`.
- DB via `sixstars.db` (psycopg 3 pool), raw SQL, parameterised.
- Jobs via `sixstars.jobs` (Postgres queue, `claim_job()`).
- Events via `sixstars.events.emit(type, …)`; alerts via `sixstars.notify.send(severity, title, body)`.
- All external calls through `sixstars.clients.*` with fakes in `clients/fakes.py`.
- UTC everywhere. ruff + mypy --strict + pytest must pass.
