-- Migration: defect intake needs nullable introducing_pr / introducing_commit (#157)
--
-- `tms_review.escaped_defects` (001) was built for the review-eval harness
-- (#54), where every defect came out of a known PR: `introducing_pr` and
-- `introducing_commit` were NOT NULL because a backjudged defect always had
-- an introducing change to point at.
--
-- The register revived in #156 has a second kind of writer: a detector. The
-- first two producers (#157) observe state, not diffs —
-- `pipeline-health:junction-drift:152` says a book's junctions are missing,
-- and the distillery silent-success checks say a run that reported success
-- wrote an empty payload. For both, no introducing PR exists: there is no
-- such fact to record. Writing `0` or `''` to satisfy NOT NULL would
-- fabricate one, which is the same unsupported assertion (#156) refuses to
-- allow for `merged_at` vs `fix_pr`.
--
-- This migration is required by #157's acceptance criterion that every row
-- filed by a producer carries a non-null `detector` and a detector-prefixed
-- fingerprint: with `introducing_pr NOT NULL` no detector-filed row can exist
-- at all, so intake is unreachable without it. It is a separate PR from the
-- intake code for that reason — not a bundled convenience.
--
-- So: no introducing PR is NULL, and NULL means "no such fact", not
-- "unknown-but-probably-fine". The rule is #156's: a column a detector cannot
-- populate honestly is nullable, and the derived status arms below never read
-- these two columns — `defects_v.status` is computed from `merged_at` /
-- `verified_at` / `regressed_at` / `fix_pr`, so nothing about the register's
-- verdicts changes.
--
-- The harness writer (`record_escaped_defect()`, 001) keeps passing both
-- values and is unaffected: making a NOT NULL column nullable is backward
-- compatible for every existing INSERT, and the 4 pre-#156 rows all carry
-- values already.
--
-- Re-runnable: `DROP NOT NULL` on an already-nullable column is a no-op in
-- PostgreSQL (no IF EXISTS form is needed or available for this alteration).
--
-- Applied by: psql service=pg-superuser -f schema/migrations/009-defect-intake-nullable.sql

BEGIN;

ALTER TABLE tms_review.escaped_defects
    ALTER COLUMN introducing_pr DROP NOT NULL;

ALTER TABLE tms_review.escaped_defects
    ALTER COLUMN introducing_commit DROP NOT NULL;

-- Document the meaning at the column, so a reader of the table does not have
-- to find this file to learn why a detector row has no introducing PR.
COMMENT ON COLUMN tms_review.escaped_defects.introducing_pr IS
    'PR that introduced the defect, when one is known. NULL = no such fact: detector-sourced rows (#157) observe state, not diffs, and have no introducing PR.';

COMMENT ON COLUMN tms_review.escaped_defects.introducing_commit IS
    'Commit that introduced the defect, when one is known. NULL = no such fact (see introducing_pr).';

COMMIT;
