"""Producer tests (tms#157) — the first two detectors that feed the register.

Both producers are split into a portable SQL fetch and a pure-Python rule set, so
the whole decision surface is exercised here against the shared sqlite shim
(``conftest.test_db``) plus two shim tables standing in for the postgres
view/ledger. The real postgres fetch is proven separately by running the
producers against the live database.

Two things this file exists to prevent, both found in review:

* a producer filing a claim its source data contradicts — "the pipeline silently
  failed" when the ledger records *why* (``insufficient-source-text`` skips,
  failed ``core-extract`` runs). ``missing-distillation`` therefore requires all
  six required passes to have succeeded; the reasons get their own signals.
* a producer re-filing a finding whose condition has since been fixed — the
  distillery detector judges only the latest successful run per (item, pass).

AC4 is also pinned here: every row a producer files must have a non-null
``detector`` and a fingerprint prefixed with that detector id, because #159
sweeps on ``detector IS NOT NULL``.
"""

import pytest

from tms.producers import distillery_silent_success, pipeline_health

BOOK_VIEW = """
CREATE TABLE IF NOT EXISTS v_pipeline_health_book (
    item_id                 INTEGER,
    title                   TEXT,
    passes_succeeded        INTEGER,
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
    error_detail TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    artifact    TEXT
)
"""

SOURCE_TEXT_SKIP = (
    "insufficient-source-text: no epub body nor OCR sidecar found"
)


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


def _book(conn, item_id, title="A Book", passes_succeeded=6, **overrides):
    row = {
        "is_missing_distillation": 0, "is_missing_junctions": 0,
        "topics_count": 4, "traditions_count": 2, "quotes_count": 9,
        "near_verbatim_rate": 0.9, "stale_running_count": 0,
    }
    row.update(overrides)
    _exec(
        conn,
        "INSERT INTO v_pipeline_health_book (item_id, title, "
        "passes_succeeded, is_missing_distillation, is_missing_junctions, "
        "topics_count, traditions_count, quotes_count, near_verbatim_rate, "
        "stale_running_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (item_id, title, passes_succeeded, row["is_missing_distillation"],
         row["is_missing_junctions"], row["topics_count"],
         row["traditions_count"], row["quotes_count"],
         row["near_verbatim_rate"], row["stale_running_count"]),
    )
    conn.commit()


def _run(conn, run_id, item_id, pass_id, artifact=None, status="success",
         error_detail=None, started_at="2026-05-25T00:00:00+00:00",
         ended_at="2026-05-25T00:10:00+00:00"):
    _exec(
        conn,
        "INSERT INTO media_digest_runs (id, item_id, pass_id, status, "
        "error_detail, started_at, ended_at, artifact) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, item_id, pass_id, status, error_detail, started_at, ended_at,
         artifact),
    )
    conn.commit()


def _register(conn):
    return _exec(
        conn,
        "SELECT repo, fingerprint, detector, discovery_source, severity, "
        "recurrence_count, description FROM escaped_defects ORDER BY fingerprint",
    ).fetchall()


def _fingerprints(conn):
    return [row[1] for row in _register(conn)]


# ── producer #1: pipeline-health ─────────────────────────────────


def test_missing_distillation_requires_all_six_passes_to_have_succeeded(
        producers_db):
    """The only silent shape is "all 6 passes OK, no row" (distillery#173).
    A book whose passes did not all succeed reported *something*."""
    _book(producers_db, 152, "Oathbringer", passes_succeeded=6,
          is_missing_distillation=1)

    pipeline_health.sweep()

    assert _fingerprints(producers_db) == [
        "pipeline-health:missing-distillation:152"
    ]


def test_a_skipped_book_is_not_filed_as_missing_distillation(producers_db):
    """The live 14-row case: no distillation, 0 of 6 passes succeeded, and every
    core-extract run was skipped for want of source text. The pipeline reported;
    a row claiming otherwise is the unsupported assertion this register forbids.
    """
    _book(producers_db, 163, passes_succeeded=0, is_missing_distillation=1)
    _run(producers_db, 1, 163, "core-extract", status="skipped",
         error_detail=SOURCE_TEXT_SKIP)

    pipeline_health.sweep()

    assert _fingerprints(producers_db) == [
        "pipeline-health:insufficient-source-text:163"
    ]
    description = _register(producers_db)[0][6]
    assert "no book_distillations row" in description
    assert "skipped with 'insufficient-source-text" in description
    assert "reported OK" not in description
    assert "no failure" not in description


