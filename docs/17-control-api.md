# 17 — Control API

FastAPI service (`sixstars api`) behind Caddy (HTTPS). Used by n8n only. All routes need `Authorization: Bearer <CONTROL_API_TOKEN>` (constant-time compare). JSON in/out. Base path `/v1`.

| Method & path | Called by | Does | Response |
|---|---|---|---|
| `GET /v1/health` | uptime checks (no auth) | process up, DB reachable | `{"ok":true,"env":"staging"}` |
| `POST /v1/discovery/run` | WF01, failover | one discovery pass (runs in background thread; returns when done, ≤ 60 s) | counts summary |
| `POST /v1/posting/hourly` | WF02, failover | record trigger heartbeat, schedule the hourly slot with jitter | `202 {"slot_at":…, "fires_at":…}` |
| `POST /v1/guardian/check` | WF03 | claims/removals check | `{"checked":n,"problems":[…]}` |
| `POST /v1/guardian/takedown` | WF04 | body `{from, subject, body_text, received_at, message_id}`; idempotent on `message_id` | `{"is_takedown":…,"actions":[…]}` |
| `POST /v1/stats/collect` | WF05 | account + post stats | `{"accounts":n,"posts":n}` |
| `GET /v1/reports/daily?format=telegram\|json` | WF06, WF08 | build report for the previous UTC day | `{"text":"…"}` or JSON |
| `GET /v1/status?format=telegram\|json` | WF08 | live sessions, queue depth, posts last hour, stop flag, spend today | `{"text":"…"}` |
| `POST /v1/control/stop` | WF08 | set emergency stop with reason | `{"stopped":true}` |
| `POST /v1/control/start` | WF08 | clear emergency stop | `{"stopped":false}` |
| `POST /v1/control/settings` | manual | body `{key, value}` | updated row |

Every trigger route writes a heartbeat (`trigger:discovery`, `trigger:hourly`, `trigger:guardian`, `trigger:stats`). Errors return `{"error": "...", "code": "..."}` with proper HTTP codes; unexpected exceptions are logged with a request id and alerted.

## Daily report content (`format=telegram`, HTML)

```
⭐ 6 Stars Clips — daily report (2026-11-19 UTC)

👥 Followers   TikTok main 1,240 (+640) · TikTok live 410 (+300) · IG 820 (+390) · YT 1,050 (+600)
👀 Views       total 412k (+180k) · median per video: TT 6.1k · IG 3.4k · YT 5.2k
🏆 Top clip    "He did NOT expect that 😭" — 88k views (YT)
📈 YPP        subs 1,050 / 1,000 ✅ · Shorts views 90d 2.1M / 10M (21%)

📤 Posts      hourly 24/24 slots (2 skipped after spikes) · spike 11 · failed 1
⚡ Spikes     detected 140 · S 12 · A 35 · B 41 · rejected 52 · median spike→post 3m10s
🎬 Clips      rendered 96 · approved 61 · rejected 35 (music 9, score 14, spoiler 3, dup 9)
🧭 Discovery  live slots used 6/6 · videos processed 18 · new creators 27 (preferred 3, restricted 2)
🛡️ Guardian   claims 0 · takedowns 0 · strikes 0/2 · stop: off
💸 Spend      Anthropic $14.20 · transcription $1.80 · Apify $0.90
⚠️ Issues     YouTube cap lowered 40→38 at 14:02
```
