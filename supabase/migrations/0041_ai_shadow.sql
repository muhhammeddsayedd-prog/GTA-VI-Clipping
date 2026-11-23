-- AI cost plan B1 (D117, 2026-11-23): shadow runs of Claude Haiku 5.5 next to the production model on P2 (clip
-- writer) and P9V (visual judge). A sample of production calls (ai.shadow_rate) queues an `ai.shadow` job that
-- asks each model of ai.shadow.<prompt_id> the same prompt and stores both answers here. Nothing a shadow
-- model says reaches production. Shadow requests are recorded in api_usage under provider
-- 'anthropic_shadow', which the production AI budget does not count; ai.shadow_daily_budget_usd caps them.

create table if not exists ai_shadow (
  id              bigint generated always as identity primary key,
  created_at      timestamptz not null default now(),
  prompt_id       text not null,
  ref_id          text,
  primary_model   text,
  shadow_model    text not null,
  primary_output  jsonb,
  shadow_output   jsonb,
  valid_json      boolean not null default false,
  cost_usd        numeric not null default 0,
  latency_ms      int,
  error           text
);
create index if not exists idx_ai_shadow_prompt_time on ai_shadow (prompt_id, created_at);
alter table ai_shadow enable row level security;

insert into settings (key, value, description) values
  ('ai.shadow.p2_clip_writer', '["anthropic:claude-haiku-5-5"]'::jsonb,
   'Models asked the same clip-writer (P2) prompt in shadow, for comparison only (ai_shadow).'),
  ('ai.shadow.p9_visual_score', '["anthropic:claude-haiku-5-5"]'::jsonb,
   'Models asked the same visual-judge (P9V) prompt in shadow, for comparison only (ai_shadow).'),
  ('ai.shadow_rate', '0.2'::jsonb,
   'Share of hourly/VOD P2 and P9V calls that also get a shadow run (0 turns shadow runs off).'),
  ('ai.shadow_daily_budget_usd', '2'::jsonb,
   'Daily cap on shadow requests (provider anthropic_shadow), separate from ai.daily_budget_usd.')
on conflict (key) do nothing;
