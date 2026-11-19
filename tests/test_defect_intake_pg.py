"""Intake against a real database — the four-call contract (tms#157).

The sqlite shim in ``conftest.py`` proves the *logic*; it cannot prove that the
partial-index conflict target, ``EXCLUDED``, ``COALESCE`` and the ``RETURNING``
predicates behave the same on postgres. This module runs the contract end to end
against a throwaway database with migrations 001→010 applied, through the real
``psycopg2`` path (``TMS_DB_CONF`` points ``review_eval._get_conn()`` at it).

That closes a review finding on the first draft of this PR, whose module
docstring cited a ``TMS_TEST_PG_SERVICE`` integration test that did not exist.

    TMS_TEST_PG_SERVICE=pg-superuser pytest tests/test_defect_intake_pg.py -v

SKIPPED unless ``TMS_TEST_PG_SERVICE`` names a libpq service. The unified
database is never touched: each test creates and drops its own
``tms_intaketest_*`` database.
"""

import os
import uuid
from pathlib import Path

import pytest

from tests.test_defect_register_migration import (MIGRATIONS, SERVICE_ENV, _psql,
                                                  _rows)

FP = "pipeline-health:junction-drift:152"
MIGRATION_010 = MIGRATIONS / "010-defect-regression-count-semantics.sql"

INTEGRATION = pytest.mark.skipif(
    not os.environ.get(SERVICE_ENV),
    reason=f"set {SERVICE_ENV} to run the real-postgres intake contract",
)


def _apply_migrations(service, db):
    for path in sorted(MIGRATIONS.glob("0*.sql")):
        _psql(service, "-f", str(path), dbname=db)


@pytest.fixture()
def intake_db(tmp_path, monkeypatch):
    """A throwaway database at 001→010, wired into intake's connection path."""
    service = os.environ.get(SERVICE_ENV)
    if not service:
        pytest.skip(f"set {SERVICE_ENV} to run the real-postgres intake contract")

    db = f"tms_intaketest_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _psql(service, "-c", f'CREATE DATABASE "{db}"')
    try:
        _apply_migrations(service, db)
        # Same host/role a producer would use; only the database differs.
        conf = tmp_path / "db.conf"
        conf.write_text("host=10.89.97.212 dbname=%s user=bogocat password=%s\n"
                        % (db, _password_for_bogocat()))
        monkeypatch.setenv("TMS_DB_CONF", str(conf))
        yield service, db
    finally:
        _psql(service, "-c",
              f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)', check=False)


def _password_for_bogocat():
    """The fleet's shared password for the bogocat role, from ~/.pgpass.

    Read rather than hardcoded: this is the same credential db.conf carries, and
    a test must not carry a secret of its own.
    """
    path = os.path.expanduser("~/.pgpass")
    with open(path) as handle:
        for line in handle:
            parts = line.strip().split(":")
            if len(parts) >= 5 and parts[3] == "bogocat":
                return parts[4]
    raise RuntimeError("no bogocat entry in ~/.pgpass")


def _defect(**overrides):
    from tms.defects import record_defect

    kwargs = dict(
        repo="home-portal",
        defect_class="silent-failure",
        fingerprint=FP,
        description="Oathbringer has no book_topics/book_traditions rows",
        detector="pipeline-health",
    )
    kwargs.update(overrides)
    return record_defect(**kwargs)


