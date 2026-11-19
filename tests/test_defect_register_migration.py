"""Migration 008 — defect register fingerprint / regression schema (tms#156).

Two layers:

  * **static shape guards** — always run, hermetic, no database needed.
    They pin the promises the migration file makes (guarded DDL, partial
    unique index, derived-status view, backfill predicate, documented
    application path).

  * **DB integration** — SKIPPED unless ``TMS_TEST_PG_SERVICE`` names a
    libpq service (e.g. ``pg-superuser``). The tests create a throwaway
    database on that server, apply migrations 001-007, seed the four live
    ``escaped_defects`` rows (verbatim 2026-11-18 state), apply 008 twice
    to prove re-run safety, and then assert the register's invariants.

      TMS_TEST_PG_SERVICE=pg-superuser pytest \
          tests/test_defect_register_migration.py -v

The integration tests never touch the unified instance: they only create
and drop a ``tms_migtest_<pid>_<rand>`` database.
"""

import os
import subprocess
import uuid
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parent.parent / "schema" / "migrations"
MIGRATION_008 = MIGRATIONS / "008-defect-fingerprint.sql"
SERVICE_ENV = "TMS_TEST_PG_SERVICE"

NEW_COLUMNS = [
    "fingerprint",
    "recurrence_count",
    "detector",
    "last_seen_at",
    "merged_at",
    "verified_at",
    "deployed_sha",
    "regressed_at",
    "regression_count",
]

# The four rows exactly as they stood on 2026-11-18 (bogocat_ro read).
LIVE_ROWS = [
    (
        "7a80e26e-7663-444e-91e7-ca45a60fc9f5", "home-portal", 102,
        "e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4", "sql-syntax",
        "critical", "2026-06-05T06:27:08+00:00", "ci",
        "DISTINCT ON placed after ORDER BY instead of immediately after "
        "SELECT — a PostgreSQL syntax error that would 500 the widget on "
        "every render. Fix: commit "
        "ce0d29b61357a93fe0134c77e76a4d56aedbac33",
        102,
    ),
    (
        "bbb46d6b-d302-4a2a-8e3e-bcddf6ef71df", "pi-dotfiles", 9,
        "db76a702ef5e50a7fbd477aad48949ae5315bbdf", "logic",
        "critical", "2026-07-11T17:30:00+00:00", "post-merge-review",
        "Verdict contract never wired for reviewer-claude-api fallback: "
        "panel= provenance rests on model self-report, which the contract "
        "forbids. [prompt-contract] Fix: pi-dotfiles#12",
        None,
    ),
    (
        "3508df65-b081-4360-b894-fde0a3d810ef", "pi-dotfiles", 9,
        "db76a702ef5e50a7fbd477aad48949ae5315bbdf", "logic",
        "critical", "2026-07-11T17:30:00+00:00", "post-merge-review",
        "Skill pseudocode auto-dispatches reviewer-claude-api; memory "
        "merged in same PR requires per-dispatch operator decision — both "
        "normative, contradictory. [prompt-contract] Fix: pi-dotfiles#13",
        None,
    ),
    (
        "b7369835-3b66-47d6-9262-c66fe5e78dd5", "pi-dotfiles", 10,
        "4dfc2b17b1884be4e601b96f87e4aded46796b1e", "logic",
        "critical", "2026-07-12T00:00:00+00:00", "post-merge-review",
        "plan-gate.md injected contract slice still teaches old fast path "
        "(no sensitive-pattern disqualifier) — contradicts SKILL at the "
        "gate-injector's own decision point. [prompt-contract] Fix: "
        "pi-dotfiles#15",
        None,
    ),
]

# sha256(repo || defect_class || description) of live row 1, uppercase-free
# hex, prefixed 'manual:' by the backfill.
HOME_PORTAL_FINGERPRINT = (
    "manual:fc45adb2b49f8578c45ce2cddf24942d8d3e6175e523e6757abdfd3e884e0ecf"
)


# ── psql helpers ──────────────────────────────────────────────────


def _conninfo(service, dbname=None):
    """libpq conninfo string — the scratch DB must ride on the service's
    host/user, so dbname goes IN the conninfo, not via ``-d`` (``-d``
    replaces the conninfo argument and loses ``service=`` entirely)."""
    ci = f"service={service}"
    if dbname:
        ci += f" dbname={dbname}"
    return ci


def _psql(service, *args, dbname=None, check=True):
    cmd = ["psql", _conninfo(service, dbname),
           "-X", "-q", "-v", "ON_ERROR_STOP=1"]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _rows(service, dbname, sql):
    """Run sql, return list of tuples (tab-separated, unaligned)."""
    out = _psql(service, "-At", "-F", "\t", "-c", sql, dbname=dbname).stdout
    return [tuple(line.split("\t")) for line in out.splitlines() if line]


