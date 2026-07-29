-- Migration: add verdict state + dispatch join keys to reviewer_runs (#71)
--
-- verdict: the parsed verdict state (PASS|FAIL) from the
--   <<REVIEW-VERDICT: ...>> marker. NULL for legacy rows written
--   before this migration; outcome for those is derived from p0/p1.
--
-- dispatch_session / aoe_id_prefix: join keys to tms_review.events so
--   a dispatch -> review -> cost chain is one query (issue #71 AC).
--   Resolved at capture time from the most recent review-dispatch
--   event for the same (repo, pr_number). NULL when no dispatch event
--   exists (in-session panels, human ad-hoc reviews, legacy rows).
--
-- Applied by: psql service=pg-superuser -f schema/migrations/007-reviewer-runs-dispatch-join.sql

ALTER TABLE tms_review.reviewer_runs
    ADD COLUMN IF NOT EXISTS verdict TEXT;

ALTER TABLE tms_review.reviewer_runs
    ADD COLUMN IF NOT EXISTS dispatch_session TEXT;

ALTER TABLE tms_review.reviewer_runs
    ADD COLUMN IF NOT EXISTS aoe_id_prefix TEXT;

COMMENT ON COLUMN tms_review.reviewer_runs.verdict IS
    'Parsed verdict state (PASS|FAIL) from the REVIEW-VERDICT marker. NULL = legacy row (pre-#71).';

COMMENT ON COLUMN tms_review.reviewer_runs.dispatch_session IS
    'Session name of the review dispatch that produced this run (join key to tms_review.events.session). NULL when no dispatch event matched.';

COMMENT ON COLUMN tms_review.reviewer_runs.aoe_id_prefix IS
    'aoe id prefix of the review dispatch (join key to tms_review.events.aoe_id_prefix). NULL when no dispatch event matched.';
