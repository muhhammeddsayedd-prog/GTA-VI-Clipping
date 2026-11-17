# 13 — n8n Workflows

n8n runs on the owner's VPS (self-hosted, HTTPS). It owns **schedules, Gmail intake and Telegram**. It contains **no business logic**: every workflow either calls the control API or relays a message.

Create each workflow twice, once per environment, named `[STG] …` and `[PROD] …`, tagged `6stars` + `staging`/`production`. Export each to `n8n/workflows/<env>-<id>.json` in the repo after building.

## Credentials to create in n8n

| Credential | Type | Used by |
|---|---|---|
| `6stars API staging` / `6stars API production` | Header Auth: `Authorization: Bearer <CONTROL_API_TOKEN>` | all HTTP Request nodes |
| `6stars Telegram` | Telegram API (bot token) | WF07, WF06, WF08, WF09 |
| `6stars Gmail` | Gmail OAuth2 (takedown inbox) | WF04 |

Variables (n8n Variables or a Set node at the start of each workflow): `API_BASE` (e.g. `https://api.6stars.example.com`), `TELEGRAM_CHAT_ID`, `NOTIFY_SECRET`.

## Common settings for every workflow
- Error workflow: `WF09 Error Alert`.
- HTTP Request nodes: timeout 30 s, retry on fail 3 times, wait 10 s between tries.
- Save execution data only on error (keeps n8n's DB small).

## WF01 — Discovery Tick
| Node | Config |
|---|---|
| Schedule Trigger | every 10 minutes |
| HTTP Request | `POST {{API_BASE}}/v1/discovery/run`, auth `6stars API` |
| (end) | Response is a summary; nothing else to do |

## WF02 — Hourly Poster
| Node | Config |
|---|---|
| Schedule Trigger | every hour at minute 0 |
| HTTP Request | `POST {{API_BASE}}/v1/posting/hourly` (returns 202; the worker applies the jitter) |

## WF03 — Guardian Tick
| Node | Config |
|---|---|
| Schedule Trigger | every 15 minutes (switch to every 60 minutes after 2026-11-25) |
| HTTP Request | `POST {{API_BASE}}/v1/guardian/check` |

## WF04 — Takedown Intake
| Node | Config |
|---|---|
| Gmail Trigger | poll every 5 minutes, label INBOX, "simplify" off (need full body) |
| Set | map `from`, `subject`, `body_text` (text or stripped HTML), `received_at`, `message_id` |
| HTTP Request | `POST {{API_BASE}}/v1/guardian/takedown`, JSON body from Set |
| Gmail | add label `6stars-processed` to the message |

## WF05 — Stats Collector
| Node | Config |
|---|---|
| Schedule Trigger | every 2 hours |
| HTTP Request | `POST {{API_BASE}}/v1/stats/collect` |

## WF06 — Daily Report
| Node | Config |
|---|---|
| Schedule Trigger | daily 00:00 UTC (set the workflow timezone to UTC) |
| HTTP Request | `GET {{API_BASE}}/v1/reports/daily?format=telegram` → `{ "text": "<markdown>" }` |
| Telegram | Send message to `TELEGRAM_CHAT_ID`, parse mode MarkdownV2 or HTML (the API returns HTML-safe text when `format=telegram`) |

## WF07 — Notify Relay
| Node | Config |
|---|---|
| Webhook | `POST /webhook/6stars-notify-<env>`, respond immediately with 200 |
| IF | header `x-6stars-secret` equals `NOTIFY_SECRET`, else stop |
| Set | text = `{{severity_emoji}} <b>{{title}}</b>\n{{body}}` (🔴 critical, 🟠 warn, 🔵 info) and prefix `[STG]` for staging |
| Telegram | Send message (HTML parse mode), disable link previews |

## WF08 — Telegram Commands
| Node | Config |
|---|---|
| Telegram Trigger | updates: message |
| IF | `chat.id` equals `TELEGRAM_CHAT_ID` (ignore everyone else) |
| Switch | on message text: `/stop`, `/start`, `/status`, `/report` |
| HTTP Request (stop) | `POST {{API_BASE}}/v1/control/stop` body `{ "reason": "manual /stop" }` |
| HTTP Request (start) | `POST {{API_BASE}}/v1/control/start` |
| HTTP Request (status) | `GET {{API_BASE}}/v1/status?format=telegram` |
| HTTP Request (report) | `GET {{API_BASE}}/v1/reports/daily?format=telegram` |
| Telegram | reply with the returned `text` |

(One Telegram bot can only have one webhook. If staging and production share a bot, only production gets WF08; staging commands are not needed.)

## WF09 — Error Alert
| Node | Config |
|---|---|
| Error Trigger | — |
| Telegram | "🔴 n8n workflow failed: {{workflow.name}} — {{execution.error.message}} — {{execution.url}}" |

## Activation order
1. WF09, WF07 (alerts work first).
2. WF01, WF03, WF05.
3. WF04 (after the takedown inbox exists).
4. WF02, WF06, WF08.

In production all workflows can be activated before launch: posting is held by `launch_at` and `dry_run` in the worker.
