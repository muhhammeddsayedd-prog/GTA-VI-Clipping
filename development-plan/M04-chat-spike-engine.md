# M04 — Chat-Spike Engine

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 04:00–06:00 UTC · **Requires:** M03 · **Launch-critical:** yes

## Goal
The core moment detector: one engine with a **replay mode** (scan a whole chat file) and a **live mode** (sliding window over real-time chat) that produce identical spike decisions, tiers and clip windows.

## Context you need (from the reference video "GTA 6 clipping, Part 2: chat tells you")
When something crazy happens on stream, chat explodes (hundreds of "LMAO" in 10 seconds). Count chat in 10-second buckets; where it spikes is the clip. Chat reacts *after* the moment, so the clip starts **20 seconds before** the spike. Our improvements: unique chatters (so a few spammers can't fake it), reaction-word ratio, "clip it" requests, optional audio jump, all as z-scores against the stream's own trailing baseline; bot/notice filtering (M03); Claude confirmation (added in M05/M08 — here only the numeric engine).

## Spec (implement exactly; all numbers are settings)

### Buckets (`spikes/buckets.py`)
Per bucket of `spikes.bucket_s` (10 s), using only messages where `filters.is_counted` is true and at most 3 messages per user:
- `msgs`, `uniq` (distinct users), `react` = share of messages containing ≥ 1 token from `spikes.reaction_tokens` (case-insensitive; multi-word tokens matched as substrings; emoji as characters), `clipreq` = messages containing a token from `spikes.clip_tokens`, `audio` = loudness jump in dB (0 when unknown).
- Replay: fixed buckets from t=0. Live: every `spikes.live_step_s` (5 s) compute the bucket for the last 10 s.

### Baseline + score (`spikes/baseline.py`, `spikes/scoring.py`)
- Baseline buckets: the trailing `spikes.baseline_s` (600 s) **excluding the most recent 30 s**. Need ≥ 12 baseline buckets, else no detection.
- For each feature: mean, std; `std = max(std, spikes.min_std[feature])`; `z = (value − mean)/std`, clamped to [−3, 10].
- `score_z = Σ spikes.weights[f] × z_f` (msgs 0.40, uniq 0.25, react 0.20, clipreq 0.10, audio 0.05).

### Detection + tiers (`spikes/detector.py`)
- Candidate bucket: `score_z ≥ spikes.z_b` (2.5) and `msgs ≥ spikes.min_messages` (15). In replay mode also `msgs ≥ 2 × whole-file median msgs`.
- Consecutive candidate buckets form one spike; **peak** = bucket with max score_z; **first** = first candidate bucket.
- Merge spikes whose peaks are < `spikes.merge_gap_s` (60 s) apart (keep the higher; mark other merged).
- Provisional tier: S if `score_z ≥ z_s` (4.0) and `uniq ≥ s_unique_multiple (3.0) × baseline mean uniq`; else A if `≥ z_a` (3.0); else B.
- Live mode emits a spike when the score has fallen below `spikes.end_z` (1.5) for `tail_s` (5 s) **or** the spike has lasted `max_len_s − lead_s` seconds.

### Window (`spikes/window.py`)
```
start = first.t − spikes.lead_s (20) − live.chat_offset_s[platform]
end   = (first time after peak where score_z < end_z) + spikes.tail_s
clamp length to [min_len_s 15, max_len_s 60]; if too long keep [start, start+max_len_s]
spike_floor = first.t  (later stages may never move the start after this)
```
Return also the padded download window `[start − context_pad_s, end + context_pad_s]` (15 s).

### Output dataclass
```python
@dataclass
class DetectedSpike:
    mode: Literal["live","replay"]
    peak_t: float; first_t: float
    score_z: float; parts: dict[str, float]
    msgs: int; uniq: int; reaction_count: int; clip_requests: int
    tier: Literal["S","A","B"]
    window_start: float; window_end: float; spike_floor: float
    pad_start: float; pad_end: float
    chat_sample: list[dict]   # 40 messages around the peak: {t_rel, user, text}
```

### APIs
- `scan_replay(messages: list[ChatMessage], stream_start: float = 0) -> list[DetectedSpike]`
- `class LiveSpikeDetector: feed(msg) ; tick(now) -> list[DetectedSpike]` (call `tick` every 5 s)
- `persist_spike(detected, *, live_session_id|video_id, creator_id) -> spike_id` writes a `spikes` row (status `detected`).
- CLI `sixstars spikes scan <chat.json|jsonl>` prints a table: rank, time (hh:mm:ss), tier, score_z, msgs, uniq, top tokens, window.

## Build
Implement the modules above with pure functions where possible (no DB in scoring/window code). Settings are read once per scan/tick and passed in as a frozen `SpikeConfig`.

## Acceptance tests (unit, no network)
1. Flat 5 msgs/10 s for 20 min then one bucket with 120 msgs from 80 users → exactly one spike, tier S or A, at the right time.
2. Same burst from 3 users → not S (unique too low).
3. 200 sub/gift notices in a minute → no spike.
4. Two bursts 40 s apart → one spike (merged).
5. Window: first bucket at 990 s → start 970 s; end/length clamps work; spike_floor = 990.
6. Steady 300 msgs/10 s ± noise for 30 min → no spikes.
7. Live vs replay on the same message list → same spikes (peak within one bucket, same tier).
8. Fewer than 12 baseline buckets → no detection.
Real data: `sixstars spikes scan tests/fixtures/chat_twitch_replay_small.json` finds the known burst in the top 3. Then run it on one full real GTA 5 RP VOD chat (downloaded with M03) and record the top 10 spikes + your spot-check notes in PROGRESS.md.

## Definition of done
Engine + CLI + tests merged; real-VOD spike table in PROGRESS.md.

## Handoff
M08 (VOD lane) calls `scan_replay`; M14 (live lane) uses `LiveSpikeDetector`; M05 adds Claude confirmation (P1) used by both.