def test_a_failed_core_extract_gets_its_own_signal(producers_db):
    _book(producers_db, 240, passes_succeeded=1, is_missing_distillation=1)
    _run(producers_db, 1, 240, "core-extract", status="failed",
         error_detail='{"error": "exit 1"}')

    pipeline_health.sweep()

    assert _fingerprints(producers_db) == [
        "pipeline-health:core-extract-failed:240"
    ]
    description = _register(producers_db)[0][6]
    assert "status='failed'" in description
    assert "1 of 6 passes succeeded" in description


def test_a_book_with_both_a_skip_and_a_failure_files_both_facts(producers_db):
    _book(producers_db, 315, passes_succeeded=0, is_missing_distillation=1)
    _run(producers_db, 1, 315, "core-extract", status="skipped",
         error_detail=SOURCE_TEXT_SKIP)
    _run(producers_db, 2, 315, "core-extract", status="failed",
         error_detail='{"error": "exit 1"}')

    pipeline_health.sweep()

    assert _fingerprints(producers_db) == [
        "pipeline-health:core-extract-failed:315",
        "pipeline-health:insufficient-source-text:315",
    ]


def test_a_book_that_has_not_run_yet_files_nothing(producers_db):
    """No distillation and no core-extract run at all is "not yet attempted" —
    not a silent failure. The register will not claim one."""
    _book(producers_db, 502, passes_succeeded=0, is_missing_distillation=1)

    pipeline_health.sweep()

    assert _register(producers_db) == []


def test_a_stalled_chain_after_a_successful_extract_is_its_own_signal(
        producers_db):
    _book(producers_db, 19394, passes_succeeded=2, is_missing_distillation=1)
    _run(producers_db, 1, 19394, "core-extract", artifact='{"chapter_summaries": ["x"]}')

    pipeline_health.sweep()

    assert _fingerprints(producers_db) == [
        "pipeline-health:distillation-stalled:19394"
    ]


def test_junction_drift_names_only_what_the_view_proves(producers_db):
    """The view proves the junctions are incomplete. It does not prove a pass
    "recorded success without writing them" — for several live books
    canonical-map did succeed with non-empty output; the projection is what did
    not land (#171)."""
    _book(producers_db, 152, topics_count=0, traditions_count=4, quotes_count=0,
          is_missing_junctions=1)

    pipeline_health.sweep()

    description = _register(producers_db)[0][6]
    assert "topics=0, traditions=4, quotes=0" in description
    assert "#171" in description, "the owning surface is not named"
    for unsupported in ("recorded success", "wrote nothing", "without writing"):
        assert unsupported not in description


def test_pipeline_health_rows_carry_detector_and_detector_prefix(producers_db):
    _book(producers_db, 152, is_missing_junctions=1)
    _book(producers_db, 163, passes_succeeded=0, is_missing_distillation=1,
          near_verbatim_rate=0.2, stale_running_count=2)
    _run(producers_db, 1, 163, "core-extract", status="skipped",
         error_detail=SOURCE_TEXT_SKIP)

    pipeline_health.sweep()

    rows = _register(producers_db)
    assert len(rows) == 4
    for repo, fingerprint, detector, source, severity, _, _ in rows:
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

    assert _fingerprints(producers_db) == ["pipeline-health:quote-decay:1"]


def test_pipeline_health_repo_is_overridable(producers_db):
    _book(producers_db, 152, is_missing_junctions=1)

    pipeline_health.sweep(repo="home-portal")

    assert _register(producers_db)[0][0] == "home-portal"


def test_pipeline_health_healthy_pipeline_files_nothing(producers_db):
    _book(producers_db, 1)
    _book(producers_db, 2)

    pipeline_health.sweep()

    assert _register(producers_db) == []


# ── the positional contract, pinned ──────────────────────────────


