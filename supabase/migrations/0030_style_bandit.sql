-- Learning which design style works on each account (development-plan/M18 Phase D). Idempotent.
--
-- factory.style_director gains the bandit: mode "bandit" learns from the posts' views per account (Thompson
-- sampling on log views against the account's median, posts min_age_h..window_d old). explore_floor of the
-- picks stay the weighted pick; a style is learned (weighted pick among the styles with fewer scored
-- posts) until it has min_samples on the account; affinity_weight scales the moment-type affinity; prior_n
-- shrinks a style's mean towards an ordinary post. Until min_samples posts are scored, every pick is the
-- weighted pick of before. Added only where the keys are still missing.

update settings
   set value = value || '{"mode": "bandit", "explore_floor": 0.1, "min_samples": 20, "affinity_weight": 0.3, "prior_n": 2, "min_age_h": 48, "window_d": 14}'::jsonb
 where key = 'factory.style_director'
   and not (value ? 'mode');

update settings
   set description = description || ' mode: weighted or bandit (learn from post views per account: explore_floor share stays weighted; min_samples scored posts per style before it is judged; affinity_weight scales the affinity; prior_n shrinks towards an ordinary post; posts min_age_h hours to window_d days old count).'
 where key = 'factory.style_director'
   and description not like '%bandit%';
