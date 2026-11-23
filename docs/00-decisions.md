# 00 — Decisions Log

Every decision made while planning, with the reason. If code and this file disagree, this file wins unless PROGRESS.md records a newer decision.

## Product

| # | Decision | Reason / source |
|---|---|---|
| D01 | Personal side project. Brand name **6 Stars Clips**. | Owner's choice |
| D02 | Fully automated, 24/7, no human approval. Telegram is notifications only. | Owner requirement |
| D03 | Global audience, English only. No regional targeting, language or branding. | Owner requirement |
| D04 | KPIs: subscribers/followers, views per video, total views. | Owner answer |
| D05 | Revenue target: YouTube ad revenue share (YouTube Partner Program). Clipping campaigns and programs are handled manually by the owner, outside the system. **Superseded by D118 (2026-11-22).** | Owner answer |
| D06 | Platforms: TikTok, Instagram Reels, YouTube Shorts. No X, no Discord. | Owner answer |
| D07 | Sources: Twitch and YouTube only. | Owner answer |
| D08 | Content: everything GTA 6 (story, open world, roleplay, online). Staging uses GTA 5 because GTA 6 is not out during testing. | Owner answer |
| D09 | The system discovers creators itself. No seed list. | Owner answer |
| D10 | Creator permission policy (b): creators who allow clipping first, others allowed with caps, creators who forbid it never. | Owner answer |
| D11 | Non-English streams allowed, translated to English subtitles, ranked lower. | Default accepted |
| D12 | Minimal branding: small handle watermark, series counter, short end card. Content first. | Owner answer |
| D13 | Layout: gameplay top, facecam bottom; blurred fill when no facecam. | Default accepted |
| D14 | Profanity is bleeped (audio) and masked (subtitles). | Owner answer |
| D15 | No voice-over. | Owner answer |
| D16 | Hook tone: hype. | Default accepted |
| D17 | Sound effects from royalty-free (CC0) sources, licenses recorded. | Owner answer |
| D18 | Same video and same caption on every platform (YouTube also gets a title). | Owner answer |
| D19 | A first comment ("Follow for more…") is posted where Upload-Post supports it. Pinning is not available through the API. | Owner answer + API limits |
| D20 | Spoilers: never in titles or hooks. Story-spoiler clips blocked until 14 days after release (until 2026-12-03). | Default applied |
| D21 | Always reject: real-world politics, gambling segments, sponsored segments, plus standard safety filters. | Default accepted |
| D22 | Ads and paid boosting: later phase, not in this build. | Owner answer |
| D23 | Admin dashboard: later phase. Launch with Telegram notifications only. | Owner answer |

## Timing

| # | Decision | Reason |
|---|---|---|
| D30 | Launch **2026-11-18 11:00 UTC**. All testing before launch. | Owner requirement |
| D31 | No ramp-up: full posting rate from the launch minute. | Owner requirement |
| D32 | If a component is not ready by launch, launch with what passed staging tests; the live lane is designed to join later without downtime. | Plan default |

## Posting

| # | Decision | Reason |
|---|---|---|
| D40 | Two posting lanes. **Spike lane** (priority 1): S-tier live spikes post immediately. **Hourly lane** (priority 2): one clip per hour per platform. | Owner requirement |
| D41 | Minimum 15 minutes between posts on the same account; max 2 spike posts per account per hour; an hourly slot is skipped if a spike posted in the previous 30 minutes. | Precedence design |
| D42 | TikTok allows about 15 API posts per day per account (shared across all apps). So TikTok uses **two accounts**: `@6starsclips` (hourly lane, best 14 hours) and `@6starsclips.live` (spike lane). Different clips on each. | Verified API limit + owner answer "multiple accounts if needed" |
| D43 | Instagram: Upload-Post caps at 50 posts per 24 h (Meta documents 100). System cap 45. | Verified |
| D44 | YouTube: per-channel daily upload limit exists but is not published. Channel must be phone/identity verified. Cap manager learns the real limit from errors. | Verified |
| D45 | All public posting goes through Upload-Post. | Upload-Post is an audited client; our own YouTube API project would be forced to private |

## Moment detection

