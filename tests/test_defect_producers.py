"""Producer tests (tms#157) — the first two detectors that feed the register.

Both producers are split into a portable SQL fetch and a pure-Python rule set, so
the whole decision surface is exercised here against the shared sqlite shim
(``conftest.test_db``) plus two shim tables standing in for the postgres
view/ledger. The real postgres fetch is proven separately by running the
producers against the live database.

The point of these tests is AC4: every row a producer files must have a non-null
``detector`` and a fingerprint prefixed with that detector id, because #159
sweeps on ``detector IS NOT NULL``.
"""

import pytest

from tms.producers import distillery_silent_success, pipeline_health

BOOK_VIEW = """
CREATE TABLE IF NOT EXISTS v_pipeline_health_book (
    item_id                 INTEGER,
    title                   TEXT,
    is_missing_distillation INTEGER,
    is_missing_junctions    INTEGER,
    topics_count            INTEGER,
    traditions_count        INTEGER,
    quotes_count            INTEGER,
    near_verbatim_rate      REAL,
    stale_running_count     INTEGER
)
"""

RUN_LEDGER = """
CREATE TABLE IF NOT EXISTS media_digest_runs (
    id          INTEGER,
    item_id     INTEGER,
    pass_id     TEXT,
    status      TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    artifact    TEXT
)
"""


@pytest.fixture
def producers_db(test_db):
    """The shared register shim, plus the two upstream tables as sqlite names."""
    conn = test_db()
    cur = conn.cursor()
    cur.execute(BOOK_VIEW)
    cur.execute(RUN_LEDGER)
    conn.commit()
    return conn


def _exec(conn, sql, params=None):
    cur = conn.cursor()
    return cur.execute(sql) if params is None else cur.execute(sql, params)


def _book(conn, item_id, title="A Book", **overrides):
    row = {
        "is_missing_distillation": 0, "is_missing_junctions": 0,
        "topics_count": 4, "traditions_count": 2, "quotes_count": 9,
        "near_verbatim_rate": 0.9, "stale_running_count": 0,
    }
    row.update(overrides)
    _exec(
        conn,
        "INSERT INTO v_pipeline_health_book (item_id, title, "
        "is_missing_distillation, is_missing_junctions, topics_count, "
        "traditions_count, quotes_count, near_verbatim_rate, "
        "stale_running_count) VALUES (?,?,?,?,?,?,?,?,?)",
        (item_id, title, row["is_missing_distillation"],
         row["is_missing_junctions"], row["topics_count"],
         row["traditions_count"], row["quotes_count"],
         row["near_verbatim_rate"], row["stale_running_count"]),
    )
    conn.commit()


def _run(conn, run_id, item_id, pass_id, artifact, status="success",
         started_at="2026-05-25T00:00:00+00:00",
         ended_at="2026-05-25T00:10:00+00:00"):
    _exec(
        conn,
        "INSERT INTO media_digest_runs (id, item_id, pass_id, status, "
        "started_at, ended_at, artifact) VALUES (?,?,?,?,?,?,?)",
        (run_id, item_id, pass_id, status, started_at, ended_at, artifact),
    )
    conn.commit()


def _register(conn):
    return _exec(
        conn,
        "SELECT repo, fingerprint, detector, discovery_source, severity, "
        "recurrence_count, description FROM escaped_defects ORDER BY fingerprint",
    ).fetchall()


# ── producer #1: pipeline-health ─────────────────────────────────


def test_pipeline_health_files_one_row_per_signal_class(producers_db):
    _book(producers_db, 152, "Oathbringer", is_missing_distillation=1,
          is_missing_junctions=1, near_verbatim_rate=0.41,
          stale_running_count=3)
    _book(producers_db, 900, "Healthy Book")

    pipeline_health.sweep()

    rows = _register(producers_db)
    assert [r[1] for r in rows] == [
        "pipeline-health:junction-drift:152",
        "pipeline-health:missing-distillation:152",
        "pipeline-health:quote-decay:152",
        "pipeline-health:stale-run:152",
    ], "one row per silent-failure signal class, nothing for the healthy book"


