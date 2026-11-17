# fix-proxy-failure-class — a proxy that refuses parks the work, it does not fail it

Date: 2026-11-17. Branch `fix-proxy-failure-class` (from `develop`). Not a milestone: a production-readiness
fix.

## The bug

YouTube requests can go through the owner's HTTP proxy (`PROXY_URL`). When the proxy itself refuses (no
balance, suspended account, wrong credentials, a provider policy against the target) yt-dlp reports
`Unable to connect to proxy … Tunnel connection failed: 407 Proxy Authentication Required` (or
`407 Account is suspended`, `402 Payment Required`, `403 Forbidden`). A 407 or 402 matched no marker of
`vod.ytdlp.classify_failure`, so it was an ordinary error: three attempts, then `give_up` marked the video
`failed` for good. A paid proxy running out of money would have failed every queued YouTube video.

## What changed

| Where | Change |
|---|---|
| `src/sixstars/chat/net.py` | The one shared classifier: `proxy_refusal(text) -> ProxyRefusal \| None` (pure, case-insensitive; `status` is the HTTP status the proxy answered with, or None when it could not be reached; `.summary` = `407 Proxy Authentication Required` / `no answer (connection refused)`; `.described` = `proxy refused: <summary>`). `redact_proxy(text, proxy=None)` removes the configured proxy URL and the `user:password@` part of any URL. `request_error(exc)` names a failed HTTP request and, for a proxy error, adds what the proxy answered. `PROXY_FIX` is the one sentence that says where the fix is. |
| `src/sixstars/vod/ytdlp.py` | New failure kind `proxy` and `ProxyRefusedError(DownloadBlockedError)`. Order in `classify_failure`: the proxy when it is the last error line; then bot check; then unavailable; then the proxy anywhere in the output; then the media server's 403; timeout; error. `scrub` uses `redact_proxy`. |
| `src/sixstars/vod/process.py` | `_blocked` recognises the proxy in the detail: the reason, the `vod.download_blocked` event and `vod.source_health` say `proxy refused: <code> <text>`, and the at-most-hourly warn has its own title (`PROXY_TITLE`) and body (`proxy_message`). `_load_chat` now also parks the video when the chat replay request fails on the proxy (before, any `ChatSourceError` that was not `VideoUnavailableError` used up attempts). |
| `src/sixstars/live/recorder.py`, `live/session.py` | `classify_failure` returns `blocked` (detail `proxy refused: …`) when the proxy answered with a refusal, or could not be reached before any data came. `looks_blocked` (live chat error) also asks `proxy_refusal`. |
| `src/sixstars/live/health.py` | Three lines: the "Fix:" sentence of the one warn is `PROXY_FIX` when the stored error is a proxy refusal (the platform text says "set PROXY_URL or cookies", which would be wrong here). Back-off, counting, recovery are untouched. |
| `src/sixstars/chat/youtube_innertube.py`, `youtube_replay.py` | The two `httpx.HTTPError` mappings use `request_error` (a `ProxyError` now carries `407 …`); the yt-dlp fallback's error text is redacted. |
| `src/sixstars/discovery/doctor.py` | `failure()` turns a proxy failure into `BLOCKED` with detail `proxy refused: <code> <text> (<error>)` for every row that goes through it (`chat_replay`, `window_download`, `ytdlp_search`); the footer then says "Refused by the proxy (PROXY_URL): …" with `PROXY_FIX` instead of "set a proxy or cookies". |

### Behaviour in the VOD lane

Exactly the download-blocked state of M08 / fix-blocked-platform-queue: the defer kind stays
`download_blocked` (discovery's backlog rule and `store.block_retry_at` key on it), so the video goes back to
`new` with `skip_reason = download_blocked: proxy refused: 407 Proxy Authentication Required`, the job is
queued again after `vod.blocked_backoff_s` as a fresh job (no attempt used), `give_up` is never reached,
`vod.source_health` records the platform, one warn per `vod.blocked_notify_interval_s`. No new setting, no
migration.

### Decisions

- **Last error first.** yt-dlp retries; a proxy hiccup followed by the bot check or "Private video" keeps
  its old class. The tunnel's `403 Forbidden` / `429` on the last error line is the proxy's answer and wins
  over the platform markers.
- **A proxy that cannot be reached** (connection refused, timeout, name resolution) is the same class in the
  VOD lane and at a live session start. In the middle of a live recording it stays `network` (reconnect as
  before), so a short proxy drop does not end a recording; a refusal with a status code ends it as blocked.
- **`proxy` is a `DownloadBlockedError`**, so captions, the transcript fallback, window downloads and the
  doctor needed no new `except`.

## Verified

- Unit: `tests/unit/test_proxy_failure.py` (table-driven: every string of the report and variants, the
  existing bot-check / unavailable / 403 / unrelated strings, redaction, the httpx mapping, the live lane
  including one warn and one recovery, the doctor rows).
- Integration: `tests/integration/test_vod_process.py` (a 407 on the last attempt of a job parks the video;
  five refusals in a row never fail it; no attempt used; record, message, no credentials anywhere in the
  database; the backlog drains when the proxy works again; the chat replay path; a non-proxy chat error is
  unchanged).
- By hand on the development machine: a local socket that answers `CONNECT` with 407 / 402 / 403 /
  `407 Account is suspended`, used as the proxy of the real yt-dlp, streamlink and httpx. All three print
  the strings assumed here and are classified as intended; a port nobody listens on gives
  `no answer (connection refused)`.

## Open issues

- Not run against a real proxy provider or on the staging server. Bright Data's `x-brd-err-code` header is
  not in the error text of yt-dlp, so the message shows the status line only.
- When the proxy cannot be reached, httpx raises `ConnectError`, which names no proxy; the chat path
  recognises that case only through the yt-dlp fallback that follows it (its text says "Unable to connect to
  proxy").
- The live warn still begins "Live source youtube is blocked" and the session outcome says "the platform
  refused this machine: proxy refused: …"; only the error and the fix sentence name the proxy.
- A bot check reported by the chat replay's yt-dlp fallback as a plain `ChatSourceError` is still an ordinary
  error, as before this fix.
