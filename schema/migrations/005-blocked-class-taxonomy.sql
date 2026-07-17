-- Migration: BLOCKED reason taxonomy column on tms_review.events (#76 follow-up)
--
-- PR A (#78) forwarded the raw BLOCKED marker reason onto transition
-- events; this adds the taxonomy class as a flat query index so blocked
-- analysis is sliceable: which BLOCKEDs are mechanical (spawn flakiness)
-- vs ambiguous-ac (issue quality) vs capacity/scope-creep (model fit).
--
--   blocked_class TEXT: mechanical | ambiguous-ac | capacity |
--                       scope-creep | other
--
-- Populated at transition time by classify_blocked_reason() in
-- lib/tms/events.py. Nullable; legacy rows stay NULL (forward-looking,
-- per #76's no-retroactive-enrichment rule). The payload JSON carries
-- the same value; this column is the SQL query index, matching the
-- denormalized-index convention from migrations 002/003.
--
-- Applied by: psql service=pg-superuser -f <this-file>

BEGIN;

ALTER TABLE tms_review.events
    ADD COLUMN IF NOT EXISTS blocked_class TEXT;

CREATE INDEX IF NOT EXISTS idx_events_blocked_class
    ON tms_review.events (blocked_class)
    WHERE blocked_class IS NOT NULL;

-- Grants: new column on an existing table needs a re-grant (same
-- pattern as 003).
GRANT SELECT ON ALL TABLES IN SCHEMA tms_review TO bogocat_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT SELECT ON TABLES TO bogocat_ro;
GRANT ALL ON ALL TABLES IN SCHEMA tms_review TO bogocat;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT ALL ON TABLES TO bogocat;

COMMIT;
