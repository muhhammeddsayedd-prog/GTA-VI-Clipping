-- Intake overrides (decisions D101 and D104). Additive and idempotent: plain updates only, safe to run
-- again and safe on a database whose settings were already changed by a seed.

-- D104: no Groq key was provided, so hosted transcription uses OpenAI. The client still falls back to
-- the other provider when one is configured.
update settings
set value = '"openai"'::jsonb
where key = 'transcribe.express_provider'
  and value is distinct from '"openai"'::jsonb;

-- D101: the real main handle is @6starsclipping. Replace the placeholder handle inside the existing
-- text, keeping its wording. The second condition makes a re-run a no-op.
update settings
set value = to_jsonb(replace(value #>> '{}', '@6starsclips', '@6starsclipping'))
where key in ('posting.first_comment', 'factory.watermark')
  and jsonb_typeof(value) = 'string'
  and value #>> '{}' like '%@6starsclips%'
  and value #>> '{}' not like '%@6starsclipping%';
