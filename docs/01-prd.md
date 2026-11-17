# 01 — Product Requirements Document (PRD)

## 1. Summary

**6 Stars Clips** is an autonomous clipping media operation for GTA 6. It watches the GTA 6 creator ecosystem on Twitch and YouTube, finds the moments chat goes wild over, and publishes them as short vertical clips on TikTok, Instagram Reels and YouTube Shorts, 24 hours a day, with no human approval.

## 2. Goals and success metrics

| Goal | Metric | Target (first 30 days, indicative) |
|---|---|---|
| Grow audience | Followers/subscribers per platform | Track daily; YouTube is the priority |
| Make each clip count | Median views per video per platform | Track daily; alert on sharp drops |
| Reach | Total views across all platforms | Track daily and cumulative |
| Monetise | Progress toward YouTube Partner Program (subscribers, Shorts views in last 90 days) | Shown in every daily report |
| Speed | Median time from live spike to post (express path) | Under 4 minutes |
| Reliability | Hourly slots posted on time | ≥ 95% |
| Safety | Copyright strikes | 0; emergency stop at 2 in 7 days |

## 3. Users

- **The owner** (single operator). Receives Telegram notifications and a daily report. Can send `/stop`, `/start`, `/status` but the system never waits for them.
- **Viewers** on TikTok, Instagram and YouTube worldwide (English).
- **Creators** being clipped, who get credit on screen and in the caption.

## 4. Scope

### In scope (launch)
1. Automatic discovery of trending GTA streams and videos (Twitch, YouTube).
2. Creator tiering by permission signals.
3. Live recording of top streams with real-time chat-spike detection.
4. Chat-first processing of finished streams and replays.
5. Transcript-based moment picking for videos without chat.
6. Clip rendering: 9:16 layout, facecam detection, styled English subtitles with word highlight, keyword pops, hook title, profanity bleep, creator credit, watermark, series counter, end card.
7. Automatic quality gate (length, music, spoilers, safety, watermarks, visual checks, score, duplicates, creator rules).
8. YouTube claim pre-check for risky clips.
9. Two posting lanes (spike + hourly) with precedence rules and per-account caps, via Upload-Post.
10. Guardian: claim/removal monitoring, takedown email handling, health checks, failover scheduling, emergency stop.
11. Stats collection and a daily Telegram report.
12. Staging (GTA 5) and production (GTA 6) environments with CI/CD.

### Out of scope (later phases)
Paid ads and boosting, admin dashboard, voice-over, additional languages, Kick, X/Discord cross-posting, clipping-program automation.

## 5. Functional requirements

### FR-1 Discovery
- FR-1.1 Every 10 minutes, list live streams in the target game's Twitch category and matching YouTube live streams, plus recent videos (last 48 h).
- FR-1.2 Compute a trend score (0–100) per candidate using the weights in settings.
- FR-1.3 Assign up to `live.max_slots` live recordings to the best live streams; release slots from streams that drop below the cutoff for 20 minutes.
- FR-1.4 Queue the best finished streams/videos for the VOD lane.
- FR-1.5 Register new creators automatically as tier `auto`; read channel descriptions to detect `preferred` or `restricted`.
- FR-1.6 The target game and search terms are settings (GTA 5 in staging, GTA 6 in production).

### FR-2 Chat-spike engine
- FR-2.1 Ingest chat from Twitch (IRC live, TwitchDownloaderCLI replay) and YouTube (`chat-downloader` live and replay).
- FR-2.2 Count per 10-second bucket: messages, unique chatters, reaction tokens, clip requests. Filter bots, system notices, subs/gifts, raids and the first 5 minutes.
- FR-2.3 Score each bucket as a weighted z-score against the stream's trailing 10-minute baseline.
- FR-2.4 Classify spikes S/A/B; confirm with Claude; merge spikes within 60 s.
- FR-2.5 Compute clip windows: start = spike − lead (20 s default), end = when score drops below 1.5 z plus 5 s, length 15–60 s.

### FR-3 Live lane
- FR-3.1 Record each assigned stream into 60-second segments with a rolling 30-minute buffer.
- FR-3.2 Run the spike engine in live mode on the stream's chat.
- FR-3.3 S-tier spikes go through the express path and must be posted within 4 minutes (target).
- FR-3.4 A/B spikes go to the normal factory and then the hourly queue.
- FR-3.5 When the stream ends, schedule a replay pass on its VOD.

