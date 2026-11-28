-- One-shot repair: restate the 3 over-claiming `distillery-silent-success` rows (tms#157 / #166)
--
-- WHAT IS WRONG
-- The first versions of producer #2 described an empty *pair of fields* as an
-- empty *run*. `description` is first-sight-wins, so correcting the producer
-- does not correct the rows already filed — and all three live rows carry
-- sentences their own artifacts disprove:
--
--   distillery-silent-success:empty-extraction:63
--     'core-extract recorded status=success in 2 runs for item 63 (last ended
--      2026-05-25 23:48:36.901557+00:00) and chapter_summaries and best_quotes
--      are both empty — the pass extracted nothing'
--   distillery-silent-success:empty-extraction:258
--     (same sentence, item 258, last ended 2026-05-26 01:06:36.027732+00:00)
--   distillery-silent-success:empty-mapping:63
--     'canonical-map reported status=success and wrote no
--      canonical_topics/canonical_traditions (2 runs, last 2026-05-25)'
--
-- What those runs actually wrote (read-only, live ledger):
--   run 6117 item 63  core-extract: high_level_summary 716 chars,
--     core_thesis_or_premise 490, methodology 233, source_quality 156
--   run 6148 item 258 core-extract: high_level_summary 1204, key_entities 10
--     entries, methodology 398, source_quality 342
--   run 6119 item 63  canonical-map: rationale 121, mapping_confidence present
--
-- So "the pass extracted nothing" is false for 3 of 3 rows, and "wrote nothing"
-- is false for the same three. The provable facts are the two fields the rule
-- names. This repair rewrites only the field-level claim, and only for rows that
-- still carry the over-claiming text (so it is a no-op on the second run and
-- cannot touch a row the corrected producer filed).
--
-- WHAT IS PRESERVED
-- The rows keep their `defect_id`, `fingerprint`, `recurrence_count`,
-- `created_at`, `detector` and `discovery_source`. The superseded sentences are
-- recorded above, in this file and in #166, because the register has no history
-- column for `description`.
--
-- ORDER OF OPERATIONS
--   1. merge the corrected producer (#166),
--   2. run THIS file (any time — it keys on the old text, not on a sweep),
--   3. the next sweep recurs the same three fingerprints instead of inserting.
--
-- Applied by: psql service=pg-superuser -f schema/repairs/2026-11-23-restate-distillery-silent-success-descriptions.sql

BEGIN;

-- Pre-flight: exactly the rows this file will rewrite, with their current text.
SELECT fingerprint, recurrence_count, description
  FROM tms_review.escaped_defects
 WHERE detector = 'distillery-silent-success'
   AND (description LIKE '%the pass extracted nothing%'
        OR description LIKE '%wrote no canonical_topics%'
        OR description LIKE '%produced no review%')
 ORDER BY fingerprint;

UPDATE tms_review.escaped_defects d
   SET description = 'item ' || split_part(d.fingerprint, ':', 3)
                     || ' (pass '
                     || CASE split_part(d.fingerprint, ':', 2)
                          WHEN 'empty-extraction' THEN 'core-extract'
                          WHEN 'empty-mapping' THEN 'canonical-map'
                          WHEN 'empty-review' THEN 'review-synthesis'
                        END
                     || '): '
                     || CASE split_part(d.fingerprint, ':', 2)
                          WHEN 'empty-extraction' THEN
                               'chapter_summaries and best_quotes are both '
                               'empty — no per-chapter summaries and no quotes '
                               'were extracted'
                          WHEN 'empty-mapping' THEN
                               'canonical_topics and canonical_traditions are '
                               'both empty — no topics or traditions were '
                               'mapped'
                          WHEN 'empty-review' THEN
                               'review_html is blank — no review was written'
                        END
 WHERE d.detector = 'distillery-silent-success'
   AND d.fingerprint LIKE 'distillery-silent-success:%'
   AND (d.description LIKE '%the pass extracted nothing%'
        OR d.description LIKE '%wrote no canonical_topics%'
        OR d.description LIKE '%produced no review%');

-- Post-check 1: the restated rows, as the register now reports them.
SELECT fingerprint, recurrence_count, description
  FROM tms_review.escaped_defects
 WHERE detector = 'distillery-silent-success'
 ORDER BY fingerprint;

-- Post-check 2: must return 0 rows — the over-claiming text is gone. A row here
-- is a fingerprint whose signal has no CASE arm above and needs a human look.
SELECT fingerprint, description
  FROM tms_review.escaped_defects
 WHERE detector = 'distillery-silent-success'
   AND (description LIKE '%the pass extracted nothing%'
        OR description LIKE '%wrote nothing%'
        OR description LIKE '%wrote no canonical_topics%'
        OR description LIKE '%produced no review%')
 ORDER BY fingerprint;

COMMIT;