def _select_aliases(sql):
    """Column aliases in a SELECT list, in order (no functions contain commas)."""
    body = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
    aliases = []
    for part in body.replace("\n", " ").split(","):
        part = part.strip()
        if not part:
            continue
        aliases.append(part.rsplit(" AS ", 1)[-1].strip())
    return aliases


def test_book_column_order_matches_the_select_list():
    assert _select_aliases(pipeline_health.BOOK_SQL) == \
        list(pipeline_health.BOOK_COLUMNS)


def test_ledger_column_order_matches_the_select_list():
    assert _select_aliases(pipeline_health.LEDGER_SQL) == \
        list(pipeline_health.LEDGER_COLUMNS)


# ── producer #2: distillery silent success ───────────────────────


def test_distillery_files_a_success_run_with_an_empty_payload(producers_db):
    _run(producers_db, 6113, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}')

    distillery_silent_success.sweep()

    rows = _register(producers_db)
    assert len(rows) == 1
    repo, fingerprint, detector, source, _, _, description = rows[0]
    assert repo == "distillery"
    assert fingerprint == "distillery-silent-success:empty-mapping:63"
    assert detector == "distillery-silent-success"
    assert source == "detector"
    assert "most recent successful canonical-map run" in description


def test_latest_successful_run_supersedes_an_earlier_silent_one(producers_db):
    """A re-run that wrote content is the pipeline saying the earlier run is no
    longer the state of the world. Filing it every sweep would climb
    recurrence_count for a fixed defect (there is no resolution path yet —
    #158/#159)."""
    _run(producers_db, 6113, 63, "canonical-map",
         '{"canonical_topics": [], "canonical_traditions": []}',
         ended_at="2026-05-25T23:46:32+00:00")
    _run(producers_db, 9001, 63, "canonical-map",
         '{"canonical_topics": ["stoicism"], "canonical_traditions": ["stoicism"]}',
         ended_at="2026-11-01T00:00:00+00:00")

    distillery_silent_success.sweep()

    assert _register(producers_db) == []


def test_distillery_judges_only_the_latest_run_per_pass(producers_db):
    """Two empty runs are still one finding, and the description points at the
    most recent one — the run count is not the identity."""
    for run_id, ended in ((6113, "2026-05-25T23:46:32+00:00"),
                          (6119, "2026-05-25T23:49:51+00:00")):
        _run(producers_db, run_id, 63, "canonical-map",
             '{"canonical_topics": [], "canonical_traditions": []}',
             ended_at=ended)

    distillery_silent_success.sweep()

    rows = _register(producers_db)
    assert len(rows) == 1
    assert "2026-05-25T23:49:51" in rows[0][6]


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

    assert _fingerprints(producers_db) == [
        "distillery-silent-success:empty-extraction:258",
        "distillery-silent-success:empty-mapping:63",
        "distillery-silent-success:empty-review:7",
    ]


def test_distillery_ignores_unregistered_passes(producers_db):
    _run(producers_db, 1, 63, "genre-sniff", '{"genre_profile": ""}')

    distillery_silent_success.sweep()

    assert _register(producers_db) == []


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


def test_is_empty_treats_zero_and_false_as_content():
    """`0` and `false` are things a pass wrote; only missing, blank or
    zero-length containers are emptiness."""
    for content in (0, 0.0, False, 1, True, "x", ["x"], {"k": "v"}):
        assert distillery_silent_success._is_empty(content) is False, content
    for empty in (None, "", "   ", [], {}, ()):
        assert distillery_silent_success._is_empty(empty) is True, empty


# ── AC4, across both producers ───────────────────────────────────


def test_every_row_a_producer_files_is_sweepable(producers_db):
    _book(producers_db, 152, is_missing_distillation=1, passes_succeeded=6,
          is_missing_junctions=1, near_verbatim_rate=0.2,
          stale_running_count=1)
    _book(producers_db, 163, passes_succeeded=0, is_missing_distillation=1)
    _run(producers_db, 0, 163, "core-extract", status="skipped",
         error_detail=SOURCE_TEXT_SKIP)
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
    assert len(rows) == 7
    for detector, fingerprint, source in rows:
        assert detector is not None, (
            "a producer row with no detector can never be swept"
        )
        assert fingerprint.startswith(detector + ":")
        assert source == "detector"
