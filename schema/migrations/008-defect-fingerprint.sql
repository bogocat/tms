-- Migration: defect register — fingerprint, regression, derived status (#156)
--
-- `tms_review.escaped_defects` (001) was built as the judgement half of the
-- reviewer-eval harness (#54) and never became the fleet's defect register:
-- its only writer was backjudge_confirm(), so it sat at 4 rows from
-- 2026-07-13 while reviewer_runs/dispatch_outcomes kept growing. #156 revives
-- it by giving the table the three things it could not express:
--
--   * one row per bug, forever — `fingerprint` + a partial unique index on
--     (repo, fingerprint). A recurrence bumps `recurrence_count`; it never
--     inserts a second row. "Has this class of defect recurred?" becomes a
--     query instead of a memory.
--   * a fix is not a fact until it is merged and then deployed —
--     `fix_pr` names a PR, `merged_at` is the merge, `verified_at`/
--     `deployed_sha` are the running fix.
--   * a fixed thing coming back is its own event — `regressed_at`,
--     `regression_count`.
--
-- `status` is DERIVED, never written. It lives in `tms_review.defects_v` and
-- is computed from the timestamps. No handler writes a status field, so the
-- register cannot drift from the facts.
--
-- Status vocabulary — five arms, every arm names a fact the row proves:
--
--   regressed     regressed_at IS NOT NULL   detector saw it come back
--   verified      verified_at  IS NOT NULL   detector passed on the deployed fix
--   fixed         merged_at    IS NOT NULL   a fix is merged
--   fix-proposed  fix_pr       IS NOT NULL   a fix PR is named, merge unproven
--   new           otherwise                  discovery only
--
--   `fix-proposed` is deliberately NOT `fixed`: `fix_pr` proves a PR was
--   named, not that its change landed. Only `merged_at` proves a merge. The
--   four live rows keep `merged_at` NULL (we hold no merge fact for them), so
--   the one carrying fix_pr=102 sits honestly at `fix-proposed` until #A3
--   can prove it deployed. This is the I6 discipline applied to the register
--   itself: do not let the first consumer of the invariant violate it.
--
-- `detector` is nullable on purpose. A defect with no re-runnable check may
-- not claim automatic regression detection; nullable is the schema saying so.
-- The CHECK constraint below enforces the consequence — a `regressed_at` on
-- a detector-less row is a verdict the data does not support. Detector naming
-- is deliberately unconstrained here; #A2 defines the convention.
--
-- Backfill: the 4 pre-#156 rows get a deterministic manual fingerprint,
--   'manual:' || encode(sha256(repo || defect_class || description), 'hex')
-- (verified collision-free on the live rows), and last_seen_at = created_at.
-- The update is bounded to rows created before the revival cutover
-- (2026-11-19) AND to `fingerprint IS NULL`, so re-running this migration is
-- a no-op: it can never reach a row written by the live writer after the
-- cutover, and it can never re-stamp a row it already fingerprinted. A
-- re-run therefore also cannot collide on the unique index below.
--
-- Invariant scope: (repo, fingerprint) uniqueness is enforced for rows that
-- carry a fingerprint. `record_escaped_defect()` still writes NULL
-- fingerprints until intake lands (#A2) — those rows are deliberately
-- unconstrained so the rollout is not a flag day. Consumers should not
-- assume the invariant holds for NULL-fingerprint rows; #A2's upsert is what
-- closes that gap.
--
-- Out of scope: intake (#A2), verification (#A3), sweep (#A4). This lands a
-- schema a writer can target and nothing more. `defect_attributions` is
-- unchanged, and `record_escaped_defect()` keeps its signature — the new
-- columns are nullable or defaulted, so existing INSERTs still work.
--
-- Applied by: psql service=pg-superuser -f schema/migrations/008-defect-fingerprint.sql
--
-- Re-runnable: every statement is guarded (ADD COLUMN IF NOT EXISTS /
-- CREATE INDEX IF NOT EXISTS / CREATE OR REPLACE VIEW / DO-block constraint
-- checks / a backfill bounded by the revival cutover). The single
-- non-concurrent CREATE INDEX is a SHARE-lock build; fine at 4 rows, and the
-- CONCURRENTLY form (outside a transaction) is the pattern if the register
-- ever grows large.

BEGIN;

-- ── 1. register columns ───────────────────────────────────────────

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS fingerprint TEXT;

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS recurrence_count INTEGER NOT NULL DEFAULT 1;

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS detector TEXT;

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ;

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS deployed_sha TEXT;

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS regressed_at TIMESTAMPTZ;

ALTER TABLE tms_review.escaped_defects
    ADD COLUMN IF NOT EXISTS regression_count INTEGER NOT NULL DEFAULT 0;

-- ── 2. backfill the pre-#156 rows (guarded + cutover-bounded) ────
--
-- Fingerprint formula is the issue's: sha256 over repo || class ||
-- description, hex-encoded, prefixed 'manual:' so a hand-filed row is
-- distinguishable from a detector-derived one. last_seen_at is created_at
-- because that is the only sighting we can prove for these rows.
--
-- The created_at bound is what makes the re-run safe: without it, a second
-- run would fingerprint rows the live writer added in the meantime and could
-- abort on the unique index if two of them shared a description.

UPDATE tms_review.escaped_defects
   SET fingerprint = 'manual:' || encode(
           sha256(convert_to(repo || defect_class || description, 'UTF8')),
           'hex'
       ),
       last_seen_at = COALESCE(last_seen_at, created_at)
 WHERE fingerprint IS NULL
   AND created_at < TIMESTAMPTZ '2026-11-19 00:00:00+00';

-- ── 3. one row per (repo, fingerprint), forever ───────────────────

CREATE UNIQUE INDEX IF NOT EXISTS idx_escaped_defects_repo_fingerprint
    ON tms_review.escaped_defects (repo, fingerprint)
    WHERE fingerprint IS NOT NULL;

-- ── 4. each status arm must be able to name its fact ─────────────
--
-- Same discipline as nullable `detector`: the view will not report a
-- regression or a verification for a row that never proved a merge. A
-- regressed_at or verified_at with merged_at IS NULL claims a fix happened
-- (and got deployed) when the row only shows a named fix PR — the exact
-- unsupported assertion the fix-proposed arm exists to avoid.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'escaped_defects_regression_requires_detector'
           AND conrelid = 'tms_review.escaped_defects'::regclass
    ) THEN
        ALTER TABLE tms_review.escaped_defects
            ADD CONSTRAINT escaped_defects_regression_requires_detector
            CHECK (regressed_at IS NULL OR detector IS NOT NULL);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'escaped_defects_regression_requires_merge'
           AND conrelid = 'tms_review.escaped_defects'::regclass
    ) THEN
        ALTER TABLE tms_review.escaped_defects
            ADD CONSTRAINT escaped_defects_regression_requires_merge
            CHECK (regressed_at IS NULL OR merged_at IS NOT NULL);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'escaped_defects_verification_requires_merge'
           AND conrelid = 'tms_review.escaped_defects'::regclass
    ) THEN
        ALTER TABLE tms_review.escaped_defects
            ADD CONSTRAINT escaped_defects_verification_requires_merge
            CHECK (verified_at IS NULL OR merged_at IS NOT NULL);
    END IF;
END
$$;

-- ── 5. defects_v — status derived, never written ──────────────────
--
-- Precedence is regressed > verified > fixed > fix-proposed > new (the
-- plan-gate vocabulary). `regressed` therefore means "currently regressed":
-- it is a state the writer must clear (verified_at set, regressed_at NULL)
-- once a re-fix is verified — regression_count is what preserves the history.
-- #A3 owns that write contract; the arm order is pinned by a test so it
-- cannot drift silently.

CREATE OR REPLACE VIEW tms_review.defects_v AS
SELECT
    d.defect_id,
    d.repo,
    d.fingerprint,
    d.defect_class,
    d.severity,
    d.description,
    d.discovery_source,
    d.discovered_at,
    d.introducing_pr,
    d.introducing_commit,
    d.fix_pr,
    d.detector,
    d.deployed_sha,
    d.recurrence_count,
    d.regression_count,
    d.created_at,
    d.last_seen_at,
    d.merged_at,
    d.verified_at,
    d.regressed_at,
    CASE
        WHEN d.regressed_at IS NOT NULL THEN 'regressed'
        WHEN d.verified_at  IS NOT NULL THEN 'verified'
        WHEN d.merged_at    IS NOT NULL THEN 'fixed'
        WHEN d.fix_pr       IS NOT NULL THEN 'fix-proposed'
        ELSE 'new'
    END AS status
  FROM tms_review.escaped_defects d;

-- ── 6. documentation ──────────────────────────────────────────────

COMMENT ON COLUMN tms_review.escaped_defects.fingerprint IS
    'Stable identity of the bug within a repo. One row per (repo, fingerprint) forever; recurrence bumps recurrence_count instead of inserting. Pre-#156 rows are backfilled as ''manual:'' || sha256(repo || defect_class || description).';

COMMENT ON COLUMN tms_review.escaped_defects.recurrence_count IS
    'Times this fingerprint has been seen. A recurrence increments this; it never inserts a second row.';

COMMENT ON COLUMN tms_review.escaped_defects.detector IS
    'Re-runnable check able to observe this defect again (name convention is #A2''s call; unconstrained here). NULL = no automatic regression detection possible, so regressed_at may not be set.';

COMMENT ON COLUMN tms_review.escaped_defects.last_seen_at IS
    'Last sighting of the fingerprint. Backfilled to created_at for pre-#156 rows.';

COMMENT ON COLUMN tms_review.escaped_defects.merged_at IS
    'When a fix for this fingerprint merged. Proves a merge — unlike fix_pr, which only names a PR.';

COMMENT ON COLUMN tms_review.escaped_defects.verified_at IS
    'When detector passed against the deployed fix.';

COMMENT ON COLUMN tms_review.escaped_defects.deployed_sha IS
    'Commit/image sha the verified fix was actually running as.';

COMMENT ON COLUMN tms_review.escaped_defects.regressed_at IS
    'When detector last observed the defect after a merged fix. Requires detector IS NOT NULL and merged_at IS NOT NULL (CHECKs). Currently-regressed state: writers clear it once a re-fix is verified; regression_count keeps the history.';

COMMENT ON COLUMN tms_review.escaped_defects.verified_at IS
    'When detector passed against the deployed fix. Requires merged_at IS NOT NULL (CHECK).';

COMMENT ON COLUMN tms_review.escaped_defects.regression_count IS
    'Times the fingerprint regressed after a fix.';

COMMENT ON VIEW tms_review.defects_v IS
    'Defect register with derived status (regressed > verified > fixed > fix-proposed > new). Status is never written — it is computed from regressed_at/verified_at/merged_at/fix_pr. One row per (repo, fingerprint), forever.';

-- ── 7. grants — same pattern as 001/004/005 ──────────────────────
--
-- The view is a relation, so the blanket grants cover it (in PostgreSQL
-- "ALL TABLES" includes views); they are re-issued here, after the view
-- exists, rather than relying on ALTER DEFAULT PRIVILEGES alone — 001
-- already regressed once on exactly this (bogocat_ro seeing 0 tables).
-- Note: ALTER DEFAULT PRIVILEGES without FOR ROLE sets defaults for the
-- role running this migration (pg-superuser), not for `bogocat`; the
-- explicit grants above are what covers today's objects.

GRANT ALL ON ALL TABLES IN SCHEMA tms_review TO bogocat;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT ALL ON TABLES TO bogocat;

GRANT USAGE ON SCHEMA tms_review TO bogocat_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA tms_review TO bogocat_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT SELECT ON TABLES TO bogocat_ro;

COMMIT;