### FR-4 VOD lane
- FR-4.1 Download chat replay; find spikes; skip ranges already clipped live.
- FR-4.2 Download only spike windows (plus padding) with yt-dlp.
- FR-4.3 Fallback for videos without chat: transcribe and let Claude pick 3–8 moments.

### FR-5 Clip factory
- FR-5.1 Produce 1080×1920 H.264/AAC MP4 at 30 fps, loudness −14 LUFS.
- FR-5.2 Layout per D13 using a cached facecam box per creator.
- FR-5.3 English subtitles, 2–4 words per line, current word highlighted, 1–2 keywords per line in accent colour with a pop; inside the safe zone (no text in top 220 px or bottom 420 px).
- FR-5.4 Hook title for the first 3 seconds; "LIVE · JUST HAPPENED" badge on spike-lane clips.
- FR-5.5 Bleep profanity in audio and mask it in subtitles.
- FR-5.6 Credit line "🎥 @creator" on screen; watermark `@6starsclips`; series counter; 1.5-second end card.

### FR-6 Quality gate
- FR-6.1 Apply all checks in `docs/10-quality-gate.md`; record each check's result in `clips.gate_result`.
- FR-6.2 Express clips use the stricter express profile.
- FR-6.3 Never post a clip that failed the gate.

### FR-7 Posting
- FR-7.1 Hourly lane fires every hour (plus 0–5 min jitter) and posts the top-ranked queued clip to every account with room.
- FR-7.2 Spike lane posts S-tier clips immediately, subject to the precedence rules.
- FR-7.3 Cap manager enforces per-account rolling 24 h caps and the 15-minute minimum gap.
- FR-7.4 Respect `dry_run`, `launch_at` and `emergency_stop`.
- FR-7.5 Record every post with platform IDs and URLs.

### FR-8 Guardian
- FR-8.1 Check recent posts for removal, blocking or claims every 15 minutes during launch week, hourly after.
- FR-8.2 Handle takedown emails from Gmail: identify creator, remove their posts where possible, block the creator, alert.
- FR-8.3 Emergency stop after 2 strikes/removals in 7 days.
- FR-8.4 Health checks: worker heartbeats, stuck jobs, queue depth, tool failures, API spend caps.
- FR-8.5 Failover: if n8n's hourly or discovery trigger hasn't arrived on time, the worker runs it itself and alerts.

### FR-9 Reporting
- FR-9.1 Collect account and per-post stats every 2 hours.
- FR-9.2 Daily report at 00:00 UTC to Telegram with the KPIs in section 2.
- FR-9.3 Immediate Telegram alerts for errors, stops, takedowns and cap problems.

## 6. Non-functional requirements

| Area | Requirement |
|---|---|
| Availability | Runs 24/7; all containers restart automatically; recovery from a crash without manual steps. |
| Latency | Express path under 4 minutes from spike to post (target), under 6 minutes (alert threshold). |
| Throughput | At least 24 approved clips per day in the hourly queue; queue never below 12. |
| Cost | API spend caps per day in settings; VOD lane pauses before the express path when caps are hit. |
| Security | Secrets in env only; control API behind HTTPS and a bearer token; Supabase accessed with server-side credentials only; RLS on with no public policies. |
| Observability | JSON logs, `events` table, Telegram alerts, daily report. |
| Configurability | All thresholds in the `settings` table; changes apply within 30 seconds without redeploying. |
| Compliance | Creator credit on every clip; restricted creators never clipped; takedowns handled the same day automatically. |

## 7. Constraints and known platform limits

- TikTok API: about 15 posts/day/account, shared across apps; 6 posts/minute.
- Instagram via Upload-Post: 50 posts per 24 h.
- YouTube: unpublished per-channel daily upload limit; verified channels get more.
- YouTube API projects that are not audited can only upload private videos (used only for pre-checks).
- Upload-Post does not obviously expose deletion of published posts; see `docs/12-guardian.md` for the fallback.
- Twitch/YouTube may block downloads from data-centre IPs; a residential proxy or cookies are required.

## 8. Release plan

| Stage | When (UTC) | What |
|---|---|---|
| Build + staging tests | Mon 16 Nov 21:30 → Wed 18 Nov 07:00 | Milestones M00–M15 |
| Production deploy (dry run) | Wed 18 Nov ~07:00 | M16; queue fills with GTA 6 pre-launch content |
| Launch | Wed 18 Nov 11:00 | `launch_at` passes; posting goes live automatically |
| Game release | Thu 19 Nov | GTA 6 gameplay content starts flowing |
