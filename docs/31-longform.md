# 31 — Long-form episodes (daily 16:9 "TOP N")

Decision D122 · plan `development-plan/M20-longform.md` · migration 0058 · settings `longform.*` (docs/16).

## What it does

Once a day, the engine builds one horizontal 1920×1080 YouTube video of 10:00 to 14:30. It uses the best 25–35 GTA 6 stream moments of the last 48 hours, all of which the Shorts engine has already found, scored, transcribed and titled. The video is a countdown from #N to #1 and posts on `youtube_main`.

## Flow

1. **Plan** (`longform.plan`, daily at `longform.build_hour_utc`). Reads the gated clips from the pool, ranks them, applies the variety rules, fills to `longform.target_s` and writes `episodes` + `episode_items`. If the total comes to less than `longform.min_s`, the episode is marked skipped.
2. **Fetch** (`longform.fetch_item`, one job per moment). Re-downloads the moment plus `fetch_pad_s` on each side.
   - VOD moments: from `clips.source_url`.
   - Live moments: from the archive VOD.
3. **Align and cut**. A local transcript of the fetched range is matched against the stored clip words. That gives the cut: from the sentence before the build-up to the first pause after the payoff, clamped to 15–45 s.
4. **Render segment** (`longform.render_item`). The frame is scaled and padded to 1920×1080 with no crop. On top go 16:9 subtitles, `#N` for 2 s and a creator credit for 3 s. Audio uses two-pass loudnorm. Every segment shares one format.
5. **Assemble** (`longform.assemble`). Joins cold open, N → 1 and end card, writes the chapters, makes one PL1 call for the title, description, tags and thumbnail text, renders a 1280×720 thumbnail, uploads to the `episodes` bucket over TUS, and notifies.
6. **Publish** (`longform.publish`, at `longform.publish_hour_utc`). Upload-Post fetches the file from a signed URL and sets the thumbnail from `thumbnail_url`. Privacy is `longform.publish_mode`.

## Day formats (go-live, 0061)

Days alternate (`longform.format_cycle`): a **mix day** (top moments of many creators, 10–14:30 min) and a
**creator day** (one top creator, 6–10 min, `longform.solo`). The creator is the one with the most good
moments in the last 7 days who had no creator episode in 14 days; nobody with enough moments → a mix day.

## Packaging and thumbnail (go-live)

- Title, description intro, hashtags, tags, thumbnail words and picture brief come from one P14 writer call
  (docs/15); the template is the fallback.
- The thumbnail is an OpenAI image edit (`longform.thumb`): the #1 and #2 peak frames (and the creator's
  profile picture on a creator day) repainted in the GTA 6 Vice City look described in
  `assets/longform/thumb_style.md` (distilled from the owner's reference thumbnails; those screenshots are
  never sent to OpenAI). The model never draws text: a small eyebrow line and a 1–3 word headline are
  rendered by `covers/compose` from `templates/thumb_16x9.html`. Refused → retry without the creator
  picture; failed, over budget or switched off → the #1 frame with the same type.

## Safety

- Publishing respects `emergency_stop`, `dry_run` and `longform.publish_mode` (`off`, `private` or `public`). Staging is always private.
- These are never in an episode: takedown or blocked creators, creators with P6 `restricted` (no compilations), music flags, spoilers and safety flags.
- A takedown that names a creator in a live episode unpublishes that episode (`longform/takedown.py`, called
  from `guardian.enforce.act_on_creator`); an episode not posted yet is cancelled (`removed`).
- An episode counts against the YouTube account's rolling 24 h total; when the Shorts lanes used the
  platform limit (`posting.platform_max_24h.youtube`), the episode waits for the next tick.
- Each creator gets at most `longform.max_creator_s` seconds and `longform.max_per_creator` moments per episode.

## Preview

The console Episodes page plays the episode with chapter seek. In staging, and in production while `publish_mode=private`, the episode also goes up as a private YouTube video. Telegram gets both links.
