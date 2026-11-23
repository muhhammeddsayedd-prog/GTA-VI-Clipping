# Progress log

One entry per milestone, newest last. Each entry records: milestone ID, date, what was built, deviations from the plan, open issues, and what the next milestone must know. Orchestration state lives in `development-plan/ORCHESTRATION.md`.

---

## M00 — Repo bootstrap, tooling, config, logging

**Date:** 2026-11-16/17 UTC (finished 2026-11-17 ~00:15 UTC) · **Branch:** `m00-repo-bootstrap`

### What was built

- `pyproject.toml`: project `sixstars` 0.1.0, Python `>=3.12,<3.13`, all runtime dependencies from the milestone file, dev dependency group (`pytest pytest-asyncio respx ruff mypy types-requests`), script `sixstars = "sixstars.cli:app"`, hatchling build, ruff (line length 110, rules E,F,I,B,UP,N,SIM), mypy strict on `src/` with the `pydantic.mypy` plugin, pytest config (`asyncio_mode = "auto"`). `uv.lock` committed.
- Package tree from `docs/03-repo-and-stack.md` §2 under `src/sixstars/`: 93 modules, every stub has a role docstring naming the milestone that implements it; every subpackage has `__init__.py`.
- `src/sixstars/config.py`: `Env(BaseSettings)` with every variable in `config/.env.example`, cached accessor `env()`, uncached `load_env()`, `Env.require(name)` for call-time checks (raises `MissingConfigError`).
- `src/sixstars/logging.py`: structlog JSON to stdout with `ts` (ISO UTC), `level`, `event`, `env`, `service`; `get_logger(service)`, `bind_context(**kw)`, `clear_context()`, `configure_logging(level)`.
- `src/sixstars/cli.py`: typer app with `version` plus placeholders `api`, `live-manager`, `worker --queue`, `scheduler`, `discovery run-once`, `vod process <url>`, `spikes scan <chat.json>`, `clip render <spike_id>`, `post hourly --dry-run`, `settings get/set`.
- Tests: `tests/unit/test_smoke.py`, `test_config.py`, `test_logging.py` (25 tests), `tests/conftest.py`; empty `tests/integration/` and `tests/fixtures/`.
- `.github/workflows/ci.yml`: on push and pull request: setup uv, `uv sync --locked --all-extras`, `ruff check .`, `ruff format --check .`, `mypy --strict src/`, `pytest tests/unit`.
- `.gitignore` additions (`data/`, `*.ts`), `.editorconfig`, `.env.example` at the repo root (byte copy of `config/.env.example`, enforced by a test).
- `docs/00-decisions.md`: new section "Decisions made during the build" with D100–D105.

### Local results

`uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src/`, `uv run pytest` (25 passed) all green on Windows, Python 3.12. `uv run sixstars version` prints `sixstars 0.1.0 (env=dev)` when no `.env` is loaded.

### Deviations from the milestone file

1. **`.env` is loaded automatically.** `Env` reads process environment variables first, then a dotenv file: `.env` in the working directory, or the path in `SIXSTARS_ENV_FILE` (empty string disables file loading). On a machine with the staging `.env` present, `uv run sixstars version` therefore prints `env=staging`; the acceptance output `env=dev` is what a checkout without `.env` prints (CI, or `SIXSTARS_ENV_FILE= uv run sixstars version`).
2. **Almost every value is optional.** Only `APP_ENV`, `SERVICE_NAME`, `LOG_LEVEL`, `DATA_DIR`, `DB_POOL_MAX`, `APIFY_YOUTUBE_ACTOR` and `TWITCH_DOWNLOADER_CLI` have defaults; everything else is `None` when unset or empty. Empty values count as unset (`env_ignore_empty`), unknown keys are ignored (`extra="ignore"`). `SERVICE_NAME` defaults to `sixstars`.
3. **Types.** `DATABASE_URL` and `PROXY_URL` are `SecretStr` (both embed credentials) rather than plain `str`. `YOUTUBE_COOKIES_FILE` and `TWITCH_DOWNLOADER_CLI` are `Path`. `TWITCH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_ID` are plain `str`.
4. **Placeholder commands exit with code 1** after printing "not implemented yet", so a stub is never mistaken for a successful run.
5. **Dev tools are a PEP 735 dependency group** (`[dependency-groups] dev`), which `uv sync` installs by default, not an optional extra. There are no extras yet, so `--all-extras` in CI is a no-op kept for when a `media` extra appears.
6. **`faster-whisper` and `panns-inference` stayed in the main dependencies**: they installed without trouble on the dev machine, so no `media` extra was needed.
7. **Ruff excludes `*.md` and `.claude/`.** Ruff 0.17 formats Python code blocks inside Markdown and wanted to rewrite `development-plan/M04-chat-spike-engine.md` and `docs/05-chat-spike-engine.md`.
8. **Not created in M00** (they belong to later milestones): `docker/`, the deploy workflows, `ai/prompts/*.md`, `api/routes_*.py`, `assets/fonts|sfx|brand`.
9. **Committed on `m00-repo-bootstrap`, not `develop`**: the orchestrator merges.

### Open issues

