# Orchestration plan

Orchestrator: Claude Code (lead). Owner is away after intake (completed 2026-11-16 23:20 UTC). Hard deadline: production posting live **2026-11-18 11:00 UTC**. This file and `PROGRESS.md` are the orchestrator's memory: read both first after any context reset.

## Intake outcome (no secrets here; values live in git-ignored `.env.staging`, `.env.production`, `.env.shared`)

| Item | State |
|---|---|
| GitHub `muhhammeddsayedd-prog/GTA-VI-Clipping` (public) | OK. Use `gh` account `muhhammeddsayedd-prog`; repo-local credential helper is `gh auth git-credential`. Local token has no `write:packages`: push images from Actions with `GITHUB_TOKEN` (`packages: write`). No required reviewer on `production`. |
| Hetzner Cloud API token | OK, no servers yet. Orchestrator creates servers and SSH key. Hostnames via `sslip.io`. |
| Supabase production `silojdzyqtrifzfuokml` (eu-west-1) | OK, DB connects, empty. |
| Supabase staging `tyhejpxcfgyvhwkrrojq` (eu-west-1) | OK, created at intake, DB connects. |
| n8n | Cloud instance `https://muhhammedsayed.app.n8n.cloud` (project `pHvQjDmjxVZ3KQLT`) for staging; API key works. Existing credentials: `Gmail account` (gmailOAuth2, id `tO7PbRb87vrmUNCz`), `Telegram account` (telegramApi, id `tcmPUVJjfja3zL0E`; confirm it is the 6 Stars bot or create a new one via API). Owner intends to move production to the VPS n8n but gave no VPS URL/key: if absent by 07:00 UTC 18 Nov, production workflows run on the cloud instance. The instance holds many unrelated workflows: never touch anything not tagged `6stars`. |
| Telegram | Bot `@gta6clippybot`, chat id stored in `.env.shared`, test message delivered. |
| Anthropic, OpenAI, YouTube Data API key, Apify (Starter) | OK. |
| Groq | Not provided: hosted transcription is OpenAI only. |
| Twitch | **Not provided.** Build and unit-test the Twitch code against fakes; discovery/chat run on YouTube only until keys arrive (`TWITCH_CLIENT_ID` empty disables Twitch sources). |
| YouTube OAuth refresh tokens | Not provided (client id/secret only): YouTube claim pre-check is deferred; stricter music rule applies. |
| Residential proxy / cookies | Not provided. YouTube downloads from Hetzner IPs may be blocked: test early on the staging server; alert the owner on Telegram if blocked. |
| Upload-Post | Key OK, plan Basic (monthly allowance unconfirmed). **One profile only: `default`** with TikTok `@6starsclipping`, Instagram `@6starsclippy`, YouTube `@6starsclips-m3x`. These are the production accounts and the owner approved using them for staging tests. No second TikTok account: `tiktok_live` stays disabled. |

Owner decisions: all defaults accepted (Anthropic $25/day prod, $5/day staging; transcription $5/day; $800/month ceiling; launch-day threshold relaxation allowed; launch without live lane allowed; no GPU move; add standard slur list; generate minimal brand assets; no creator exclusions).

Consequences to record as D100+ in `docs/00-decisions.md` during M00/M01:
- D100 YouTube-only sources until Twitch keys arrive.
- D101 Single Upload-Post profile `default` for both environments; seeds updated with real handles; `tiktok_live` disabled.
- D102 Staging posts on the production accounts: TikTok `SELF_ONLY`, YouTube `private`; Instagram cannot be private, so at most one Instagram test post in M15, listed in the handover for deletion.
- D103 YouTube pre-check deferred (no refresh token); stricter music rule.
- D104 Hosted transcription = OpenAI only.
- D105 Images pushed to GHCR by Actions `GITHUB_TOKEN`; server pulls with a short-lived login during deploy.

## Dependency graph

```
M00 → M01 → M02 → M03 → M04 ─┐
              └──→ M05 → M06 → M07 → M08 → M09
                              └────→ M10 → M11 → M12 → M13 → M14 → M15 → M16
```

## Parallel waves and time budget (UTC)

