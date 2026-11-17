# 12 — Guardian

Protects the accounts and keeps the system alive without a human.

## 1. Claims and removals check

Triggered by `POST /v1/guardian/check` (n8n `WF03`, every 15 min in launch week, then hourly — change the n8n schedule, the code doesn't care).

For each published post younger than `guardian.check_lookback_h` (72 h):
| Platform | How | Problem detected when |
|---|---|---|
| YouTube | Data API `videos.list?part=status,contentDetails&id=<ids>` (API key, 50 ids per call) | video missing (removed), `uploadStatus` rejected, `regionRestriction.blocked` non-empty, `privacyStatus` no longer public |
| TikTok / Instagram | Upload-Post post analytics for the request id (`GET /api/uploadposts/post-analytics/<id>`; verify path) | post not found / error says removed |

On a problem:
1. `posts.claim_status` set; `takedowns` row (source `claim` or `platform`).
2. Creator `claims_count + 1` → tier `blocked`.
3. Remove that creator's other posts (§3).
4. Count strikes: removals/claims in `guardian.strike_window_d` (7 days). If ≥ `guardian.strike_limit` (2) → **emergency stop** (§4).
5. Alert (critical).

## 2. Takedown emails

`WF04` (Gmail trigger on the takedown inbox) → `POST /v1/guardian/takedown` with `{from, subject, body_text, received_at, message_id}`.
1. Claude P7 classifies: `is_takedown`, `creator_handles[]`, `urls[]`, `platform`.
2. Not a takedown → `takedowns.status = ignored` (info notification).
3. Takedown → match creator(s) by handle/URL; block them; remove all their posts (§3); count as a strike; alert critical with what was done.

## 3. Removing posts

| Platform | Method |
|---|---|
| YouTube | Data API `videos.delete` with the owner's OAuth (same Google project as the pre-check). This works for videos on the owner's channel even though they were uploaded via Upload-Post. |
| TikTok, Instagram | Upload-Post delete endpoint **if available** (check the current docs; none was visible during planning). If not available: the post is listed in a critical Telegram alert with direct links ("remove manually"), `posts.status = remove_failed`. This is the only case that can need a human. |

## 4. Emergency stop

- Set `control.emergency_stop = true`, `control.stop_reason`.
- Posting stops everywhere; discovery and rendering continue (queue keeps filling).
- Critical alert with the reason and the posts involved.
- Cleared only by `/start` in Telegram (`WF08`) or `sixstars settings set control.emergency_stop false`. (This is the one deliberate human decision point; it only happens after 2 strikes.)

## 5. Health checks (scheduler, every 60 s)

| Check | Rule | Action |
|---|---|---|
| Heartbeats | any service heartbeat older than `heartbeat_stale_s` (300 s) | alert warn; Docker restarts crashed containers on its own |
| Restart storm | ≥ 3 restarts of a service in 1 h (from heartbeat `info.started_at`) | alert critical |
| Stuck jobs | `running` longer than `stuck_job_s` (2 h) | reset to `queued` (attempts kept); after max attempts → `failed` |
| Queue depth | approved hourly clips < `queue_low_watermark` (12) | alert warn; discovery widens for next runs |
| Express latency | `spike_to_post_s` > `express_slow_s` (360) three times in a row | alert warn |
| Tool failures | 5 consecutive yt-dlp/streamlink failures | run `pip install -U yt-dlp streamlink` in the container, rotate proxy session, alert if still failing |
| Spend | today's `api_usage` per provider > budget setting | pause VOD lane (normal workers stop claiming `vod.process`); express continues; alert |
| Disk | `$DATA_DIR` > 80% full | aggressive cleanup; alert |

## 6. Failover scheduling

The scheduler checks heartbeats `trigger:hourly` and `trigger:discovery`. If either is older than `guardian.failover_grace_s` (hourly 70 min, discovery 25 min), it runs the job itself, writes the heartbeat, and alerts "n8n trigger missing — running failover". It keeps doing so until n8n triggers resume.

## 7. Notifications

`sixstars.notify.send(severity, title, body)`:
1. Insert into `notify_outbox`.
2. POST to `N8N_NOTIFY_WEBHOOK_URL` with header `X-6Stars-Secret`. On success set `sent_at`.
3. The scheduler retries unsent outbox rows every minute.
4. `info` messages are batched into the daily report instead of sent instantly (except discovery/launch milestones).
