-- M20: the long-form fetch transcribes with the faster-whisper model baked into the image. The image holds
-- only `medium` (docker/Dockerfile, WHISPER_MODEL) and runs with HF_HUB_OFFLINE=1, so `small` failed every
-- time ("transcription failed in modes ['local']"). Only a value still at the old default is changed.
update settings
   set value = '"medium"'::jsonb,
       description = 'Long-form episodes: local faster-whisper model for alignment and subtitles. Must be a model baked into the image (docker/Dockerfile WHISPER_MODEL, medium).'
 where key = 'longform.whisper_model'
   and value = '"small"'::jsonb;
