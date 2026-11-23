# 30 — Title writer v2

Built 2026-11-23 from the research "GTA 6 clip titles: what the top 200 do and how we write ours" (top 200
English GTA 6 streamer clips on YouTube Shorts and TikTok, plus 175 GTA 6 Shorts of 11 competitor channels
compared against their own channel median).

## Why

Winning titles say **who, what happened and why it matters** in about 10 words: a big streamer's name first,
one specific event, a stake or a number, "in GTA 6", one or two emoji. Ours mostly said that chat reacted
("chat" in 23% of our titles, 1% of the top 200; "GTA 6" in 34% of ours, 98% of theirs). Inside the same
competitor channel, titles with a real stake earned 1.56x the channel median; plain in-game actions 0.33x.

The clip writer (P2) only knew the transcript and the chat, so it could only write about chat. v2 runs after
the gate approves a clip, when the visual judge (P9V) has already said what happens on screen.

## Flow

```
gate approves a clip ──▶ titles.service.after_approval (flag titles.v2 on?)
   facts  (titles.facts)   streamer name + tier, other streamers, event (P9V `seen`), peak line,
                           numbers (hours into stream, hours played, %, money), chat words, spoiler
   P11    (ai.calls.write_titles, prompt p11_title_writer.md, fast model)   5 candidates + caption_short
   checks (titles.checks)  pure code checks; nothing passes → one retry with the failures; still nothing
                           → fallback "<Streamer>'s <Topic> in GTA 6 😭" (event clip.title_fallback, warn)
   choose  first survivor in pattern order; on `explore_share` of clips (stable per clip id) the runner-up
   store   clip_titles row (facts, all candidates with failed checks, chosen, features)
           clips.title / caption / hashtags  (only while the clip is not posted)
           scoring_log stage "title", event clip.retitled
```

The hook runs before the spike handoff, so a spike post already carries the new title. It never raises: any
failure (API, budget, bad data) leaves the clip writer's title in place (log `title_v2_failed`). The P2
prompt is unchanged, so with the flag off nothing changes.

## Checks (code, not the model)

| Check | Rule |
|---|---|
| Name first | Starts with the streamer or a name in other_streamers (any alias; "Speed's ..." counts) |
| GTA 6 | Contains `gta_label` ("GTA 6") |
| Length | 6 to 12 words ("GTA 6" is one word), at most 70 characters |
| Banned | chat, loses it, losing it, lost it, goes sideways, off the rails, takes a turn, this moment, you won't believe, wait for it, insane, craziest (whole words) |
| Emoji | 0 to 2, all at the end |
| Caps | At most 1 ALL-CAPS word (GTA, VI, NPC, FPS, POV, RP, IRL and words with a digit do not count) |
| Format | No `#`, no `?`, not only a quote |
| Facts | Every number (except the 6 of GTA 6; "$10K" = 10000; a fact may be rounded) and every "first", "only", "record", "beat", "finish" must be in the facts |
| Names | Every streamer of the name book named in the title is the streamer or in other_streamers |
| Spoiler | For a story-spoiler clip: no ending, ends, finale, dies, death, twist, betray(al), killed off |
| Pattern | One of stakes, emotion, hot_take, streamer_on_streamer, discovery, fail |

The caption (TikTok and Instagram) must be under 80 characters, have no `#`, no banned phrase and no story
hint; otherwise the chosen title without emoji is used.

## Platform text

