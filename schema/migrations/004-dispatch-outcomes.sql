-- Migration: dispatch_outcomes companion table (#76 PR B)
--
-- Outcomes change over time (open -> merged/closed) so they need UPDATEs.
-- Storing them on the append-only events table would break the tms#53
-- contract. This companion table is keyed by aoe_id_prefix (same 8-char
-- prefix as events), written by a separate outcomes job.
--
-- Migration number: 004 (001 = tms_review, 002 = events, 003 = metadata)
--
-- Applied by: psql service=pg-superuser -f <this-file>

BEGIN;

CREATE TABLE tms_review.dispatch_outcomes (
    aoe_id_prefix   TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    issue           INTEGER NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN (
                        'merged', 'closed_unmerged', 'open', 'unknown'
                    )),
    derived_via     TEXT,   -- 'gh_pr_list' | 'transition_event' | 'timeout' | 'manual'
    derived_at      TEXT NOT NULL,  -- ISO timestamp of last derivation
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_outcomes_repo_issue ON tms_review.dispatch_outcomes (repo, issue);

GRANT SELECT ON ALL TABLES IN SCHEMA tms_review TO bogocat_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT SELECT ON TABLES TO bogocat_ro;
GRANT ALL ON ALL TABLES IN SCHEMA tms_review TO bogocat;
ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review
    GRANT ALL ON TABLES TO bogocat;

COMMIT;
