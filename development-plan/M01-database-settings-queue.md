# M01 — Database, Settings, Job Queue, Events, Notify Outbox

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Mon 23:00–Tue 00:30 UTC · **Requires:** M00 · **Launch-critical:** yes

## Goal
The persistence foundation: schema applied to both Supabase projects, a DB access layer, cached runtime settings, the Postgres job queue with workers' claim/complete/fail/retry, the event log and the notification outbox.

## Context you need
- Schema: `supabase/migrations/0001_init.sql` (already written — read it fully). Tables: settings, creators, creator_layouts, candidates, live_sessions, videos, chat_files, spikes, clips, platform_accounts, posts, account_stats, takedowns, jobs, events, notify_outbox, api_usage, heartbeats; function `claim_job(queue, worker)`; sequence `clip_series_seq`; views `v_posts_last_24h`, `v_queue_depth`; private bucket `clips`.
- Default settings: `supabase/migrations/0002_seed_settings.sql` (all runtime keys; reference `docs/16-settings-reference.md`).
- Environment seeds: `supabase/seed/production.sql`, `supabase/seed/staging.sql` (posting accounts + staging overrides). Run each only on its own project.
- Connection: `DATABASE_URL` (Supabase session pooler, port 5432). RLS is on everywhere with no policies: only server credentials work.
- Job queue rules: queues `express` and `normal`; claim ordered by `priority desc, run_after, id` with `FOR UPDATE SKIP LOCKED`; retries with backoff 30 s, 120 s, 600 s; `max_attempts` default 3; unique `dedupe_key` among queued/running jobs.
- Notify: `notify.send(severity, title, body)` inserts into `notify_outbox` and POSTs JSON `{severity,title,body,env,ts}` to `N8N_NOTIFY_WEBHOOK_URL` with header `X-6Stars-Secret: <N8N_NOTIFY_SECRET>`; marks `sent_at` on 2xx. Unsent rows are retried by the scheduler (M13). Rate-limit `warn` alerts: max one per `title` per 30 minutes (check outbox history).

## Build
1. `supabase/config.toml` via `supabase init` (keep migrations as provided). Document in PROGRESS.md the exact commands used to link and push to both projects.
2. Apply migrations to **staging** and **production** projects; run `supabase/seed/staging.sql` on staging and `supabase/seed/production.sql` on production. Verify bucket `clips` exists in both.
3. `db.py`: `ConnectionPool` (min 1, max `DB_POOL_MAX`, `kwargs={"autocommit": True}`), helpers `fetch_one(sql, params)`, `fetch_all`, `execute`, `transaction()` context manager, `row_factory=dict_row`. Health check `ping()`.
4. `settings.py`: `get(key, default=None)` with a 30-second in-process cache of the whole table; `set(key, value)`; typed helpers `get_bool/get_int/get_float/get_dict/get_list/get_dt`. Unknown key without default → `KeyError` (catches typos).
5. `jobs.py`: `enqueue(queue, type, payload, priority=0, run_after=None, dedupe_key=None, max_attempts=3) -> int | None` (returns None if dedupe conflict), `claim(queue, worker_name) -> Job | None` (calls `claim_job`), `complete(job_id)`, `fail(job_id, error, retry=True)` (re-queue with backoff if attempts < max, else `failed`), `cancel(dedupe_key)`. `Job` dataclass. A generic `run_worker(queue, handlers: dict[str, Callable[[Job], None]], stop_event)` loop: claim → dispatch by `type` → complete/fail; sleep 1 s when idle; heartbeat every 30 s to `heartbeats` (`name = worker-<queue>-<hostname>-<pid>`).
6. `events.py`: `emit(type, level="info", entity_type=None, entity_id=None, message=None, data=None, service=None)`.
7. `notify.py`: as described above, with httpx timeout 10 s; failures never raise to the caller (log + leave unsent).
8. `heartbeat(name, info=None)` helper in `db.py` or `events.py`.
9. CLI: `sixstars settings get <key>` / `set <key> '<json>'`; `sixstars db ping`.
10. CI: add a `postgres:16` service; apply `0001`, `0002` with `psql` (stub `storage.buckets` by creating schema `storage` and table `buckets(id text primary key, name text, public bool)` before applying, since plain Postgres has no Supabase storage schema); run `tests/integration`.

## Acceptance tests
- Integration: enqueue 3 jobs with priorities → claimed in priority order; two concurrent claimers never get the same job (use two connections/threads).
- Dedupe: second enqueue with the same `dedupe_key` returns None while the first is queued; succeeds after the first completes.
- Fail with retry → `run_after` moves by 30 s, then 120 s; after 3 attempts → `failed`.
- Settings: `get("spikes.lead_s") == 20`; `set` then `get` returns the new value after cache expiry; unknown key raises.
- Notify: with a respx-mocked webhook, row inserted and `sent_at` set; with webhook down, row stays unsent and no exception.
- `sixstars db ping` works against staging.

## Definition of done
Both Supabase projects migrated and seeded; layer modules with tests; CI green; PROGRESS.md updated with project refs (not secrets).

## Handoff to M02
Every client in M02 records cost/usage in `api_usage` and uses `settings` for model names and budgets.
