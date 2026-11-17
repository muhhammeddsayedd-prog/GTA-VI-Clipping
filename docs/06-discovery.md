# 06 — Discovery Engine

Runs when n8n calls `POST /v1/discovery/run` (every 10 minutes), or when the scheduler's failover fires. One run takes under 60 seconds.

## 1. Sources

| Source | Call | Gives |
|---|---|---|
| Twitch live | `GET https://api.twitch.tv/helix/streams?game_id=<id>&first=100` (paginate up to 300 streams) | user_id, user_login, title, viewer_count, language, started_at, id (stream id) |
| Twitch finished streams | `GET /helix/videos?game_id=<id>&type=archive&period=day&sort=views&first=50` | VOD id, user, title, view_count, duration, created_at |
| Twitch game id | `GET /helix/games?name=<settings env.target_game>` once, cached 24 h | game id |
| YouTube live | Apify YouTube scraper actor, search each term in `env.search_terms`, filter live | video id, channel id, title, concurrent viewers if available |
| YouTube recent | Same actor, filter uploads from last `discovery.vod_lookback_h` hours | video id, channel id, title, views, published time, duration |
| YouTube known creators | `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` for every creator in `creators` with platform youtube and tier not blocked/restricted | latest uploads |

Apify runs are slower (30–90 s); run them asynchronously: start the actor run at the beginning of discovery and read results from the previous completed run (results are at most 10 minutes old). Store the last run id in `settings` key `discovery.apify_last_run` (written by code, not seeded).

## 2. Creator handling

- Upsert `creators` on (platform, channel_id).
- New creators: tier `auto`; enqueue a lightweight permission read: fetch channel description (Twitch `GET /helix/channels?broadcaster_id=` is for title only; use `GET /helix/users?id=` → `description`; YouTube channel description via Data API `channels.list part=snippet` with the API key) and classify with prompt P6 → `preferred` / `auto` / `restricted`. Re-check every 14 days.
- Never touch `restricted` or `blocked` creators.

## 3. Trend scores

All parts normalised to 0..1 before weighting; final score 0..100.

### Live streams
| Part | Normalisation |
|---|---|
| viewers | `min(1, log10(viewers+1) / 5)` (100k viewers → 1) |
| growth | viewers now vs 30 min ago from `candidates` history: `clamp((now/then − 1) / 1.0, 0, 1)`; 0.5 if no history |
| chat_rate | messages per minute (from the live chat if already recorded; else Twitch `viewer_count` proxy 0.5) normalised by `min(1, log10(mpm+1)/3)` |
| global_reach | 1.0 if language = en, 0.6 otherwise; +0.2 if `creators.audience_spread` > 0.5 (cap 1) |
| history | `creators.avg_clip_views` percentile among our creators; 0.5 if unknown; −0.5 per claim (floor 0) |

### Videos
| Part | Normalisation |
|---|---|
| views_per_hour | `min(1, log10(vph+1) / 5)` |
| engagement | `(likes + 2×comments) / views`, scaled so 0.08 → 1 |
| size_ratio | `views / max(follower_count, 1000)`, scaled so 1.0 → 1 |
| global_reach | as above |
| history | as above |

Then: × `discovery.non_english_penalty` for non-English; + `discovery.preferred_bonus` for preferred creators; skip titles matching `discovery.title_blocklist`; skip videos shorter than `discovery.min_vod_duration_s`.

## 4. Live slot assignment

1. Rank live candidates with score ≥ `live_score_cutoff`.
2. One source per creator (if a creator is live on both, keep the higher score).
3. Keep currently recording sessions unless they've been below the cutoff for `release_after_below_s` (set `below_cutoff_since` when they first drop).
4. Fill free slots up to `live.max_slots` with the best new streams → insert `live_sessions` (status `assigned`).
5. Sessions to stop → status `released`. `live-manager` reacts within 15 s.

## 5. VOD queueing

1. Rank video candidates with score ≥ `vod_score_cutoff`, not already in `videos`.
2. Insert up to `max_vod_per_run` rows into `videos` (status `new`) and enqueue `vod.process` jobs with `dedupe_key = 'vod:<video_id>'` and priority 30.
3. If the hourly queue is below `posting.queue_low_watermark`, double `max_vod_per_run` and lower `vod_score_cutoff` by 10 for this run (guardian also alerts).

## 6. Bookkeeping

- Insert every observation into `candidates` (needed for growth maths).
- Write heartbeat `trigger:discovery`.
- Event `discovery.run` with counts: live seen, assigned, released, videos queued, new creators.
