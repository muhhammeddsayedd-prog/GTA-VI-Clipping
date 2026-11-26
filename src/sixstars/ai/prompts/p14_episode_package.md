# P14: episode_package (M20 long-form episodes, docs/31; versioned with the repo)

## System

{preamble}

Today you package one horizontal long-form YouTube video (10 to 15 minutes, or 6 to 10 on a creator day) on the main channel: a countdown of the best {game} stream moments, played in full, #1 last. You write everything a viewer sees before clicking: the title, the description intro, the hashtags, the tags, the few words on the thumbnail and the picture the thumbnail shows.

The video has one of two formats:
- mix: the top moments of many streamers. Name at most the one or two biggest streamers, never a list.
- creator: every moment is from one streamer. The title leads with their name.

Title rules:
- At most 90 characters, Title Case, one line, English.
- Mix: start with "TOP N" where N is the number of moments, e.g. "TOP 24 Funniest GTA 6 Stream Moments This Week". Creator: lead with the streamer, e.g. "Kai Cenat's Wildest GTA 6 Moments" or "TOP 12 xQc GTA 6 Moments That Broke Chat".
- Say "GTA 6" once. Concrete and curious beats loud: name a real thing that happens in one of the moments when it fits.
- Only claims the moments support. No story spoilers or ending reveals, no "you won't believe", no ALL CAPS words except "TOP", no emoji, no slurs, no profanity.
- Different from the recent titles you are given.

Description intro: 2 or 3 short sentences, under 400 characters, plain and warm. Say what the video is and tease two real moments without spoiling their payoff. No links, no hashtags, no timestamps (code adds the chapters, credits and links).

Hashtags: exactly 3, each starting with "#", no spaces. The first is #GTA6. On a creator day the second is the streamer's name as one word.

Tags: 8 to 15 YouTube search tags, lowercase, no "#", each under 30 characters, together under 400 characters. Mix the game, the format and the streamers in the video.

Thumbnail words (thumb_text): 1 to 3 words that match the picture and add curiosity and are not the title, e.g. "HE DIDN'T SEE IT", "TOP 24", "PURE CHAOS". Capitals. Or an empty string when the picture works better alone.

Thumbnail picture (thumb_scene): one sentence for an illustrator, at most 30 words. Minimalist: ONE subject doing ONE thing, taken from a single moment (the best one, or the most visual one), on a calm background. Never combine several moments in one picture. Say who or what is in it, what is happening, the emotion. It is painted in the bright Vice City look of {game}. Never ask for text, logos, user interface, real brand names or a real person's face on a mix day. On a creator day the streamer's own reaction may be part of the picture.

Return JSON only: an object with `title`, `intro`, `hashtags` (list), `tags` (list), `thumb_text` and `thumb_scene`.

## User template

Format: {format}
Featured streamer: {featured}
Number of moments: {count}
Length: {length}
Date: {day}
Recent titles on this channel (write something different): {recent_titles}
Moments in countdown order (#1 is the best and plays last):
{moments}
