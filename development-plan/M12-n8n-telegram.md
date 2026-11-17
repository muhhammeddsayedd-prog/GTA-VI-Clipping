# M12 — n8n Workflows + Telegram

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 21:30–23:00 UTC · **Requires:** M11 · **Launch-critical:** yes

## Goal
Build the n8n workflows that fire schedules, read the takedown inbox and deliver Telegram messages, first for staging (`[STG]`), later duplicated for production (`[PROD]`, in M16).

## Context you need
- n8n is self-hosted on the owner's VPS with HTTPS. It holds **no business logic**: it calls the control API or relays messages.
- Control API base (staging): from M11. Auth: `Authorization: Bearer <CONTROL_API_TOKEN>` (create an n8n Header Auth credential `6stars API staging`).
- Telegram: bot token + chat id (owner provides). Gmail: the brand takedown inbox (OAuth credential in n8n).
- If the n8n MCP server is available to Claude Code, build the workflows with it (call `get_user_preferences`, read the SDK reference and best practices first); otherwise write step-by-step click instructions in PROGRESS.md for the owner and build them together.

## Workflows (each: error workflow = WF09; HTTP nodes timeout 30 s, retry 3× with 10 s wait; save executions on error only)

| ID | Name | Trigger | Steps |
|---|---|---|---|
| WF01 | Discovery Tick | Schedule every 10 min | HTTP `POST {API_BASE}/v1/discovery/run` |
| WF02 | Hourly Poster | Schedule hourly at minute 0 | HTTP `POST {API_BASE}/v1/posting/hourly` |
| WF03 | Guardian Tick | Schedule every 15 min | HTTP `POST {API_BASE}/v1/guardian/check` (route exists after M13; create now, activate in M13) |
| WF04 | Takedown Intake | Gmail Trigger (poll 5 min, full message) | Set `{from, subject, body_text, received_at, message_id}` → HTTP `POST {API_BASE}/v1/guardian/takedown` → Gmail add label `6stars-processed` (activate in M13) |
| WF05 | Stats Collector | Schedule every 2 h | HTTP `POST {API_BASE}/v1/stats/collect` (activate in M13) |
| WF06 | Daily Report | Schedule daily 00:00 UTC (workflow timezone UTC) | HTTP `GET {API_BASE}/v1/reports/daily?format=telegram` → Telegram send `{{$json.text}}` (HTML) (activate in M13) |
| WF07 | Notify Relay | Webhook `POST /webhook/6stars-notify-staging` (respond 200 immediately) | IF header `x-6stars-secret` = secret → Set text `"{emoji} <b>{title}</b>\n{body}"` (🔴 critical, 🟠 warn, 🔵 info; prefix `[STG]`) → Telegram send (HTML, no link preview) |
| WF08 | Telegram Commands | Telegram Trigger (message) | IF chat id matches → Switch `/stop` `/start` `/status` `/report` → matching HTTP call → Telegram reply with returned `text`. (One bot = one webhook: build WF08 for production only unless a separate staging bot exists.) |
| WF09 | Error Alert | Error Trigger | Telegram: "🔴 n8n workflow failed: {workflow} — {error} — {execution url}" |

## Build
1. Create credentials and workflows WF01–WF09 for staging; tag `6stars`, `staging`.
2. Put the WF07 webhook URL and secret into staging `.env` (`N8N_NOTIFY_WEBHOOK_URL`, `N8N_NOTIFY_SECRET`); redeploy.
3. Activate WF09, WF07, WF01, WF02 now; keep WF03–WF06 inactive until M13.
4. Export every workflow JSON to `n8n/workflows/staging-WFxx-<name>.json` (strip credential ids).

## Acceptance tests
- `sixstars notify test` (add this CLI command) → Telegram message "[STG] 🔵 Test" arrives.
- WF01 runs every 10 minutes: `heartbeats.trigger:discovery` updates; `candidates` grows.
- WF02 at the top of the hour: `heartbeats.trigger:hourly` updates; a dry-run `posts` row appears ~0–5 min later.
- WF09 fires when an HTTP node is forced to fail (temporarily wrong URL) → Telegram alert.

## Definition of done
Staging is driven entirely by n8n schedules; alerts reach Telegram; workflow JSON exported to the repo.

## Handoff to M13
Activate WF03–WF06 once their routes exist.
