-- YouTube lane tuning C (2026-11-21): hourly clip length per platform and a follow-line test on YouTube.
-- Additive and idempotent: keys and styles are added only where they are missing.
--
-- 1. gate.hourly_len_by_platform. YouTube views 2026-11-17..21 by length (hourly lane): <=18 s average 3,309
--    (n=2), 18-25 s 1,050 (n=8), 25 s+ 145 (n=5); the best Short so far is 8.6 s long. YouTube's hourly clips
--    are 8-30 s; every other platform keeps gate.hourly (20-60 s). The gate and the clip writer take the
--    union (8-60 s), the posting engine sends a short hourly clip only where it fits. Spike clips (gate
--    profile express) and story cuts (factory.story) are not affected.
insert into settings (key, value, description) values
  ('gate.hourly_len_by_platform', '{"youtube": {"min_len": 8, "max_len": 30}}'::jsonb,
   'Hourly clip length per platform, replacing gate.hourly min_len/max_len for that platform (end card included). The gate and the clip writer allow the union; posting sends a short hourly clip only to platforms whose window it fits. Spike clips and story cuts are not affected. Absent: gate.hourly everywhere.')
on conflict (key) do nothing;

-- 2. Follow-line test (factory.end_card_s stays 0, so every clip still loops). Each of the three styles
--    YouTube used most (cam_top_viral 8, cam_top_wordcap 7, cam_top_title 7 of 28 posts) gets a twin
--    `<style>_follow`: the same rules plus a follow line over the last 1.5 s, the same weight, hourly lane
--    only (spikes unchanged), youtube_main only (the text names its handle). The director then picks the
--    original and the twin about equally often for YouTube's hourly clips; its moment-type affinity is
--    copied to the twin so it does not tip the balance. The style bandit (migration 0030) compares them.
do $$
declare
  base text;
  twin text;
  styles jsonb;
  director jsonb;
  kind text;
begin
  select value into styles from settings where key = 'factory.styles';
  select value into director from settings where key = 'factory.style_director';
  if styles is null then
    return;
  end if;
  foreach base in array array['cam_top_viral', 'cam_top_wordcap', 'cam_top_title'] loop
    twin := base || '_follow';
    if styles ? base and not styles ? twin then
      styles := styles || jsonb_build_object(
        twin,
        (styles -> base)
          || '{"lanes": ["hourly"], "accounts": ["youtube_main"]}'::jsonb
          || jsonb_build_object(
               'rules',
               coalesce(styles -> base -> 'rules', '{}'::jsonb)
                 || '{"follow_overlay_s": 1.5, "follow_overlay_text": "Follow @6starsClips-m3x for Part 2"}'::jsonb));
    end if;
    if director is not null and jsonb_typeof(director -> 'affinity') = 'object' then
      for kind in select jsonb_object_keys(director -> 'affinity') loop
        if (director -> 'affinity' -> kind) ? base and not (director -> 'affinity' -> kind) ? twin then
          director := jsonb_set(director, array['affinity', kind, twin], director -> 'affinity' -> kind -> base);
        end if;
      end loop;
    end if;
  end loop;
  update settings set value = styles where key = 'factory.styles' and value is distinct from styles;
  if director is not null then
    update settings set value = director where key = 'factory.style_director' and value is distinct from director;
  end if;
end $$;