def _seed_sql():
    """INSERT the four live rows using dollar-quoted text (quote-safe)."""
    stmts = []
    for (defect_id, repo, pr, commit, cls, sev, discovered, source,
         description, fix_pr) in LIVE_ROWS:
        fix = "NULL" if fix_pr is None else str(fix_pr)
        stmts.append(
            f"""INSERT INTO tms_review.escaped_defects (
                    defect_id, repo, introducing_pr, introducing_commit,
                    defect_class, severity, discovered_at, discovery_source,
                    description, fix_pr)
                VALUES ('{defect_id}', '{repo}', {pr}, '{commit}', '{cls}',
                        '{sev}', '{discovered}', '{source}', $q${description}$q$,
                        {fix});"""
        )
    return "\n".join(stmts)


def _insert_defect_sql(repo="home-portal", fingerprint=None, description="x"):
    defect_id = str(uuid.uuid4())
    fp = "NULL" if fingerprint is None else f"'{fingerprint}'"
    return f"""INSERT INTO tms_review.escaped_defects (
                    defect_id, repo, introducing_pr, introducing_commit,
                    defect_class, severity, discovered_at, discovery_source,
                    description, fingerprint)
                VALUES ('{defect_id}', '{repo}', 1, 'deadbeef', 'logic',
                        'minor', '2026-11-18T00:00:00+00:00', 'manual',
                        '{description}', {fp});"""


@pytest.fixture()
def scratch_db():
    """Throwing a migration-tested database away — on a scratch DB only."""
    service = os.environ.get(SERVICE_ENV)
    if not service:
        pytest.skip(
            f"set {SERVICE_ENV} (e.g. pg-superuser) to run DB migration tests"
        )

    db = f"tms_migtest_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _psql(service, "-c", f'CREATE DATABASE "{db}"')
    try:
        for path in sorted(MIGRATIONS.glob("00[1-7]-*.sql")):
            _psql(service, "-f", str(path), dbname=db)
        _psql(service, "-c", _seed_sql(), dbname=db)
        # Apply 008 twice: the second run is the re-run-safety assertion.
        _psql(service, "-f", str(MIGRATION_008), dbname=db)
        _psql(service, "-f", str(MIGRATION_008), dbname=db)
        yield db
    finally:
        _psql(service, "-c", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)',
              check=False)


def _needs_db():
    return pytest.mark.skipif(
        not os.environ.get(SERVICE_ENV),
        reason=f"set {SERVICE_ENV} to run DB migration tests",
    )


# ── static shape guards (always run) ──────────────────────────────


def test_migration_file_exists():
    assert MIGRATION_008.is_file(), (
        f"missing {MIGRATION_008.relative_to(MIGRATION_008.parents[2])}"
    )


def test_header_documents_application_path():
    text = MIGRATION_008.read_text()
    assert "psql service=pg-superuser -f" in text


def test_adds_every_new_column_guarded():
    text = MIGRATION_008.read_text()
    for col in NEW_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {col} " in text, (
            f"column {col} not added with ADD COLUMN IF NOT EXISTS"
        )


def test_partial_unique_index_on_repo_fingerprint():
    text = MIGRATION_008.read_text()
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in text
    assert "(repo, fingerprint)" in text
    assert "WHERE fingerprint IS NOT NULL" in text


def test_view_derives_five_arm_status():
    text = MIGRATION_008.read_text()
    assert "CREATE OR REPLACE VIEW tms_review.defects_v" in text
    # Every arm names a fact the row proves; fix-proposed is not fixed.
    for status in ("'regressed'", "'verified'", "'fixed'",
                   "'fix-proposed'", "'new'"):
        assert status in text, f"status arm {status} missing from defects_v"


def test_backfill_is_guarded_and_uses_fingerprint_formula():
    text = MIGRATION_008.read_text()
    assert "'manual:'" in text
    assert "sha256(" in text
    assert "WHERE fingerprint IS NULL" in text
    assert "last_seen_at = COALESCE(last_seen_at, created_at)" in text


def test_regression_requires_detector_check():
    text = MIGRATION_008.read_text()
    assert "CHECK (regressed_at IS NULL OR detector IS NOT NULL)" in text


def test_grants_cover_both_roles():
    text = MIGRATION_008.read_text()
    assert "TO bogocat_ro" in text
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA tms_review" in text
    assert "TO bogocat" in text


def test_001_and_readme_document_the_revival():
    old = (MIGRATIONS / "001-create-tms-review.sql").read_text()
    assert "008-defect-fingerprint" in old, (
        "001 header must point at the revival migration"
    )
    assert "defect register" in old.lower()

    readme = (MIGRATIONS.parents[1] / "README.md").read_text()
    assert "defects_v" in readme
    assert "fix-proposed" in readme


# ── DB integration (opt-in) ───────────────────────────────────────


@_needs_db()
def test_columns_have_expected_shape(scratch_db):
    service = os.environ[SERVICE_ENV]
    rows = _rows(service, scratch_db, """
        SELECT column_name, data_type, is_nullable, coalesce(column_default, '')
        FROM information_schema.columns
        WHERE table_schema = 'tms_review' AND table_name = 'escaped_defects'
    """)
    shape = {name: (dtype, nullable, default) for name, dtype, nullable, default
             in rows}

    assert shape["fingerprint"] == ("text", "YES", "")
    assert shape["recurrence_count"] == ("integer", "NO", "1")
    assert shape["detector"] == ("text", "YES", "")
    assert shape["last_seen_at"] == ("timestamp with time zone", "YES", "")
    assert shape["merged_at"] == ("timestamp with time zone", "YES", "")
    assert shape["verified_at"] == ("timestamp with time zone", "YES", "")
    assert shape["deployed_sha"] == ("text", "YES", "")
    assert shape["regressed_at"] == ("timestamp with time zone", "YES", "")
    assert shape["regression_count"] == ("integer", "NO", "0")