def test_pipeline_health_rows_carry_detector_and_detector_prefix(producers_db):
    _book(producers_db, 152, is_missing_junctions=1)

    pipeline_health.sweep()

    for repo, fingerprint, detector, source, severity, _, _ in _register(
            producers_db):
        assert detector == "pipeline-health"
        assert fingerprint.startswith(detector + ":")
        assert source == "detector"
        assert severity == "ungraded"
        assert repo == "distillery"


def test_pipeline_health_second_sweep_bumps_recurrence_not_rows(producers_db):
    _book(producers_db, 152, is_missing_junctions=1)

    pipeline_health.sweep()
    first = _register(producers_db)
    pipeline_health.sweep()
    second = _register(producers_db)

    assert len(second) == len(first) == 1
    assert first[0][5] == 1
    assert second[0][5] == 2, "a second sweep is a recurrence, not a new defect"


def test_pipeline_health_dry_run_writes_nothing(producers_db, capsys):
    _book(producers_db, 152, is_missing_junctions=1)

    pipeline_health.sweep(dry_run=True)

    assert _register(producers_db) == []
    out = capsys.readouterr().out
    assert "would file" in out
    assert "pipeline-health:junction-drift:152" in out


def test_quote_decay_follows_the_page_threshold(producers_db):
    """0.65 is the floor home-portal's page uses (`< 0.65` counts as decay)."""
    _book(producers_db, 1, near_verbatim_rate=0.64)
    _book(producers_db, 2, near_verbatim_rate=0.65)
    _book(producers_db, 3, near_verbatim_rate=None)

    pipeline_health.sweep()

    assert [r[1] for r in _register(producers_db)] == [
        "pipeline-health:quote-decay:1"
    ]


def test_pipeline_health_repo_is_overridable(producers_db):
    _book(producers_db, 152, is_missing_junctions=1)

    pipeline_health.sweep(repo="home-portal")

    assert _register(producers_db)[0][0] == "home-portal"


def test_pipeline_health_healthy_pipeline_files_nothing(producers_db):
    _book(producers_db, 1)
    _book(producers_db, 2)

    pipeline_health.sweep()

    assert _register(producers_db) == []


# ── producer #2: distillery silent success ───────────────────────


def test_distillery_files_a_success_run_with_an_empty_payload(producers_db):
    _run(producers_db, 6113, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}')

    distillery_silent_success.sweep()

    rows = _register(producers_db)
    assert len(rows) == 1
    repo, fingerprint, detector, source, _, _, description = rows[0]
    assert repo == "distillery"
    assert fingerprint == (
        "distillery-silent-success:empty-mapping:63")
    assert detector == "distillery-silent-success"
    assert source == "detector"
    assert "1 run" in description


def test_distillery_groups_repeated_runs_of_the_same_bug(producers_db):
    """Two silent successes on the same item/pass are one bug, not two rows."""
    for run_id, ended in ((6113, "2026-05-25T23:46:32+00:00"),
                          (6119, "2026-05-25T23:49:51+00:00")):
        _run(producers_db, run_id, 63, "canonical-map",
             '{"canonical_topics": [], "canonical_traditions": []}',
             ended_at=ended)

    distillery_silent_success.sweep()

    rows = _register(producers_db)
    assert len(rows) == 1
    assert "2 runs" in rows[0][6]


def test_distillery_ignores_failed_runs(producers_db):
    """A run that recorded `failed` is not a silent success — it said so."""
    _run(producers_db, 1, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}',
         status="failed")

    distillery_silent_success.sweep()

    assert _register(producers_db) == []


def test_distillery_ignores_successful_runs_that_wrote_something(producers_db):
    _run(producers_db, 1, 63, "canonical-map",
         '{"canonical_topics": ["stoicism"], "canonical_traditions": []}')
    _run(producers_db, 2, 64, "core-extract",
         '{"chapter_summaries": [], "best_quotes": ["a quote"]}')
    _run(producers_db, 3, 65, "review-synthesis",
         '{"review_html": "<p>a real review</p>"}')

    distillery_silent_success.sweep()

    assert _register(producers_db) == []


