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
        (999, 'Gained a distillation since', 3, false),
        (555, 'Retried and core-extract worked', 3, true);  -- success since filing

    -- The ledger: the sentence repair #1 writes asserts two facts about these
    -- rows, so the repair only fires where they hold (see the WHERE clause).
    CREATE TABLE bogocat.media_digest_runs (
        item_id bigint, pass_id text, status text, error_detail text,
        pipeline_version int);

    INSERT INTO bogocat.media_digest_runs VALUES
        (163, 'core-extract', 'skipped',
         'insufficient-source-text: no epub body nor OCR sidecar found', 2),
        (240, 'core-extract', 'skipped',
         'insufficient-source-text: no epub body nor OCR sidecar found', 2),
        (240, 'core-extract', 'failed', '{"error": "exit 1"}', 2),
        (152, 'core-extract', 'success', NULL, 2),
        (999, 'core-extract', 'success', NULL, 2),
        (555, 'core-extract', 'success', NULL, 2);

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
         NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector', 'OLD unproven claim, retried',
         'pipeline-health:missing-distillation:555', 'pipeline-health',
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
        "pipeline-health:missing-distillation:555",
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


def test_repair_leaves_a_missing_distillation_row_alone_if_extract_later_worked(
        repair_db):
    """The sentence repair #1 writes asserts `no core-extract run ever
    succeeded`. Books are retried, so the UPDATE is guarded on that ledger fact:
    a success between review and apply must leave the old row for a human, not
    write a sentence the ledger already contradicts (and not make the next sweep
    file a different signal for the same book)."""
    service, db = repair_db

    _psql(service, "-f", str(REPAIR), dbname=db)

    (description,) = _rows(service, db, """
        SELECT description FROM tms_review.escaped_defects
         WHERE fingerprint = 'pipeline-health:missing-distillation:555'
    """)[0]
    assert description == "OLD unproven claim, retried"


# ── the third one-shot repair: producer #1's junction-drift claim ─────

JUNCTION = (Path(__file__).resolve().parent.parent / "schema" / "repairs"
            / "2026-11-27-restate-junction-drift-claims.sql")

JUNCTION_SEED = """
    CREATE SCHEMA IF NOT EXISTS bogocat;
    CREATE TABLE bogocat.v_pipeline_health_book (
        item_id bigint, title text, passes_succeeded int,
        is_missing_distillation boolean, is_missing_junctions boolean,
        topics_count int, traditions_count int, quotes_count int);

    INSERT INTO bogocat.v_pipeline_health_book VALUES
        (152, 'Oathbringer', 6, false, true, 0, 4, 0),
        (178, 'The Holy Bible', 6, false, true, 39, 0, 16),
        (200, 'Healed since filing', 6, false, false, 6, 2, 3),
        (300, 'The Sunlit Man', 6, false, true, 0, 4, 0);

    INSERT INTO tms_review.escaped_defects (
        defect_id, repo, defect_class, severity, discovered_at,
        discovery_source, description, fingerprint, detector, last_seen_at,
        recurrence_count)
    VALUES
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'book 152 (Oathbringer) has a distillation but its junctions are incomplete (topics=0, traditions=4, quotes=0) — a pass recorded success without writing them',
         'pipeline-health:junction-drift:152', 'pipeline-health', NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'book 178 (The Holy Bible) has a distillation but its junctions are incomplete (topics=39, traditions=0, quotes=16) — a pass recorded success without writing them',
         'pipeline-health:junction-drift:178', 'pipeline-health', NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'book 200 (Healed since filing) has a distillation but its junctions are incomplete (topics=0, traditions=2, quotes=0) — a pass recorded success without writing them',
         'pipeline-health:junction-drift:200', 'pipeline-health', NOW(), 2),
        (gen_random_uuid(), 'distillery', 'silent-failure', 'ungraded',
         '2026-11-19T00:00:00+00:00', 'detector',
         'book 300 (The Sunlit Man) has a distillation but its junction rows are incomplete (topics=0, traditions=4, quotes=0). The view proves the junctions are incomplete; it does not say which step dropped them (the canonical_map projection, #171, owns that).',
         'pipeline-health:junction-drift:300', 'pipeline-health', NOW(), 1);
"""