- **CI has not run on GitHub.** The branch was not pushed (the orchestrator does that), so the "CI passes on GitHub" acceptance test is unverified. The workflow uses `astral-sh/setup-uv@v6` and `actions/checkout@v4`; uv installs Python 3.12 from `requires-python`.
- **Linux install size is unverified.** `panns-inference` pulls in PyTorch. Whether the locked Linux wheel is the CPU build or the much larger CUDA build was not checked; M11 should check the image size and, if needed, pin `torch` to the PyTorch CPU index through `[tool.uv.sources]`.
- `development-plan/ORCHESTRATION.md` is untracked and was left that way (it is the orchestrator's file and names project refs and n8n ids; the repo is public).

### Notes for M01

- Read config with `from sixstars.config import env`; get a required value with `env().require("DATABASE_URL")` (returns the plain string, raises `MissingConfigError` if unset). `SecretStr` fields need `.get_secret_value()` if read directly. Never log either.
- Tests: `tests/conftest.py` has an autouse fixture that disables dotenv loading, deletes every `Env` variable from the process environment and clears the `env()` cache. Integration tests that need a real `DATABASE_URL` must set it inside the test or fixture with `monkeypatch.setenv` (from a separately named variable such as `TEST_DATABASE_URL`), then call `config.env.cache_clear()`.
- The local `.env` has extra keys that are not `Env` fields and are ignored: `SUPABASE_PROJECT_REF`, `SUPABASE_DB_PASSWORD`, `SSH_HOST`, `SSH_USER`, `SSH_KEY_PATH`, `UPLOAD_POST_PROFILE`. If code needs one of them (M10 will need the Upload-Post profile name), add it to `Env` **and** to `config/.env.example` and the root `.env.example`: `test_env_example_matches_env_fields` and `test_root_env_example_is_a_copy` fail otherwise.
- Empty in the staging `.env` today (so `None`): `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `GROQ_API_KEY`, `YOUTUBE_OAUTH_REFRESH_TOKEN`, `PROXY_URL`.
- Logging: `get_logger("worker")` then `log.info("event_name", key=value)`; call `bind_context(job_id=...)` at the start of a job and `clear_context()` at the end. The module is named `sixstars.logging`; inside it the standard library module is imported as `stdlib_logging`.
- `db.py`, `settings.py`, `jobs.py`, `events.py`, `notify.py` and `services/worker.py` exist as docstring-only stubs: replace their contents, keep the docstrings.
- CI runs `pytest tests/unit` only; M01 adds the Postgres service and the integration test step.
- Stub modules must stay importable under `mypy --strict`; third-party packages without type stubs (for example `chat_downloader`, `panns_inference`) will need a `[[tool.mypy.overrides]]` entry with `ignore_missing_imports` when first imported.

---

## M01 — Database, settings, job queue, events, notify outbox

**Date:** 2026-11-17 UTC (finished ~01:15 UTC) · **Branch:** `m01-database-settings-queue` (from `m00-repo-bootstrap`)

### What was built

- `supabase/config.toml` and `supabase/.gitignore` from `supabase init` (CLI 3.2.0 through `npx supabase@latest`); `project_id = "6stars-clips"`. Migrations kept as provided.
- **Both Supabase projects migrated and seeded** (Postgres 17.12, eu-west-1):
  - staging `tyhejpxcfgyvhwkrrojq`: `0001`, `0002`, then `supabase/seed/staging.sql`;
  - production `silojdzyqtrifzfuokml`: `0001`, `0002`, then `supabase/seed/production.sql` (additive only).
  - Verified on each: 18 public tables, all with RLS on; views `v_posts_last_24h`, `v_queue_depth`; function `claim_job`; sequence `clip_series_seq`; 109 settings rows; 4 `platform_accounts` rows; private bucket `clips` (created by the insert at the end of `0001`); migration history `0001`, `0002`.
  - Staging: `env.target_game = "Grand Theft Auto V"`, `launch.dry_run = true`, `launch.launch_at = 2026-11-17T00:00:00Z`, `live.max_slots = 3`, `ai.daily_budget_usd = 5`; TikTok rows `SELF_ONLY`, YouTube `private`.
  - Production: `launch.dry_run = true`, `control.emergency_stop = false`, `launch.launch_at = 2026-11-18T11:00:00Z`; TikTok `PUBLIC_TO_EVERYONE`, YouTube `public`.
- **Seeds follow D101/D102**: one Upload-Post profile `default` in both environments, handles TikTok `@6starsclipping`, Instagram `@6starsclippy`, YouTube `@6starsclips-m3x`; `tiktok_live` has `enabled = false` in both. Seeds stay idempotent (`on conflict (id) do nothing`, plain `update` statements).
- `src/sixstars/db.py`: lazy `ConnectionPool` (min 1, max `DB_POOL_MAX`, autocommit, 15 s connect timeout, connection check on checkout), `fetch_one`, `fetch_all`, `execute`, `transaction()`, `ping()`, `heartbeat()`, `close_pool()`.
- `src/sixstars/settings.py`: 30-second whole-table cache, `get`, `set`, `invalidate`, `get_bool/get_int/get_float/get_str/get_dict/get_list/get_dt`.
- `src/sixstars/jobs.py`: `Job` dataclass, `enqueue`, `claim`, `get`, `complete`, `fail`, `cancel`, `worker_name`, `run_worker`.
- `src/sixstars/events.py`: `emit`. `src/sixstars/notify.py`: `send`, `retry_unsent`.
- `src/sixstars/services/worker.py`: `register(job_type, handler)`, `run(queue)` with SIGINT/SIGTERM handling.
- CLI: `sixstars settings get <key>`, `sixstars settings set <key> '<json>'`, `sixstars db ping`, `sixstars worker --queue express|normal` (runs, with no handlers registered yet).
- Tests: `tests/integration/` (db, settings, jobs, events, notify, CLI; they need a throwaway Postgres) and `tests/unit/test_settings_cache.py` (no network). 83 tests in total: 41 unit (19 new) and 42 integration; without `TEST_DATABASE_URL` the 42 integration tests are skipped.
- CI: `postgres:17` service container, migrations applied with `psql`, then `pytest tests/integration`.
- `assets/profanity.txt`: standard English slur list under the "Slurs" comment (owner approved at intake). The `# Slurs:` marker line is unchanged, so a parser can use it to split the two sections.
- `.gitignore`: `.claude/worktrees/`.

### Exact Supabase commands

Run from the repo root in Git Bash. `SUPABASE_ACCESS_TOKEN` comes from `.env.shared`; `SUPABASE_DB_PASSWORD` from `.env.staging` or `.env.production`. Both are exported as environment variables and never passed on the command line.

```bash
npx supabase@latest init
# staging
export SUPABASE_ACCESS_TOKEN=...   SUPABASE_DB_PASSWORD=<staging password>
npx supabase@latest link --project-ref tyhejpxcfgyvhwkrrojq
npx supabase@latest db push --remote --dry-run
npx supabase@latest db push --remote --yes
npx supabase@latest db query --remote=tyhejpxcfgyvhwkrrojq --yes -f supabase/seed/staging.sql
# production
export SUPABASE_DB_PASSWORD=<production password>
npx supabase@latest link --project-ref silojdzyqtrifzfuokml
npx supabase@latest db push --remote --dry-run
npx supabase@latest db push --remote --yes
npx supabase@latest db query --remote=silojdzyqtrifzfuokml --yes -f supabase/seed/production.sql
# leave the checkout linked to staging
export SUPABASE_DB_PASSWORD=<staging password>
npx supabase@latest link --project-ref tyhejpxcfgyvhwkrrojq
```

CLI 3.x differs from what `docs/04-database.md` said before: `db push` targets the **local** database unless `--remote` is passed, and hosted changes need `--yes`. `SUPABASE_DB_PASSWORD` is only sent to the linked project; for another ref the CLI falls back to a temporary login role, so link first. The checkout is currently linked to **staging**; a later `db push --remote` goes there. New migrations: `npx supabase@latest migration new <name>`.

### Local results

`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src/` (93 files) and `uv run pytest` all green on Windows. `uv run sixstars db ping` against staging prints `ok (env=staging)`; `uv run sixstars settings get spikes.lead_s` prints `20`.

Local test database:

```bash
docker run -d --name sixstars-test-pg -e POSTGRES_PASSWORD=postgres -p 54329:5432 postgres:17
TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54329/postgres uv run pytest
```

Use `127.0.0.1`, not `localhost`: on this Windows machine with Docker Desktop, a psycopg connection to `localhost` with no connect timeout hung forever (most likely the IPv6 address `::1` is tried first and never answers).

### Deviations from the milestone file

1. **CI uses `postgres:17`, not `postgres:16`**: both Supabase projects run Postgres 17.12.
2. **Integration tests skip when `TEST_DATABASE_URL` is unset** and refuse to run when it points at a Supabase host. The session fixture applies the migrations itself if the schema is missing (it creates the `storage.buckets` stub first); CI applies them with `psql` as the milestone asks.
3. **`settings.get` uses a private sentinel as the default**, so `get(key, None)` returns `None` for an unknown key and only `get(key)` raises `KeyError`. `settings.set` also clears the cache of the calling process; other processes see the change within 30 seconds.
4. **Extra helpers**: `settings.get_str`, `settings.invalidate`, `jobs.get`, `jobs.worker_name`, `notify.retry_unsent`, `db.close_pool`.
5. **`jobs.fail` returns the new status** (`"queued"`, `"failed"`, or `None` when the job was not running). `jobs.cancel` returns the number of cancelled jobs and only cancels **queued** jobs; a running job is not interrupted.
6. **The worker heartbeat runs in a background thread** every 30 s, so a long job does not make the worker look dead to the guardian (`guardian.heartbeat_stale_s` is 300).
7. **Constants that are not settings**: retry backoff (30, 120, 600 s), idle sleep (1 s), heartbeat interval (30 s), settings cache (30 s), notify HTTP timeout (10 s), warn rate limit (30 min). The milestone fixes these numbers and `docs/16-settings-reference.md` has no keys for them.
8. **`notify` calls httpx directly**, not through a client in `clients/`, and does not retry in process: the outbox plus `retry_unsent()` is the retry mechanism, as the milestone describes.
9. **The `worker` CLI command is real** (it was a placeholder); the smoke test list was updated.
10. **Bucket `clips` was not created separately**: the migration creates it and it was verified in both projects (`public = false`).

### Open issues

- **Production `tiktok_main` has `lanes = {hourly}` only and `tiktok_live` is disabled**, so in production no enabled TikTok account accepts spike-lane posts. This follows the M01 brief and the original caps design, but it means TikTok gets no spike clips at launch. If spike posts should go to `@6starsclipping`, run on production: `update platform_accounts set lanes = '{hourly,spike}', spike_reserve = 4 where id = 'tiktok_main';` (staging already has this shape). Decision for the orchestrator or M10.
- **CI has not run on GitHub for this branch** (not pushed). The workflow change is untested there; the same `psql` steps were run by hand against a local `postgres:17`.
- `transcribe.express_provider` is still `"groq"` in both projects although D104 says OpenAI only; M02 or M05 should set it to `"openai"` (settings row, or a new migration).
- `posting.first_comment` and `factory.watermark` still say `@6starsclips`, while the real handles differ per platform (D101). Left unchanged: brand text is the owner's call.
- Whether the Supabase access token in `.env.shared` is a scoped token was not checked.

### Notes for M02

- **Database**: `from sixstars import db`.
  - `db.fetch_one(sql: str, params: Sequence | Mapping | None = None) -> dict[str, Any] | None`
  - `db.fetch_all(sql, params=None) -> list[dict[str, Any]]`
  - `db.execute(sql, params=None) -> int` (affected rows)
  - `db.transaction()`: context manager yielding the connection; the three helpers called inside the block use that transaction; nesting makes a savepoint.
  - `db.ping() -> bool`, `db.heartbeat(name: str, info: Mapping | None = None) -> None`, `db.pool() -> ConnectionPool`, `db.close_pool() -> None`.
  - Placeholders are `%s` or `%(name)s`. Wrap JSON values with `psycopg.types.json.Jsonb(...)`. Python lists adapt to Postgres arrays.
- **Settings**: `from sixstars import settings`.
  - `settings.get(key: str, default=<raise KeyError>) -> Any`, `settings.set(key: str, value: Any, description: str | None = None) -> None`, `settings.invalidate() -> None`
  - `get_bool`, `get_int`, `get_float`, `get_str`, `get_dict`, `get_list` take `(key, default=<raise>)` and raise `TypeError` on a wrong JSON type; `get_dt(key, default=<raise>) -> datetime | None` returns UTC-aware datetimes, and `None` for JSON null.
  - Model names: `settings.get_str("ai.model_writer")`, `settings.get_str("ai.model_fast")`; budgets: `settings.get_float("ai.daily_budget_usd")`, `settings.get_float("transcribe.daily_budget_usd")`.
- **Jobs**: `from sixstars import jobs`.
  - `jobs.enqueue(queue: Literal["express","normal"], type: str, payload: Mapping | None = None, priority: int = 0, run_after: datetime | None = None, dedupe_key: str | None = None, max_attempts: int = 3) -> int | None`
  - `jobs.claim(queue, worker_name: str) -> Job | None`, `jobs.get(job_id: int) -> Job | None`, `jobs.complete(job_id: int) -> None`, `jobs.fail(job_id: int, error: str, retry: bool = True) -> str | None`, `jobs.cancel(dedupe_key: str) -> int`
  - `jobs.run_worker(queue, handlers: Mapping[str, Callable[[Job], None]], stop_event: threading.Event, idle_sleep_s: float = 1.0, heartbeat_interval_s: float = 30.0) -> None`
  - `Job` fields: `id, queue, type, payload, priority, status, attempts, max_attempts, run_after, locked_by, locked_at, last_error, dedupe_key, created_at, updated_at`.
  - Register handlers for the real worker process with `sixstars.services.worker.register(job_type, handler)`. A handler that raises is retried with backoff. To fail without retry, call `jobs.fail(job.id, msg, retry=False)` and return (the `complete` that follows is then a no-op).
- **Events**: `events.emit(type: str, level: Literal["debug","info","warn","error","critical"] = "info", entity_type: str | None = None, entity_id: str | int | None = None, message: str | None = None, data: Mapping | None = None, service: str | None = None) -> int`.
- **Notify**: `notify.send(severity: Literal["info","warn","critical"], title: str, body: str = "") -> bool` (never raises; `warn` is limited to one per title per 30 minutes); `notify.retry_unsent(limit: int = 50) -> int` for the M13 scheduler.
- **`api_usage`** has no helper yet: M02 should add one (for example `usage.record(provider, operation, model=None, input_tokens=None, output_tokens=None, units=None, cost_usd=0, ref_id=None)`) and call it from every client.
- **Tests**: integration tests get the database from the autouse `database` fixture in `tests/integration/conftest.py`; it truncates `jobs`, `events`, `notify_outbox`, `heartbeats` before each test and restores `settings` after it. A test that writes to another table must clean it up (or add the table to `MUTABLE_TABLES`). Test file basenames must be unique across `tests/unit` and `tests/integration` (there are no `__init__.py` files).
- No new environment variables were added. `TEST_DATABASE_URL` is read only by the test fixtures and is not an `Env` field.

---

## M02 — External API clients + fakes

**Date:** 2026-11-17 UTC (finished ~02:15 UTC) · **Branch:** `m02-api-clients` (based on M01)

### What was built

- `src/sixstars/clients/_http.py`: shared timeouts (connect 10 s, read 60 s, uploads 600 s), the retry policy (`send()`: 3 attempts, backoff 2→30 s, only network errors, 429 and 5xx, honours `Retry-After` and `X-RateLimit-Reset`, capped at 120 s), call logging (service, endpoint, status, duration; never query strings, headers or bodies), and the base errors `ClientError`, `NotConfiguredError`, `ApiError`.
- `src/sixstars/usage.py`: `record(...)` writes one `api_usage` row and never raises (no database configured: silently skipped); `spent_today(provider)` sums today's cost (UTC). Every client calls `record`.
- Clients: `twitch.py`, `youtube.py` (+ `youtube_oauth.py` for the local sign-in), `apify.py`, `anthropic_client.py`, `transcribe.py`, `upload_post.py`. Fakes for all six in `fakes.py`.
- CLI: `sixstars youtube auth [--port N] [--no-browser] [--timeout S]` and `sixstars youtube whoami`.
- Migration `supabase/migrations/0003_intake_overrides.sql` (idempotent updates): `transcribe.express_provider = "openai"`; `@6starsclips` → `@6starsclipping` inside `posting.first_comment` and `factory.watermark` (wording kept; a value without the placeholder, such as the staging first comment, is left alone). **Applied to the local test database only. Not pushed to Supabase: the orchestrator does that** (`npx supabase@latest db push --remote --yes` per project).
- Tests: 134 new unit tests (`test_http_retry`, `test_twitch_client`, `test_youtube_client`, `test_apify_client`, `test_anthropic_client`, `test_transcribe_client`, `test_upload_post_client`, `test_fakes`), 9 new integration tests (`tests/integration/test_usage.py`: usage rows, budget guards, posting guard, migration 0003) and 10 live smoke tests in `tests/live/`.
- `docs/14-api-integrations.md` corrected with every fact below; `docs/16-settings-reference.md` shows the new defaults of the three settings changed by migration 0003.

### Local results

`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src/` (96 files) green. `TEST_DATABASE_URL=... uv run pytest`: 226 passed, 10 skipped (the live tests). `uv run pytest tests/unit` without a database: 175 passed.

Live run (`RUN_LIVE_TESTS=1 uv run pytest tests/live -v`, keys from `.env`, 2026-11-17): **7 passed, 3 skipped**.

| Test | Result |
|---|---|
| Twitch game id | skipped: no Twitch keys (D100) |
| YouTube `channels.list` (API key) | passed |
| YouTube channel RSS feed | passed |
| YouTube OAuth `whoami` | skipped: no refresh token (D103) |
| Apify search, 5 results, charge cap $0.05 | passed |
| Anthropic tiny JSON on `claude-haiku-5-5` | passed (146 input, 90 output tokens, $0.00006) |
| Anthropic tiny JSON on `claude-sonnet-5-5` | passed (146 input, 27 output tokens, $0.0006) |
| OpenAI transcription of a 5 s ffmpeg speech clip, word timestamps | passed (9 words, all with start and end) |
| Groq transcription | skipped: no key (D104) |
| Upload-Post `GET /api/uploadposts/users` + analytics for `default` (tiktok, instagram, youtube) | passed |

Nothing was uploaded, posted or deleted anywhere. Apify spend for schema discovery plus the live tests was under $0.15 (about 10 runs of 3 to 5 videos at $0.003 each).

### Verified API facts (the ⚠️ items)

**Upload-Post** (raw docs at `https://docs.upload-post.com/<page>.md`)
- Upload status: `GET /api/uploadposts/status?request_id=<id>` (also `job_id=` for scheduled posts). Statuses `pending`, `queued`, `processing`, `in_progress`, `completed`, `failed`, `not_found` (HTTP 404). Poll every 10 s; never faster than 5 s (the status is cached 2 to 3 s).
- Post analytics: `GET /api/uploadposts/post-analytics/{request_id}` takes the **request id of the upload**, optional `?platform=`. A second form takes the platform post id: `GET /api/uploadposts/post-analytics?platform_post_id=&platform=&user=` (100 requests per 5 minutes).
- Delete: a delete endpoint now exists, `POST /api/uploadposts/posts/unpublish` with JSON `{platform, user, post_id}`. It works for YouTube. **Instagram and TikTok are not supported** (`delete_post` raises `NotSupported` for them).
- Idempotency: `Idempotency-Key` (or `X-Idempotency-Key`) lasts 24 hours; resending the key returns the original job; a simultaneous duplicate gets HTTP 409 `IDEMPOTENCY_KEY_IN_USE`. Your own `request_id` goes in the form field `request_id` (or header `X-Request-Id`).
- An upload answers HTTP 200 even when a platform failed: read `results.<platform>.success`. An unconnected platform is `skipped: true`, not an error.
- **Limits per account per 24 h set by Upload-Post: TikTok 15, Instagram 50, YouTube 30.** API rate limit on Basic: 60 requests/min, 300 per 10 min.
- TikTok specifics that M10 must handle: `fallback_to_inbox: true` (the video went to the drafts inbox, not public); `post_url: null` with a temporary `platform_post_id` when TikTok releases the post id late (read the status again later); `privacy_level` values are per account (`tiktok_privacy_unavailable` when the account lacks one).
- `GET /api/uploadposts/users` returns `{"profiles": [{"username": "default", "social_accounts": {...}}]}` (verified live).
- Profile analytics (verified live) has keys such as `followers`, `impressions`, `reach`, `likes`, `comments`, `profileViews`, `reach_timeseries` per platform.

**Twitch** (not testable live: no keys)
- Token: `POST https://id.twitch.tv/oauth2/token` with the three parameters in a **form body**; response `access_token`, `expires_in`; no refresh token.
- Get Videos: exactly one of `id`, `user_id`, `game_id`; `first` 1 to 100; `period` all|day|week|month; `sort` time|trending|views; `type` all|archive|highlight|upload; at most 500 videos for a `game_id`; `duration` like `1h0m5s`.
- Get Streams: `first` 1 to 100, sorted by viewers descending, pages can repeat or miss streams (the client de-duplicates by id).
- Get Channel Followers: the reference lists **only a user access token**. Whether an app token returns `total` is unverified, so `get_followers_total` returns `None` on 401/403 and remembers the denial.

**YouTube**
- Quota: 10,000 units/day for everything except `search.list` and `videos.insert`; list = 1 unit; `videos.delete` = 50; **`videos.insert` = 1 unit per call in its own bucket, default 100 calls per day.** The project's actual limits on the Cloud Console quota page were not checked.
- Resumable upload: `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status` with `X-Upload-Content-Length` and `X-Upload-Content-Type`, session URL in the `Location` header, then `PUT` the bytes (201 with the video resource).
- OAuth desktop flow: loopback redirect `http://127.0.0.1:<port>`, PKCE S256, `state`; refresh tokens are always returned for desktop apps.
- The API key is accepted in the `X-goog-api-key` header (verified live), so it never appears in a URL.

**Apify** (`streamers/youtube-scraper`, schema from `GET /v2/acts/streamers~youtube-scraper/builds/default`, behaviour from real runs)
- Input: `searchQueries`, `maxResults` (finished videos per term), `maxResultsShorts`, `maxResultStreams`, `isLive`, `dateFilter` (hour|today|week|month|year), `sortingOrder` (relevance|rating|date|views).
- **Live search = `isLive: true` + `maxResultStreams`.** With `isLive` and only `maxResults` the actor returns no videos.
- Output: `id`, `title`, `type`, `streamType`, `viewCount` (current viewers when live), `date`, `likes`, `commentsCount`, `duration` (`HH:MM:SS`, null when live), `channelId`, `channelName`, `numberOfSubscribers`, `url`.
- A search with no result writes one row `{"error": "NO_VIDEOS", ...}`. One of six test runs returned that for a query that worked seconds later: empty results are normal and are not raised as errors.
- Price: pay per event, **$0.003 per video written**. 50 results × 4 terms × 2 searches (live, finished) per discovery pass = up to $1.20 per pass; at one pass every 10 minutes that is far over a $19/month plan. **M09 must use small `max_results` and a long interval for Apify, or rely on RSS and the Data API between Apify runs.** Every run carries `maxTotalChargeUsd` (default $1, argument `max_charge_usd`).
- Run: `POST /v2/acts/<user>~<actor>/runs` → 201; statuses `READY`, `RUNNING`, `SUCCEEDED`, `FAILED`, `TIMING-OUT`, `TIMED-OUT`, `ABORTING`, `ABORTED`. Bearer token header works.

**Anthropic** (SDK `anthropic` 2.9.0, which is built on `httpx2`, not `httpx`)
- Prices per million tokens: `claude-sonnet-5-5` input $2, output $10, 5-minute cache write $2.50, cache read $0.10. `claude-haiku-5-5` input $0.10, output $0.50, cache write $0.125, cache read $0.01 up to 100,000 prompt tokens; above that $0.50 / $2.50 / $0.625 / $0.05.
- Both models think by default; thinking counts toward `max_tokens`; `thinking: disabled`, `budget_tokens`, non-default `temperature`/`top_p`/`top_k` and assistant prefill are rejected or limited. Depth is controlled with `output_config.effort`.
- A policy decline is HTTP 200 with `stop_reason: "refusal"` and a category in `stop_details`.

**Transcription**
- OpenAI: word timestamps only on **`whisper-1`** (`response_format=verbose_json`, `timestamp_granularities[]=word`); $0.006/min; 25 MB; `whisper-1` shuts down on 2027-02-26. Live result: `language` is `"english"` (a name, not a code), `duration` present, words `{word, start, end}`.
- Groq (docs only): `https://api.groq.com/openai/v1/audio/transcriptions`, `whisper-large-v3-turbo`, $0.04 per hour, minimum 10 s billed, word timestamps with `verbose_json`.

### Differences from the kit and deviations from the milestone file

1. **Upload-Post profile is `default`**, not `6stars-staging` (D101). The live test uses `default` and is read-only.
2. **`delete_post` is implemented for YouTube** (the docs now have an unpublish endpoint) and raises `NotSupported` for Instagram and TikTok.
3. **`get_post_analytics(request_id, platform=None)`** uses the request id; `get_post_analytics_by_platform_id(user, platform, platform_post_id)` was added for the second form.
4. **`upload_video` returns an `UploadStatus`** (usually `pending`), not a bare id, because a fast synchronous upload already carries per-platform results.
5. **`upload_video` refuses to run when `control.emergency_stop` or `launch.dry_run` is true** (`PostingBlocked`). This is a last line of defence under the posting engine; M10 still implements dry-run behaviour itself. Pass `posting_guard=None` to switch it off in a test.
6. **A 429 on upload is not retried** (daily and monthly caps do not clear in seconds; the 50 MB body would be sent three times): it is raised as `RateLimited` at once. 5xx and network errors are retried with the same `Idempotency-Key` and `request_id`. GET requests retry 429 as the milestone says.
7. **Apify `start_run` is not retried on 5xx or network errors** (a retry could start a second paid run); 429 is retried.
8. **The Anthropic client uses the SDK's own retries** (`max_retries=2` = three attempts, same conditions, honours `retry-after`) instead of tenacity on top. respx cannot intercept the SDK (httpx2), so its unit tests inject a stand-in SDK object.
9. **`complete_json` appends the JSON Schema of the Pydantic model to the system prompt** (deterministic text, so the cached prefix is stable). M05 prompts need not repeat the schema. Extra arguments: `effort="low"` (default; pass `None` for the model default) and `ref_id`. `max_tokens` below 1024 is raised to 1024 to leave room for thinking. A reply cut off by `max_tokens` is retried once with twice the limit instead of the "append the error" retry. The repair retry is a fresh single-turn request (previous reply and error appended as text), not a multi-turn conversation, so no thinking blocks are replayed.
10. **Server-side refusal fallback is not used.** For Sonnet 5.5 it only rescues `cyber` and `frontier_llm` declines, which are not what game clips would trigger; a refusal raises `AnthropicRefusal(category)` and M05 decides (skip the clip or use a neutral caption).
11. **Hosted transcription defaults to OpenAI** (setting, D104). Order: the provider in `transcribe.express_provider`, then the other one; providers without a key are skipped; a provider that fails after its retries falls back to the next.
12. **YouTube lists return the raw API items** (`list[dict]`), not dataclasses: discovery and the gate read different parts.
13. **Constants that are not settings** (the milestone fixes them or no key exists): timeouts and retry counts, poll intervals (Apify 5 s, Upload-Post 5 s then 10 s), hosted model names (`whisper-1`, `whisper-large-v3-turbo`), price tables, token refresh margin (5 minutes).
14. **Usage rows**: `provider` is `anthropic`, `openai`, `groq`, `local`, `apify`, `upload_post`, `youtube` or `twitch`. For Anthropic `input_tokens` is the total prompt (uncached + cache writes + cache reads); the cost uses the three rates separately. One row per logical call (a paginated Twitch call is one row with `units` = requests).
15. **Ruff**: `N818` (exception names must end in `Error`) is ignored for `src/sixstars/clients/*.py`, because the milestone names the exceptions `RateLimited`, `PostRejected`, `NotSupported`. Mypy: `faster_whisper` has no type stubs (`ignore_missing_imports`).
16. No new environment variables. `UPLOAD_POST_PROFILE` in `.env` is still not an `Env` field: the profile name lives in `platform_accounts`.

### Open issues

- **YouTube refresh token (acceptance item not met).** `sixstars youtube auth` is built and unit-tested (including an end-to-end run of the loopback server against a mocked token endpoint) but was never run against Google: it needs the owner to sign in in a browser. Until then `whoami`, `upload_private`, `get_status` and `delete_video` are unverified against the real API and the pre-check stays deferred (D103). The owner must: create or confirm an OAuth client of type "Desktop app", set the consent screen to "In production", run `uv run sixstars youtube auth`, paste the printed line into `.env`. If the existing client is of type "Web application", Google rejects the loopback redirect with `redirect_uri_mismatch`.
- **Twitch client is untested against the real API** (no keys). In particular `get_followers_total` with an app token.
- **Groq is untested against the real API** (no key).
- **Upload-Post upload, status polling and delete were not exercised live** (forbidden in this milestone). The request shape follows the docs and is unit-tested; the first real upload happens in M10/M15. The response of `post-analytics?platform_post_id=` was not read in full, so the fake's shape for `get_post_analytics_by_platform_id` is an approximation.
- **Upload-Post caps YouTube at 30 posts per 24 h** per account: 24 hourly posts leave 6 for spikes. `platform_accounts` caps for `youtube_main` should be checked against this (M10).
- **Upload-Post monthly upload allowance on the Basic plan is still unknown.** The synchronous upload response and the 429 body carry `usage: {count, limit}`; reading it needs an upload. The dashboard shows it.
- **Apify cost** (see above): at $0.003 per video the discovery design in `docs/06` does not fit a $19/month plan. Decision for the orchestrator or M09.
- **`whisper-1` is deprecated** (shutdown 2027-02-26) and the newer OpenAI transcription models have no word timestamps. Not urgent.
- Migration 0003 must be pushed to both Supabase projects by the orchestrator.
- Local faster-whisper was not run for real in this milestone (the model download is large); `_load_whisper` and the call arguments are covered by a unit test with a stand-in model. M08 runs it.

### Notes for M03, M05, M07, M09, M10

Import the classes; create one instance per process (they hold an httpx client and token caches). Every client has `is_configured() -> bool`; a method called without credentials raises `NotConfiguredError`. All client errors derive from `sixstars.clients._http.ClientError`; an unexpected HTTP status is `ApiError(service, endpoint, status, message)`. Tests: use `sixstars.clients.fakes.*` (each fake is a subclass of the real client, has `calls` and `calls_to(name)`), or respx for httpx-based clients. The root `tests/conftest.py` has an autouse fixture `sleeps` that replaces `clients._http.sleep` (retries and polling do not wait in tests).

```python
# sixstars.usage
record(provider, operation, model=None, input_tokens=None, output_tokens=None, units=None, cost_usd=0.0, ref_id=None) -> bool
spent_today(provider: str | tuple[str, ...]) -> float

# sixstars.clients.twitch
is_configured() -> bool                      # module level, reads the environment
TwitchClient(client_id=None, client_secret=None, http=None)
  .is_configured() -> bool
  .get_game_id(name) -> str | None
  .get_streams(game_id, max_items=300) -> list[TwitchStream]
  .get_videos_by_game(game_id, period="day", sort="views", type="archive", first=50) -> list[TwitchVideo]
  .get_videos_by_user(user_id, type="archive", first=5) -> list[TwitchVideo]
  .get_users(ids) -> list[TwitchUser]
  .get_followers_total(broadcaster_id) -> int | None
TwitchStream(id, user_id, user_login, user_name, game_id, game_name, title, viewer_count, started_at, language, tags, thumbnail_url)
TwitchVideo(id, stream_id, user_id, user_login, user_name, title, description, created_at, published_at, url, thumbnail_url, view_count, language, type, duration_s)
TwitchUser(id, login, display_name, description, profile_image_url, broadcaster_type, created_at)

# sixstars.clients.youtube
YouTubeClient(api_key=None, oauth_client_id=None, oauth_client_secret=None, oauth_refresh_token=None, http=None, upload_http=None)
  .is_configured() -> bool                   # API key present
  .has_oauth() -> bool                       # refresh token present
  .videos_list(ids, parts=("snippet","status","contentDetails","statistics","liveStreamingDetails")) -> list[dict]
  .channels_list(ids, parts=("snippet","statistics")) -> list[dict]
  .channel_feed(channel_id) -> list[FeedEntry]
  .whoami() -> YouTubeChannel
  .upload_private(path: Path, title, description) -> str          # video id
  .get_status(video_id) -> YouTubeVideoStatus | None
  .delete_video(video_id) -> bool
FeedEntry(video_id, channel_id, title, published_at, updated_at)
YouTubeChannel(id, title)
YouTubeVideoStatus(video_id, upload_status, privacy_status, rejection_reason, failure_reason, license, licensed_content, region_blocked, region_allowed, raw)  + .is_restricted

# sixstars.clients.apify
ApifyClient(token=None, youtube_actor=None, http=None)
  .start_run(actor_id, input, max_charge_usd=1.0, timeout_s=300) -> str
  .get_run(run_id) -> ApifyRun
  .wait_for_run(run_id, timeout_s=360.0, poll_s=5.0, operation="run") -> ApifyRun      # raises ApifyRunFailed
  .get_items(dataset_id, limit=1000) -> list[dict]
  .youtube_search(terms, live=False, uploaded_within_hours=None, max_results=50, sort=None, max_charge_usd=1.0) -> list[YouTubeItem]
ApifyRun(id, status, dataset_id, cost_usd, started_at, finished_at)  + .finished, .succeeded
YouTubeItem(video_id, channel_id, channel_name, title, is_live, viewers, views, published_at, duration_s, likes, comments, subscribers, url, raw)

# sixstars.clients.anthropic_client
AnthropicClient(api_key=None, sdk=None)
  .complete_json(prompt_id, system, user_content, schema, model, max_tokens, images=None, cache_system=True, effort="low", ref_id=None) -> schema instance
budget_ok(kind: "vod" | "express" = "vod") -> bool
image_block(data: bytes, media_type="image/jpeg") -> dict;  cost_usd(model, input_tokens, output_tokens, cache_write=0, cache_read=0) -> float
errors: AnthropicError, AnthropicRefusal(.category), InvalidModelOutput

# sixstars.clients.transcribe
Transcriber(openai_api_key=None, groq_api_key=None, http=None, express_provider=None, whisper_model=None, model_factory=None)
  .transcribe(audio_path: Path, mode: "local" | "hosted" = "local", language=None) -> Transcript
  .configured_providers() -> set[str];  .is_configured() -> bool
transcribe(audio_path, mode="local", language=None) -> Transcript       # module-level shortcut
extract_audio(source: Path, destination: Path, ffmpeg="ffmpeg") -> Path  # 16 kHz mono
budget_ok() -> bool                                                      # transcribe.daily_budget_usd
Transcript(language, words: tuple[Word, ...], duration_s, provider, model)  + .text;   Word(w, s, e)
errors: TranscriptionError

# sixstars.clients.upload_post
UploadPostClient(api_key=None, http=None, upload_http=None, posting_guard=default_posting_guard)
  .upload_video(user, platform, video_path: Path, fields: Mapping, idempotency_key, request_id) -> UploadStatus
  .get_upload_status(request_id) -> UploadStatus
  .wait_for_upload(request_id, timeout_s=600.0, poll_s=10.0) -> UploadStatus   # check .finished
  .list_profiles() -> list[dict]
  .get_profile_analytics(user, platforms) -> dict
  .get_post_analytics(request_id, platform=None) -> dict
  .get_post_analytics_by_platform_id(user, platform, platform_post_id) -> dict
  .delete_post(user, platform, post_id) -> bool            # NotSupported for instagram, tiktok
UploadStatus(request_id, status, completed, total, results: tuple[PlatformResult, ...], message)  + .finished, .result_for(platform)
PlatformResult(platform, success, status, post_id, post_url, error, error_code, skipped, raw)  + .error_kind ("rate_limited" | "auth" | "rejected" | None)
errors: UploadPostError > RateLimited(.account, .retry_after), AccountAuthError, PostRejected, NotSupported, PostingBlocked

# sixstars.clients.fakes
FakeTwitchClient(configured=True)      .game_ids .streams .videos .followers
FakeYouTubeClient(oauth=True)          .videos .channels .feeds .statuses .uploaded .channel
FakeApifyClient()                      .runs .datasets .search_results
FakeAnthropicClient(responses=None, default=None)   # responses[prompt_id] = dict | model | list | Exception
FakeTranscriber(transcript=None)       .transcript .error
FakeUploadPostClient()                 .uploads .deleted .upload_error .platform_results .analytics
```

- M05: read models with `settings.get_str("ai.model_writer")` / `("ai.model_fast")` and pass them to `complete_json`. Give `max_tokens` room for thinking (2000 or more for anything but a tiny answer). Check `budget_ok("vod")` before VOD-lane calls.
- M07/M08: hosted transcription takes a file of at most 25 MB (a clip window of a few minutes of 16 kHz mono FLAC is far below that); use `extract_audio` first.
- M10: `fields` for `upload_video` are the documented multipart names (`title`, `privacy_level`, `youtube_title`, `youtube_description`, `privacyStatus`, `categoryId="20"`, `media_type="REELS"`, `first_comment`, ...). After `wait_for_upload`, a platform can be successful with `post_url is None` (TikTok inbox or late post id): store `post_id` and read the status again later.

### Test database note (added at the end of M02)

- Local Docker Desktop hung near the end of the milestone, so the Postgres on `127.0.0.1:54329` is gone. **Integration tests now use port 54330**: `TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54330/postgres` (a throwaway `postgres:17` on the staging server through an SSH tunnel). Through the tunnel the full suite takes about 3 minutes instead of 30 seconds.
- The final full run was made in a separate, freshly created database on that server (`.../m02_test`, dropped afterwards): **226 passed, 10 skipped**, with migrations 0001 to 0003 applied from scratch by the fixture.
- Two runs against the shared `postgres` database on 54330 failed in different M01 tests each time (job claiming, savepoints, an events insert), while the same commit passes in an isolated database. The likely cause is another test session using the same database at the same time (the fixtures truncate `jobs` and rewrite `settings`); this was not proven. **Agents that run integration tests in parallel should each use their own database**: `create database <name>` on the server and put the name at the end of `TEST_DATABASE_URL`.
- `tests/integration/conftest.py` changed: migrations after `0002` are applied on every test session (they must be idempotent), so a database migrated by an older checkout catches up. `api_usage` was added to the tables truncated before each test.

---

## M06 addendum — Viral editing style

**Date:** 2026-11-18 UTC · **Branch:** `m06-viral-style` (from `develop` at `450ca53`)

### What was built

- Analysis of three reference clips with millions of views, plus short-form research: `docs/27-style-reference.md`.
- Setting `factory.style` (migration `0021_viral_style.sql`): `factory/style.py` (`StyleRules`, classic look when the setting is missing or invalid), `factory/effects.py` (ebur128 envelope parsing, punch-in selection, SFX cue plan), `lines.chunk_line` / `collapse_repeats` (1–2-word captions, `X2` repeats), `subtitles.chunk_events` / `payoff_events` / title-bar hook, `layout.ZoomCut` / `PunchIns` (step zoom, full-frame close-up branch), audio extra SFX tracks + compressor, `render.build_envelope_command`.
- Clip writer P2: optional `payoff_s` and `payoff_emoji` (invalid values are dropped, not repaired); trim guidance to end within ~1 s of the payoff.
- Assets: `fonts/Anton-Regular.ttf` (OFL), generated `sfx/whoosh.wav`, `impact.wav`, `boom.wav` (CC0).
- `clip render-file --payoff/--payoff-emoji`; log event `clip_edit_effects`.
- Tests: `tests/unit/factory/test_factory_style_effects.py`; the offline-defaults drift test now also reads migration 0021.

### Deviations

- Owner decisions: layout A keeps gameplay on top; end card off (`factory.end_card_s` 0 instead of a new flag); white captions with a magenta accent (`#FF2BD6`).
- `factory.loudness_lufs` -14 -> -12 with the compressor. `punchin_db` 5 instead of the planned 8 (a real stream peaked only 5.8 dB over its median); caption size 150 instead of 104 (104 looked small next to the references).
- Emoji use the bundled monochrome Noto Emoji font, not Twemoji PNGs.

### Open issues / next

- Merge to `develop`, apply 0021 on staging, look at a few staging renders before `main`. Clips rendered with the 1.5 s end card that are gated after 0021 is applied expect a 1.5 s shorter file; apply it when the render queue is empty.
- Not done: generated explainer graphics, face stickers, speaker-coloured captions (docs/27 section 4).

## Hotfix 2026-11-18 — Hook title only in the first seconds

### What was built

- The `title_bar` hook (TikTok-style PNG) now shows only for the first `factory.hook_seconds` (3 s), then goes so the content has the frame. Before, it stayed for the whole clip. `render.title_filters` gives the overlay `enable=lt(t,…)`; with `hook_seconds` 0 the bar is not drawn. The ASS fallback (no title font) uses the same timing.
- Look, position, font and watermark placement are unchanged. The watermark stays below where the bar was, for the whole clip.

### Deviations

- None. No new setting or migration. To change how long the title shows, edit `factory.hook_seconds` in `settings`.

### Open issues / next

- Only clips rendered after the deploy are affected. Clips already rendered or posted keep the title for the whole clip.

## M18 Phase A — Design styles, one per platform account (2026-11-19)

### What was built

- Plan: `development-plan/M18-design-styles.md` (owner decisions D112-D115, reference study of ten Instagram reels, phases A-E).
- Layout C (D112): facecam 1080x672 on top, gameplay 1080x1248 below, seam and progress bar at y 669, captions at y 1400, title bar on the seam, payoff emoji at y 1000 (`factory/layout.py`, `subtitles.py`, `render.py`, `pipeline.render_file`). Layout A/B unchanged.
- `StyleRules` gains `facecam_top`, `captions`, `title_whole_clip`. Catalogue `factory.styles` (`factory/styles.py`) with `cam_top_viral` (the docs/27 edit, facecam on top) and `cam_top_title` (persistent title with the payoff emoji, no spoken captions, calm). Without the setting: the single `factory.style`, as before.
- Style director (`factory/director.py`, setting `factory.style_director`): one style per enabled account, no repeat of the account's last style, different styles across the accounts of a clip, weighted pick seeded with the clip id.
- Per-account renders (`factory/variants.py`, job `clip.variants`): the clip is rendered in the reference account's style; the clip's span of the source is copied (near-lossless) with a JSON recipe; after the gate approves, each other style is rendered, checked (size, length, black/frozen picture in its own band) and uploaded as `<clip key>.<style>.mp4`. Rejected clips skip it; the copy is deleted.
- Posting sends each account the ready render of its style, else the first render (`posts.render_id`).
- Gate: the black/freeze band follows the clip's layout (`layout.gameplay_band`, `clips.layout_kind`); P5 knows the facecam may be on top and that some styles have no captions.
- Migration 0027: `factory.styles`, `factory.style_director`, `clips.style_id/layout_kind/style_plan`, table `clip_renders`, `posts.render_id`.
- Console: clip page "Design styles" card (each render, its accounts, play button), style column in the clip's posts; API `GET /v1/admin/clips/{id}` returns `renders`, `GET /v1/admin/clips/{id}/media?render=<id>` signs one render.
- `sixstars clip render-file --style <id>`.
- Tests: `tests/unit/factory/test_design_styles.py` (19), `tests/integration/test_design_styles.py` (4, the last renders with ffmpeg), admin route test for renders.

### Deviations

- The burned-in watermark stays the brand name (D111), not each account's handle: per-account handles would need a render per account even when two accounts share a style. Easy to add as a style rule later.
- Spike posts go to every spike-lane account right after the gate, before `clip.variants` has run, so spike posts use the first render; hourly posts get their own style.
- `clip.variants` checks only the technical rules; P5 and the score ran once on the first render (same source, same cut).
- Catalogue order is by style id (a `jsonb` setting does not keep key order).

### Open issues / next

- Measure render time and disk on the staging server (variants add one render per extra style per approved clip, plus a temporary source copy).
- Pre-existing: on a machine with ffmpeg, 57 integration tests fail on `main` as well (stale end-card and gate expectations from before migration 0021); CI skips them because it has no ffmpeg.
- Phase B: `headline_card`, `pov_fullframe`, `cam_top_question`, P2 `moment_type` and text variants.

## M18 Phase B — The calm styles: question, news card, POV (2026-11-19)

### What was built

- Clip writer P2: optional `moment_type` (reaction, discovery, action, story, npc_funny, fail) and the styles' texts `title_line` (≤ 48), `headline` (≤ 90), `question` (≤ 48, ends in "?"), `pov_line` (≤ 48, starts with "POV"). Each is a decoration: a value that breaks its rule is dropped. Stored in `clips.moment_type` and `clips.style_texts` (migration 0028). Prompt and docs/15 updated.
- Styles (migration 0028, `factory.styles`): `cam_top_question` (layout C, red Anton capitals with a black outline at the top of the gameplay), `headline_card` (layout H: brand header with the star avatar, headline, the stream as a 4:3 crop that keeps the facecam; no watermark), `pov_fullframe` (layout P: 3:4 gameplay crop between black bars, centred POV line). `cam_top_title` now shows `title_line`. New rule keys `frame`, `title_source`, `title_look`, `watermark`; entry key `requires`.
- Drawing: `factory/cards.py` (Pillow, colour emoji like the title bar); assets `brand/logo.png` as the avatar and Anton for questions.
- Director: a style is used only when the clip has what it `requires` (`director.clip_facts`); `factory.style_director.affinity` multiplies weights per `moment_type`. The plan is now made after the writer and the layout detection (`produce(choose_style=...)`); variants carry the texts in their recipe.
- Gate bands: H 670-1400, P 300-760; layout C's band moved to 960-1240 (below the tallest question, above the credit line). P5 knows the new elements.
- Console: names of the new styles and layouts.
- `clip render-file --title-line/--headline/--question/--pov`.
- Tests: `tests/unit/factory/test_design_styles_b.py` (19), an end-to-end integration test rendering three styles of one clip (H, P, C).

### Deviations

- Layout H keeps the stream at 4:3 (like R09) rather than full 16:9; the crop moves to keep the streamer's facecam.
- The question title is drawn in layout C only by default (the style asks for the facecam on top); without a facecam it sits at the top of layout B's gameplay.

### Open issues / next

- Watch staging: how often P2 gives each text, which styles accounts get, render time with up to three styles per clip.
- Phase C: story cuts and one-word captions (`cam_top_wordcap`).

## M18 Phase C — Story cuts and one-word captions (2026-11-19)

### What was built

- Platform limits checked in the docs (docs/14): TikTok API 10 min, Instagram Reels 15 min (Upload-Post), YouTube Shorts 3 min (YouTube Help). Setting `posting.max_duration_s`.
- `factory.story` (`factory/story.py`): story cuts of 60-150 s for hourly, normal-mode renders of VOD and live sources. Their windows are widened (VOD section, live cut) by 90 s before and 30 s after, and the writer reads the whole window. Express renders are unchanged.
- P2: optional `story_start_s` / `story_end_s` (kept only inside the window, within the limits and overlapping the short cut; otherwise dropped). Prompt section "Story cut", `{story_rule}` in the user message.
- `pipeline.render_story`: after the short clip, the same job renders the story as its own clip (`clips.version = 'story'`, `parent_clip_id`), reusing transcript, writer reply and layout; style planned for the moment type `story`; variants; `clip.gate` job. Failures reject only the story.
- Gate: story clips use the story length limits; duplicate checks ignore the clip's own family.
- Posting: `posting.sibling_gap_h` (12 h between family members on one account) and `posting.max_duration_s` in the hourly pick.
- Style `cam_top_wordcap` (R06): one or two Montserrat Black capitals on the facecam's bottom edge (y 580), spoken word in yellow, no title. New rule keys `caption_y`, `caption_active_color`, `show_title`.
- Migration 0029: `clips.version`, `clips.parent_clip_id` (one story per short clip), `factory.story`, `posting.sibling_gap_h`, `posting.max_duration_s`, `cam_top_wordcap`, story affinity.
- Console: "story cut" badge and links between a short clip and its story.
- Tests: `tests/unit/factory/test_story_cuts.py`, `tests/unit/posting/test_posting_story_family.py`, integration tests for a real story render, the family-aware gate and the VOD widening. The VOD and live integration tests that pin window positions run with story cuts off.

### Deviations

- The story is rendered inline after its short clip (same job, source still on disk) instead of a separate job, so no second transcription or writer call is needed.
- A story is never made for express (spike-lane) renders or viewer-made Twitch clips (their window is the viewer's cut).

### Open issues / next

- Wider windows cost transcription time on the normal queue (up to about 120 s more audio per hourly clip). Watch the staging queue; lower `pad_before_s` or turn `factory.story.enabled` off if it falls behind.
- Phase D: the per-account bandit on post performance. Phase E: `hype_montage`.

## M18 Phase D — Learning which style works per account (2026-11-19)

### What was built

- `factory/performance.py`: a post's score is `log(1 + views)` minus the median of its account's scored posts (posts 48 h to 14 days old, published, not dry-run); `StyleStats` per account and style, posterior with shrinkage, pooled spread.
- Director `mode: bandit` (`director.bandit_pick`): exploration floor, learning until `min_samples` scored posts per style, then Thompson sampling with the moment-type affinity as a small nudge; the branch taken is in `clips.style_plan`.
- `variants.scored_posts` / `style_stats` read the views (with the style of the render posted).
- Migration 0030: the bandit keys in `factory.style_director` (mode bandit, floor 0.1, min_samples 20, affinity_weight 0.3, prior_n 2, min_age_h 48, window_d 14).
- Console: Insights → Design styles; API `GET /v1/admin/styles`.
- Tests: `tests/unit/factory/test_style_bandit.py` (incl. a seven-day simulation), integration tests for the loader and the page.

### Deviations

- No views snapshot at exactly 48 h exists (the collector overwrites `posts.views`), so posts are scored from 48 h to 14 days old against the same account's median. YouTube's average view percentage is not stored by the collector and is not used.
- In the exploit step the no-repeat and diversity rules do not apply (the account takes its best style); they still apply while exploring and learning.

### Open issues / next

- Staging has no views (dry-run/private posts): the bandit stays in learning there. It starts judging in production once 20 posts of a style are two days old on an account.
- Phase E: `hype_montage`.

## M18 Phase E — The hype montage (2026-11-19)

### What was built

- Style `hype_montage` (reference R10, migration 0031): layout M (3:4 letterbox, gameplay over the facecam, soft fade over the seam), B-roll cuts every other 3 s from the busiest footage around the clip, two-tier glowing keyword captions on the seam, whoosh/impact on each cut, payoff emoji, no title. Hourly only; requires a facecam and at least 9 s of footage around the clip.
- `factory/montage.py`: motion reading (ffmpeg `tblend` + `signalstats`), B-roll picks (outside the clip, no stills, no overlaps), the cut timeline (payoff guard, opening and closing seconds), the seam fade PNG.
- Renderer: B-roll as extra ffmpeg inputs laid over the gameplay panel (`BrollCut`), the fade overlay, the progress bar at the bottom of the letterbox. Anything that goes wrong in planning the B-roll means a montage without cuts, never a failed render.
- Variants: when a montage is planned for another account the source copy holds the padded window (`recipe.window_start_s`, `recipe.broll`).
- Director: fact `broll`; affinity discovery 3, reaction 2.
- Console names; P5 knows the montage.
- Tests: `tests/unit/factory/test_hype_montage.py` (11; one found that a payoff right after a slot start made an empty window and would have failed the render — fixed), an integration test rendering a montage variant from the padded copy.

### Deviations

- The plan said B-roll from "other high-scoring moments of the same video". Those windows are deleted once their own render ends, so the B-roll comes from the footage around the moment that the source already holds (Phase C's wider windows). Picked by on-screen motion, not by chat.
- The keyword glow uses the brand accent (`factory.accent_color`), not R10's gold/blue; no logo cards.

### Open issues / next

- The padded source copy for a montage variant is larger than a clip copy (up to about three minutes of near-lossless video, deleted after the variants); watch disk on staging.
- M18 is complete. Before `develop` → `main`: run a day on staging with the five phases, check render time and disk, and decide whether production starts with every style enabled.

## L0 — TikTok diagnosis and the delivery guard (2026-11-19)

Spec: `docs/28-learning-and-growth.md` (learning loop + growth engine; owner amendments at the end). Branch `learn-l0`.

### What was built

- **Diagnosis** (`development-plan/progress-notes/L0-tiktok-diagnosis.md`): the 16 TikTok posts are public (oEmbed and the public page answer, `privateItem:false`) and have real post ids, yet TikTok itself shows `playCount: 0`. Not a privacy or moderation problem: TikTok isn't distributing the account (likely For You ineligibility). The owner approved stopping TikTok: production `tiktok_main.lanes` is now `{}` (undo in CHANGELOG-scoring).
- **Delivery guard** `learning/delivery.py` (flag `learning.delivery_guard`, off): after each stats pass, an account whose last 5 posts ≥ 6 h old have median 0 views gets `hourly_paused_until` set (until resumed), the posts marked `delivery_suspect`, an `events` row and a critical Telegram alert. 0 views counts only once the post was read (`stats_updated_at`); a TikTok post without a post id after 6 h counts as 0.
- Posting: `Account.hourly_paused_until`, new skip reason `hourly_paused` in `decide_hourly_account` (only the hourly lane; `paused_until` still pauses both).
- `POST /v1/control/lane/resume` (`{"account_id"}` or `{"text": "/resume <id>"}`) and WF08 `/resume <account>` (WF08 now sends the whole message text for commands with arguments: `MESSAGE_BODY`).
- Migration `0032_learning_delivery.sql`; flag `learning.exclude_tiktok` (on) for L2+.

### Tests

Unit: 3624 passed. Integration (throwaway postgres:17): new `test_delivery_guard.py` 5 passed, stats/posting/api/guardian suites pass, except 6 gate tests (`file_duration` rejections) that fail the same way on clean `develop` locally (environment, not this change).

### Deviations / open

- Migration numbers start at 0032 (0027–0031 are M18). The guard's pause is a far-future timestamp (2100-01-01), not `infinity` (psycopg can't read infinity into datetime).
- WF08 must be re-applied (`build_workflows.py apply`) to get `/resume`; [STG] WF08 stays inactive, so test the route directly on staging.
- The TikTok account restriction needs the owner: TikTok app → Account status / video Analytics.
- TikTok Ads Manager shows the ad account currency is **EGP**: A1 stores a currency on money rows and converts the USD cap.

## L1 — Fixed-age snapshots (2026-11-19)

Branch `learn-l1` (on top of L0, PR #67 merged to develop and deployed to staging 06:02 UTC).

### What was built

- Migration `0033_learning_snapshots.sql`: table `post_metrics_snapshots` (insert only, unique per post and target age; missing fields null), flag `learning.snapshots` (off), setting `stats.job_budget_s` (240).
- `learning/snapshots.py` (pure): due windows (1/3/6/24/72/168 h, accepted until `t + max(0.75 h, 0.25 t)`), youngest first; `parse_snapshot` for the real TikTok / Instagram / YouTube answers.
- `stats/snapshot_pass.py`: runs **before** the totals refresh (owner amendment 3). YouTube: one Data API call for all due videos, plus one Analytics API call when `youtube_analytics` is true in the flag and OAuth exists. Others: Upload-Post, one call per post, paced by a token bucket (90, +0.3/s). Each reading also refreshes the post's totals; the totals pass skips those posts.
- Overflow: readings left at the end of the inline budget queue the worker job `stats.collect` (dedupe key, `stats.job_budget_s`; `stats/handlers.py`). The job never queues itself.
- `clients/youtube_analytics.py` (reports.query, `dimensions=video`, filters up to 200 ids) with `FakeYouTubeAnalyticsClient`; `FakeUploadPostClient.post_metrics` per request id.
- WF05 builder: every 30 minutes.

### Owner amendment 5 (checked on production, 19 Nov)

Upload-Post's YouTube post analytics return only `views, likes, comments, favorites`: **no** `subscribers_gained` or `average_view_percentage`. Those need the YouTube Analytics API. Its docs accept the `youtube` scope we already request, but **no YouTube refresh token exists** in staging or production, so the owner must run `sixstars youtube auth` once (then set `YOUTUBE_OAUTH_REFRESH_TOKEN` and `youtube_analytics: true`). Instagram gives `avg_watch_seconds`, `reach`, `saves`, `shares` (no completion, no followers per post).

### Tests

Unit 3640 passed (25 new). Integration: `test_stats_snapshots.py` (4), plus stats, guard, db, api and guardian route suites pass on a throwaway postgres:17. `test_db` table count 24 → 25.

### Deviations / open

- `stats.collect_budget_s` stays 20 (not 25): the account step isn't budgeted, and 25 s risks the n8n 30 s timeout. Extra time comes from the job instead.
- WF05 on staging/production must be re-applied by hand (`build_workflows.py apply --env staging`).
- Paid split: `paid` is set from `posts.boosted`; `paid_views` / `paid_followers` stay null until the ads controller (A5) reads Zernio's split.

## YouTube lane tuning A — cap, hours, hourly gap (2026-11-21)

**Branch:** `yt-lane-a` (from `main` at `a0bebe8`, so it can be released without the L0/L1 work on `develop`).

### Why

Production audit 2026-11-21: `youtube_main` had been lowered to the learned-cap floor (10) by YouTube's per-channel daily upload limit (9 rejections since 2026-11-18). The channel has since passed ID verification. YouTube views 2026-11-17..21 by lane: spike 18 posts, median 946, average 1,404; hourly 15 posts, median 453, average 1,049. So the spike lane is left as it is and the hourly lane is moved to the audience's hours and spaced out.

### What was built

- Migration `0034_youtube_lane_tuning.sql` (idempotent; each value changes only from one the system set):
  - new column `platform_accounts.hourly_min_gap_s` (null = no extra gap);
  - `youtube_main`: `cap_24h` 10 → 16 (only from 10..16 or the seeded 28; never above `posting.platform_max_24h.youtube`), `spike_reserve` 4 (unchanged), `hourly_hours_utc` null → `{0,10,13,14,16,17,18,19,20,21,22,23}` (12 = 16 − 4), `hourly_min_gap_s` null → 3000;
  - `posting.base_hashtags` `["#GTA6","#GTAVI","#GTA6Clips","#Gaming"]` → `["#GTA6","#GTAVI","#GTA6Clips"]`;
  - `posting.first_comment` "Follow {handle} for the best GTA moments every hour 🎮⭐" → "Would you have survived this? 👇 Follow {handle} for daily GTA 6 chaos".
- Posting: `Account.hourly_min_gap_s`; `caps.hourly_gap_ends_at` (from the last hourly post only); `decide_hourly_account` skips with `min_gap` while it runs. Spike lane, `cap_state` and the global 900 s gap unchanged.
- Seeds carry the same values. Tests: `tests/unit/posting/test_posting_youtube_lane.py` (gap edges, spikes ignored, the 12 hours, 30 days of jittered slots with a 240 s upload never blocking the next slot, seeded hours = `cap_24h − spike_reserve`), `tests/integration/test_youtube_lane_migration.py` (applied twice; operator values kept; other accounts untouched; Upload-Post ceiling).

### Deviations

- The prompt named the migration `0032`: 0032 and 0033 are taken on `develop` (L0/L1). This is 0034; it reaches production before 0032/0033, which `supabase db push --include-all` (both deploy workflows) applies out of order.
- `posting.base_hashtags` already held GTA 6 tags (fixed in the production database on 2026-11-18); only `#Gaming` was dropped.
- `spike_reserve` stays 4 and the hours are 12, not 16 (owner, after the lane report). Instagram is not touched (owner).
- The hourly gap reuses the `min_gap` reason code (with its own message) instead of a new code.
- Integration tests' `T0` moved from 12:00 to 14:00 UTC (12 is no longer a YouTube hour).

### Open issues / next

- R3 (`posting.skip_hourly_after_spike_s`, 30 min) still skips an hourly slot right after a spike; spikes beyond the reserve still use hourly capacity. Both unchanged on purpose.
- 57 integration tests in `test_gate_run`, `test_gate_cli`, `test_gate_visual_score`, `test_precheck_flow` and `test_factory_pipeline` fail locally on a clean `main` too (not caused by this change).
- Next: PR-B (cross-platform gap, platform priority, titles), PR-C (hourly length per platform, follow-overlay test through the style bandit), later the cap ramp.

## YouTube lane tuning B — cross-platform gap (D116) and titles (2026-11-21)

**Branch:** `yt-lane-b` (on top of `yt-lane-a`).

### What was built

- Migration `0035_cross_platform_gap.sql` (added only where missing): `posting.cross_platform_min_gap_s = 2700`, `posting.platform_priority = ["youtube","tiktok","instagram"]`.
- Posting (D116, hourly lane only): `precedence.cross_platform_block` (reason `cross_platform_too_soon`) in `selection.pick_clip`, so an account takes its next best clip when the top one went to another platform less than 45 min ago — planned posts of the same slot included. `selection.hourly_order` lets platforms pick in priority order, then by id.
- A clip one platform skipped stays available to it: with D116 on, the hourly queue also holds hourly clips already `posted` (`store.load_hourly_queue(posted_since)`, `selection.hourly_candidates`), and the publisher sends an hourly post of a `posted` clip. "Already posted" and D42 keep it off the accounts and platforms that had it.
- Titles: rules in `ai/prompts/p2_clip_writer.md` and docs/15 (name the streamer, set up the tension, never state the outcome; "GTA 6" in at most one title in three; working patterns). `ai/titles.spoils_ending` catches "ends in/with/up", "crashes/crashed", "dies/died", "sinks/sank", "fails at"; `calls.write_clip` asks once more with a correction note and keeps the first reply when the second still spoils or the retry fails (log `title_spoiler_kept`).
- Docs: D116 in docs/00, R13 in docs/11, both keys in docs/16.
- Tests: `tests/unit/posting/test_posting_cross_platform.py`, `tests/integration/test_cross_platform_slot.py`, `tests/unit/ai/test_ai_titles.py`. The older lane integration tests describe D18 and switch D116 off in their fixtures (`posting_support.d116_off`).

### Deviations

- The prompt named the decision "D-next" and the plan said D112; D112–D115 are taken (M18), so it is **D116**.
- The "skipped clip stays approved" requirement: a clip is marked `posted` as soon as one platform published it (`settle_clip`), so instead of changing the clip lifecycle the hourly queue keeps recent `posted` clips (within `posting.max_clip_age_h`) while D116 is on.
- The rule applies between every pair of platforms, not only YouTube/Instagram (e.g. TikTok then waits 45 min before Instagram takes the same clip).
- "GTA 6 in at most one title in three" is prompt guidance only; it cannot be enforced per call.

### Open issues / next

- PR-C: hourly length per platform (YouTube 8–30 s) and the follow-overlay test.

## YouTube lane tuning C — hourly length per platform and the follow-line test (2026-11-21)

**Branch:** `yt-lane-c` (on top of `yt-lane-b`).

### What was built

- Migration `0036_youtube_length_follow.sql`:
  - `gate.hourly_len_by_platform = {"youtube": {"min_len": 8, "max_len": 30}}` (added where missing);
  - three follow twins in `factory.styles` (`cam_top_viral_follow`, `cam_top_wordcap_follow`, `cam_top_title_follow`; YouTube's most used styles, 22 of its 28 posts): same rules plus a follow line "Follow @6starsClips-m3x for Part 2" over the last 1.5 s, `youtube_main` and the hourly lane only, same weight; their moment-type affinity copied in `factory.style_director`. Added only where missing.
- `sixstars/lengths.py` (pure): per-platform windows, the union, lenient parsing.
- Gate: the hourly verdict allows the union (`gate.platform_lengths` → `profiles.with_platform_lengths`, only where a clip is judged; the Twitch-clip source keeps reading plain `gate.hourly`); express and story limits unchanged.
- Writer: hourly limits widened to the overrides (`factory/config.FactoryConfig.from_mapping`); `cut.adaptive` still replaces them when on.
- Posting (R14): `selection.family_block` holds a short hourly clip (`clips.gate_profile = 'hourly'`, not a story cut) off a platform whose window does not fit it, end card included. Each platform's window is its override or `gate.hourly` lowered by `cut.adaptive.min_s` exactly as the gate lowers it (`params.with_cut_minimum`), so no platform loses a clip the gate approved for it.
- Factory: style rules `follow_overlay_s` / `follow_overlay_text`; `overlays.follow_layout` (pure, inside the safe zone) and `follow_filter` (drawtext, last N seconds, no extra time); wired through `RenderSpec.follow_text/follow_s`.
- Docs: docs/16 (`gate.hourly_len_by_platform`), docs/27 (follow line), docs/11 R14.
- Tests: `tests/unit/posting/test_posting_hourly_length.py`, `tests/unit/gate/test_gate_len_by_platform.py`, `tests/unit/factory/test_factory_follow_length.py`, `tests/integration/test_youtube_length_follow_migration.py`; a real ffmpeg frame check of the line (before / during the last 1.5 s).

### Deviations

- **Production already cuts short clips:** the scoring flag `cut.adaptive` is on in production (writer 6–25 s, target 12–18 s; the gate minimum follows `min_s` = 6), so `gate.hourly.min_len = 20` was not blocking short clips there. In production the YouTube window mainly keeps clips of 6–8 s and Twitch clips over 30 s off YouTube; on staging (flag off) it lets the writer cut 8–19 s clips for YouTube.
- No `spikes.len_by_platform`: spikes are unchanged (owner).
- The end card is not switched on (`factory.end_card_s` stays 0, owner): the follow line is an overlay that keeps the loop, tested 50/50 through the director/bandit. The handle is in the style's text because a render does not know its account; the twins are limited to `youtube_main`.
- Twins for three styles, not all seven, so the bandit's samples per style on YouTube are not spread too thin (`min_samples` 20).

### Open issues / next

- The bandit judges a style on posts 48 h to 14 days old; read the twins against their originals after about two weeks of YouTube hourly posts.
- Later: `posting.youtube_cap_ramp` (not built; after PR-A has run clean for 48 h).

## Title writer v2 (2026-11-23)

**Branch:** `titles-v2` (off `develop`). Reference: `docs/30-titles-v2.md`.

### What was built

- Migration `0040_titles_v2.sql` (additive): `creators.title_name` + `creators.title_aliases` (30 names seeded where empty), table `clip_titles` (facts, all candidates with failed checks, chosen title, caption, tags, explored, features, prior title, applied), scoring_config `titles.v2` (**disabled**).
- Prompt `p11_title_writer.md` (brief Part 1 + spoiler rule), schema `TitleSet`, `ai.calls.write_titles` (fast model by default, low effort, cached system prompt).
- `sixstars/titles/`: `facts` (name book, facts builder: P9V `seen` as the event, peak line, hours into stream, hours/percent/money said, chat words, other streamers), `checks` (Part 2 as pure functions, choice in pattern order, 20% exploration stable per clip id, features, fallback), `store`, `service` (one retry, fallback, apply only while not posted, scoring_log stage `title`, events `clip.retitled` / `clip.title_fallback`), `commands`.
- Gate: `title_v2.after_approval` right after approval and before the spike handoff. Never raises; the P2 title stays on any failure.
- CLI: `sixstars titles retitle|compare|queue`.
- Tests: `tests/unit/titles/` (checks, facts, service with stubs).

### Deviations

See docs/30 "Differences from the brief": spoiler rule kept, two brief examples left out of the prompt (they break the banned-phrase rule), "GTA 6" counts as one word, `title_name` instead of `display_name`, no campaign concept, no per-line audio level, migration 0040 (0038/0039 are taken by `ai-cost-a5`), no ranking boost.

### Open issues / next

- Staging: enable `titles.v2`, run `sixstars titles compare --limit 30 --status approved --status rejected --status posted` (staging has only one posted clip), owner reviews old vs new.
- Production: only after owner approval: flag on, then `sixstars titles queue --apply`. The comparison on production's 30 most recent posted clips needs a production read (not done from this machine).
- Learning Loop L2 reads `clip_titles.features` where `applied`.
## Clipping campaigns PR 1 — registry and lanes (2026-11-23)

**Branch:** `clipcamp-1` (from `main` 874d640, so production gets campaign work without develop's L0/L1). Plan: "6 Stars Clips — Lanes & Clipping Campaigns Plan" (Plan and Operating flows tabs), decision **D118** (supersedes D05).

### What was built

- Migration `0050_clip_campaigns.sql` (additive, idempotent):
  - Tables `clip_campaigns`, `clip_campaign_creators`, `clip_campaign_posts`, `render_profiles` (five seeded looks: `brand`, `clean-disclose`, `partner-logo`, `overlay-keep`, `brand-disclose`), `lane_quotas`, `creator_live_status`.
  - Tier `campaign` in `creators_tier_check`, plus `creators.tier_before_campaign`.
  - Nullable `campaign_id` on `live_sessions`, `videos`, `viewer_clips`, `spikes`, `clips`, `posts`, and `clips.render_profile`.
  - Settings `lanes.campaign_share` and `lanes.campaign_max_per_day`; `normal`/`spike` quota rows for every account.
- `src/sixstars/campaigns/`:
  - `models.py`.
  - `rules.py` (pure): transitions, save checks, quota fit, lane rows, tier moves, audit diff.
  - `store.py`.
  - `service.apply_change` is the one write path: it checks, saves with version + 1, rewrites lane quotas, moves streamers in and out of tier `campaign`, emits a `campaign.changed` event and sends a Telegram line.
  - `lookup.py`: a streamer is found by Twitch login, YouTube @handle or channel id, never by name alone.
  - `commands.py`.
- CLI `sixstars campaign add|edit|activate|pause|resume|end|budget|list|show|lookup|quotas`.
- Clients:
  - `TwitchClient.get_users_by_login` (Helix Get Users `login=`).
  - `YouTubeClient.channel_for_handle` (`channels.list?forHandle=`, 1 unit).
  - Both have fakes.
- Tier `campaign` is treated like `preferred` everywhere `preferred` was tested: the gate creator check, the express tier rule, the discovery preferred bonus, the ranking boost, and the console tier list.
  - The P6 permission read never overwrites it.
  - A P6 `restricted` verdict, or a block, still wins and pauses the streamer's active campaigns (`service.on_creator_forbidden`).
- Tests:
  - `tests/unit/campaigns/` (rules, lookup).
  - New cases in `tests/unit/gate/test_gate_decision.py` and `tests/unit/discovery/test_discovery_sources.py`.
  - `tests/integration/test_campaign_registry.py`.

### Deviations

- Numbering: `develop` took D117 and migrations 0037-0040 (AI cost, titles v2) while this was built, so this is **D118** and the campaign migrations are **0050** (PR 1) and **0051** (PR 2), with room in between for other branches. Gaps are fine for `supabase db push --include-all`.

- Naming (owner, 23 Nov): `clip_campaigns` / `clip_campaign_posts` / `lane_quotas`, and Telegram `/clipcamp …` later. This avoids the docs/28 `ad_campaigns` and `/campaign` and the existing `/resume`.
- A campaign is a tag (`campaign_id`), not a new lane value: `clips.lane` / `posts.lane` stay `spike|hourly`.
- Instagram campaign share is 18, not 20, because the account cap is 45, not 50.
- Pausing or ending is never blocked by the save checks (a bare status change to paused/ended skips them), so the guardian can always stop a campaign.
- The gate gave `campaign` no daily cap (like `preferred`).
- The console Campaigns page moved to PR 6, as the Operating flows tab says. Until then, campaigns are added through the CLI.
- **TikTok (owner, 23 Nov):** TikTok campaign posts are wanted. Production `tiktok_main.lanes` is `{}`; re-enabling it is asked for at the PR 4 release. `tiktok_live` has no real second account.

### Open issues / next

- PR 2 (sourcing):
  - campaign watcher (Twitch `/streams?user_id=` every 60 s, YouTube RSS every 3 min);
  - `live.campaign_slots`;
  - VOD and Twitch-clip priority;
  - spike tagging;
  - cross-lane moment dedupe.
- Settle first: multi-day live sessions without a forced restart.
- Disk: merged worktrees m03/m04/m05/m10/m11/media/styles/console were removed (13 GB free on D:). `6stars-wt-m06` (unmerged) and `6stars-wt-tz` (uncommitted work holding an untracked `0034_app_timezone.sql`) were kept.
