-- Migration: regression_count is incremented at DETECTION (#157 / #A3)
--
-- `regression_count`'s live column comment (008) says the opposite of what
-- intake does: "the writer increments it when clearing `regressed_at` (i.e. when
-- a re-fix is verified); #A3 owns that contract". 008 §5 says the same thing in
-- prose, and README repeated it.
--
-- #157's intake is now the writer, and it increments the counter at DETECTION —
-- the sighting of a `verified` row that sets `regressed_at`. That is the only
-- moment the fact exists: a detector observed the bug after a merged fix. The
-- later writer (#158 verification / #159 nightly sweep) clears `regressed_at`
-- when a re-fix is verified, and must NOT touch this counter.
--
-- Why this correction is a migration and not a code change: the wrong contract
-- is written down in three places a future author will read *instead of* the
-- code — the column comment (live in every database), 008 §5's prose, and the
-- README. If #A3 is implemented against any of them, one regression is counted
-- twice (0 -> 1 at detection, 1 -> 2 at re-verify) and the register's only
-- history of regressions is silently inflated. No test can catch a
-- documentation/code divergence; only the documentation can be fixed.
--
-- 008's comments are NOT edited: they are the record of what #156 shipped and
-- the migration is already applied. This migration supersedes the column
-- comment, 008 §5 is annotated below in words, and the README is corrected in
-- the same commit.
--
-- Re-runnable: `COMMENT ON` replaces the comment, so applying it twice is a
-- no-op.
--
-- Applied by: psql service=pg-superuser -f schema/migrations/010-defect-regression-count-semantics.sql

BEGIN;

COMMENT ON COLUMN tms_review.escaped_defects.regression_count IS
    'Number of regressions recorded for this fingerprint. Incremented by defect INTAKE (#157) at DETECTION: the sighting of a verified row that sets regressed_at. The re-fix writer (#158/#159) clears regressed_at when a re-fix is verified and must NOT increment this counter — one regression is counted once, at the moment a detector saw it come back.';

COMMENT ON COLUMN tms_review.escaped_defects.regressed_at IS
    'When a detector last observed the defect after a merged fix (set by intake, #157). Requires detector IS NOT NULL and merged_at IS NOT NULL (CHECKs). A current state, not a history flag: the re-fix writer clears it once a re-fix is verified, leaving regression_count to preserve the history.';

COMMIT;
