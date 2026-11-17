-- Local transcription of whole videos for the transcript fallback is switched off (decision D107).
-- On the staging server each 600 s block took 8-13 minutes with the medium model, so one Twitch video
-- (no caption track) held a worker for up to two hours and render jobs waited behind it.
-- The fallback still runs for videos that have a caption track. Idempotent: only the seeded value changes.
update settings set value = '0'::jsonb
where key = 'vod.fallback_transcribe_max_s' and value = '5400'::jsonb;
