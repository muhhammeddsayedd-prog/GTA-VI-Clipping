-- Quality hardening of the AI layer and the clip factory: settings only, idempotent.
--
-- 1. factory.cut_snap: the "snap the cut to a pause" rule. Whisper often returns no punctuation, so the
--    clip writer cannot see where a sentence begins and 4 of 9 real clips opened mid-sentence (M08).
--    The factory now moves the start back (or, failing that, a little forward) and the end forward (or a
--    little back) to the nearest pause between words of at least pause_s seconds, preferring a sentence
--    end when the transcript has punctuation. vad switches on the voice activity pass that finds the
--    pauses Whisper hides inside stretched words. punctuate_below: when a smaller share of the words
--    than this ends a sentence (Whisper left the punctuation out), the fast model is asked where the
--    sentences end (prompt P10, about $0.0005 per window) so there is a boundary to snap to even
--    where people talk without pausing; 0 switches that off. pause_s = 0 switches the whole rule off.
--
-- 2. ai.score_samples: how many times the clip score is judged. 1 keeps the clip writer's own score.
--    3 asks the fast model for the score three more times with the same rubric and stores the median
--    (about $0.001 per clip with claude-haiku-5-5). Off by default: see the hardening notes.
--
-- 3. gate.music_min_run_frames 1 -> 2. The music detector (PANNs) flags one isolated second of speech
--    over a game sound as music in about half of all renders (M06: 3 of 6; M07 note 1), and with the
--    zero-music limits (gate.express.max_music_ratio and gate.strict_max_music_ratio, both 0) that one
--    second rejects about half of the good clips. The zero limit exists to keep out copyrighted music
--    that is long enough to be matched by a platform; a single second is below that. With 2, a flagged
--    second with no flagged neighbour is ignored by the gate, exactly as the factory already ignores it
--    for ducking. The cost: one real second of music could pass. No music ratio limit is raised.
--    The value is changed only where it is still the seeded 1, so a deliberate later choice is kept.
insert into settings (key, value, description) values
  ('factory.cut_snap', '{"pause_s": 0.4, "start_back_s": 4.0, "start_forward_s": 2.0, "end_forward_s": 3.0, "end_back_s": 2.0, "vad": true, "punctuate_below": 0.02}'::jsonb,
   'Snap the cut to a pause: a gap between words of pause_s seconds or more (or a sentence end) is a phrase boundary; the clip start moves back up to start_back_s, else forward up to start_forward_s (never past the spike floor); the end moves forward up to end_forward_s, else back up to end_back_s; clip length limits always hold. vad: find pauses with voice activity detection. punctuate_below: ask the fast model for the sentence ends when a smaller share of the words than this ends a sentence (0: never). pause_s 0 switches the rule off.'),
  ('ai.score_samples', '1'::jsonb,
   'How many times a clip is scored. 1: the clip writer''s own score. 3 (or another odd number): the fast model scores the finished cut that many times with the same rubric and the median is stored.'),
  ('gate.music_min_run_frames', '2'::jsonb,
   'Music frames in a run shorter than this many seconds are ignored by the gate. 1 counts every flagged frame. 2 ignores an isolated flagged second (detector noise on speech).')
on conflict (key) do nothing;

update settings
   set value = '2'::jsonb,
       description = 'Music frames in a run shorter than this many seconds are ignored by the gate. 1 counts every flagged frame. 2 ignores an isolated flagged second (detector noise on speech).'
 where key = 'gate.music_min_run_frames'
   and value = '1'::jsonb;