def _mark_verified(service, db):
    """The #158 write path, simulated by SQL: merged, then verified."""
    _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET merged_at = NOW() - interval '2 days',
               verified_at = NOW() - interval '1 day'
         WHERE fingerprint = 'pipeline-health:junction-drift:152'
    """, dbname=db)


@INTEGRATION
def test_four_calls_across_the_register_states(intake_db):
    """inserted → recurred → regressed → recurred, one row throughout."""
    service, db = intake_db

    first = _defect()
    assert first.outcome == "inserted"
    assert first.recurrence_count == 1
    assert first.regression_count == 0

    second = _defect()
    assert second.outcome == "recurred"
    assert second.recurrence_count == 2
    assert second.defect_id == first.defect_id

    _mark_verified(service, db)

    third = _defect()
    assert third.outcome == "regressed"
    assert third.regression_count == 1
    assert third.recurrence_count == 3

    fourth = _defect()
    assert fourth.outcome == "recurred", (
        "still-broken-after-regression is a recurrence, not a second regression"
    )
    assert fourth.regression_count == 1, "one regression must not be counted twice"
    assert fourth.recurrence_count == 4

    assert _rows(service, db, """
        SELECT count(*), max(recurrence_count), max(regression_count),
               max(regressed_at) IS NOT NULL, max(description)
          FROM tms_review.escaped_defects
    """) == [("1", "4", "1", "t",
              "Oathbringer has no book_topics/book_traditions rows")]


@INTEGRATION
def test_derived_status_after_the_four_calls(intake_db):
    """The register's read side agrees with the columns intake wrote."""
    service, db = intake_db

    _defect()
    _mark_verified(service, db)
    assert _rows(service, db, "SELECT status FROM tms_review.defects_v") == [
        ("verified",)
    ]

    _defect()
    assert _rows(service, db, """
        SELECT status, detector, discovery_source, introducing_pr IS NULL
          FROM tms_review.defects_v
    """) == [("regressed", "pipeline-health", "detector", "t")]


@INTEGRATION
def test_a_manual_row_is_adopted_but_keeps_its_source(intake_db):
    """A detector re-sighting a hand-filed row fills `detector` (sweepable) and
    leaves `discovery_source` alone (how it was first discovered).

    The row is seeded with SQL because the guard added after the live incident
    (a three-segment fingerprint with no detector is now rejected) makes it
    impossible to *create* this shape through intake. It still exists in
    production — `3976ac52-…` was filed before that guard — so adoption has to
    keep working for it.
    """
    service, db = intake_db

    _psql(service, "-c", """
        INSERT INTO tms_review.escaped_defects (
            defect_id, repo, defect_class, severity, discovered_at,
            discovery_source, description, fingerprint, detector, last_seen_at)
        VALUES (gen_random_uuid(), 'home-portal', 'silent-failure', 'ungraded',
                '2026-11-19T00:00:00+00:00', 'manual', 'filed by hand',
                'pipeline-health:junction-drift:152', NULL, NOW())
    """, dbname=db)

    adopted = _defect()
    assert adopted.outcome == "recurred", (
        "a detector re-sighting a manual row must not insert a second row"
    )
    assert _rows(service, db, """
        SELECT detector, discovery_source, recurrence_count
          FROM tms_review.escaped_defects
    """) == [("pipeline-health", "manual", "2")]


@INTEGRATION
def test_010_is_applied_and_says_intake_owns_the_counter(intake_db):
    """The documented contract matches the writer (review finding: 008's column
    comment said the counter is bumped when `regressed_at` is cleared, which is
    the re-fix writer's job and would double-count one regression)."""
    service, db = intake_db

    (comment,) = _rows(service, db, """
        SELECT col_description('tms_review.escaped_defects'::regclass,
                               (SELECT attnum FROM pg_attribute
                                 WHERE attrelid = 'tms_review.escaped_defects'::regclass
                                   AND attname = 'regression_count'))
    """)[0]

    assert "DETECTION" in comment
    assert "must NOT increment" in comment


# ── hermetic documentation guards for 010 (no database needed) ────

def test_migration_010_replaces_the_counter_comment():
    """A COMMENT-only migration is unreviewable if its file can drift from what
    it claims to say"""
    assert MIGRATION_010.is_file(), "missing 010-defect-regression-count-semantics.sql"
    text = MIGRATION_010.read_text()
    assert "COMMENT ON COLUMN tms_review.escaped_defects.regression_count" in text
    assert "DETECTION" in text
    assert "must NOT increment" in text
    assert "psql service=pg-superuser -f" in text, (
        "the documented application path is missing from the header"
    )
    assert text.count("BEGIN;") == 1 and text.count("COMMIT;") == 1


def test_readme_states_intake_increments_the_counter():
    """Three places said the opposite of the code. The README is one of them and
    it is the one a human reads first."""
    readme = (Path("README.md").resolve()).read_text()
    assert "incremented by **intake, at detection**" in readme
    assert "must not touch it" in readme
