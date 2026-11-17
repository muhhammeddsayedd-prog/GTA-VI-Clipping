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
