-- Migration: add specialist_composition column to reviewer_runs (#94)
--
-- Records which specialist reviewer personas composed this review panel.
-- Stored as JSONB array of signal names (e.g. ["security", "schema"]).
-- Empty array = generalist-only panel.
--
-- Applied by: psql service=pg-superuser -f <this-file>

ALTER TABLE tms_review.reviewer_runs
    ADD COLUMN IF NOT EXISTS specialist_composition JSONB
    NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN tms_review.reviewer_runs.specialist_composition IS
    'Specialist reviewer personas on this panel (JSONB array of signal names). Empty array = generalist-only.';
