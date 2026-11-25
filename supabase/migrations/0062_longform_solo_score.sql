-- Long-form creator days (D122, owner decision 2026-11-26): a creator day takes that creator's clips scored
-- 5.5 or more (mix days keep longform.min_score, 7). At 7 only two creators had enough moments in a week, so
-- most creator days fell back to a mix day. Idempotent: added only where the key is missing.
update settings
   set value = value || '{"min_score": 5.5}'::jsonb,
       description = description || ' min_score: lowest clip score on a creator day (mix days use longform.min_score).'
 where key = 'longform.solo'
   and jsonb_typeof(value) = 'object'
   and not (value ? 'min_score');
