"""Defect intake — ``record_defect()`` dedupe semantics (tms#157).

The register (#156) is only a register if a second sighting of the same bug
lands on the same row. These tests pin the three branches of the intake
contract and the fingerprint convention that makes a row sweepable later
(#159 selects on ``detector IS NOT NULL``).

Database isolation: the shared ``test_db`` fixture from ``conftest.py``
patches the connection to a sqlite3 in-memory database carrying the 008/009
``escaped_defects`` shape (partial unique index + the four status-arm CHECKs).
"""

import pytest

from tms.defects import (
    DEFECT_CLASSES,
    DISCOVERY_SOURCES,
    INSERTED,
    RECURRED,
    REGRESSED,
    DefectRecord,
    record_defect,
    validate_fingerprint,
)

FP = "pipeline-health:junction-drift:152"


def _fetch(test_db, sql, params=None):
    cur = test_db().cursor()
    if params is None:
        return cur.execute(sql).fetchall()
    return cur.execute(sql, params).fetchall()


def _file(test_db, **overrides):
    """record_defect() with the producer defaults, overridable per test."""
    kwargs = dict(
        repo="home-portal",
        defect_class="silent-failure",
        fingerprint=FP,
        description="Oathbringer has no book_topics/book_traditions rows",
        detector="pipeline-health",
    )
    kwargs.update(overrides)
    return record_defect(**kwargs)


def _seed_verified_row(test_db, fingerprint=FP, repo="home-portal"):
    """A row that has been through the fix loop: verified + merged + detector.

    Set up by direct INSERT because intake has no way to write merged_at /
    verified_at — that is #158/#159's write path, deliberately not ours.
    """
    test_db().cursor().execute(
        """INSERT INTO escaped_defects (
               defect_id, repo, introducing_pr, introducing_commit,
               defect_class, severity, discovered_at, discovery_source,
               description, fingerprint, detector, last_seen_at,
               merged_at, verified_at)
           VALUES (?, ?, NULL, NULL, 'silent-failure', 'ungraded',
                   '2026-11-01T00:00:00+00:00', 'detector',
                   'first sight', ?, 'pipeline-health',
                   '2026-11-01T00:00:00+00:00',
                   '2026-11-10T00:00:00+00:00',
                   '2026-11-11T00:00:00+00:00')""",
        ("11111111-1111-1111-1111-111111111111", repo, fingerprint),
    )
    test_db().commit()


# ── fingerprint convention ────────────────────────────────────────


def test_fingerprint_must_be_prefixed_with_its_detector():
    """A producer-supplied fingerprint that is not prefixed with its own
    detector id is rejected, not silently accepted: #159 sweeps on
    ``detector IS NOT NULL`` and cannot re-check a row whose identity does
    not name the detector that produced it."""
    with pytest.raises(ValueError, match="prefixed"):
        validate_fingerprint("junction-drift:152", detector="pipeline-health")


def test_detector_prefixed_fingerprint_is_accepted():
    validate_fingerprint(FP, detector="pipeline-health")


def test_fingerprint_without_a_detector_still_needs_a_key():
    validate_fingerprint("manual:" + "a" * 64)
    with pytest.raises(ValueError, match="segment"):
        validate_fingerprint("single", detector=None)


def test_fingerprint_rejects_whitespace_and_an_empty_key():
    with pytest.raises(ValueError, match="whitespace"):
        validate_fingerprint("pipeline-health:junction-drift: 152", detector=None)
    with pytest.raises(ValueError, match="segment"):
        validate_fingerprint("pipeline-health:", detector="pipeline-health")


def test_detector_must_be_a_slug():
    with pytest.raises(ValueError, match="detector"):
        validate_fingerprint("bad detector:x:1", detector="bad detector")


def test_detector_shaped_fingerprint_without_a_detector_is_rejected():
    """A three-segment fingerprint is detector-derived by convention. Filing one
    without its ``detector`` leaves a row #159 can never re-check — this exact
    mistake shipped a real CLI call on 2026-11-19 and landed an unsweepable row,
    because the prefix rule could only fire when a detector was supplied."""
    with pytest.raises(ValueError, match="detector"):
        validate_fingerprint("distillery-silent-success:empty-mapping:63")

    # the hand-filed form is unaffected: two segments, 'manual:<hash>'
    validate_fingerprint("manual:" + "d" * 64)


