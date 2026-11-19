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

The integration tests do not mutate the unified database: they only create
and drop their own ``tms_migtest_<pid>_<rand>`` database. Note that the
``pg-superuser`` service points at the production *server*, so a run does
create a (throwaway) database there; the fixture drops it in ``finally``.
If a run is SIGKILLed, clean up with:
``psql "service=pg-superuser dbname=postgres" -c "\\l tms_migtest*"`` and
``dropdb`` (no shared objects or privileges are touched — grants and
``ALTER DEFAULT PRIVILEGES`` inside the scratch DB are per-database).
"""

import os
import re
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

# The four rows exactly as they stood on 2026-11-18 (bogocat_ro read),
# including their created_at — the backfill is cutover-bounded, so the
# fixture must carry the real (pre-cutover) timestamps.
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
        "2026-07-12T05:09:59.449635+00:00",
    ),
    (
        "bbb46d6b-d302-4a2a-8e3e-bcddf6ef71df", "pi-dotfiles", 9,
        "db76a702ef5e50a7fbd477aad48949ae5315bbdf", "logic",
        "critical", "2026-07-11T17:30:00+00:00", "post-merge-review",
        "Verdict contract never wired for reviewer-claude-api fallback: "
        "panel= provenance rests on model self-report, which the contract "
        "forbids. [prompt-contract] Fix: pi-dotfiles#12",
        None,
        "2026-07-13T02:05:17.137997+00:00",
    ),
    (
        "3508df65-b081-4360-b894-fde0a3d810ef", "pi-dotfiles", 9,
        "db76a702ef5e50a7fbd477aad48949ae5315bbdf", "logic",
        "critical", "2026-07-11T17:30:00+00:00", "post-merge-review",
        "Skill pseudocode auto-dispatches reviewer-claude-api; memory "
        "merged in same PR requires per-dispatch operator decision — both "
        "normative, contradictory. [prompt-contract] Fix: pi-dotfiles#13",
        None,
        "2026-07-13T02:05:17.149914+00:00",
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
        "2026-07-13T02:05:17.164669+00:00",
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
         description, fix_pr, created_at) in LIVE_ROWS:
        fix = "NULL" if fix_pr is None else str(fix_pr)
        stmts.append(
            f"""INSERT INTO tms_review.escaped_defects (
                    defect_id, created_at, repo, introducing_pr,
                    introducing_commit, defect_class, severity, discovered_at,
                    discovery_source, description, fix_pr)
                VALUES ('{defect_id}', '{created_at}', '{repo}', {pr},
                        '{commit}', '{cls}', '{sev}', '{discovered}',
                        '{source}', $q${description}$q$, {fix});"""
        )
    return "\n".join(stmts)


def _insert_defect_sql(repo="home-portal", fingerprint=None, description="x",
                       created_at="2026-11-20T00:00:00+00:00"):
    """Insert a row. created_at is explicit (and post-cutover by default) so
    tests pin the backfill bound's semantics rather than the server clock."""
    defect_id = str(uuid.uuid4())
    fp = "NULL" if fingerprint is None else f"'{fingerprint}'"
    return f"""INSERT INTO tms_review.escaped_defects (
                    defect_id, created_at, repo, introducing_pr,
                    introducing_commit, defect_class, severity, discovered_at,
                    discovery_source, description, fingerprint)
                VALUES ('{defect_id}', '{created_at}', '{repo}', 1, 'deadbeef',
                        'logic', 'minor', '2026-11-18T00:00:00+00:00',
                        'manual', '{description}', {fp});"""


def _insert_legacy_defect_sql(repo="tms", description="x",
                              created_at="2026-07-01T00:00:00+00:00"):
    """Insert a row using only the pre-008 columns (for setup that happens
    before the migration runs — the fingerprint column does not exist yet)."""
    return f"""INSERT INTO tms_review.escaped_defects (
                    defect_id, created_at, repo, introducing_pr,
                    introducing_commit, defect_class, severity, discovered_at,
                    discovery_source, description)
                VALUES ('{uuid.uuid4()}', '{created_at}', '{repo}', 1,
                        'deadbeef', 'logic', 'minor',
                        '2026-11-18T00:00:00+00:00', 'manual',
                        $q${description}$q$);"""


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
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS \w+\s+"
        r"ON tms_review\.escaped_defects \(repo, fingerprint\)\s+"
        r"WHERE fingerprint IS NOT NULL;",
        text,
    ), "unique index statement is not on (repo, fingerprint) with the expected predicate"


