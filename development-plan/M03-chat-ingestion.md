# M03 — Chat Ingestion (live + replay adapters)

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 02:30–04:00 UTC · **Requires:** M02 · **Launch-critical:** yes

## Goal
Turn every chat source into one stream of normalised `ChatMessage` objects, filtered for bots and system notices, ready for the spike engine (M04).

## Context you need
The spike engine detects moments from chat bursts. It needs, per message: time, user id, text and kind. Sub/gift/raid/donation/system messages must be marked so they can be dropped (they cause fake spikes). Two modes:
- **Live**: messages arrive in real time; time = wall clock (unix seconds, UTC).
- **Replay**: a whole downloaded chat file; time = seconds from the start of the VOD.

### The data contract (`src/sixstars/chat/models.py`)
```python
@dataclass(frozen=True, slots=True)
class ChatMessage:
    platform: Literal["twitch", "youtube"]
    t: float
    user_id: str
    username: str
    text: str
    kind: Literal["chat", "notice", "sub", "gift", "raid", "donation", "system"]
    is_mod: bool = False
    is_broadcaster: bool = False
```

## Build
1. **`chat/twitch_irc.py`** — async live client:
   - `wss://irc-ws.chat.twitch.tv:443`; send `PASS SCHMOOPIIE`, `NICK justinfan<5 random digits>`, `CAP REQ :twitch.tv/tags twitch.tv/commands`, `JOIN #<login>`.
   - Reply `PONG :tmi.twitch.tv` to `PING`. Reconnect with backoff (1, 2, 5, 10, 30 s) on disconnect or `RECONNECT`.
   - Parse IRCv3 tags. `PRIVMSG` → kind `chat` (`tmi-sent-ts`/1000 → t; `user-id`; `display-name`; badges `broadcaster/1`, `moderator/1`). `USERNOTICE` → `msg-id` mapping: `sub`,`resub` → sub; `subgift`,`submysterygift`,`giftpaidupgrade`,`anongiftpaidupgrade` → gift; `raid` → raid; others → notice. Messages with `bits` tag → donation.
   - API: `async def stream(login) -> AsyncIterator[ChatMessage]`.
2. **`chat/youtube_live.py`** — wraps `chat-downloader` (`ChatDownloader().get_chat(url)`) in a thread; maps `timestamp` (µs) → t; `author.id`; `message_type`: `text_message` → chat, `paid_message`/`paid_sticker` → donation, `membership_item` → sub, others → notice. Async iterator via a queue.
3. **`chat/twitch_replay.py`** — runs `TwitchDownloaderCLI chatdownload --id <vod_id> -o <path>` (⚠️ check current flags with `TwitchDownloaderCLI chatdownload --help`; disable embedded images/emotes to keep the file small), then parses JSON: `comments[].content_offset_seconds` → t, `commenter._id`, `commenter.display_name`, `message.body`, notices via `message.user_notice_params` or `message.fragments` when present; bits → donation. Returns `list[ChatMessage]` sorted by t, plus metadata (duration, message count).
4. **`chat/youtube_replay.py`** — `chat-downloader` on a past-live video URL; `time_in_seconds` → t. Detect "no chat replay" and return `None` (VOD lane falls back to transcripts).
5. **`chat/filters.py`**:
   - `is_counted(msg, ctx) -> bool`: kind == chat; not broadcaster; username not in `settings spikes.ignore_users` or `ctx.bot_names`; text not empty; `t ≥ ctx.stream_start + spikes.warmup_s`.
   - `learn_bots(messages) -> set[str]`: users with ≥ 20 messages where ≥ 80% share the same first 20 characters, or names ending in "bot".
   - `cap_per_user(messages_in_bucket, 3)`.
6. Fixtures: save a trimmed real Twitch replay (~2,000 messages, GTA 5 RP stream with an obvious burst) and a YouTube replay sample into `tests/fixtures/`. Also `tests/fixtures/synthetic_chat.py` generators: flat chat, burst, bot flood, sub train, raid.
7. CLI: `sixstars chat dump <twitch-login|youtube-url> --seconds 60` (prints live messages), `sixstars chat replay <vod-id|url> -o file.jsonl`.

## Acceptance tests
- Unit: IRC line parser on recorded raw lines (PRIVMSG with emotes, USERNOTICE sub, raid, bits); replay parsers on fixtures produce the right counts and kinds; filters drop bots/notices/broadcaster/warm-up.
- Live smoke (manual, staging): `sixstars chat dump <a live GTA 5 Twitch channel> --seconds 60` prints messages; same for a live YouTube GTA stream.
- Replay smoke: download chat for one real GTA 5 VOD (4+ hours) in under 60 seconds.

## Definition of done
Adapters, filters, fixtures, tests merged; TwitchDownloaderCLI install steps recorded in PROGRESS.md (needed again in the Dockerfile, M11).

## Handoff to M04
M04 consumes `Iterable[ChatMessage]` (replay) and `AsyncIterator[ChatMessage]` (live) and applies `filters.is_counted`.