def test_detector_source_requires_a_detector(test_db):
    with pytest.raises(ValueError, match="detector"):
        _file(test_db, detector=None, discovery_source="detector")


def test_detector_rows_default_to_the_detector_source(test_db):
    rec = _file(test_db)
    row = _fetch(
        test_db,
        "SELECT discovery_source FROM escaped_defects WHERE defect_id = ?",
        (rec.defect_id,),
    )
    assert row == [("detector",)]


def test_manual_rows_default_to_the_manual_source(test_db):
    rec = record_defect(
        repo="tms", defect_class="convention",
        fingerprint="manual:" + "b" * 64,
        description="hand-filed",
    )
    row = _fetch(
        test_db,
        "SELECT discovery_source, detector FROM escaped_defects "
        "WHERE defect_id = ?",
        (rec.defect_id,),
    )
    assert row == [("manual", None)]


def test_unknown_discovery_source_is_rejected(test_db):
    with pytest.raises(ValueError, match="discovery_source"):
        _file(test_db, discovery_source="vibes")
    assert "detector" in DISCOVERY_SOURCES


def test_unknown_defect_class_is_rejected(test_db):
    with pytest.raises(ValueError, match="defect_class"):
        _file(test_db, defect_class="not-a-class")
    assert "silent-failure" in DEFECT_CLASSES


# ── AC1: twice = one row, recurrence 2 ───────────────────────────


def test_same_fingerprint_twice_is_one_row_with_recurrence_two(test_db):
    first = _file(test_db)
    assert first.outcome == INSERTED
    assert first.recurrence_count == 1
    assert first.regression_count == 0

    second = _file(test_db)
    assert second.outcome == RECURRED, (
        "a recurrence must not report as a fresh insert — that is the "
        "ambiguity this register exists to remove"
    )
    assert second.recurrence_count == 2
    assert second.defect_id == first.defect_id

    rows = _fetch(
        test_db,
        "SELECT count(*), max(recurrence_count), max(last_seen_at) "
        "FROM escaped_defects",
    )
    assert rows[0][0] == 1
    assert rows[0][1] == 2
    assert rows[0][2] is not None