@_needs_db()
def test_backfill_fills_all_four_rows_uniquely(scratch_db):
    service = os.environ[SERVICE_ENV]
    (nulls,), = _rows(service, scratch_db, """
        SELECT count(*) FROM tms_review.escaped_defects
        WHERE fingerprint IS NULL
    """)
    assert nulls == "0"

    (total, distinct) = _rows(service, scratch_db, """
        SELECT count(*), count(DISTINCT (repo, fingerprint))
        FROM tms_review.escaped_defects
    """)[0]
    assert (total, distinct) == ("4", "4")

    (fp,), = _rows(service, scratch_db, """
        SELECT fingerprint FROM tms_review.escaped_defects
        WHERE repo = 'home-portal'
    """)
    assert fp == HOME_PORTAL_FINGERPRINT

    (mismatched,), = _rows(service, scratch_db, """
        SELECT count(*) FROM tms_review.escaped_defects
        WHERE last_seen_at IS DISTINCT FROM created_at
    """)
    assert mismatched == "0"


@_needs_db()
def test_defects_v_status_vocabulary_after_backfill(scratch_db):
    service = os.environ[SERVICE_ENV]
    counts = {status: n for status, n in _rows(service, scratch_db, """
        SELECT status, count(*) FROM tms_review.defects_v GROUP BY 1
    """)}
    # fix_pr=102 proves a fix PR is named, NOT that it merged -> fix-proposed.
    assert counts == {"fix-proposed": "1", "new": "3"}

    (merged,), = _rows(service, scratch_db, """
        SELECT count(*) FROM tms_review.defects_v
        WHERE status = 'fixed'
    """)
    assert merged == "0", "backfill must not claim a merge it cannot prove"


@_needs_db()
def test_defects_v_derives_verified_and_regressed(scratch_db):
    service = os.environ[SERVICE_ENV]
    # detector-less row may not claim a regression
    bad = _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET regressed_at = NOW()
         WHERE defect_id = '7a80e26e-7663-444e-91e7-ca45a60fc9f5'
    """, dbname=scratch_db, check=False)
    assert bad.returncode != 0
    assert "escaped_defects_regression_requires_detector" in bad.stderr

    _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET detector = 'pytest-detector', regressed_at = NOW()
         WHERE defect_id = '7a80e26e-7663-444e-91e7-ca45a60fc9f5'
    """, dbname=scratch_db)
    (status,), = _rows(service, scratch_db, """
        SELECT status FROM tms_review.defects_v
         WHERE defect_id = '7a80e26e-7663-444e-91e7-ca45a60fc9f5'
    """)
    assert status == "regressed"

    _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET detector = 'pytest-detector', merged_at = NOW(),
               verified_at = NOW()
         WHERE defect_id = 'bbb46d6b-d302-4a2a-8e3e-bcddf6ef71df'
    """, dbname=scratch_db)
    (status,), = _rows(service, scratch_db, """
        SELECT status FROM tms_review.defects_v
         WHERE defect_id = 'bbb46d6b-d302-4a2a-8e3e-bcddf6ef71df'
    """)
    assert status == "verified"


@_needs_db()
def test_one_row_per_fingerprint_is_enforced(scratch_db):
    service = os.environ[SERVICE_ENV]
    dup = _psql(service, "-c",
                _insert_defect_sql(fingerprint=HOME_PORTAL_FINGERPRINT,
                                   description="duplicate of live row"),
                dbname=scratch_db, check=False)
    assert dup.returncode != 0
    assert "duplicate key value" in dup.stderr

    # fingerprint IS NULL rows are unconstrained (writer-friendly pre-#A2)
    for i in range(2):
        _psql(service, "-c", _insert_defect_sql(description=f"nullfp {i}"),
              dbname=scratch_db)


@_needs_db()
def test_bogocat_ro_can_select_defects_v(scratch_db):
    service = os.environ[SERVICE_ENV]
    out = _psql(service, "-At",
                "-c", "SET ROLE bogocat_ro;",
                "-c", "SELECT count(*) FROM tms_review.defects_v;",
                dbname=scratch_db).stdout
    assert out.strip().splitlines()[-1] == "4"


@_needs_db()
def test_readme_documents_corrected_vocabulary(scratch_db):
    """README's status table matches what the view actually returns."""
    service = os.environ[SERVICE_ENV]
    statuses = {s for (s,) in _rows(service, scratch_db, """
        SELECT DISTINCT status FROM tms_review.defects_v
    """)}
    readme = (MIGRATIONS.parents[1] / "README.md").read_text()
    for status in statuses:
        assert status in readme
