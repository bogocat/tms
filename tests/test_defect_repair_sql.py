"""The one-shot relabel repair (tms#157) — proven on a throwaway database.

`schema/repairs/2026-11-19-relabel-unproven-missing-distillation.sql` is run
against the **production** register by the operator, so it gets the same
treatment as a migration: it is executed here against a scratch database seeded
with the three shapes that matter, and it must touch exactly one of them.

    TMS_TEST_PG_SERVICE=pg-superuser pytest tests/test_defect_repair_sql.py -v
"""

import os
import uuid
from pathlib import Path

import pytest

from tests.test_defect_register_migration import (MIGRATIONS, SERVICE_ENV, _psql,
                                                  _rows)

REPAIR = (Path(__file__).resolve().parent.parent / "schema" / "repairs"
          / "2026-11-19-relabel-unproven-missing-distillation.sql")

SEED = """
    CREATE SCHEMA bogocat;
    CREATE TABLE bogocat.v_pipeline_health_book (
        item_id bigint, title text, passes_succeeded int,
        is_missing_distillation boolean);

    INSERT INTO bogocat.v_pipeline_health_book VALUES
        (163, 'The Old Man And The Sea', 0, true),   -- skipped, no source text
        (240, 'Necropolitics', 1, true),             -- partial, no distillation
        (152, 'Oathbringer', 6, true),               -- genuine silent case (#173)
        (999, 'Gained a distillation since', 3, false);

    INSERT INTO tms_review.escaped_defects (
        defect_id, repo, defect_class, severity, discovered_at,
        discovery_source, description, fingerprint, detector, last_seen_at,
        recurrence_count)
    VALUES
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector', 'OLD unproven claim',
         'pipeline-health:missing-distillation:163', 'pipeline-health',
         NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector', 'OLD unproven claim',
         'pipeline-health:missing-distillation:240', 'pipeline-health',
         NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector', 'all 6 passes OK, no row',
         'pipeline-health:missing-distillation:152', 'pipeline-health',
         NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector', 'OLD unproven claim, book fixed',
         'pipeline-health:missing-distillation:999', 'pipeline-health',
         NOW(), 2);
"""


@pytest.fixture()
def repair_db():
    service = os.environ.get(SERVICE_ENV)
    if not service:
        pytest.skip(f"set {SERVICE_ENV} to run the repair SQL")

    db = f"tms_repairtest_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _psql(service, "-c", f'CREATE DATABASE "{db}"')
    try:
        for path in sorted(MIGRATIONS.glob("0*.sql")):
            _psql(service, "-f", str(path), dbname=db)
        _psql(service, "-c", SEED, dbname=db)
        yield service, db
    finally:
        _psql(service, "-c",
              f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)', check=False)


def _fingerprints(service, db):
    return {row[0]: row for row in _rows(service, db, """
        SELECT fingerprint, recurrence_count, description
          FROM tms_review.escaped_defects
    """)}


def test_repair_relabels_the_unproven_rows_and_keeps_their_history(repair_db):
    service, db = repair_db

    _psql(service, "-f", str(REPAIR), dbname=db)

    rows = _fingerprints(service, db)
    assert set(rows) == {
        "pipeline-health:insufficient-source-text:163",
        "pipeline-health:insufficient-source-text:240",
        "pipeline-health:missing-distillation:152",
        "pipeline-health:missing-distillation:999",
    }, "the repair touched a row it should have left alone"

    label, recurrence, description = rows[
        "pipeline-health:insufficient-source-text:163"]
    assert recurrence == "2", "recurrence history must survive the relabel"
    assert "0 of 6 passes succeeded" in description
    assert "insufficient-source-text" in description
    for unsupported in ("reported no failure", "never produced"):
        assert unsupported not in description


def test_repair_leaves_the_genuine_silent_case_alone(repair_db):
    """`passes_succeeded = 6` with no distillation is the real #173 shape and
    must keep its claim."""
    service, db = repair_db

    _psql(service, "-f", str(REPAIR), dbname=db)

    (description,) = _rows(service, db, """
        SELECT description FROM tms_review.escaped_defects
         WHERE fingerprint = 'pipeline-health:missing-distillation:152'
    """)[0]
    assert description == "all 6 passes OK, no row"


def test_repair_is_a_no_op_the_second_time(repair_db):
    service, db = repair_db

    _psql(service, "-f", str(REPAIR), dbname=db)
    after_first = _fingerprints(service, db)
    _psql(service, "-f", str(REPAIR), dbname=db)

    assert _fingerprints(service, db) == after_first
