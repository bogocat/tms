"""Migration 009 against a real database — nullable introducing columns (tms#157).

The static guards for 009 live in ``tests/test_defect_intake_migration.py`` and
**they cannot falsify the migration**: they grep the file, and a grep matches a
commented-out ``ALTER COLUMN … DROP NOT NULL`` exactly as happily as a live one.
The properties that matter are therefore asserted against real postgres here, on
the same throwaway-DB harness the 008 tests use (imported, not copied).

    TMS_TEST_PG_SERVICE=pg-superuser pytest \\
        tests/test_defect_intake_migration_db.py -v

SKIPPED unless ``TMS_TEST_PG_SERVICE`` names a libpq service. Each test creates
and drops its own ``tms_migtest9_*`` database; the unified database is never
touched.
"""

import os
import uuid

import pytest

from tests.test_defect_register_migration import (MIGRATIONS, SERVICE_ENV,
                                                  _psql, _rows)

MIGRATION_009 = MIGRATIONS / "009-defect-intake-nullable.sql"

_STATUS_CHECKS = (
    "escaped_defects_regression_requires_detector",
    "escaped_defects_regression_requires_merge",
    "escaped_defects_verification_requires_merge",
    "escaped_defects_verification_requires_detector",
)


@pytest.fixture()
def scratch_db_before_009():
    """A throwaway database at 001→008, before 009 is applied."""
    service = os.environ.get(SERVICE_ENV)
    if not service:
        pytest.skip(
            f"set {SERVICE_ENV} (e.g. pg-superuser) to run DB migration tests"
        )

    db = f"tms_migtest9_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _psql(service, "-c", f'CREATE DATABASE "{db}"')
    try:
        for path in sorted(MIGRATIONS.glob("00[1-8]-*.sql")):
            _psql(service, "-f", str(path), dbname=db)
        yield db
    finally:
        _psql(service, "-c", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)',
              check=False)


def _attnotnull(service, db):
    """{column: attnotnull} for the two columns 009 touches."""
    return dict(_rows(service, db, """
        SELECT attname, attnotnull
          FROM pg_attribute
         WHERE attrelid = 'tms_review.escaped_defects'::regclass
           AND attname IN ('introducing_pr', 'introducing_commit')
    """))


def _status_checks(service, db):
    return {row[0] for row in _rows(service, db, """
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'tms_review.escaped_defects'::regclass
           AND contype = 'c'
    """)}


def test_009_actually_drops_both_not_null(scratch_db_before_009):
    """THE test the static greps cannot be: comment out either ALTER and this
    fails. Everything 009 exists for depends on these two columns accepting
    NULL."""
    service = os.environ[SERVICE_ENV]
    db = scratch_db_before_009

    assert _attnotnull(service, db) == {
        "introducing_pr": "t", "introducing_commit": "t",
    }, "fixture is not at the pre-009 state"

    _psql(service, "-f", str(MIGRATION_009), dbname=db)

    assert _attnotnull(service, db) == {
        "introducing_pr": "f", "introducing_commit": "f",
    }


def test_009_lets_a_detector_row_exist_and_derive_new(scratch_db_before_009):
    """The fact 009 is for: a detector-filed defect has no introducing PR."""
    service = os.environ[SERVICE_ENV]
    db = scratch_db_before_009

    # Pre-009: the insert a producer needs is impossible.
    pre = _psql(service, "-c", """
        INSERT INTO tms_review.escaped_defects (
            defect_id, repo, defect_class, severity, discovered_at,
            discovery_source, description, fingerprint, detector,
            last_seen_at)
        VALUES (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
                '2026-11-19T00:00:00+00:00', 'detector', 'pre-009 probe',
                'pipeline-health:probe:1', 'pipeline-health', NOW())
    """, dbname=db, check=False)
    assert pre.returncode != 0, (
        "a detector row inserted without introducing_pr before 009 — "
        "the premise of this migration is wrong"
    )

    _psql(service, "-f", str(MIGRATION_009), dbname=db)
    _psql(service, "-c", """
        INSERT INTO tms_review.escaped_defects (
            defect_id, repo, defect_class, severity, discovered_at,
            discovery_source, description, fingerprint, detector,
            last_seen_at)
        VALUES (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
                '2026-11-19T00:00:00+00:00', 'detector', 'post-009 probe',
                'pipeline-health:probe:1', 'pipeline-health', NOW())
    """, dbname=db)

    assert _rows(service, db, """
        SELECT status, detector, introducing_pr IS NULL, introducing_commit IS NULL
          FROM tms_review.defects_v
    """) == [("new", "pipeline-health", "t", "t")]


def test_009_is_rerunnable(scratch_db_before_009):
    service = os.environ[SERVICE_ENV]
    db = scratch_db_before_009

    _psql(service, "-f", str(MIGRATION_009), dbname=db)
    _psql(service, "-f", str(MIGRATION_009), dbname=db)

    assert _attnotnull(service, db) == {
        "introducing_pr": "f", "introducing_commit": "f",
    }


def test_009_leaves_the_status_checks_biting(scratch_db_before_009):
    """009 must not be a step towards a register that reports regressions it
    cannot support: the four 008 CHECKs still exist AND still reject."""
    service = os.environ[SERVICE_ENV]
    db = scratch_db_before_009

    _psql(service, "-f", str(MIGRATION_009), dbname=db)

    assert _status_checks(service, db) >= set(_STATUS_CHECKS)

    _psql(service, "-c", """
        INSERT INTO tms_review.escaped_defects (
            defect_id, repo, defect_class, severity, discovered_at,
            discovery_source, description, fingerprint, detector,
            last_seen_at, merged_at)
        VALUES (gen_random_uuid(), 'tms', 'logic', 'minor',
                '2026-11-19T00:00:00+00:00', 'manual', 'no detector',
                'manual:aaaa', NULL, NOW(), NOW())
    """, dbname=db)
    violation = _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET regressed_at = NOW()
         WHERE fingerprint = 'manual:aaaa'
    """, dbname=db, check=False)

    assert violation.returncode != 0, (
        "a regression was recorded on a detector-less row after 009"
    )
    assert "escaped_defects_regression_requires_detector" in violation.stderr


def test_009_does_not_touch_the_existing_rows(scratch_db_before_009):
    service = os.environ[SERVICE_ENV]
    db = scratch_db_before_009

    _psql(service, "-c", """
        INSERT INTO tms_review.escaped_defects (
            defect_id, repo, introducing_pr, introducing_commit, defect_class,
            severity, discovered_at, discovery_source, description,
            fingerprint, last_seen_at)
        VALUES ('7a80e26e-7663-444e-91e7-ca45a60fc9f5', 'home-portal', 102,
                'e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4', 'sql-syntax',
                'critical', '2026-06-05T06:27:08+00:00', 'ci',
                'a pre-existing row', 'manual:fc45adb2', NOW())
    """, dbname=db)

    _psql(service, "-f", str(MIGRATION_009), dbname=db)

    assert _rows(service, db, """
        SELECT repo, introducing_pr, introducing_commit, fix_pr IS NULL
          FROM tms_review.escaped_defects
    """) == [("home-portal", "102",
              "e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4", "t")]