@pytest.fixture()
def junction_db():
    service = os.environ.get(SERVICE_ENV)
    if not service:
        pytest.skip(f"set {SERVICE_ENV} to run the repair SQL")

    db = f"tms_junctiontest_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    _psql(service, "-c", f'CREATE DATABASE "{db}"')
    try:
        for path in sorted(MIGRATIONS.glob("0*.sql")):
            _psql(service, "-f", str(path), dbname=db)
        _psql(service, "-c", JUNCTION_SEED, dbname=db)
        yield service, db
    finally:
        _psql(service, "-c",
              f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)', check=False)


def _junction_rows(service, db):
    return {row[0]: row for row in _rows(service, db, """
        SELECT fingerprint, defect_id, recurrence_count, description
          FROM tms_review.escaped_defects
         WHERE fingerprint LIKE 'pipeline-health:junction-drift:%'
    """)}


def test_junction_repair_restates_only_the_proven_half(junction_db):
    service, db = junction_db
    before = _junction_rows(service, db)

    _psql(service, "-f", str(JUNCTION), dbname=db)

    rows = _junction_rows(service, db)
    for item, traditions in ((152, 4), (178, 0)):
        fingerprint = f"pipeline-health:junction-drift:{item}"
        _, defect_id, recurrence, description = rows[fingerprint]
        assert defect_id == before[fingerprint][1], "defect_id must not change"
        assert recurrence == "2", "recurrence history must survive the restate"
        assert "a pass recorded success without writing them" not in description
        assert "The view proves the junctions are incomplete" in description
        assert "#171" in description
        assert f"traditions={traditions}, " in description, (
            "the counts must come from the view, not the old sentence"
        )


def test_junction_repair_writes_the_producers_own_sentence(junction_db):
    """The repair's SQL literal and the producer's f-string cannot drift: the
    point of the repair is that the live rows say what a fresh sweep says."""
    from tms.producers import pipeline_health

    service, db = junction_db
    _psql(service, "-f", str(JUNCTION), dbname=db)

    (description,) = _rows(service, db, """
        SELECT description FROM tms_review.escaped_defects
         WHERE fingerprint = 'pipeline-health:junction-drift:178'
    """)[0]

    (finding,) = pipeline_health.find_findings(
        [{"item_id": 178, "title": "The Holy Bible",
          "passes_succeeded": 6, "is_missing_distillation": 0,
          "is_missing_junctions": 1, "topics_count": 39,
          "traditions_count": 0, "quotes_count": 16,
          "near_verbatim_rate": None, "stale_running_count": 0}],
        [],
    )
    assert finding.description == description


def test_junction_repair_leaves_healed_books_and_corrected_rows_alone(
        junction_db):
    service, db = junction_db
    before = _junction_rows(service, db)

    _psql(service, "-f", str(JUNCTION), dbname=db)

    rows = _junction_rows(service, db)
    assert rows["pipeline-health:junction-drift:200"] == \
        before["pipeline-health:junction-drift:200"], (
        "a book whose junctions healed must keep its old row for a human, not be "
        "given a sentence the view now contradicts"
    )
    assert rows["pipeline-health:junction-drift:300"] == \
        before["pipeline-health:junction-drift:300"], (
        "a row the corrected producer already filed must not be rewritten"
    )


def test_junction_repair_is_a_no_op_the_second_time(junction_db):
    service, db = junction_db

    _psql(service, "-f", str(JUNCTION), dbname=db)
    after_first = _junction_rows(service, db)
    _psql(service, "-f", str(JUNCTION), dbname=db)

    assert _junction_rows(service, db) == after_first
