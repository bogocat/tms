-- Migration: create tms_review.events table for fleet dispatch metrics (#65)
--
-- Replaces ~/.local/state/tmq/events.jsonl with a postgres table.
-- Uses TEXT-based columns and Python-side UUID generation for sqlite3
-- test shim compatibility (same pattern as #54 reviewer_eval tables).
--
-- Design:
--   - id + created_at are generated in Python (uuid.uuid4() + isoformat),
--     never by SQL defaults — required for sqlite3 test shim.
--   - event_timestamp is the domain time from the JSONL record.timestamp.
--   - Flat columns are denormalized query indices; payload is the
--     canonical full record (json.dumps of the event dict) for forward
--     compatibility with new event types (e.g. tms#56 staleness).
--   - Composite UNIQUE on (event_type, aoe_id_prefix, event_timestamp)
--     guards backfill idempotency (ON CONFLICT DO NOTHING).
--
-- Applied by: psql service=pg-superuser -f <this-file>

BEGIN;

CREATE TABLE tms_review.events (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    repo            TEXT,
    issue           INTEGER,
    agent           TEXT,
    provider        TEXT,
    model           TEXT,
    dispatch_type   TEXT,
    worktree        TEXT,
    session         TEXT,
    aoe_id_prefix   TEXT,
    reason          TEXT,
    from_status     TEXT,
    to_status       TEXT,
    payload         TEXT NOT NULL
);

-- Composite UNIQUE for backfill idempotency (ON CONFLICT DO NOTHING)
CREATE UNIQUE INDEX idx_events_unique
    ON tms_review.events (event_type, aoe_id_prefix, event_timestamp);

CREATE INDEX idx_events_type ON tms_review.events (event_type);
CREATE INDEX idx_events_aoe  ON tms_review.events (aoe_id_prefix);

-- Grants: fix the bogocat_ro gap discovered during proposal review
-- (migration 001 also fixed in this PR — before this migration,
-- bogocat_ro saw 0 tables in tms_review).
GRANT SELECT ON ALL TABLES IN SCHEMA tms_review TO bogocat_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT SELECT ON TABLES TO bogocat_ro;

-- Also grant to bogocat (write role) for completeness (tables created
-- here, so the ALTER DEFAULT from 001 doesn't cover them).
GRANT ALL ON ALL TABLES IN SCHEMA tms_review TO bogocat;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT ALL ON TABLES TO bogocat;

COMMIT;