| Wave | Work | Budget |
|---|---|---|
| 0 | M00 (alone). Infra agent in parallel: SSH key, staging server, provisioning. | Mon 23:20 – Tue 00:30 |
| 1 | M01 (alone) | Tue 00:30 – 02:00 |
| 2 | M02 clients ‖ M06-static (ASS builder, filter graph, layouts, overlays) | Tue 02:00 – 04:30 |
| 3 | M03 chat ‖ M05 AI layer ‖ M11-infra (Dockerfile, compose, Caddy, CI/CD) | Tue 04:30 – 07:00 |
| 4 | M04 spikes ‖ M06 finish | Tue 07:00 – 10:30 |
| 5 | M07 gate | Tue 10:30 – 12:30 |
| 6 | M08 VOD lane ‖ M10 posting | Tue 12:30 – 16:00 |
| 7 | M09 discovery ‖ M11 finish + staging deploy | Tue 16:00 – 19:30 |
| 8 | M12 n8n | Tue 19:30 – 21:00 |
| 9 | M13 guardian ‖ M14 pure parts | Tue 21:00 – Wed 00:30 |
| 10 | M14 live lane (first to be cut) | Wed 00:30 – 04:00 |
| 11 | M15 staging acceptance | Wed 04:00 – 06:30 |
| 12 | M16 production deploy, dry run, 10:30 go/no-go, 11:00 launch | Wed 06:30 – 11:00 |

Allowed scope cuts, in order: M14 live lane; YouTube pre-check (already cut); stats detail in the daily report.

## Subagent rules

- One branch/worktree per subagent (`mNN-<slug>[-part]`); never two agents on the same files; merges go through the orchestrator into `develop`.
- Every brief names the milestone file, owned files, forbidden files, contracts, tests, definition of done, and "do not ask the owner; report blockers with options".
- Roles: implementer, independent test writer (M04, M07, M10), reviewer per milestone, infra agent, API verifier for ⚠️ items.
- Orchestrator runs `ruff`, `mypy --strict`, `pytest` and the acceptance tests itself before merging.

## Status log (orchestrator)

### 2026-11-17 07:40 UTC
- Merged to `develop`, CI green: M00, M01, M02, M03, M04, M05, M06, M07, M08, M09, M10, M11, M12. Per-milestone detail is in `development-plan/progress-notes/` (one file per milestone; `PROGRESS.md` holds M00–M02).
- Staging is live: `https://api-staging.46.224.14.255.sslip.io/v1/health` OK over HTTPS; server `46.224.14.255` (user `deploy`, key `~/.ssh/6stars_ed25519`); every merge to `develop` builds, migrates (`supabase db push --remote --yes --include-all`) and deploys.
- n8n staging: WF01, WF02, WF07, WF09 active; WF03–WF06 wait for M13; `[STG] WF08` is never activated. Build script: `n8n/build_workflows.py`.
- Supabase pooler size raised to 40 on both projects; `DB_POOL_MAX=3` per container.
- Test database for agents: Postgres 17 container `sixstars-test-pg` on the staging server, reached through an SSH tunnel on local port 54330 (databases `v1`…`v6`, one per agent). Local Docker Desktop is hung (drive C: full); do not use it. The whole integration suite takes 30+ minutes through the tunnel: run touched files locally and let CI run the rest.
- **Launch blocker (owner alerted twice on Telegram):** from the Hetzner IP, YouTube refuses video downloads (bot check) and yt-dlp search; there are no Twitch keys, proxy or cookies. Needs one of: residential `PROXY_URL`, Twitch keys, `YOUTUBE_COOKIES_FILE`. Production stays `launch.dry_run = true` until footage can be fetched on the server and the queue rule in `docs/20` is met.
- Yield finding (M08): chat spikes on thin YouTube chat rarely make clips (13.7 h of streams → 1 clip); the transcript fallback on edited uploads does (6 of 6). Tuning in progress.
- In progress: M13 guardian (`m13-guardian`), source hardening (`hardening-sources`: Data API `search.list` fallback, RSS, language, doctor CLI, migration 0010), AI/factory hardening (`hardening-ai-factory`: structured outputs, snap cuts to pauses, music run-length, score stability, migration 0011).
- Not started: M14 live lane (first scope cut), M15 staging acceptance, M16 production (production server not yet created; production GitHub secrets not yet set; `[PROD]` n8n set not yet created; first real private staging post not yet made).
