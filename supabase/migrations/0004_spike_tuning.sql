-- Spike tuning from the M04 real-chat check (decision D106). Settings only, idempotent.
-- The seeded values found 2 of 6 genuine moments on real YouTube GTA 5 chats; these found 7 of 9.
update settings set value = '8'::jsonb where key = 'spikes.min_messages';
update settings set value = '900'::jsonb where key = 'spikes.warmup_s';
update settings set value = '{"msgs": 0.15, "uniq": 0.15, "react": 0.55, "clipreq": 0.1, "audio": 0.05}'::jsonb
where key = 'spikes.weights';
update settings set value = '2.0'::jsonb where key = 'spikes.z_b';
update settings set value = '2.6'::jsonb where key = 'spikes.z_a';
update settings set value = '3.4'::jsonb where key = 'spikes.z_s';

-- Replay scans ignore spikes this close to the end of the video (goodbye messages are not moments).
insert into settings (key, value, description)
values ('spikes.end_guard_s', '300'::jsonb, 'Replay scans ignore spikes in the last N seconds of a video.')
on conflict (key) do nothing;
