# 19 — Testing Plan

## 1. Levels

| Level | Where | What | When |
|---|---|---|---|
| Unit | CI, local | Pure logic with no network: spike scoring, baseline, window maths, tiering, precedence rules, cap maths, ranking, caption building, subtitle line grouping, profanity matching, trend scoring, settings parsing | Every milestone |
| Integration | CI (Postgres container) | DB queries, job queue claim/retry/dedupe, migrations, API routes with fake clients | From M01 |
| Component (real tools) | Local / staging | ffmpeg renders, TwitchDownloaderCLI, chat-downloader, yt-dlp sections, streamlink recording, faster-whisper, PANNs | Milestone acceptance |
| End-to-end | Staging (GTA 5) | Full loop: discovery → live/VOD → spikes → render → gate → posting to private accounts | M15 |
| Launch rehearsal | Production (dry run) | Everything on GTA 6 content with `dry_run = true` | M16, before 11:00 UTC |

## 2. Fixtures (in `tests/fixtures/`)
- `chat_twitch_replay_small.json` — trimmed TwitchDownloader output (~2,000 messages) with one known spike.
- `chat_youtube_replay_small.json` — chat-downloader output sample.
- `synthetic_chat.py` — generators for flat chat, bursts, bot floods, sub trains, raids.
- `clip_source_30s.mp4` — 1920×1080 test video with a facecam box (create with ffmpeg test sources: `testsrc2` + a coloured rectangle as "facecam") and speech-like audio (`sine` + a short TTS-free tone pattern) for pipeline tests.
- Transcript JSON matching the fixture video.

## 3. Mandatory unit test cases

Spike engine: the 7 cases in `docs/05` §8.

Precedence and caps (each as a table-driven test):
1. Spike at hourly slot → spike posts, hourly waits.
2. Spike 10 min after a post → waits 5 min then posts.
3. Spike 20 min before slot → slot skipped on that account.
4. Third spike in an hour → demoted.
5. Spike older than 30 min → becomes hourly with boosted rank.
6. TikTok main at 14/14 → skipped, others post.
7. Spike reserve: hourly cannot take the last `spike_reserve` posts.
8. Emergency stop → nothing posts.
9. Dry run before launch_at → `posts` rows with `dry_run = true`, nothing sent.
10. Hourly picks a different creator than the last hourly post.

Ranking: freshness half-life maths; expiry after 72 h.

## 4. Staging acceptance checklist (M15)

| # | Check | Pass when |
|---|---|---|
| A1 | Discovery finds GTA 5 streams and videos | ≥ 20 live candidates, ≥ 10 videos in one run |
| A2 | Live recording | 3 streams recorded for 2 hours without manual help; buffer stays ≤ 30 min |
| A3 | Live spikes | ≥ 5 confirmed spikes in 2 hours across 3 streams; replay scan of the same streams afterwards finds the same top spikes (± one bucket) |
| A4 | Express path | 3 S-tier clips spike→(private) post in < 4 min median |
| A5 | VOD lane | One 4–6 h stream → ≥ 8 clips; ≥ 70% judged clip-worthy by the owner (one-time spot check) |
| A6 | Render quality | Subtitles in sync, highlight works, safe zones respected, bleeps land on the right words, layout A and B both correct |
| A7 | Gate | Test clips with music, black frames, a watermark, a spoiler phrase, a low score are all rejected with the right reason |
| A8 | Pre-check | A clip with copyrighted music (owner-provided test) is caught by the private YouTube pre-check |
| A9 | Posting | Hourly lane posts privately to all staging accounts for 6 consecutive hours; spike lane interleaves correctly |
| A10 | Caps | Forcing TikTok cap to 2 skips TikTok only |
| A11 | Guardian | Fake takedown email → creator blocked, (private) YouTube post deleted, alert received |
| A12 | Emergency stop | `/stop` stops posting within one slot; `/start` resumes |
| A13 | Failover | Disable WF02 for 75 min → scheduler posts the slot and alerts |
| A14 | Crash recovery | `docker kill` each service → it restarts and resumes; no duplicate posts |
| A15 | Report | Daily report arrives with real numbers |
| A16 | Spend | Spend in 24 h within budget settings |

## 5. Load test
Run 3 live recordings (staging max) + 2 concurrent VOD jobs + continuous express renders from a replayed chat fixture (inject spikes every 2 minutes) for 1 hour. Pass: express p90 < 6 min, no job stuck, CPU < 90% average, disk stable.

Production capacity check (M16, dry run): 6 live slots + VOD lane for 2 hours; same criteria.