def test_a_different_fingerprint_is_a_different_row(test_db):
    _file(test_db)
    _file(test_db, fingerprint="pipeline-health:quote-decay:152")
    assert _fetch(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 2


def test_legacy_row_with_recurrence_count_one_is_not_a_fresh_insert(test_db):
    """The four July rows carry ``recurrence_count = 1``. A bump on such a row
    is a recurrence — it must not be reported as ``inserted`` just because the
    counter reads 1 (before or after the bump). The outcome states what *this
    call* did, derived from this call's own uuid, not from counter arithmetic."""
    test_db().cursor().execute(
        """INSERT INTO escaped_defects (
               defect_id, repo, introducing_pr, introducing_commit,
               defect_class, severity, discovered_at, discovery_source,
               description, fingerprint, detector, last_seen_at,
               recurrence_count)
           VALUES ('33333333-3333-3333-3333-333333333333', 'home-portal',
                   NULL, NULL, 'silent-failure', 'ungraded',
                   '2026-07-13T00:00:00+00:00', 'detector',
                   'legacy row', ?, 'pipeline-health',
                   '2026-07-13T00:00:00+00:00', 1)""",
        (FP,),
    )
    test_db().commit()

    rec = _file(test_db)

    assert rec.outcome == RECURRED, (
        "a legacy row with recurrence_count=1 was reported as a fresh insert"
    )
    assert rec.recurrence_count == 2
    assert rec.defect_id == "33333333-3333-3333-3333-333333333333"
    assert _fetch(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 1


def test_outcome_does_not_depend_on_the_counter_value(test_db):
    """Same row, arbitrary starting counter: still a recurrence, and the
    counter is bumped from whatever it really was."""
    test_db().cursor().execute(
        """INSERT INTO escaped_defects (
               defect_id, repo, defect_class, severity, discovered_at,
               discovery_source, description, fingerprint, detector,
               last_seen_at, recurrence_count)
           VALUES ('44444444-4444-4444-4444-444444444444', 'home-portal',
                   'silent-failure', 'ungraded', '2026-07-13T00:00:00+00:00',
                   'detector', 'legacy row', ?, 'pipeline-health',
                   '2026-07-13T00:00:00+00:00', 9)""",
        (FP,),
    )
    test_db().commit()

    rec = _file(test_db)

    assert rec.outcome == RECURRED
    assert rec.recurrence_count == 10


def test_first_sight_description_and_severity_are_not_overwritten(test_db):
    _file(test_db, description="first sight", severity="ungraded")
    _file(test_db, description="second sight wording", severity="critical")
    rows = _fetch(
        test_db, "SELECT description, severity FROM escaped_defects",
    )
    assert rows == [("first sight", "ungraded")]


# ── AC2: a verified row regresses, in place ─────────────────────


def test_verified_row_regresses_without_a_second_row(test_db):
    _seed_verified_row(test_db)

    rec = _file(test_db)

    assert rec.outcome == REGRESSED
    assert rec.regression_count == 1
    assert rec.defect_id == "11111111-1111-1111-1111-111111111111"

    rows = _fetch(
        test_db,
        "SELECT count(*), max(regressed_at), max(regression_count), "
        "max(recurrence_count) FROM escaped_defects",
    )
    assert rows[0][0] == 1, "a regression must not insert a second row"
    assert rows[0][1] is not None
    assert rows[0][2] == 1
    assert rows[0][3] == 2


def test_an_already_regressed_row_only_bumps_recurrence(test_db):
    """Still-broken after a regression is a recurrence, not a second
    regression — regression_count counts distinct regressions."""
    _seed_verified_row(test_db)
    assert _file(test_db).outcome == REGRESSED

    again = _file(test_db)

    assert again.outcome == RECURRED
    assert again.regression_count == 1
    assert again.recurrence_count == 3


def test_a_merged_but_unverified_row_does_not_regress(test_db):
    """`fixed` is not `verified`: only a row that a detector passed through
    can regress. merged_at alone is not evidence the detector ever ran."""
    test_db().cursor().execute(
        """INSERT INTO escaped_defects (
               defect_id, repo, introducing_pr, introducing_commit,
               defect_class, severity, discovered_at, discovery_source,
               description, fingerprint, detector, last_seen_at, merged_at)
           VALUES ('22222222-2222-2222-2222-222222222222', 'home-portal',
                   NULL, NULL, 'silent-failure', 'ungraded',
                   '2026-11-01T00:00:00+00:00', 'detector', 'first sight',
                   ?, 'pipeline-health', '2026-11-01T00:00:00+00:00',
                   '2026-11-10T00:00:00+00:00')""",
        (FP,),
    )
    test_db().commit()

    rec = _file(test_db)

    assert rec.outcome == RECURRED
    assert rec.regression_count == 0
    rows = _fetch(test_db, "SELECT regressed_at FROM escaped_defects")
    assert rows == [(None,)]


# ── the return value ─────────────────────────────────────────────


def test_result_is_a_record_not_a_bare_id(test_db):
    rec = _file(test_db)

    assert isinstance(rec, DefectRecord)
    assert rec.repo == "home-portal"
    assert rec.fingerprint == FP
    assert rec.detector == "pipeline-health"
    # str() must name the outcome, so a log line cannot read as "recorded"
    assert INSERTED in str(rec)
    assert FP in str(rec)
    assert rec.as_dict()["outcome"] == INSERTED


def test_detector_rows_carry_no_invented_introducing_pr(test_db):
    """009 dropped NOT NULL on introducing_pr/commit: a detector-sourced
    defect has no introducing PR, and 0/'' would be a fabricated fact."""
    rec = _file(test_db)
    rows = _fetch(
        test_db,
        "SELECT introducing_pr, introducing_commit FROM escaped_defects "
        "WHERE defect_id = ?",
        (rec.defect_id,),
    )
    assert rows == [(None, None)]


def test_introducing_pr_is_written_when_known(test_db):
    rec = _file(test_db, introducing_pr=102, introducing_commit="deadbeef")
    rows = _fetch(
        test_db,
        "SELECT introducing_pr, introducing_commit FROM escaped_defects "
        "WHERE defect_id = ?",
        (rec.defect_id,),
    )
    assert rows == [(102, "deadbeef")]
