-- AI cost plan A0 (2026-11-23): per-call detail on api_usage, so repairs, refusals, failed HTTP calls and
-- prompt-cache use can be read from the database instead of the JSON logs.
--
--   stop_reason         the model's stop_reason (end_turn, max_tokens, refusal, ...)
--   attempt             1 for the first ask of a logical call, 2+ for a repair or a doubled max_tokens
--   outcome             ok | invalid (reply failed validation) | refusal | http_error (no usable response)
--   cache_read_tokens   prompt tokens read from the cache (also counted in input_tokens)
--   cache_write_tokens  prompt tokens written to the cache (also counted in input_tokens)
--   detail              short reason: the failing validator, the HTTP status, or a call tag (title_retry)
--
-- All columns are nullable: rows of other providers and rows written before this migration leave them empty.

alter table api_usage add column if not exists stop_reason text;
alter table api_usage add column if not exists attempt smallint;
alter table api_usage add column if not exists outcome text;
alter table api_usage add column if not exists cache_read_tokens int;
alter table api_usage add column if not exists cache_write_tokens int;
alter table api_usage add column if not exists detail text;

create index if not exists idx_api_usage_operation_day on api_usage (operation, created_at);