def test_distillery_covers_each_registered_pass(producers_db):
    _run(producers_db, 1, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}')
    _run(producers_db, 2, 258, "core-extract",
         '{"chapter_summaries": [], "best_quotes": []}')
    _run(producers_db, 3, 7, "review-synthesis", '{"review_html": "   "}')

    distillery_silent_success.sweep()

    assert [r[1] for r in _register(producers_db)] == [
        "distillery-silent-success:empty-extraction:258",
        "distillery-silent-success:empty-mapping:63",
        "distillery-silent-success:empty-review:7",
    ]


def test_distillery_ignores_unregistered_passes(producers_db):
    _run(producers_db, 1, 63, "genre-sniff", '{"genre_profile": ""}')

    distillery_silent_success.sweep()

    assert _register(producers_db) == []


def test_distillery_ignores_payloads_of_a_different_shape(producers_db):
    """`core-extract` is also used by the pipeline_version=3 film/music shapes,
    whose artifacts have `scene_summaries`/`best_lines`/`track_breakdowns` and
    no `chapter_summaries` at all. An absent field is not an empty field: read
    that way, every v3 run looked like 'wrote nothing' (75 false findings on the
    live ledger) — a claim the payload does not support."""
    _run(producers_db, 1, 14965, "core-extract",
         '{"themes": [], "best_lines": ["a line"], '
         '"track_breakdowns": [{"track": 1}], "high_level_summary": "x"}')
    _run(producers_db, 2, 7852, "core-extract",
         '{"best_quotes": [], "scene_summaries": ["s"], '
         '"high_level_summary": "x"}')

    distillery_silent_success.sweep()

    assert _register(producers_db) == []


def test_dry_run_summary_counts_what_it_listed(producers_db, capsys):
    """A dry run that lists 2 findings and then says "would file 0 finding(s)"
    is the silent-success pattern this whole issue is about."""
    _book(producers_db, 152, is_missing_distillation=1, is_missing_junctions=1)

    assert pipeline_health.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    listed = [line for line in out.splitlines() if line.startswith("would file ")]
    assert len(listed) == 2
    assert "would file 2 finding(s)" in out


def test_distillery_dry_run_writes_nothing(producers_db, capsys):
    _run(producers_db, 1, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}')

    distillery_silent_success.sweep(dry_run=True)

    assert _register(producers_db) == []
    assert "distillery-silent-success:empty-mapping:63" in capsys.readouterr().out


def test_distillery_window_excludes_old_runs(producers_db):
    _run(producers_db, 1, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}',
         started_at="2026-05-25T00:00:00+00:00")

    distillery_silent_success.sweep(since_days=7, now="2026-11-19T00:00:00+00:00")

    assert _register(producers_db) == []


def test_distillery_default_is_the_whole_ledger(producers_db):
    """No window by default: an old silent success is still a silent success —
    a bounded window would hide exactly the class of defect this looks for."""
    _run(producers_db, 1, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}',
         started_at="2026-05-25T00:00:00+00:00")

    distillery_silent_success.sweep(now="2026-11-19T00:00:00+00:00")

    assert len(_register(producers_db)) == 1


# ── AC4, across both producers ───────────────────────────────────


def test_every_row_a_producer_files_is_sweepable(producers_db):
    _book(producers_db, 152, is_missing_distillation=1,
          is_missing_junctions=1, near_verbatim_rate=0.2,
          stale_running_count=1)
    _run(producers_db, 1, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}')
    _run(producers_db, 2, 258, "core-extract",
         '{"chapter_summaries": [], "best_quotes": []}')

    pipeline_health.sweep()
    distillery_silent_success.sweep()

    rows = _exec(
        producers_db,
        "SELECT detector, fingerprint, discovery_source FROM escaped_defects",
    ).fetchall()
    assert len(rows) == 6
    for detector, fingerprint, source in rows:
        assert detector is not None, "a producer row with no detector can never be swept"
        assert fingerprint.startswith(detector + ":")
        assert source == "detector"