def test_view_derives_five_arm_status():
    text = MIGRATION_008.read_text()
    assert "CREATE OR REPLACE VIEW tms_review.defects_v" in text
    # Every arm names a fact the row proves; fix-proposed is not fixed.
    for status in ("'regressed'", "'verified'", "'fixed'",
                   "'fix-proposed'", "'new'"):
        assert status in text, f"status arm {status} missing from defects_v"

    # Arm ORDER is the contract (regressed is sticky). Pinned so a reordered
    # CASE cannot silently change what the register reports.
    view_sql = text.split("CREATE OR REPLACE VIEW tms_review.defects_v")[1]
    order = [
        view_sql.index("'regressed'"),
        view_sql.index("'verified'"),
        view_sql.index("'fixed'"),
        view_sql.index("'fix-proposed'"),
        view_sql.index("'new'"),
    ]
    assert order == sorted(order), (
        f"status arms are out of precedence order: {order}"
    )


def test_backfill_is_guarded_and_uses_fingerprint_formula():
    text = MIGRATION_008.read_text()
    # pin the WHOLE formula, not just a couple of fragments
    assert ("sha256(convert_to(repo || defect_class || description, 'UTF8'))"
            in text)
    assert "encode(" in text and "'hex'" in text
    assert "'manual:' || encode(" in text
    assert "last_seen_at = COALESCE(last_seen_at, created_at)" in text
    # Bounded by the revival cutover: an unbounded NULL-fingerprint backfill
    # would re-stamp rows written after the migration and could abort a re-run
    # on the unique index.
    assert "WHERE fingerprint IS NULL" in text
    assert "AND created_at < TIMESTAMPTZ '2026-11-19 00:00:00+00'" in text
    # ... and the pre-cutover collision is diagnosed before the index exists
    assert "RAISE EXCEPTION" in text
    assert "defect backfill would collide" in text


def test_status_arms_are_constrained_by_the_schema():
    text = MIGRATION_008.read_text()
    for name, expr in (
        ("escaped_defects_regression_requires_detector",
         "CHECK (regressed_at IS NULL OR detector IS NOT NULL)"),
        ("escaped_defects_regression_requires_merge",
         "CHECK (regressed_at IS NULL OR merged_at IS NOT NULL)"),
        ("escaped_defects_verification_requires_merge",
         "CHECK (verified_at IS NULL OR merged_at IS NOT NULL)"),
        ("escaped_defects_verification_requires_detector",
         "CHECK (verified_at IS NULL OR detector IS NOT NULL)"),
    ):
        # the constraint must actually be ATTACHED, not merely named
        assert f"ADD CONSTRAINT {name}" in text, (
            f"constraint {name} is never added to the table"
        )
        assert expr in text, f"constraint {name} has no matching CHECK"


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
    # detector-less row may not claim a regression (merged_at set, so the
    # detector CHECK is the only one that can fire — no ordering dependence)
    bad = _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET merged_at = NOW() - interval '1 day', regressed_at = NOW()
         WHERE defect_id = '7a80e26e-7663-444e-91e7-ca45a60fc9f5'
    """, dbname=scratch_db, check=False)
    assert bad.returncode != 0
    assert "escaped_defects_regression_requires_detector" in bad.stderr

    # ... nor may a never-merged row (a regression presupposes a merged fix)
    bad = _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET detector = 'pytest-detector', regressed_at = NOW()
         WHERE defect_id = '7a80e26e-7663-444e-91e7-ca45a60fc9f5'
    """, dbname=scratch_db, check=False)
    assert bad.returncode != 0
    assert "escaped_defects_regression_requires_merge" in bad.stderr

    _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET detector = 'pytest-detector', merged_at = NOW() - interval '2 day',
               regressed_at = NOW()
         WHERE defect_id = '7a80e26e-7663-444e-91e7-ca45a60fc9f5'
    """, dbname=scratch_db)
    (status,), = _rows(service, scratch_db, """
        SELECT status FROM tms_review.defects_v
         WHERE defect_id = '7a80e26e-7663-444e-91e7-ca45a60fc9f5'
    """)
    assert status == "regressed"

    # "detector passed" without a detector is equally unsupported (merged_at
    # set, so only the detector CHECK can fire)
    bad = _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET merged_at = NOW(), verified_at = NOW()
         WHERE defect_id = '3508df65-b081-4360-b894-fde0a3d810ef'
    """, dbname=scratch_db, check=False)
    assert bad.returncode != 0
    assert "escaped_defects_verification_requires_detector" in bad.stderr

    # verified without a merge is rejected too
    bad = _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET detector = 'pytest-detector', verified_at = NOW()
         WHERE defect_id = '3508df65-b081-4360-b894-fde0a3d810ef'
    """, dbname=scratch_db, check=False)
    assert bad.returncode != 0
    assert "escaped_defects_verification_requires_merge" in bad.stderr

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
def test_regressed_precedence_is_sticky(scratch_db):
    """Pin the plan-gate arm order: regressed wins over a later verified_at.

    `regressed_at` means *currently regressed*; a re-verified row must clear
    it (regression_count keeps the history). This test is what makes that
    contract executable instead of prose.
    """
    service = os.environ[SERVICE_ENV]
    _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET detector = 'pytest-detector',
               merged_at = NOW() - interval '3 day',
               regressed_at = NOW() - interval '2 day',
               verified_at = NOW() - interval '1 day'
         WHERE defect_id = 'b7369835-3b66-47d6-9262-c66fe5e78dd5'
    """, dbname=scratch_db)

    (status,), = _rows(service, scratch_db, """
        SELECT status FROM tms_review.defects_v
         WHERE defect_id = 'b7369835-3b66-47d6-9262-c66fe5e78dd5'
    """)
    assert status == "regressed"

    # clearing regressed_at is how the state advances (writer contract, #A3)
    _psql(service, "-c", """
        UPDATE tms_review.escaped_defects
           SET regressed_at = NULL, regression_count = regression_count + 1
         WHERE defect_id = 'b7369835-3b66-47d6-9262-c66fe5e78dd5'
    """, dbname=scratch_db)
    (status, count), = _rows(service, scratch_db, """
        SELECT status, regression_count FROM tms_review.defects_v
         WHERE defect_id = 'b7369835-3b66-47d6-9262-c66fe5e78dd5'
    """)
    assert (status, count) == ("verified", "1")


