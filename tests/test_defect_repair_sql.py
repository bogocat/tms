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

RESTATE = (Path(__file__).resolve().parent.parent / "schema" / "repairs"
           / "2026-11-23-restate-distillery-silent-success-descriptions.sql")

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
    assert "no core-extract run ever succeeded" in description
    assert "insufficient-source-text" in description
    for unsupported in ("reported no failure", "never produced",
                        "every core-extract run"):
        assert unsupported not in description, unsupported


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


def test_the_repair_writes_the_producers_own_sentence(repair_db):
    """The repair's SQL literal and the producer's f-string must not drift: the
    whole point of the repair is that the live rows say exactly what a fresh
    sweep would say. Computed here from the producer, compared to the row."""
    from tms.producers import pipeline_health

    service, db = repair_db
    _psql(service, "-f", str(REPAIR), dbname=db)

    (description,) = _rows(service, db, """
        SELECT description FROM tms_review.escaped_defects
         WHERE fingerprint = 'pipeline-health:insufficient-source-text:163'
    """)[0]

    (finding,) = pipeline_health.find_findings(
        [{"item_id": 163, "title": "The Old Man And The Sea",
          "passes_succeeded": 0, "is_missing_distillation": 1,
          "is_missing_junctions": 0, "topics_count": 0,
          "traditions_count": 0, "quotes_count": 0,
          "near_verbatim_rate": None, "stale_running_count": 0}],
        [{"item_id": 163, "skipped_no_source": 1,
          "core_extract_failed": 0, "core_extract_succeeded": 0}],
    )
    assert finding.description == description


# ── the second one-shot repair: producer #2's over-claiming sentences ──

RESTATE_SEED = """
    INSERT INTO tms_review.escaped_defects (
        defect_id, repo, defect_class, severity, discovered_at,
        discovery_source, description, fingerprint, detector, last_seen_at,
        recurrence_count)
    VALUES
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'core-extract recorded status=''success'' in 2 runs for item 63 (last ended 2026-05-25 23:48:36+00:00) and chapter_summaries and best_quotes are both empty — the pass extracted nothing',
         'distillery-silent-success:empty-extraction:63',
         'distillery-silent-success', NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'core-extract recorded status=''success'' in 2 runs for item 258 (last ended 2026-05-26 01:06:36+00:00) and chapter_summaries and best_quotes are both empty — the pass extracted nothing',
         'distillery-silent-success:empty-extraction:258',
         'distillery-silent-success', NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'canonical-map reported status=success and wrote no canonical_topics/canonical_traditions (2 runs, last 2026-05-25)',
         'distillery-silent-success:empty-mapping:63',
         'distillery-silent-success', NOW(), 4),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'item 7 (pass review-synthesis): review_html is blank — no review was written',
         'distillery-silent-success:empty-review:7',
         'distillery-silent-success', NOW(), 1),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'an unrelated detector row that happens to say wrote no canonical_topics',
         'pipeline-health:junction-drift:152', 'pipeline-health', NOW(), 1);
"""


@pytest.fixture()
def restate_db():
    service = os.environ.get(SERVICE_ENV)
    if not service:
        pytest.skip(f"set {SERVICE_ENV} to run the repair SQL")

    db = f"tms_restatetest_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _psql(service, "-c", f'CREATE DATABASE "{db}"')
    try:
        for path in sorted(MIGRATIONS.glob("0*.sql")):
            _psql(service, "-f", str(path), dbname=db)
        _psql(service, "-c", RESTATE_SEED, dbname=db)
        yield service, db
    finally:
        _psql(service, "-c",
              f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)', check=False)


def test_restate_repair_rewrites_only_the_over_claiming_rows(restate_db):
    service, db = restate_db

    _psql(service, "-f", str(RESTATE), dbname=db)

    rows = {row[0]: row for row in _rows(service, db, """
        SELECT fingerprint, recurrence_count, description
          FROM tms_review.escaped_defects
    """)}

    extraction = rows["distillery-silent-success:empty-extraction:63"]
    assert extraction[1] == "2", "recurrence history must survive the restate"
    assert "chapter_summaries and best_quotes are both empty" in extraction[2]
    assert "no per-chapter summaries and no quotes were extracted" in extraction[2]
    assert extraction[2].startswith("item 63 (pass core-extract): ")
    for unsupported in ("extracted nothing", "wrote nothing", "last ended"):
        assert unsupported not in extraction[2], unsupported

    mapping = rows["distillery-silent-success:empty-mapping:63"]
    assert "no topics or traditions were mapped" in mapping[2]
    assert "wrote no canonical_topics" not in mapping[2]

    assert rows["distillery-silent-success:empty-review:7"][2] == (
        "item 7 (pass review-synthesis): review_html is blank — "
        "no review was written"
    ), "an already-correct row must not be rewritten"

    assert rows["pipeline-health:junction-drift:152"][2] == (
        "an unrelated detector row that happens to say wrote no canonical_topics"
    ), "another detector's row must not be touched"


def test_restate_repair_is_a_no_op_the_second_time(restate_db):
    service, db = restate_db

    _psql(service, "-f", str(RESTATE), dbname=db)
    after_first = _rows(service, db, """
        SELECT fingerprint, description FROM tms_review.escaped_defects
         ORDER BY fingerprint
    """)
    _psql(service, "-f", str(RESTATE), dbname=db)

    assert _rows(service, db, """
        SELECT fingerprint, description FROM tms_review.escaped_defects
         ORDER BY fingerprint
    """) == after_first


def test_the_preflight_refuses_to_run_after_the_corrected_producer_swept(
        repair_db):
    """The ordering rule is enforced, not just documented: if the corrected
    producer already filed `insufficient-source-text:163`, the relabel of
    `missing-distillation:163` would die on the unique index. The pre-flight must
    fail *first*, with the reason."""
    import subprocess

    service, db = repair_db
    _psql(service, "-c", """
        INSERT INTO tms_review.escaped_defects (
            defect_id, repo, defect_class, severity, discovered_at,
            discovery_source, description, fingerprint, detector, last_seen_at,
            recurrence_count)
        VALUES (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
                '2026-11-19T00:00:00+00:00', 'detector', 'already swept',
                'pipeline-health:insufficient-source-text:163',
                'pipeline-health', NOW(), 1)
    """, dbname=db)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _psql(service, "-f", str(REPAIR), dbname=db)

    assert "already swept" in excinfo.value.stderr
    assert "collide on (repo, fingerprint)" in excinfo.value.stderr
