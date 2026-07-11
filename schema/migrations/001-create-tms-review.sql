-- Migration: create tms_review schema for reviewer eval harness (#54)
--
-- Tables: reviewer_runs, escaped_defects, defect_attributions
-- Schema: tms_review (alongside existing bogocat schema)
-- Database: unified postgres at 10.89.97.212:5432, dbname=postgres
--
-- This migration follows the same pattern as the existing bogocat
-- schema — multiple logical schemas in one database, connected via
-- the same DSN. Joins between tms_review and bogocat schemas are
-- possible without cross-database foreign data wrappers.
--
-- Applied by: psql service=pg-superuser -f <this-file>
-- Or via: python3 -c "from tms.review_eval import _apply_migration; _apply_migration()"

BEGIN;

CREATE SCHEMA IF NOT EXISTS tms_review;

-- ── reviewer_runs — one row per review dispatch (AC1) ─────────────

CREATE TABLE tms_review.reviewer_runs (
    run_id          UUID PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    review_round    INTEGER NOT NULL,
    reviewer_agent  TEXT NOT NULL,
    model           TEXT NOT NULL,
    provider_used   TEXT NOT NULL,
    diff_sha_reviewed TEXT NOT NULL,
    p0              INTEGER NOT NULL DEFAULT 0,
    p1              INTEGER NOT NULL DEFAULT 0,
    p2              INTEGER NOT NULL DEFAULT 0,
    wall_time_ms    INTEGER,
    findings        JSONB,
    input_tokens    INTEGER,
    output_tokens   INTEGER
);

CREATE INDEX idx_reviewer_runs_pr
    ON tms_review.reviewer_runs (repo, pr_number);

CREATE INDEX idx_reviewer_runs_model
    ON tms_review.reviewer_runs (model);

-- ── escaped_defects — one row per discovered escaped defect (AC2) ─

CREATE TABLE tms_review.escaped_defects (
    defect_id           UUID PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    repo                TEXT NOT NULL,
    introducing_pr      INTEGER NOT NULL,
    introducing_commit  TEXT NOT NULL,
    defect_class        TEXT NOT NULL,
    severity            TEXT NOT NULL,
    discovered_at       TEXT NOT NULL,
    discovery_source    TEXT NOT NULL,
    description         TEXT NOT NULL,
    fix_pr              INTEGER
);

-- ── defect_attributions — one row per attribution (AC2) ───────────
--
-- Each row links a defect to a run (author or reviewer) with an
-- outcome. run_source tags which harness table the run_id comes from
-- (coding_runs from #52, or reviewer_runs from this schema).
-- Append-only: re-judgment adds rows, never mutates original verdicts.

CREATE TABLE tms_review.defect_attributions (
    attribution_id  UUID PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    defect_id       UUID NOT NULL REFERENCES tms_review.escaped_defects (defect_id),
    run_source      TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    role            TEXT NOT NULL,
    outcome         TEXT NOT NULL
);

CREATE INDEX idx_defect_attributions_defect
    ON tms_review.defect_attributions (defect_id);

CREATE INDEX idx_defect_attributions_run
    ON tms_review.defect_attributions (run_id);

-- Grant: the bogocat role owns this schema (same role that owns
-- bogocat.*). Other roles access via the same DSN.

GRANT ALL ON SCHEMA tms_review TO bogocat;
GRANT ALL ON ALL TABLES IN SCHEMA tms_review TO bogocat;

COMMIT;
