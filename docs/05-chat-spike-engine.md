# 05 — Chat-Spike Engine

The core moment detector. One engine, two modes: **live** (messages arrive in real time) and **replay** (a whole chat file is scanned at once). Both modes share the same bucketing, baseline, scoring, tiering and window code, so they always agree on what a spike is.

Origin: the "GTA 6 clipping, Part 2: chat tells you" reference video (chat explodes after a big moment → count chat in 10-second buckets → cut where it spikes → start the clip 20 seconds early). This engine automates that and adds unique-chatter counting, reaction weighting, per-stream baselines, bot filtering and Claude confirmation.

## 1. Input: ChatMessage

```python
@dataclass(frozen=True)
class ChatMessage:
    platform: Literal["twitch", "youtube"]
    t: float            # live: unix seconds (wall clock); replay: seconds from VOD start
    user_id: str        # stable id; fallback to lowercase username
    username: str
    text: str
    kind: Literal["chat", "notice", "sub", "gift", "raid", "donation", "system"]
    is_mod: bool = False
    is_broadcaster: bool = False
```

Adapters (in `chat/`) convert each source into ChatMessage:
- **Twitch live (IRC)**: `PRIVMSG` → `chat`; `USERNOTICE` with `msg-id` in (sub, resub, subgift, submysterygift, raid, …) → matching kind. `t = tmi-sent-ts / 1000`.
- **Twitch replay (TwitchDownloaderCLI JSON)**: `comments[].content_offset_seconds` → `t`; `commenter._id` → `user_id`; `message.body` → `text`; `message.user_notice_params`/`message.fragments` detect notices where present.
- **YouTube live/replay (chat-downloader)**: `timestamp` (µs) → `t` for live; `time_in_seconds` → `t` for replay; `author.id` → `user_id`; `message_type` (`text_message`, `paid_message`, `membership_item`, …) → kind.

## 2. Filtering (before counting)

Drop a message if any is true:
1. `kind != "chat"` (subs, gifts, raids, donations and system notices are not reactions — they cause fake spikes).
2. `username` in `spikes.ignore_users` or in the session's learned `bot_names`.
3. Broadcaster's own messages.
4. Empty text after stripping.
5. `t` within the first `spikes.warmup_s` of the stream.

**Bot learning** (first 10 minutes of a session, and over the whole file in replay): a user is a bot if they post ≥ 20 messages with ≥ 80% identical-prefix texts, or their name ends in `bot`. Store in `live_sessions.bot_names` / `chat_files.bot_names`.

**Spam control**: each user counts at most 3 messages per bucket (extra messages ignored). Copy-paste walls still count, since chat spamming the same emote *is* a reaction, but at most 3 per user.

## 3. Bucketing

For each bucket of `spikes.bucket_s` (10 s):

| Field | Definition |
|---|---|
| `msgs` | messages after filtering (≤ 3 per user) |
| `uniq` | distinct `user_id`s |
| `react` | messages containing at least one reaction token ÷ `msgs` (0 if msgs = 0) |
| `clipreq` | messages containing a clip token |
| `audio` | loudness jump in dB vs previous 30 s (live mode, or replay when audio is available); 0 if unknown |

Tokenising: uppercase the text, split on whitespace and punctuation, also match multi-word tokens ("NO WAY", "CLIP IT") as substrings. Emoji tokens are matched as characters.

- **Replay mode**: fixed buckets `[0,10), [10,20), …` over the whole file.
- **Live mode**: every `spikes.live_step_s` (5 s), compute a bucket over the last 10 s (so buckets overlap by 5 s and a spike is never split in half).

## 4. Baseline and z-scores

