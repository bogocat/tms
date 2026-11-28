-- One-shot repair: restate the 13 `junction-drift` rows (tms#157 / #166 round 3)
--
-- WHAT IS WRONG
-- The first version of producer #1 ended its `junction-drift` description with:
--
--   '<book> has a distillation but its junctions are incomplete (topics=…,
--    traditions=…, quotes=…) — a pass recorded success without writing them'
--
-- The first half is proven by `bogocat.v_pipeline_health_book`
-- (`is_missing_junctions` + the three counts). The second half is not: it claims
-- a *specific* pass dropped its output, and the ledger says the opposite. The
-- latest successful `canonical-map` run for all 13 live items wrote
-- `canonical_topics` (13 of 13) and all but one wrote `canonical_traditions` too
-- (12 of 13; item 178 wrote 39 topics and no traditions, which is why the view
-- shows topics=39/traditions=0 for it, not 0/0):
--
--   items | wrote_topics | wrote_traditions | wrote_neither
--   ------+--------------+------------------+---------------
--      13 |           13 |               12 |             0
--
-- What is incomplete is the `book_topics`/`book_traditions` projection — exactly
-- what the corrected producer now says, and what #171 owns. `description` is
-- first-sight-wins, so fixing the producer alone leaves the retracted claim live
-- in 13 rows forever.
--
-- WHAT THIS REPAIR WRITES (the producer's current sentence, verbatim)
--   'book <id> (<title>) has a distillation but its junction rows are incomplete
--    (topics=<n>, traditions=<n>, quotes=<n>). The view proves the junctions are
--    incomplete; it does not say which step dropped them (the canonical_map
--    projection, #171, owns that).'
-- The counts are taken from the view at apply time, so the row says what a fresh
-- sweep would say.
--
-- SAFETY
-- * keyed on the old trailing claim, so it is a no-op the second time and cannot
--   touch a row the corrected producer filed (that text does not contain it);
--   verified read-only on 2026-11-27: 13 of 13 register rows carry the old text
--   and all 13 map to a view row, so one run restates the whole class;
-- * guarded on the view's current `is_missing_junctions`, so a book whose
--   junctions have healed since filing is left alone rather than given a
--   now-false sentence — post-check 2 lists those for a human;
-- * `defect_id`, `fingerprint`, `recurrence_count`, `created_at`, `detector` and
--   `discovery_source` are untouched. The superseded sentence is preserved here
--   and in #166, because the register has no history column for `description`.
--
-- Applied by: psql service=pg-superuser -f schema/repairs/2026-11-27-restate-junction-drift-claims.sql

BEGIN;

-- Pre-flight: exactly the rows this file will rewrite, with their current text.
SELECT d.fingerprint, d.recurrence_count, d.description
  FROM tms_review.escaped_defects d
  JOIN bogocat.v_pipeline_health_book b
    ON d.fingerprint = 'pipeline-health:junction-drift:' || b.item_id
 WHERE d.repo = 'distillery'
   AND d.description LIKE '%a pass recorded success without writing them%'
   AND b.is_missing_junctions
 ORDER BY b.item_id;

UPDATE tms_review.escaped_defects d
   SET description = 'book ' || b.item_id
                     || coalesce(' (' || b.title || ')', '')
                     || ' has a distillation but its junction rows are '
                     || 'incomplete (topics=' || b.topics_count
                     || ', traditions=' || b.traditions_count
                     || ', quotes=' || b.quotes_count || '). The view proves the '
                     || 'junctions are incomplete; it does not say which step '
                     || 'dropped them (the canonical_map projection, #171, owns '
                     || 'that).'
  FROM bogocat.v_pipeline_health_book b
 WHERE d.repo = 'distillery'
   AND d.fingerprint = 'pipeline-health:junction-drift:' || b.item_id
   AND d.description LIKE '%a pass recorded success without writing them%'
   AND b.is_missing_junctions;

-- Post-check 1: the restated rows, as the register now reports them.
SELECT fingerprint, recurrence_count, description
  FROM tms_review.escaped_defects
 WHERE fingerprint LIKE 'pipeline-health:junction-drift:%'
 ORDER BY fingerprint;

-- Post-check 2: must return 0 rows. A row here still carries the retracted claim
-- and was NOT restated — either its junctions have healed since filing (so the
-- producer's sentence would now be false too) or the view no longer has it.
-- Each one needs a human decision, not another automatic rewrite.
SELECT d.fingerprint, d.description
  FROM tms_review.escaped_defects d
  LEFT JOIN bogocat.v_pipeline_health_book b
    ON d.fingerprint = 'pipeline-health:junction-drift:' || b.item_id
 WHERE d.repo = 'distillery'
   AND d.fingerprint LIKE 'pipeline-health:junction-drift:%'
   AND d.description LIKE '%a pass recorded success without writing them%'
 ORDER BY d.fingerprint;

COMMIT;
