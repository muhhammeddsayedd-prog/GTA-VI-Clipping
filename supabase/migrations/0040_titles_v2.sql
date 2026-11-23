-- 0040: title writer v2 (docs/30-titles-v2.md). Additive and idempotent: safe to run again.
--
-- Evidence: the 200 most-viewed English GTA 6 streamer clips (YouTube Shorts and TikTok, 2026-11-23) and 175
-- GTA 6 Shorts of 11 competitor channels compared against their own channel median. Winning titles say who,
-- what happened and why it matters; ours mostly said that chat reacted.
--
-- Nothing changes until scoring_config `titles.v2` is enabled (it is created disabled here).

-- 1. The name fans search for, per creator. `display_name` is not used for this because discovery overwrites
--    it with the platform's spelling on every refresh ("KaiCenat", "caseoh_", "Joe_Bartolozzi").
--    title_aliases: lowercase words or phrases that mean this streamer in a transcript or a title.
alter table creators add column if not exists title_name text;
alter table creators add column if not exists title_aliases text[] not null default '{}';
comment on column creators.title_name is
  'The streamer''s name as fans search it, used in titles (title writer v2). Null: derived from display_name.';
comment on column creators.title_aliases is
  'Lowercase names that mean this streamer in a transcript or title (title writer v2), e.g. {speed,ishowspeed}.';

-- Seed the names we post most. Set only while still empty, so an operator's value wins.
update creators c
   set title_name = s.title_name,
       title_aliases = s.aliases
  from (values
    ('ishowspeed',     'IShowSpeed',      '{ishowspeed,speed}'::text[]),
    ('kaicenat',       'Kai Cenat',       '{kai cenat,kaicenat,kai}'::text[]),
    ('caseoh_',        'CaseOh',          '{caseoh,case oh}'::text[]),
    ('jynxzi',         'Jynxzi',          '{jynxzi}'::text[]),
    ('clix',           'Clix',            '{clix}'::text[]),
    ('lacy',           'Lacy',            '{lacy}'::text[]),
    ('xqc',            'xQc',             '{xqc}'::text[]),
    ('jerma985',       'Jerma',           '{jerma,jerma985}'::text[]),
    ('vargskelethor',  'Vinesauce Vinny', '{vinesauce vinny,vinny,vinesauce,vargskelethor}'::text[]),
    ('darkviperau',    'DarkViperAU',     '{darkviperau,darkviper}'::text[]),
    ('ohnepixel',      'ohnePixel',       '{ohnepixel}'::text[]),
    ('joe_bartolozzi', 'Joe Bartolozzi',  '{joe bartolozzi,bartolozzi}'::text[]),
    ('theburntpeanut', 'TheBurntPeanut',  '{theburntpeanut,burnt peanut}'::text[]),
    ('lirik',          'Lirik',           '{lirik}'::text[]),
    ('hutchmf',        'HutchMF',         '{hutchmf,hutch}'::text[]),
    ('nickmercs',      'NickMercs',       '{nickmercs}'::text[]),
    ('lvndmark',       'Lvndmark',        '{lvndmark}'::text[]),
    ('hasanabi',       'HasanAbi',        '{hasanabi,hasan}'::text[]),
    ('moistcr1tikal',  'MoistCr1TiKaL',   '{moistcr1tikal,penguinz0}'::text[]),
    ('stableronaldo',  'Stable Ronaldo',  '{stable ronaldo,stableronaldo}'::text[]),
    ('montanablack88', 'MontanaBlack',    '{montanablack,montanablack88}'::text[]),
    ('ludwig',         'Ludwig',          '{ludwig}'::text[]),
    ('summit1g',       'summit1g',        '{summit1g}'::text[]),
    ('sasatikk',       'Sasatikk',        '{sasatikk}'::text[]),
    ('elajjaz',        'Elajjaz',         '{elajjaz}'::text[]),
    ('joshimuz',       'Joshimuz',        '{joshimuz}'::text[]),
    ('39daph',         '39daph',          '{39daph,daph}'::text[]),
    ('emiru',          'Emiru',           '{emiru}'::text[]),
    ('quin69',         'Quin69',          '{quin69,quin}'::text[]),
    ('rdcgaming',      'RDCgaming',       '{rdcgaming,rdc}'::text[])
  ) as s(handle, title_name, aliases)
 where lower(c.handle) = s.handle
   and c.title_name is null;

-- 2. Every title the v2 writer made: the facts it was given, all candidates with the checks each failed,
--    the one chosen, and the title traits the Learning Loop compares against our account median.
create table if not exists clip_titles (
  id              bigserial primary key,
  created_at      timestamptz not null default now(),
  clip_id         uuid not null references clips(id) on delete cascade,
  config_version  bigint not null default 0,
  model           text,
  facts           jsonb not null,
  candidates      jsonb not null default '[]'::jsonb,  -- [{"pattern","title","facts_used","failed":[...]}]
  chosen_index    int,                                 -- into candidates; null = fallback title
  chosen_title    text not null,
  caption_short   text,
  hashtags        text[] not null default '{}',
  explored        boolean not null default false,      -- the runner-up was used on purpose
  source          text not null check (source in ('v2', 'fallback')),
  features        jsonb not null default '{}'::jsonb,
  prior_title     text,                                -- clips.title before this run
  applied         boolean not null default false       -- written to clips (false: a dry comparison)
);
create index if not exists idx_clip_titles_clip on clip_titles (clip_id, created_at desc);
create index if not exists idx_clip_titles_applied on clip_titles (clip_id) where applied;
comment on table clip_titles is
  'Title writer v2: facts, candidates with failed checks, the chosen title and its traits (one row per run).';

alter table clip_titles enable row level security;

-- 3. The flag, disabled. See docs/30-titles-v2.md for every key.
insert into scoring_config (key, value, description) values (
  'titles.v2',
  '{
     "enabled": false,
     "model": "fast",
     "explore_share": 0.2,
     "gta_label": "GTA 6",
     "tag_tail": ["streamerclips"],
     "s_tier_names": ["IShowSpeed", "Kai Cenat", "N3on", "Adin Ross", "Jynxzi", "CaseOh", "Clix", "Lacy"],
     "tier_followers": {"A": 1000000, "B": 200000},
     "extra_names": {"N3on": ["n3on"], "Adin Ross": ["adin ross"], "Jynxzi": ["jynxzi"], "Clix": ["clix"]}
   }'::jsonb,
  'Title writer v2 after the gate approves a clip: 5 candidates in proven patterns, checked by code.'
) on conflict (key) do nothing;
