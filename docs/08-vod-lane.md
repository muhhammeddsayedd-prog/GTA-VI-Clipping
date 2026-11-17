# 08 — VOD Lane (chat-first)

Processes finished streams and uploads. Job `vod.process` (queue normal) and `replay.scan` (same handler, plus live overlap skipping).

## 1. Steps

| # | Step | Detail |
|---|---|---|
| 1 | Claim | `videos.status = processing`, attempts + 1 |
| 2 | Get chat | Twitch: `TwitchDownloaderCLI chatdownload --id <vod_id> -o <dir>/chat.json --embed-images false` (check the CLI's current flags). YouTube: `chat-downloader` on the video URL (works only for past live streams with chat replay). Save to `chat_files`. If no chat → step 9. |
| 3 | Find spikes | Replay detector over the whole file (`docs/05`). Keep the best `vod.max_spikes_per_video` by score. |
| 4 | Skip clipped ranges | If `live_session_id` is set, drop spikes overlapping `clipped_ranges` (±30 s). |
| 5 | Confirm | Claude P1 per spike, batched (up to 10 spikes per call to save cost). |
| 6 | Download windows only | For each confirmed spike: `yt-dlp --proxy $PROXY_URL --download-sections "*<start−pad>-<end+pad>" --force-keyframes-at-cuts -f "bv*[height<=1080]+ba/b[height<=1080]" -o <dir>/<spike_id>.%(ext)s <url>` |
| 7 | Queue renders | `clip.render` job per spike (priority 55 for S/A, 40 for B), payload has the local file path and window offsets |
| 8 | Done | `videos.status = done`; delete chat file after 7 days |
| 9 | Fallback (no chat) | Download full video (≤ 1080p), transcribe locally (faster-whisper `vod.whisper_model`, word timestamps), Claude P3 picks 3–8 moments → `spikes` rows with mode `transcript` → step 6 cut locally with ffmpeg → `clip.render` |

## 2. Rules

- Prefer chat-first sources: discovery gives videos with chat replays (Twitch VODs, YouTube past streams) a +5 score bonus.
- Skip videos longer than 12 hours (fallback only; chat-first handles any length).
- If yt-dlp fails 3 times on a video → `failed`, event, continue.
- The replay detector uses the whole-stream median rate in `chat_files.baseline_rate` as a sanity floor: a spike must also exceed 2× that median.
- Transcription for `clip.render` happens in the factory step on the short window, not here.

## 3. Why chat-first

A 6-hour stream used to mean downloading and transcribing 6 hours. Chat-first downloads a few MB of chat, finds 10–40 spikes, and pulls only those minutes. Same server can process many more streams per day, which keeps 24 hourly slots supplied.