- YouTube title: the chosen title. Description: caption + credit + tags (unchanged code).
- TikTok and Instagram: `caption_short`, the credit line (D18, unchanged), then the base tags
  (`posting.base_hashtags`: #GTA6 #GTAVI #GTA6Clips) and the clip's own: `#<streamer>` `#streamerclips`.

## Names

`creators.title_name` is the name fans search ("Kai Cenat", "Vinesauce Vinny"); `creators.title_aliases` the
lowercase words that mean them in a transcript ("speed", "ishowspeed"). `display_name` is not used because
discovery overwrites it with the platform spelling ("KaiCenat"). Without a `title_name` the display name is
cleaned (underscores to spaces, a capital for an all-lowercase name, the handle for a non-Latin name); the
lowercase login is never used. Migration 0040 seeds 30 names. Streamers who are not our creators (N3on, Adin
Ross on Kick) come from `extra_names` in the flag.

Tier: S for `s_tier_names`; A and B by followers (`tier_followers`); C otherwise.

## Flag `titles.v2` (scoring_config)

| Key | Default | Meaning |
|---|---|---|
| enabled | false | Title clips after approval |
| model | "fast" | "fast" (`ai.model_fast`) or "writer" (`ai.model_writer`) |
| explore_share | 0.2 | Share of clips that get the runner-up |
| gta_label | "GTA 6" | How the game is written |
| tag_tail | ["streamerclips"] | Clip tags after `#<streamer>` |
| s_tier_names | 8 names | Streamers whose name alone pulls views |
| tier_followers | A 1M, B 200k | Follower floors of tiers A and B |
| extra_names | N3on, Adin Ross, Jynxzi, Clix | Names (with aliases) that are not our creators |

Rollback: set `enabled` to false. Titles already written stay; the previous title of every changed clip is
in `clip_titles.prior_title`.

## Learning Loop

`clip_titles.features` (rows with `applied = true`): `title_pattern`, `word_count`, `char_count`,
`has_number`, `has_stake`, `emoji_count`, `caps_words`, `name_tier`, `names`, `explored`, `source`. After about
50 posts per pattern, compare each pattern's views against the account median (Star Score, docs/28 L2).

## CLI

```
sixstars titles retitle <clip_id> [--apply]     one clip, every candidate and the checks it failed
sixstars titles compare --limit 30 [--status posted] [--out file.md]   dry run, old vs new (stored, not applied)
sixstars titles queue [--limit 200] [--apply]   re-title approved clips without a v2 title
```

## Differences from the brief

- **Spoilers**: the story-spoiler rule of P2 still holds, so a spoiler clip's title cannot say "ending" (the
  research's top title, "Kai Cenat & IShowSpeed Got The Same GTA 6 Ending! 💔", would be refused for a spoiler
  clip). Lift it by changing `SPOILER_WORDS` once `gate.spoiler_block_until` has passed, if wanted.
- **Two example titles** of the brief were left out of the prompt: they use phrases the rules ban ("After Chat
  Steals It", "This GTA 6 Moment") and two ALL-CAPS words; showing them would teach the model to break rules.
- **Peak line**: there is no per-line audio level stored, so the "loudest" line is the line with the most
  words at the chat peak (replay spikes), else the line with the most "!" then words.
- **Campaign**: the app has no clipping-campaign concept yet; `campaign` is always null.
- **Word count**: "GTA 6" counts as one word (the research's own rewrite "Kai Cenat's Argument With a Cyclist
  Gets Out of Hand in GTA 6 😭" is 13 tokens otherwise).
- **Name book**: `display_name` is overwritten by discovery, so new columns `title_name` and `title_aliases`
  hold the names. `gta6-priority-creators.csv` was not in the project; the seed list is the most-clipped
  staging creators plus the handles the research names.
- **Ranking boost** for big names (research item 7) is not part of this change.
- **"this happens"** is banned too (the model wrote "X Is 8 Hours Into GTA 6 When This Happens", the same
  emptiness as "this moment").
- **Hours into the stream** are given as whole hours, and the prompt calls them a weak stake: in the first
  staging run the model copied "8.9 Hours" and used them in 13 of 27 titles.

## Staging comparison (2026-11-23)

30 most recent staging clips (posted, approved and rejected; staging has one posted clip), dry run. First run:
22 v2, 5 fallbacks, 3 refused by the budget; the fallbacks came from two check bugs (digits inside names such
as "Quin69" read as numbers; an all-caps two-word name counted as caps words), fixed with regression tests.
Second run: 30 of 30 v2 titles, 0 fallbacks. Cost: $0.034 for 70 P11 calls over both runs (about $0.0005
per clip on the fast model).
