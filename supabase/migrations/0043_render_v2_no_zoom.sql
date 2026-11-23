-- Render v2 R0 (development-plan/M19, decision D119, 2026-11-23): no zoom of any kind. Idempotent.
--
-- The code no longer has a tail zoom, step zoom cuts or punch-ins (factory/layout.py, render.py, effects.py,
-- pipeline.py). Their rules are dropped from the settings so nothing suggests they still work:
--   punchin_max, punchin_zoom, punchin_len_s, punchin_min_gap_s, punchin_db, zoomcut_every_s, zoomcut_scale,
--   cut_sfx (the whoosh and impact on each punch-in).
-- StyleRules.from_setting ignores these keys anyway (logged once), so this migration and the code can ship in
-- either order.
--
-- cam_top_viral and cam_top_viral_follow were the only styles built on punch-ins and zoom cuts (38 of 150
-- published clips had punch-ins): they are switched off. They stay in the catalogue, so a clip whose
-- style_plan already names them still renders (now without zoom) and the bandit keeps their history.
-- Production audit 2026-11-23: docs "Render & layout audit: 6 Stars Clips vs competitors".

-- 1. factory.style (the single legacy style, the catalogue's fallback).
update settings
   set value = value - array['punchin_max', 'punchin_zoom', 'punchin_len_s', 'punchin_min_gap_s',
                             'punchin_db', 'zoomcut_every_s', 'zoomcut_scale', 'cut_sfx']
 where key = 'factory.style'
   and jsonb_typeof(value) = 'object'
   and value ?| array['punchin_max', 'punchin_zoom', 'punchin_len_s', 'punchin_min_gap_s',
                      'punchin_db', 'zoomcut_every_s', 'zoomcut_scale', 'cut_sfx'];

-- 2. factory.styles: the same keys out of every entry's rules; cam_top_viral(_follow) disabled.
do $$
declare
  retired constant text[] := array['punchin_max', 'punchin_zoom', 'punchin_len_s', 'punchin_min_gap_s',
                                   'punchin_db', 'zoomcut_every_s', 'zoomcut_scale', 'cut_sfx'];
  styles jsonb;
  cleaned jsonb;
begin
  select value into styles from settings where key = 'factory.styles';
  if styles is null or jsonb_typeof(styles) <> 'object' then
    return;
  end if;
  select coalesce(jsonb_object_agg(
           id,
           case
             when jsonb_typeof(entry) <> 'object' then entry
             else
               (case when jsonb_typeof(entry -> 'rules') = 'object'
                     then jsonb_set(entry, '{rules}', (entry -> 'rules') - retired)
                     else entry end)
               || (case when id in ('cam_top_viral', 'cam_top_viral_follow')
                        then '{"enabled": false}'::jsonb else '{}'::jsonb end)
           end), '{}'::jsonb)
    into cleaned
    from jsonb_each(styles) as s(id, entry);
  update settings set value = cleaned where key = 'factory.styles' and value is distinct from cleaned;
end $$;