| # | Decision | Reason |
|---|---|---|
| D50 | Chat spikes are the main moment detector, in both live and replay mode (method from the "chat tells you" reference video). | Video analysis |
| D51 | Chat counted in 10-second buckets; live mode slides every 5 seconds. | Video method + improvement |
| D52 | Clip starts 20 seconds before the spike (setting, 15–30). | Video method |
| D53 | Spike score uses message count, unique chatters, reaction words/emotes, "clip" requests and audio jump, each as a z-score against the stream's own recent baseline. | Improvement over raw counts |
| D54 | VOD lane is chat-first: download the chat replay, find spikes, download only those windows. Transcript-based picking is the fallback for videos with no chat. | Cost and speed |
| D55 | After a live stream ends, a replay pass catches spikes the live tracker missed. | Coverage |

## Technical

| # | Decision | Reason |
|---|---|---|
| D60 | Worker language: Python 3.12. | Owner answer; faster-whisper and audio models are Python |
| D61 | Hosting: Hetzner. Production: dedicated-vCPU server (16 vCPU, 32–64 GB RAM). Staging: small cloud server (4 vCPU, 8 GB). Vercel cannot run long-lived recorders/renders; Railway bills CPU-heavy 24/7 work by usage and has no GPU. | Owner asked for the best platform |
| D62 | n8n: owner's existing self-hosted instance on a VPS with HTTPS. n8n runs schedules, Gmail intake and Telegram; Python owns all business logic. | Owner answer + reliability |
| D63 | Supabase Pro organisation. Two projects: `6stars-production` (the existing empty project) and `6stars-staging` (new). | Owner has Pro; staging required |
| D64 | Staging posts to a separate Upload-Post profile connected to private test accounts. TikTok `SELF_ONLY`, YouTube `private`, Instagram private test account. | Staging must never touch real accounts |
| D65 | Job queue in Postgres (`jobs` table, `FOR UPDATE SKIP LOCKED`). No Redis. | Fewer moving parts |
| D66 | Claude models: `claude-sonnet-5-5` for writing and judgement, `claude-haiku-5-5` for fast classification. Model names are settings. | Cost/speed balance |
| D67 | Transcription: faster-whisper locally for VOD windows; a hosted Whisper API (Groq, OpenAI fallback) for the express path. | Speed where it matters |
| D68 | YouTube claim pre-check uses the owner's own Google Cloud project to upload **private** test copies of risky clips, check status after 30 minutes, then delete. Unverified API projects can only upload private videos, which is fine for this purpose. | API restriction |
| D69 | Deliverables are Markdown files in the repo. GitHub repo exists and is empty. | Owner answer |
| D70 | Claude Code is the main coding agent; development plan is split into self-contained milestones. | Owner answer |

## Open items (owner to provide)

- Takedown Gmail inbox address (connected to n8n).
- Telegram bot token and chat ID.
- n8n base URL.
- All API keys listed in `config/.env.example`.

## Decisions made during the build

Made at owner intake on 2026-11-16 (details in `development-plan/ORCHESTRATION.md`). Where a row disagrees with an earlier decision, the row here wins.

