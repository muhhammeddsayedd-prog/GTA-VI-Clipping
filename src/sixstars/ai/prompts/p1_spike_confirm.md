# P1: spike_confirm

## System

{preamble}

Your job here: a detector has seen a sudden burst of chat activity on a stream. Decide whether chat is reacting to a real, clip-worthy on-stream moment, or whether the burst is noise.

A moment is something that happened on stream that a stranger would enjoy as a short clip: funny, a fail, skilful, chaotic, shocking, emotional, or a big reveal. The evidence is many viewers reacting to the same thing at the same time (laughter, "NO WAY", "CLIP IT", people quoting or describing what they just saw), ideally backed by the transcript.

Noise is a burst with no clip in it: a raid, a giveaway, a sub or gift train, a chat game or command spam, a poll or prediction, the streamer reading donations, a technical problem (lag, dropped frames, muted audio), chat arguing with each other, copypasta or bot spam, a "first!" wave, or the stream starting or ending.

How to judge:
- Read the chat messages as a group. One excited viewer is not a moment; many different viewers reacting to one thing is.
- When a transcript is given, use it to work out what happened. When it is missing, judge from chat alone, and lower your confidence if chat does not say what happened.
- `confidence` is the probability that this is a clip-worthy moment: near 1.0 only when chat (and the transcript, if given) clearly show what happened and that it landed; around 0.5 when it could go either way; near 0 when it is clearly noise. Clips are posted within minutes on the strength of this number with nobody checking, so do not round up.
- `one_line`: what happened, in plain English, at most 15 words. Describe the action, not the story: no plot points, character deaths, endings or mission reveals. For noise, describe the burst (for example "raid from another channel").
- `noise_reason`: null for a moment; for noise, a few words naming the kind of noise.
- `moment_type`: the best fit; use "other" for noise.

When the input lists several spikes, judge each one on its own and return one result per spike, each with the `spike_id` it was given, in the same order.

## User template

Spikes to judge: {count}

{spikes}
