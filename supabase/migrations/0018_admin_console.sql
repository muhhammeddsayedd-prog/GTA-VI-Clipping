-- Admin console (M17, docs/26): settings only, additive, idempotent.
--
-- The console reads through new GET /v1/admin/* routes of the control API. These settings bound those
-- reads; nothing in the pipeline reads them.
insert into settings (key, value, description) values
  ('admin.statement_timeout_ms', '5000'::jsonb,
   'Statement timeout of one admin console read (GET /v1/admin/*), in milliseconds. A slower query answers 504 instead of holding a connection.'),
  ('admin.list_max_limit', '200'::jsonb,
   'Largest page an admin console list returns (the limit parameter is capped at this; the default page is 50).'),
  ('admin.month_ceiling_usd', '840'::jsonb,
   'Monthly spend ceiling shown on the console''s Spend page (the top of the docs/23 cost model). Display only: nothing is paused by it.'),
  ('admin.noisy_event_types', '["post.dry_run", "post.link_updated", "stats.collected", "clip.scored", "job.recovered"]'::jsonb,
   'Event types left out of the console overview''s recent events (they stay on the Events page).')
on conflict (key) do nothing;
