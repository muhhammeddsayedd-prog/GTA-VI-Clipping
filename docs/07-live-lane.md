# 07 — Live Lane

Runs inside the `live-manager` service (one asyncio process).

## 1. Session lifecycle

```
assigned ──start──► recording ──stream ends / released──► ended ──VOD found──► replay_queued ──► replay_done
     └────────────── errors after max reconnects ──────────► failed
```

Every 15 s the manager:
1. Loads sessions in `assigned` → starts recorder + chat listener → `recording`.
2. Loads sessions in `released` → stops tasks → `ended`.
3. Checks each recording task; if the stream went offline (recorder exits and Twitch/YouTube says offline) → `ended`.
4. Writes heartbeat `live-manager` with active session ids.

## 2. Recorder

Command (per session):
```
streamlink --stdout --http-proxy $PROXY_URL --twitch-disable-ads <url> 1080p60,1080p,936p60,720p60,720p,best \
  | ffmpeg -hide_banner -loglevel error -i pipe:0 -c copy -map 0 \
      -f segment -segment_time 60 -reset_timestamps 1 -strftime 1 \
      $DATA_DIR/live/<session_id>/seg_%Y%m%dT%H%M%S.ts
```
- Segment file names carry the **wall-clock start time** (UTC; set `TZ=UTC` in the container).
- A buffer janitor deletes segments older than `live.buffer_s` (30 min) unless a render job has pinned them (pin = a `.pin` file listing segment names, removed when the render job finishes).
- Reconnect up to `live.max_reconnects` times with 10 s backoff; increment `reconnects`.

### Extracting a window
`buffer.extract(session, start_wall, end_wall)`:
1. Pick segments overlapping the window.
2. Concatenate with the concat demuxer into a temp `.ts`.
3. Trim with `-ss`/`-to` relative to the first segment's start (re-encode in the render step, so input-side seek accuracy is fine).
4. If the window's end is in the future (spike still running), wait until the segment containing `end_wall` is closed (max 70 s).

## 3. Chat listeners

- **Twitch**: websocket `wss://irc-ws.chat.twitch.tv:443`, `PASS SCHMOOPIIE`, `NICK justinfan<random 5 digits>`, `CAP REQ :twitch.tv/tags twitch.tv/commands`, `JOIN #<login>`. Answer `PING` with `PONG`. Parse tags for `tmi-sent-ts`, `user-id`, `display-name`, `badges` (broadcaster/moderator), `msg-id` on `USERNOTICE`. Reconnect on drop.
- **YouTube**: `chat-downloader` `ChatDownloader().get_chat(url, message_groups=['messages','superchat'])` in a thread; push messages into an asyncio queue.

Each listener feeds a `LiveSpikeDetector` instance (see `docs/05`). Chat and recorder share the session's wall clock (UTC).

## 4. Live spike flow

1. Detector emits a spike candidate when the window closes (score has fallen below `end_z` + tail) **or** when the spike has lasted `max_len_s − lead_s` seconds (don't wait forever).
2. Insert `spikes` row (mode `live`, provisional tier).
3. Claude confirmation (P1, fast model) with 40 chat messages around the peak. (No transcript yet — keeps it fast.)
4. Route: S → `express.render` job; A/B → `clip.render` job; pin buffer segments.
5. Append the window to `live_sessions.clipped_ranges` once the clip renders.

## 5. Express path (S-tier) — target under 4 minutes spike-to-post

Handled by `worker-express` (2 processes, never paused by CPU rules):

| Step | Budget |
|---|---|
| Extract window + context pad from buffer | 10 s |
| Hosted transcription (Groq; fallback OpenAI) with word timestamps | 20 s |
| Claude clip writer P2 (writer model) — hook, title, caption, keywords, trims, safety, score | 30 s |
| Facecam box: cached per creator; if missing, P4 vision call on one frame | 0–15 s |
| Render (preset `factory.preset_express`, or NVENC) | ≤ 90 s |
| Express gate profile | 15 s |
| Spike posting lane (`posting.spike.try_post`) | 20 s |

Record `spikes.spike_to_post_s = posted_at − spike_wall`.

## 6. Stream end → replay pass

When a session ends:
1. Twitch: poll `GET /helix/videos?user_id=<id>&type=archive&first=1` every 10 min for up to 2 h until a VOD whose `stream_id` matches appears. YouTube: the same video id becomes the replay; check `liveStreamingDetails.actualEndTime` via Data API.
2. Insert/attach a `videos` row with `live_session_id`, set session `replay_queued`, enqueue `replay.scan` (dedupe `replay:<session_id>`).
3. The VOD lane processes it like any video, but skips `clipped_ranges` (converted to VOD offsets: `offset = wall − session.started_at`, ±30 s) and never routes to the spike lane.
4. Delete the session's buffer directory.

## 7. Capacity rules

- Max `live.max_slots` recordings (6 in production).
- Each recording uses ~1–3 Mbit/s download and little CPU (stream copy).
- Express renders have 2 dedicated workers; normal renders have 4.
- If CPU > 85% for 2 minutes: normal workers pause claiming `vod.process` jobs; A/B renders continue; express never pauses.