| # | Date | Decision | Reason |
|---|---|---|---|
| D100 | 2026-11-16 | YouTube-only sources until Twitch keys arrive. The Twitch code is built and unit-tested against fakes; an empty `TWITCH_CLIENT_ID` disables Twitch sources for discovery and chat. | Twitch client id and secret were not provided at intake |
| D101 | 2026-11-16 | A single Upload-Post profile, `default`, is used for both environments. Seeds are updated with the real handles (TikTok `@6starsclipping`, Instagram `@6starsclippy`, YouTube `@6starsclips-m3x`); `tiktok_live` stays disabled. Replaces the separate staging profile in D64. | The owner has one Upload-Post profile and no second TikTok account |
| D102 | 2026-11-16 | Staging posts go to the production accounts: TikTok `SELF_ONLY`, YouTube `private`. Instagram cannot post privately, so at most one Instagram test post is made in M15 and it is listed in the handover for deletion. Replaces the private test accounts in D64. | No private test accounts exist; the owner approved using the production accounts for staging tests |
| D103 | 2026-11-16 | The YouTube claim pre-check (D68) is deferred and the stricter music rule applies instead. | No YouTube OAuth refresh token was provided (client id and secret only) |
| D104 | 2026-11-16 | Hosted transcription is OpenAI only. Narrows D67 (Groq with OpenAI fallback). | No Groq API key was provided |
| D105 | 2026-11-16 | Images are pushed to GHCR by GitHub Actions with `GITHUB_TOKEN` (`packages: write`); the server pulls with a short-lived login during deploy. | The local GitHub token has no `write:packages` scope |
| D106 | 2026-11-17 | Spike settings retuned from real GTA 5 chats (migration 0004): `min_messages` 8, `warmup_s` 900, reaction weight 0.55, `z_b`/`z_a`/`z_s` 2.0/2.6/3.4, new `spikes.end_guard_s` 300. Reason: seeded values found 2 of 6 genuine moments on YouTube chats; tuned values found 7 of 9 (judged from chat text, tuned on the same streams). |
| D107 | 2026-11-17 | Twitch keys arrived; Twitch is a live source again (supersedes D100). YouTube video downloads stay blocked on the Hetzner IP (cookies were invalidated twice within an hour), so clips come from Twitch until a residential proxy exists. A blocked platform gets no new VOD jobs or live slots (migration 0013). Local whole-video fallback transcription is off (migration 0014). | Staging runs on the server: Twitch chat, spikes, downloads and live recording work; YouTube downloads do not; fallback transcription starved the render queue. |
| D108 | 2026-11-17 | The clip score the gate reads now comes from a vision call on frames of the rendered clip plus chat reaction (P9V, migration 0016); the minimum score stays 7. Allowed black and frozen picture rise from 2 s to 4 s (migration 0017). The music rule is unchanged. | On staging 0 of 24 clips passed: the transcript-only score could not see visual payoffs (a no-parachute jump scored 6), and the game's 3.2 s death screen failed the black/freeze checks. Measured on 15 rendered clips: 3 reach 7, every weak clip stays below. |
| D109 | 2026-11-17 | Admin console (supersedes D23): a private Next.js app in `web/` on Vercel, one project per environment, one operator account in Supabase Auth with sign-ups off. It reads through new read-only `/v1/admin/*` control-API routes with its own `DASHBOARD_API_TOKEN`; the browser never talks to Postgres or Storage and RLS keeps no policies. Controls are limited to the ones the API already had: emergency stop/start and the settings editor. It is milestone M17, built after launch and not merged to `main` before M16 is done. | Owner asked for a full UI to view the system. Vercel keeps the console up when the pipeline server is down; a separate token can be rotated without touching n8n. |
| D110 | 2026-11-17 | Viewer-made Twitch Clips are a discovery source (`twitch_clips`, migration 0019): the most viewed clips of the target game are downloaded, rendered and gated like every other clip, on the hourly lane, and credited to the broadcaster, never to the viewer who made the clip. No chat-spike detection and no P1 confirmation for this source. (Planned as D109; that number was already taken by the admin console.) | Chat-spike detection on long streams approved 0 of about 45 rendered clips: it mostly finds banter and menus. Viewers already clip the best moments, and Helix lists them per game with view counts. |
| D111 | 2026-11-18 | Each platform names its own account. `posting.first_comment` takes a `{handle}` placeholder that becomes the posting account's handle (TikTok `@6starsclipping`, Instagram `@6starsclippy`, YouTube `@6starsClips-m3x`). The burned-in watermark (`factory.watermark`) is the brand name `6 Stars Clips`, not a handle (migration 0020). | The three accounts have different handles and one video file goes to all three, so the fixed TikTok handle was wrong on Instagram and YouTube (owner audit request, 2026-11-18). A watermark with the right handle per platform needs one render per platform; not built. |
| D112 | 2026-11-19 | The facecam goes on top (about 35% of the height, layout C) in every design style that shows one; the gameplay is below. Supersedes the "gameplay on top" choice of docs/27 section 2. | 6 of the 7 owner references with a facecam put it on top (development-plan/M18 section 1). |
| D113 | 2026-11-19 | Every version of a moment is posted: a short cut, and a story cut (60-150 s) when the window holds an arc. Built in M18 Phase C. | Owner decision; two of the references are 90-160 s long. |
| D114 | 2026-11-19 | One design style per platform account: the style director picks a style per account (`factory.styles`, `factory.style_director`), each account is posted its own render (`clip_renders`, job `clip.variants`), falling back to the first render. Migration 0027, M18 Phase A. | Owner decision: different looks per platform beat one global look, and the performance per style can be compared. |
| D115 | 2026-11-19 | The planned `headline_card` style may use the brand header "6 Stars Clips" with our avatar, never a verified tick or another platform's interface. | Owner approval (M18 Phase B). |
| D116 | 2026-11-21 | Hourly lane: a clip does not go to a second platform less than `posting.cross_platform_min_gap_s` (45 min) after it went to the first; the account takes its next best clip and the skipped clip stays approved for its next slot. Platforms pick in `posting.platform_priority` order (YouTube, TikTok, Instagram). Spike posts are not affected. D18 (one shared ranked queue, same caption) and D42 (never two accounts of one platform) stay. | YouTube and Instagram published the same clip within about five minutes of each other and Instagram cut distribution for a full day on 2026-11-20 (owner, YouTube lane tuning). Spikes stay immediate on every platform (owner). |
| D117 | 2026-11-23 | AI cost plan (owner-approved): cut calls on clips that cannot post before changing models. Order A0 per-call logging (migration 0037) → A7 budget-starvation fixes → A5a deterministic pre-P2 rejects → A5b queue throttle (log-only until the owner reviews) → A2 adaptive P9V + frame caching, posting rank without the P9V score → A3 P3/P8/P4 on Haiku → A6 fewer P2 repairs → B1 Haiku shadow. No production setting value (budgets, music limit, exploration posts) changes without the owner. | Production analysis 2026-11-23: spend capped at about $26/day, 90% of it on clips that never post, hourly queue 4–5× oversupplied, P9V score uncorrelated with views on approved clips (ρ≈0.04). |
| D118 | 2026-11-22 | Paid clipping campaigns (Content Rewards) are the revenue route; the YouTube Partner Program stays a long-term option. Every account is split into lanes: Normal, Spike and one Campaign lane per live campaign, sharing one engine and one daily cap per account (`lanes.campaign_share`). A campaign is a row in `clip_campaigns` (entered by hand, never scraped) and a tag (`campaign_id`) on the work, not a new lane value. Its streamers get tier `campaign` (above `preferred`, never overwritten by P6; a streamer who forbids clipping still becomes `restricted` and the campaign pauses). Every change goes through `campaigns.service.apply_change`. Supersedes D05. Built in six PRs from migration 0050; decision gate 2026-12-06. | Owner decision, "Lanes & Clipping Campaigns Plan" (22 Nov) |
| D119 | 2026-11-23 | Render v2: static gameplay-first split (G) and no-cam frame (G0); zoom, punch-ins and the H/P/M/A frames removed; facecam verified by a face detector. G (verified facecam): cam panel 1080x720 on top, gameplay 1080x1200 below; G0 (no verified cam): 4:5 gameplay 1080x1350 at y=285 over the blurred, darkened copy (layout B's background). Nothing in the frame changes scale or position over time. One text element at a time on the seam plus the brand mark and the creator credit; no LIVE pill, payoff emoji, progress bar or seam line. The style director varies only the text treatment (word captions, a full line, a question, the follow line). Built in milestones R0-R7 (`development-plan/M19-render-v2.md`); R0 (no zoom, migration 0043) can ship alone. The cam panel is 37.5% of the frame, superseding the cover-engine rule of a facecam panel of at least 40%. Supersedes D112's 1080x672 facecam panel and the docs/27 zoom cuts and punch-ins. | Owner's render and layout audit, 2026-11-23: every published clip zoomed; 38 of 150 had full-screen punch-ins of a ~136 px wide cam slice scaled about 10x; cam panels cut faces (fixed 1.61:1 crop) or showed gameplay (no face check, 13 posts with confidence < 0.7); the top GTA VI streamer-clip Shorts (5.3M, 1.3M, 359K, 116K views) all use a still cam-on-top / gameplay-below split with one caption. |
| D120 | 2026-11-21 | One display time zone for the whole app: setting `app.timezone` (default `Africa/Cairo`, migration 0055) controls every time shown in the console, Telegram messages and the daily report, and the day the report covers (WF06 asks hourly with `due=1`; the API sends each local day once). Storage, the API, caps, budgets (UTC day), hourly slots (`hourly_hours_utc`), `reserve_release_hour_utc`, AI prompts and stored decision reasons stay UTC; times tied to a UTC limit show both. `reports.timezone` is removed. | Owner request: the owner works in Cairo time but limits must keep their UTC meaning. |