@_needs_db()
def test_rerun_does_not_touch_post_cutover_rows(scratch_db):
    """A re-run must not fingerprint rows the live writer added after #156.

    Those rows carry NULL fingerprints (record_escaped_defect does not set
    one); stamping them would let two same-description rows collide on the
    unique index and abort the migration. The backfill is cutover-bounded.
    """
    service = os.environ[SERVICE_ENV]
    for _ in range(2):
        _psql(service, "-c", _insert_defect_sql(
            description="identical post-cutover description"),
            dbname=scratch_db)

    # second application must succeed (the fixture already applied it once)
    _psql(service, "-f", str(MIGRATION_008), dbname=scratch_db)

    (still_null,), = _rows(service, scratch_db, """
        SELECT count(*) FROM tms_review.escaped_defects
         WHERE fingerprint IS NULL
    """)
    assert still_null == "2", "re-run must leave post-cutover rows alone"


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


@_needs_db()
def test_preflight_rejects_colliding_pre_cutover_rows():
    """Two pre-cutover rows with the same prose hash equal, so the backfill
    cannot give both a manual fingerprint. The migration must fail closed with
    a diagnosis naming the rows — and must not half-apply.

    This is the assertion behind the header's collision caveat; without it the
    claim would be prose the code cannot falsify.
    """
    service = os.environ[SERVICE_ENV]
    db = f"tms_migtest_col_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _psql(service, "-c", f'CREATE DATABASE "{db}"')
    try:
        for path in sorted(MIGRATIONS.glob("00[1-7]-*.sql")):
            _psql(service, "-f", str(path), dbname=db)
        _psql(service, "-c", _seed_sql(), dbname=db)
        for _ in range(2):
            _psql(service, "-c", _insert_legacy_defect_sql(
                repo="tms",
                description="same prose, filed twice pre-cutover",
                created_at="2026-07-01T00:00:00+00:00",
            ), dbname=db)

        bad = _psql(service, "-f", str(MIGRATION_008), dbname=db, check=False)
        assert bad.returncode != 0
        assert "defect backfill would collide" in bad.stderr
        assert "tms" in bad.stderr, "diagnosis must name the colliding repo"

        # fail-closed: the BEGIN/COMMIT rolled back, nothing half-applied
        (cols,), = _rows(service, db, """
            SELECT count(*) FROM information_schema.columns
             WHERE table_schema = 'tms_review'
               AND table_name = 'escaped_defects'
               AND column_name = 'fingerprint'
        """)
        assert cols == "0"
    finally:
        _psql(service, "-c", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)',
              check=False)
