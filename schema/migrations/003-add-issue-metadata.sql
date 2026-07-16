-- Migration: add issue metadata columns to tms_review.events (#76 PR A)
--
-- Adds flat query indices for per-class dispatch analysis. These are
-- denormalized columns populated at tmq spawn time from the existing
-- `gh issue view --json labels,title` call (no extra API call).
--
-- Design decisions (per proposal-review 2026-07-16):
--   - issue_labels (JSONB): array of label names for GROUP BY via
--     jsonb_array_elements_text(). GIN-indexable for ? queries.
--   - point_estimate (TEXT): extracted from point:N label (nullable).
--     TEXT not SMALLINT because labels are strings; cast in queries.
--   - area (TEXT): extracted from area:* label (nullable).
--   - issue_title is NOT added: already in payload for 100% of rows.
--     Denormalizing it duplicates storage with no new query capability.
--   - dispatch_outcome is NOT added here: outcomes change over time and
--     need UPDATEs, which break the append-only design (tms#53).
--     Will live in a companion tms_review.dispatch_outcomes table (PR B).
--   - BLOCKED reason is handled by fixing detect_transitions() to
--     forward parsed[1] (source change in this PR, no schema migration).
--
-- Migration number: 003 (001 = tms_review schema, 002 = events table)
--
-- Applied by: psql service=pg-superuser -f <this-file>

BEGIN;

ALTER TABLE tms_review.events
  ADD COLUMN issue_labels   JSONB,
  ADD COLUMN point_estimate  TEXT,
  ADD COLUMN area            TEXT;

-- Indexes for per-class stats queries
CREATE INDEX idx_events_point ON tms_review.events (point_estimate)
  WHERE point_estimate IS NOT NULL;
CREATE INDEX idx_events_area  ON tms_review.events (area)
  WHERE area IS NOT NULL;
-- GIN index for jsonb_array_elements_text(issue_labels) in GROUP BY queries
CREATE INDEX idx_events_labels ON tms_review.events USING GIN (issue_labels);

-- Grant: new columns are on an existing table, so ALTER DEFAULT from
-- migration 002 covers future tables but not this ALTER. Re-grant.
GRANT SELECT ON ALL TABLES IN SCHEMA tms_review TO bogocat_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT SELECT ON TABLES TO bogocat_ro;
GRANT ALL ON ALL TABLES IN SCHEMA tms_review TO bogocat;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT ALL ON TABLES TO bogocat;

COMMIT;