Baseline = buckets in the trailing `spikes.baseline_s` (600 s) **excluding** the most recent 30 s (so a rising spike doesn't inflate its own baseline).

For each feature `f` in (msgs, uniq, react, clipreq, audio):
```
mean_f, std_f = mean/std of f over baseline buckets
std_f = max(std_f, spikes.min_std[f])
z_f = (value_f − mean_f) / std_f
```

Spike score:
```
score_z = Σ weights[f] × clamp(z_f, −3, 10)
```
with `spikes.weights` = msgs 0.40, uniq 0.25, react 0.20, clipreq 0.10, audio 0.05.

If fewer than 12 baseline buckets exist (start of stream), skip detection.

## 5. Detecting and tiering

A bucket is a **spike candidate** when `score_z ≥ spikes.z_b` **and** `msgs ≥ spikes.min_messages`.

Group consecutive candidate buckets into one spike; the spike's position is the bucket with the highest `score_z` (the **peak**). Spikes whose peaks are closer than `spikes.merge_gap_s` merge (keep the higher, mark the other `merged`).

Provisional tier from numbers:
| Tier | Rule |
|---|---|
| S | `score_z ≥ z_s` and `uniq ≥ s_unique_multiple × mean_uniq` |
| A | `score_z ≥ z_a` |
| B | `score_z ≥ z_b` |

Then **Claude confirmation** (`docs/15-claude-prompts.md` P1, fast model) with the chat sample (40 messages around the peak) and, when available, the transcript of the window. Outcomes:
- `is_moment = false` → `rejected`.
- S provisional needs `confidence ≥ s_min_confidence`, else downgraded to A.
- A/B need `confidence ≥ ab_min_confidence`, else `rejected`.

In **replay mode, S is treated as A** for routing (a replay is never "just happened"); the tier is still stored as S for analytics.

## 6. Clip window ("start 20 seconds early")

```
peak_t   = time of the peak bucket start
first_t  = start of the first candidate bucket in the spike
start    = first_t − lead_s − chat_offset_s[platform]          (lead_s default 20)
end      = first time after peak_t where score_z < end_z, plus tail_s (5 s)
length   = clamp(end − start, min_len_s, max_len_s)
```
- If the computed length exceeds `max_len_s`, keep `[start, start + max_len_s]` and let Claude trim (P2 can move the end earlier but not later than computed end).
- Claude's writer prompt receives the window plus `context_pad_s` (15 s) either side and may move the **start earlier** (setup began sooner) or the **end later** (payoff came after), within the min/max length. It may **never** move the start after the first spike bucket.
- `chat_offset_s` defaults to 0 for both platforms; tune it in staging by comparing detected windows with the actual moment.

## 7. Outputs

Each spike writes one `spikes` row: mode, session/video, `spike_wall` (live) or `spike_offset_s` (replay), `score_z`, `parts` (all z-scores), counts, `chat_sample`, tier, Claude confidence/reason, window (offsets and/or wall-clock), status.

Routing:
| Mode | Tier | Route |
|---|---|---|
| live | S | `express.render` job (queue express, priority 100) |
| live | A | `clip.render` (normal, priority 60) |
| live | B | `clip.render` (normal, priority 40) |
| replay | S/A | `clip.render` (normal, priority 55) |
| replay | B | `clip.render` (normal, priority 40) |

## 8. Testing the engine

Unit tests with synthetic chat (no network):
1. Flat chat at 5 msgs/10 s for 20 minutes, then 120 msgs from 80 users in one bucket → one S/A spike at the right time.
2. Same burst but from 3 users → no S (unique chatters too low), maybe B at most.
3. A sub train of 200 USERNOTICEs → no spike.
4. Two bursts 40 s apart → merged into one.
5. Window maths: peak at t=1000 s, first bucket 990 s → start 970 s.
6. Big stream with steady 300 msgs/10 s → normal fluctuations don't spike (min_std floor and baseline).
7. Live and replay modes on the same message list give spikes within one bucket of each other.

Real-data test (staging): run `sixstars spikes scan <chat.json>` on a finished 4–6 hour GTA 5 roleplay stream and confirm by watching that ≥ 70% of the top 10 spikes are genuinely clip-worthy.
