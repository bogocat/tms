-- One-shot repair: relabel the 14 unproven `missing-distillation` rows (tms#157)
--
-- WHAT IS WRONG
-- The first version of producer #1 filed `pipeline-health:missing-distillation:<item>`
-- for every book with no `book_distillations` row, and the description claimed
-- the pipeline "never produced a distillation for it and reported no failure".
-- The ledger contradicts that sentence for all 14 rows: every one of them has
-- `core-extract` runs with status='skipped' and
--   error_detail = 'insufficient-source-text: no epub body nor OCR sidecar found'
-- and `passes_succeeded` of 0 (10 rows) or 1 (4 rows). The pipeline reported;
-- the row denies it.
--
-- The genuine silent shape — all six required passes OK and still no
-- distillation row (distillery#173; home-portal's own stat card calls it
-- "All 6 passes OK, no row") — matches ZERO live rows today.
--
-- WHY RELABEL INSTEAD OF DELETE
-- These are real defects; only the claim was wrong. Deleting real findings to
-- make a metric look clean is the failure the register exists to remove.
-- Fingerprints, `recurrence_count` (2) and `created_at` are preserved.
--
-- ORDER OF OPERATIONS (matters)
--   1. merge the corrected producer (#166),
--   2. run THIS file,  -- before any sweep with the corrected code
--   3. run one corrected sweep (`python3 -m tms.producers.pipeline_health`).
-- If a sweep ran first it would file fresh `insufficient-source-text` rows and
-- this UPDATE would collide on the (repo, fingerprint) unique index. Running
-- this file first means step 3 recurs those 14 rows and adds the 4
-- `core-extract-failed` rows the corrected producer also finds. The pre-flight
-- block below fails with that reason instead of letting the UPDATE abort on a
-- unique violation.
--
-- WHAT THE REPLACED SENTENCE SAID (kept here because the UPDATE overwrites
-- `description` and the register keeps no history of the text):
--   'book <id> (<title>) has no book_distillations row: the V2 digest pipeline
--    never produced a distillation for it and reported no failure'
-- It is false for all 14: the ledger records a skip for every one of them.
--
-- Applied by: psql service=pg-superuser -f schema/repairs/2026-11-19-relabel-unproven-missing-distillation.sql

BEGIN;

-- Pre-flight: refuse to start if the corrected producer has already swept *for
-- the books this file relabels* — those rows would collide on the
-- (repo, fingerprint) unique index. Scoped to the actual collision rather than
-- to the prefix, so a second run (no rows left to relabel) is a clean no-op.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM tms_review.escaped_defects d
          JOIN bogocat.v_pipeline_health_book b
            ON d.fingerprint =
               'pipeline-health:missing-distillation:' || b.item_id
         WHERE d.repo = 'distillery'
           AND b.is_missing_distillation
           AND b.passes_succeeded < 6
           AND EXISTS (
                SELECT 1
                  FROM tms_review.escaped_defects e
                 WHERE e.repo = 'distillery'
                   AND e.fingerprint =
                       'pipeline-health:insufficient-source-text:' || b.item_id
           )
    ) THEN
        RAISE EXCEPTION
            'the corrected producer already swept: an insufficient-source-text row exists for a book this repair would relabel, so the UPDATE would collide on (repo, fingerprint). Resolve those rows deliberately; do not force this through.';
    END IF;
END
$$;

-- Pre-flight: what is about to change (and nothing else).
SELECT d.fingerprint AS old_fingerprint,
       b.passes_succeeded,
       d.recurrence_count
  FROM tms_review.escaped_defects d
  JOIN bogocat.v_pipeline_health_book b
    ON d.fingerprint = 'pipeline-health:missing-distillation:' || b.item_id
 WHERE d.repo = 'distillery'
   AND b.is_missing_distillation
   AND b.passes_succeeded < 6
 ORDER BY b.item_id;

UPDATE tms_review.escaped_defects d
   SET fingerprint = 'pipeline-health:insufficient-source-text:' || b.item_id,
       description = 'book ' || b.item_id
                     || coalesce(' (' || b.title || ')', '')
                     || ' has no book_distillations row and '
                     || b.passes_succeeded || ' of 6 passes succeeded: '
                     || 'no core-extract run ever succeeded, and at least one '
                     || 'was skipped with ''insufficient-source-text: no epub '
                     || 'body nor OCR sidecar found'', so no distillation '
                     || 'could be produced'
  FROM bogocat.v_pipeline_health_book b
 WHERE d.repo = 'distillery'
   AND d.fingerprint = 'pipeline-health:missing-distillation:' || b.item_id
   AND b.is_missing_distillation
   AND b.passes_succeeded < 6;

-- Post-check 1: the relabelled rows, as the register now reports them.
SELECT repo, fingerprint, recurrence_count, detection_status
  FROM (SELECT repo, fingerprint, recurrence_count,
               CASE WHEN verified_at IS NOT NULL THEN 'verified'
                    WHEN merged_at IS NOT NULL THEN 'fixed'
                    ELSE 'new' END AS detection_status
          FROM tms_review.escaped_defects) d
 WHERE fingerprint LIKE 'pipeline-health:insufficient-source-text:%'
 ORDER BY fingerprint;

-- Post-check 2: the unproven `missing-distillation` rows this repair
-- deliberately does NOT touch — the book has since gained a distillation, or all
-- six passes now succeed (that one is the genuine #173 shape and keeps its
-- claim). Zero rows means every remaining `missing-distillation` row is proven;
-- anything listed here needs a human look.
SELECT d.fingerprint, b.is_missing_distillation, b.passes_succeeded
  FROM tms_review.escaped_defects d
  LEFT JOIN bogocat.v_pipeline_health_book b
    ON d.fingerprint = 'pipeline-health:missing-distillation:' || b.item_id
 WHERE d.repo = 'distillery'
   AND d.fingerprint LIKE 'pipeline-health:missing-distillation:%';

COMMIT;
