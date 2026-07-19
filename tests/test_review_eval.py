"""Tests for lib/tms/review_eval.py — reviewer eval harness (issue #54).

Database isolation: tests monkeypatch _get_conn() to return a
sqlite3 in-memory connection with matching test tables. The SQL
is ANSI-standard so both postgres and sqlite3 backends work.
"""

import json
import os
import sqlite3
import tempfile

import pytest

from tms.review_eval import (
    log_reviewer_run,
    SEEDED_RESULTS_PATH,
)


# ── Test fixtures ─────────────────────────────────────────────────

_CREATE_TEST_TABLES = """
CREATE TABLE IF NOT EXISTS reviewer_runs (
    run_id          TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    review_round    INTEGER NOT NULL,
    reviewer_agent  TEXT NOT NULL,
    model           TEXT NOT NULL,
    provider_used   TEXT NOT NULL,
    diff_sha_reviewed TEXT NOT NULL,
    p0              INTEGER NOT NULL DEFAULT 0,
    p1              INTEGER NOT NULL DEFAULT 0,
    p2              INTEGER NOT NULL DEFAULT 0,
    wall_time_ms    INTEGER,
    findings        TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    specialist_composition TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS escaped_defects (
    defect_id           TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    repo                TEXT NOT NULL,
    introducing_pr      INTEGER NOT NULL,
    introducing_commit  TEXT NOT NULL,
    defect_class        TEXT NOT NULL,
    severity            TEXT NOT NULL,
    discovered_at       TEXT NOT NULL,
    discovery_source    TEXT NOT NULL,
    description         TEXT NOT NULL,
    fix_pr              INTEGER
);
CREATE TABLE IF NOT EXISTS defect_attributions (
    attribution_id  TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    defect_id       TEXT NOT NULL,
    run_source      TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    role            TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    FOREIGN KEY (defect_id) REFERENCES escaped_defects (defect_id)
);
"""


@pytest.fixture
def test_db(monkeypatch):
    """Replace _get_conn() with sqlite3 in-memory, with test tables.

    All connections within a single test share the same in-memory
    database, so data written by log_reviewer_run() etc. is visible
    to the test's query."""
    from tms import review_eval as mod
    _orig = mod._get_conn

    _conn = sqlite3.connect(":memory:")
    _conn.execute("PRAGMA foreign_keys = ON")
    for stmt in _CREATE_TEST_TABLES.split(";"):
        stmt = stmt.strip()
        if stmt:
            _conn.execute(stmt)
    _conn.commit()

    class _SharedCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=None):
            import re
            sql = sql.replace("tms_review.", "")
            # psycopg2 pyformat (%(name)s) → sqlite3 named (:name)
            sql = re.sub(r'%\(([^)]+)\)s', r':\1', sql)
            # psycopg2 format (%s) → sqlite3 positional (?)
            sql = sql.replace("%s", "?")
            if params is not None:
                return self._cur.execute(sql, params)
            return self._cur.execute(sql)

        def fetchall(self):
            return self._cur.fetchall()

        @property
        def description(self):
            return self._cur.description

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class _SharedConnection:
        def cursor(self):
            return _SharedCursor(_conn.cursor())

        def commit(self):
            _conn.commit()

        def close(self):
            pass  # shared — don't close

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def _make_conn():
        return _SharedConnection()

    monkeypatch.setattr(mod, "_get_conn", _make_conn)
    yield _make_conn
    monkeypatch.setattr(mod, "_get_conn", _orig)
    _conn.close()


@pytest.fixture
def tmp_seeded_path(monkeypatch):
    """Redirect seeded_results path to a temp file."""
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(
            "tms.review_eval.SEEDED_RESULTS_PATH",
            os.path.join(td, "seeded_results.jsonl"),
        )
        yield td


def _read_jsonl(path):
    """Read all JSONL records from a file. Returns list of dicts."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _query_all(conn, table):
    """Return all rows from a table as list of dicts."""
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── AC2: escaped_defects + defect_attributions ────────────────────

class TestRecordEscapedDefect:
    """record_escaped_defect() writes rows to the database."""

    def test_writes_defect_with_attributions(self, test_db):
        from tms.review_eval import record_escaped_defect

        defect_id = record_escaped_defect(
            repo="home-portal",
            introducing_pr=102,
            introducing_commit="e9e1cd4",
            defect_class="sql-syntax",
            severity="critical",
            discovered_at="2026-06-05T06:27:08+00:00",
            discovery_source="ci",
            description="DISTINCT ON placed after ORDER BY",
            fix_pr=102,
            attributions=[
                {"run_source": "coding_runs",
                 "run_id": "placeholder-author",
                 "role": "author", "outcome": "shipped"},
                {"run_source": "reviewer_runs",
                 "run_id": "placeholder-reviewer-1",
                 "role": "reviewer", "outcome": "missed"},
            ],
        )

        assert defect_id is not None
        assert len(defect_id) == 36

        with test_db() as conn:
            defects = _query_all(conn, "escaped_defects")
            assert len(defects) == 1
            d = defects[0]
            assert d["defect_id"] == defect_id
            assert d["repo"] == "home-portal"
            assert d["introducing_pr"] == 102
            assert d["defect_class"] == "sql-syntax"
            assert "DISTINCT ON" in d["description"]

            attrs = _query_all(conn, "defect_attributions")
            assert len(attrs) == 2
            assert attrs[0]["defect_id"] == defect_id
            assert attrs[0]["role"] == "author"
            assert attrs[1]["role"] == "reviewer"
            assert attrs[1]["outcome"] == "missed"

    def test_re_judge_appends_never_mutates_original(self, test_db):
        from tms.review_eval import record_escaped_defect

        defect_id = record_escaped_defect(
            repo="distillery", introducing_pr=100,
            introducing_commit="abc", defect_class="logic",
            severity="major",
            discovered_at="2026-07-01T00:00:00+00:00",
            discovery_source="incident",
            description="wrong calculation", fix_pr=105,
            attributions=[
                {"run_source": "reviewer_runs", "run_id": "r1",
                 "role": "reviewer",
                 "outcome": "flagged-but-rebutted"},
            ],
        )

        # Second pass: re-judgment with same defect_id
        record_escaped_defect(
            repo="distillery", introducing_pr=100,
            introducing_commit="abc", defect_class="logic",
            severity="major",
            discovered_at="2026-07-01T00:00:00+00:00",
            discovery_source="incident",
            description="wrong calculation", fix_pr=105,
            defect_id=defect_id,
            attributions=[
                {"run_source": "reviewer_runs", "run_id": "r2",
                 "role": "reviewer", "outcome": "missed"},
            ],
        )

        with test_db() as conn:
            defects = _query_all(conn, "escaped_defects")
            assert len(defects) == 1  # still 1 defect row

            attrs = _query_all(conn, "defect_attributions")
            assert len(attrs) == 2  # 1 original + 1 re-judgment

    def test_attribution_has_source_tag(self, test_db):
        from tms.review_eval import record_escaped_defect

        record_escaped_defect(
            repo="tms", introducing_pr=50, introducing_commit="def",
            defect_class="perf", severity="minor",
            discovered_at="2026-07-10T00:00:00+00:00",
            discovery_source="manual", description="slow query",
            fix_pr=55,
            attributions=[
                {"run_source": "coding_runs", "run_id": "author-1",
                 "role": "author", "outcome": "shipped"},
            ],
        )

        with test_db() as conn:
            attrs = _query_all(conn, "defect_attributions")
            assert len(attrs) == 1
            assert attrs[0]["run_source"] == "coding_runs"

    def test_defect_classes_are_validated(self, test_db):
        from tms.review_eval import record_escaped_defect

        valid = ["sql-syntax", "logic", "auth", "schema",
                 "data-loss", "perf", "convention"]
        for dc in valid:
            record_escaped_defect(
                repo="tms", introducing_pr=1, introducing_commit="x",
                defect_class=dc, severity="minor",
                discovered_at="2026-01-01T00:00:00+00:00",
                discovery_source="manual",
                description=f"test {dc}", fix_pr=2,
                attributions=[],
            )
        with pytest.raises(ValueError, match="defect_class"):
            record_escaped_defect(
                repo="tms", introducing_pr=1, introducing_commit="x",
                defect_class="not-real", severity="minor",
                discovered_at="2026-01-01T00:00:00+00:00",
                discovery_source="manual", description="bad",
                fix_pr=2, attributions=[],
            )

    def test_attribution_outcomes_are_validated(self, test_db):
        from tms.review_eval import record_escaped_defect

        valid = ["shipped", "missed", "flagged-but-rebutted",
                 "not-in-scope"]
        for outcome in valid:
            record_escaped_defect(
                repo="tms", introducing_pr=1, introducing_commit="x",
                defect_class="logic", severity="minor",
                discovered_at="2026-01-01T00:00:00+00:00",
                discovery_source="manual",
                description=f"test {outcome}", fix_pr=2,
                attributions=[
                    {"run_source": "reviewer_runs", "run_id": "r",
                     "role": "reviewer", "outcome": outcome},
                ],
            )
        with pytest.raises(ValueError, match="outcome"):
            record_escaped_defect(
                repo="tms", introducing_pr=1, introducing_commit="x",
                defect_class="logic", severity="minor",
                discovered_at="2026-01-01T00:00:00+00:00",
                discovery_source="manual", description="bad",
                fix_pr=2,
                attributions=[
                    {"run_source": "reviewer_runs", "run_id": "r",
                     "role": "reviewer", "outcome": "maybe"},
                ],
            )

    def test_timestamp_is_recorded(self, test_db):
        from tms.review_eval import record_escaped_defect

        record_escaped_defect(
            repo="tms", introducing_pr=1, introducing_commit="x",
            defect_class="perf", severity="minor",
            discovered_at="2026-01-01T00:00:00+00:00",
            discovery_source="manual", description="test",
            fix_pr=2, attributions=[],
        )
        with test_db() as conn:
            defects = _query_all(conn, "escaped_defects")
            assert defects[0]["created_at"] is not None


# ── AC4: attribution rules 1-3 ───────────────────────────────────

class TestAttributionRules:
    """apply_attribution_rules() enforces the three rules in code."""

    def test_rule1_round_scoping_reviewer_not_charged_if_diff_mismatch(
        self,
    ):
        from tms.review_eval import apply_attribution_rules

        attributions = apply_attribution_rules(
            introducing_diff_sha="commit-buggy",
            author_run={"run_source": "coding_runs",
                        "run_id": "author-1"},
            reviewer_runs=[
                {"run_source": "reviewer_runs",
                 "run_id": "reviewer-round1",
                 "diff_sha_reviewed": "commit-before-bug",
                 "flagged_defect": False},
            ],
            author_rebuttals={},
        )
        reviewer = [a for a in attributions
                     if a["run_id"] == "reviewer-round1"][0]
        assert reviewer["outcome"] == "not-in-scope"

    def test_rule1_round_scoping_reviewer_charged_if_diff_matches(
        self,
    ):
        from tms.review_eval import apply_attribution_rules

        attributions = apply_attribution_rules(
            introducing_diff_sha="commit-buggy",
            author_run={"run_source": "coding_runs",
                        "run_id": "author-1"},
            reviewer_runs=[
                {"run_source": "reviewer_runs",
                 "run_id": "reviewer-round2",
                 "diff_sha_reviewed": "commit-buggy",
                 "flagged_defect": False},
            ],
            author_rebuttals={},
        )
        reviewer = [a for a in attributions
                     if a["run_id"] == "reviewer-round2"][0]
        assert reviewer["outcome"] == "missed"

    def test_rule2_rebutted_finding_flips_attribution(self):
        from tms.review_eval import apply_attribution_rules

        attributions = apply_attribution_rules(
            introducing_diff_sha="commit-buggy",
            author_run={"run_source": "coding_runs",
                        "run_id": "author-1"},
            reviewer_runs=[
                {"run_source": "reviewer_runs",
                 "run_id": "reviewer-alert",
                 "diff_sha_reviewed": "commit-buggy",
                 "flagged_defect": True},
            ],
            author_rebuttals={"reviewer-alert": True},
        )
        reviewer = [a for a in attributions
                     if a["run_id"] == "reviewer-alert"][0]
        assert reviewer["outcome"] == "flagged-but-rebutted"

    def test_rule2_not_rebutted_reviewer_miss_stands(self):
        from tms.review_eval import apply_attribution_rules

        attributions = apply_attribution_rules(
            introducing_diff_sha="commit-buggy",
            author_run={"run_source": "coding_runs",
                        "run_id": "author-1"},
            reviewer_runs=[
                {"run_source": "reviewer_runs",
                 "run_id": "reviewer-blind",
                 "diff_sha_reviewed": "commit-buggy",
                 "flagged_defect": False},
            ],
            author_rebuttals={},
        )
        reviewer = [a for a in attributions
                     if a["run_id"] == "reviewer-blind"][0]
        assert reviewer["outcome"] == "missed"

    def test_rule3_individual_panel_misses(self):
        from tms.review_eval import apply_attribution_rules

        attributions = apply_attribution_rules(
            introducing_diff_sha="commit-buggy",
            author_run={"run_source": "coding_runs",
                        "run_id": "author-1"},
            reviewer_runs=[
                {"run_source": "reviewer_runs", "run_id": "r1",
                 "diff_sha_reviewed": "commit-buggy",
                 "flagged_defect": False},
                {"run_source": "reviewer_runs", "run_id": "r2",
                 "diff_sha_reviewed": "commit-buggy",
                 "flagged_defect": False},
                {"run_source": "reviewer_runs", "run_id": "r3",
                 "diff_sha_reviewed": "commit-buggy",
                 "flagged_defect": True},
            ],
            author_rebuttals={},
        )
        reviewer_ids = [a["run_id"] for a in attributions
                        if a["role"] == "reviewer"]
        assert sorted(reviewer_ids) == ["r1", "r2", "r3"]
        r1 = [a for a in attributions if a["run_id"] == "r1"][0]
        r2 = [a for a in attributions if a["run_id"] == "r2"][0]
        r3 = [a for a in attributions if a["run_id"] == "r3"][0]
        assert r1["outcome"] == "missed"
        assert r2["outcome"] == "missed"
        assert r3["outcome"] == "missed"

    def test_multiple_rounds_one_reviewer_didnt_see_buggy_commit(self):
        from tms.review_eval import apply_attribution_rules

        attributions = apply_attribution_rules(
            introducing_diff_sha="commit-round2-buggy",
            author_run={"run_source": "coding_runs",
                        "run_id": "author-1"},
            reviewer_runs=[
                {"run_source": "reviewer_runs",
                 "run_id": "r-round1",
                 "diff_sha_reviewed": "commit-round1-clean",
                 "flagged_defect": False},
                {"run_source": "reviewer_runs",
                 "run_id": "r-round2",
                 "diff_sha_reviewed": "commit-round2-buggy",
                 "flagged_defect": False},
            ],
            author_rebuttals={},
        )
        r1 = [a for a in attributions
              if a["run_id"] == "r-round1"][0]
        r2 = [a for a in attributions
              if a["run_id"] == "r-round2"][0]
        assert r1["outcome"] == "not-in-scope"
        assert r2["outcome"] == "missed"


# ── AC1: reviewer_runs audit records ──────────────────────────────

class TestLogReviewerRun:
    """log_reviewer_run() inserts rows into the database."""

    def test_writes_round_scoped_diff_sha(self, test_db):
        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="abc123def456",
            p0=0, p1=3, p2=2, wall_time_ms=45200,
        )

        with test_db() as conn:
            rows = _query_all(conn, "reviewer_runs")
            assert len(rows) == 1
            r = rows[0]
            assert r["repo"] == "tms"
            assert r["pr_number"] == 54
            assert r["review_round"] == 1
            assert r["model"] == "deepseek-v4-pro"
            assert r["diff_sha_reviewed"] == "abc123def456"
            assert r["p0"] == 0
            assert r["p1"] == 3
            assert r["p2"] == 2
            assert r["wall_time_ms"] == 45200
            assert r["run_id"] is not None
            assert len(r["run_id"]) == 36

    def test_multiple_rounds_append_separate_records(self, test_db):
        log_reviewer_run(
            repo="distillery", pr_number=287, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha-round1",
            p0=2, p1=4, p2=1, wall_time_ms=60000,
        )
        log_reviewer_run(
            repo="distillery", pr_number=287, review_round=2,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha-round2",
            p0=0, p1=1, p2=0, wall_time_ms=30000,
        )

        with test_db() as conn:
            rows = _query_all(conn, "reviewer_runs")
            assert len(rows) == 2
            assert rows[0]["review_round"] == 1
            assert rows[1]["review_round"] == 2

    def test_unique_run_ids(self, test_db):
        for _ in range(5):
            log_reviewer_run(
                repo="tms", pr_number=54, review_round=1,
                reviewer_agent="reviewer", model="deepseek-v4-pro",
                provider_used="deepseek",
                diff_sha_reviewed="sha", p0=0, p1=0, p2=0,
                wall_time_ms=100,
            )

        with test_db() as conn:
            rows = _query_all(conn, "reviewer_runs")
            run_ids = [r["run_id"] for r in rows]
            assert len(set(run_ids)) == 5

    def test_run_id_format(self, test_db):
        import re
        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha", p0=0, p1=0, p2=0,
            wall_time_ms=100,
        )
        with test_db() as conn:
            rows = _query_all(conn, "reviewer_runs")
        uuid4_re = (
            r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
            r'[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        )
        assert re.match(uuid4_re, rows[0]["run_id"])

    def test_findings_list_stored_when_provided(self, test_db):
        findings = [
            {"severity": "P0", "file": "q.ts", "description": "x"},
        ]
        log_reviewer_run(
            repo="home-portal", pr_number=102, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha", p0=0, p1=0, p2=0,
            wall_time_ms=100, findings=findings,
        )
        with test_db() as conn:
            rows = _query_all(conn, "reviewer_runs")
            stored = json.loads(rows[0]["findings"])
            assert stored == findings

    def test_token_cost_stored_when_provided(self, test_db):
        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha", p0=0, p1=0, p2=0,
            wall_time_ms=100,
            input_tokens=1200, output_tokens=800,
        )
        with test_db() as conn:
            rows = _query_all(conn, "reviewer_runs")
            assert rows[0]["input_tokens"] == 1200
            assert rows[0]["output_tokens"] == 800

    def test_specialist_composition_stored(self, test_db):
        import json
        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha", p0=0, p1=0, p2=0,
            wall_time_ms=100,
            specialist_composition=["security", "schema"],
        )
        with test_db() as conn:
            rows = _query_all(conn, "reviewer_runs")
            stored = json.loads(rows[0]["specialist_composition"])
            assert sorted(stored) == ["schema", "security"]

    def test_specialist_composition_defaults_to_empty(self, test_db):
        import json
        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha", p0=0, p1=0, p2=0,
            wall_time_ms=100,
        )
        with test_db() as conn:
            rows = _query_all(conn, "reviewer_runs")
            stored = json.loads(rows[0]["specialist_composition"])
            assert stored == []


# ── AC5: seeded-defect gold set ───────────────────────────────────

class TestSeededGoldManifest:
    """The manifest and fixtures are valid and complete."""

    def test_manifest_loads_and_has_sufficient_fixtures(self):
        from tms.review_eval import load_seeded_gold_manifest
        manifest = load_seeded_gold_manifest()
        assert len(manifest["fixtures"]) >= 5

    def test_fixtures_span_all_seven_defect_classes(self):
        from tms.review_eval import load_seeded_gold_manifest
        manifest = load_seeded_gold_manifest()
        classes = {f["defect_class"] for f in manifest["fixtures"]}
        expected = {
            "sql-syntax", "logic", "auth", "schema",
            "data-loss", "perf", "convention",
        }
        assert classes == expected

    def test_each_fixture_file_exists(self):
        from tms.review_eval import load_seeded_gold_manifest
        manifest = load_seeded_gold_manifest()
        for fixture in manifest["fixtures"]:
            assert os.path.isfile(
                os.path.join(
                    os.path.dirname(__file__),
                    "..", "lib", "tms", "seeded_gold",
                    fixture["fixture_file"],
                )
            ), f"Fixture file missing: {fixture['fixture_file']}"

    def test_each_fixture_has_expected_detection(self):
        from tms.review_eval import load_seeded_gold_manifest
        manifest = load_seeded_gold_manifest()
        for fixture in manifest["fixtures"]:
            assert fixture["expected_detection"] is True

    def test_each_fixture_diff_is_nonempty(self):
        from tms.review_eval import (
            load_seeded_gold_manifest, load_fixture_diff,
        )
        manifest = load_seeded_gold_manifest()
        for fixture in manifest["fixtures"]:
            diff_text = load_fixture_diff(fixture)
            assert len(diff_text) > 100
            assert "PLANTED" in diff_text

    def test_unique_fixture_ids(self):
        from tms.review_eval import load_seeded_gold_manifest
        manifest = load_seeded_gold_manifest()
        ids = [f["id"] for f in manifest["fixtures"]]
        assert len(ids) == len(set(ids))


class TestSeededGoldRunner:
    """run_seeded_fixture and run_seeded_gold dispatch to reviewers."""

    def test_run_fixture_detection_hit(self, tmp_seeded_path):
        from tms.review_eval import (
            load_seeded_gold_manifest, run_seeded_fixture,
        )

        manifest = load_seeded_gold_manifest()
        fixture = manifest["fixtures"][0]

        def _detective(diff_text):
            return {"detected": True, "false_positives": 0,
                    "model": "test", "provider": "test",
                    "wall_time_ms": 42}

        result_path = os.path.join(
            tmp_seeded_path, "seeded_results.jsonl"
        )
        result = run_seeded_fixture(
            fixture, _detective, seed_result_path=result_path,
        )
        assert result["detected"] is True
        assert result["fixture_id"] == fixture["id"]

    def test_run_fixture_detection_miss(self, tmp_seeded_path):
        from tms.review_eval import (
            load_seeded_gold_manifest, run_seeded_fixture,
        )
        manifest = load_seeded_gold_manifest()
        fixture = manifest["fixtures"][0]

        def _blind(diff_text):
            return {"detected": False, "false_positives": 0,
                    "model": "blind", "provider": "test",
                    "wall_time_ms": 10}

        result_path = os.path.join(
            tmp_seeded_path, "seeded_results.jsonl"
        )
        result = run_seeded_fixture(
            fixture, _blind, seed_result_path=result_path,
        )
        assert result["detected"] is False

    def test_run_all_fixtures(self, tmp_seeded_path):
        from tms.review_eval import (
            run_seeded_gold, load_seeded_gold_manifest,
        )

        def _perfect(diff_text):
            return {"detected": True, "false_positives": 0,
                    "model": "perfect", "provider": "test",
                    "wall_time_ms": 1}

        result_path = os.path.join(
            tmp_seeded_path, "seeded_results.jsonl"
        )
        results = run_seeded_gold(
            _perfect, seed_result_path=result_path,
        )
        manifest = load_seeded_gold_manifest()
        assert len(results) == len(manifest["fixtures"])
        for r in results:
            assert r["detected"] is True

    def test_run_fixture_false_positive_recording(self, tmp_seeded_path):
        from tms.review_eval import (
            load_seeded_gold_manifest, run_seeded_fixture,
        )
        manifest = load_seeded_gold_manifest()
        fixture = manifest["fixtures"][0]

        def _nervous(diff_text):
            return {"detected": True, "false_positives": 3,
                    "model": "nervous", "provider": "test",
                    "wall_time_ms": 100}

        result_path = os.path.join(
            tmp_seeded_path, "seeded_results.jsonl"
        )
        result = run_seeded_fixture(
            fixture, _nervous, seed_result_path=result_path,
        )
        assert result["false_positives"] == 3


# ── AC6: reports ──────────────────────────────────────────────────

class TestComputeReviewStats:
    """compute_review_stats() produces per-reviewer/author reports."""

    def test_per_reviewer_counts(self, test_db):
        from tms.review_eval import compute_review_stats

        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha", p0=0, p1=2, p2=1,
            wall_time_ms=50000,
        )
        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer-m3", model="MiniMax-M3",
            provider_used="minimax",
            diff_sha_reviewed="sha", p0=1, p1=3, p2=0,
            wall_time_ms=60000,
        )

        stats = compute_review_stats()
        assert "per_reviewer" in stats
        assert stats["per_reviewer"]["deepseek-v4-pro"][
            "total_reviews"] >= 1

    def test_per_author_escape_rate(self, test_db):
        from tms.review_eval import (
            compute_review_stats, record_escaped_defect,
        )

        record_escaped_defect(
            repo="distillery", introducing_pr=100,
            introducing_commit="abc", defect_class="logic",
            severity="major",
            discovered_at="2026-07-01T00:00:00+00:00",
            discovery_source="incident",
            description="wrong calculation", fix_pr=105,
            attributions=[
                {"run_source": "coding_runs",
                 "run_id": "author-claude",
                 "role": "author", "outcome": "shipped"},
                {"run_source": "reviewer_runs", "run_id": "r1",
                 "role": "reviewer", "outcome": "missed"},
            ],
        )

        stats = compute_review_stats()
        assert "per_author" in stats
        assert len(stats["per_author"]) >= 1

    def test_panel_uniqueness(self, test_db):
        from tms.review_eval import compute_review_stats

        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed="sha", p0=0, p1=2, p2=1,
            wall_time_ms=50000,
            findings=[
                {"severity": "P1", "file": "a.py",
                 "description": "x"},
                {"severity": "P1", "file": "b.py",
                 "description": "y"},
            ],
        )
        log_reviewer_run(
            repo="tms", pr_number=54, review_round=1,
            reviewer_agent="reviewer-m3", model="MiniMax-M3",
            provider_used="minimax",
            diff_sha_reviewed="sha", p0=1, p1=3, p2=0,
            wall_time_ms=60000,
            findings=[
                {"severity": "P0", "file": "d.py",
                 "description": "w"},
            ],
        )

        stats = compute_review_stats()
        assert "panel_uniqueness" in stats

    def test_panel_composition_suggestions(self, test_db):
        from tms.review_eval import compute_review_stats
        stats = compute_review_stats()
        assert "panel_suggestions" in stats
        assert isinstance(stats["panel_suggestions"], list)

    def test_empty_state_returns_structure(self, test_db):
        from tms.review_eval import compute_review_stats
        stats = compute_review_stats()
        assert stats["total_reviewer_runs"] == 0
        assert stats["total_escaped_defects"] == 0
        assert stats["per_reviewer"] == {}
        assert stats["per_author"] == {}
        assert stats["panel_uniqueness"] == {}
        assert stats["panel_suggestions"] == []

    def test_format_review_report_pretty(self, test_db):
        from tms.review_eval import (
            compute_review_stats, format_review_report,
        )
        import io
        stats = compute_review_stats()
        buf = io.StringIO()
        import sys
        old = sys.stdout
        sys.stdout = buf
        try:
            format_review_report(stats)
        finally:
            sys.stdout = old
        assert "Reviewer Eval Report" in buf.getvalue()

    def test_format_review_report_json(self, test_db):
        from tms.review_eval import (
            compute_review_stats, format_review_report,
        )
        import io
        stats = compute_review_stats()
        buf = io.StringIO()
        import sys
        old = sys.stdout
        sys.stdout = buf
        try:
            format_review_report(stats, as_json=True)
        finally:
            sys.stdout = old
        parsed = json.loads(buf.getvalue())
        assert parsed["total_reviewer_runs"] == 0


# ── AC7: backfill audiobook incident ─────────────────────────────

class TestBackfillAudiobookIncident:
    """The 2026-06-05 audiobook DISTINCT ON incident is backfilled."""

    def test_backfill_writes_defect_and_attributions(self, test_db):
        from tms.review_eval import backfill_audiobook_incident

        defect_id = backfill_audiobook_incident()
        assert defect_id is not None
        assert len(defect_id) == 36

        with test_db() as conn:
            defects = _query_all(conn, "escaped_defects")
            assert len(defects) == 1
            d = defects[0]
            assert d["repo"] == "home-portal"
            assert d["introducing_pr"] == 102
            assert d["defect_class"] == "sql-syntax"
            assert d["severity"] == "critical"
            assert d["discovery_source"] == "ci"
            assert "DISTINCT ON" in d["description"]
            assert d["fix_pr"] == 102

            attrs = _query_all(conn, "defect_attributions")
            assert len(attrs) == 4  # 1 author + 3 reviewers
            author = [a for a in attrs if a["role"] == "author"]
            assert len(author) == 1
            assert author[0]["outcome"] == "shipped"
            reviewers = [a for a in attrs
                         if a["role"] == "reviewer"]
            assert len(reviewers) == 3
            for ra in reviewers:
                assert ra["outcome"] == "missed"

    def test_backfill_is_idempotent(self, test_db):
        from tms.review_eval import (
            backfill_audiobook_incident, record_escaped_defect,
        )

        defect_id = backfill_audiobook_incident()

        # Re-judgment with same defect_id
        record_escaped_defect(
            repo="home-portal", introducing_pr=102,
            introducing_commit=(
                "e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4"
            ),
            defect_class="sql-syntax", severity="critical",
            discovered_at="2026-06-05T06:27:08+00:00",
            discovery_source="ci",
            description="DISTINCT ON fix", fix_pr=102,
            defect_id=defect_id,
            attributions=[
                {"run_source": "reviewer_runs",
                 "run_id": "new-reviewer",
                 "role": "reviewer", "outcome": "missed"},
            ],
        )

        with test_db() as conn:
            defects = _query_all(conn, "escaped_defects")
            assert len(defects) == 1  # still 1 defect

            attrs = _query_all(conn, "defect_attributions")
            assert len(attrs) >= 5  # 4 original + 1 new


# ── AC3: backjudge CLI ────────────────────────────────────────────

class TestBackjudgePropose:
    """backjudge_propose() walks git history and proposes attributions."""

    def test_walks_commit_to_introducing_pr(self, monkeypatch):
        from tms.review_eval import backjudge_propose
        import subprocess

        fix_sha = "ce0d29b61357a93fe0134c77e76a4d56aedbac33"
        intro_sha = "e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4"

        def _mock_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "-1" in cmd and "--format=%s" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout="fix(audiobook): address automated review\n",
                    stderr="",
                )
            if "-S" in cmd and "src/" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=f"{fix_sha} fix\n{intro_sha} feat\n",
                    stderr="",
                )
            if "pr" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps([{
                        "number": 102,
                        "mergeCommit": {"oid": intro_sha},
                        "mergedAt": "2026-06-06T01:24:56Z",
                    }]),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                cmd, 0, stdout="", stderr="",
            )

        monkeypatch.setattr("subprocess.run", _mock_run)
        monkeypatch.setattr(
            "tms.review_eval._read_sql_reviewer_runs",
            lambda: [],
        )

        proposal = backjudge_propose(
            repo_path="/fake/home-portal",
            fix_commit=fix_sha,
        )
        assert proposal["repo"] == "home-portal"
        assert proposal["introducing_pr"] == 102
        assert proposal["introducing_commit"] == intro_sha

    def test_proposal_suggests_reviewer_runs_for_pr(
        self, test_db, monkeypatch,
    ):
        from tms.review_eval import backjudge_propose
        import subprocess

        intro_sha = "e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4"
        fix_sha = "ce0d29b61357a93fe0134c77e76a4d56aedbac33"

        log_reviewer_run(
            repo="home-portal", pr_number=102, review_round=1,
            reviewer_agent="reviewer", model="deepseek-v4-pro",
            provider_used="deepseek",
            diff_sha_reviewed=intro_sha,
            p0=0, p1=0, p2=0, wall_time_ms=1000,
        )
        log_reviewer_run(
            repo="home-portal", pr_number=102, review_round=1,
            reviewer_agent="reviewer-m3", model="MiniMax-M3",
            provider_used="minimax",
            diff_sha_reviewed=intro_sha,
            p0=0, p1=0, p2=0, wall_time_ms=1000,
        )

        def _mock_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "-1" in cmd and "--format=%s" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="fix\n", stderr="",
                )
            if "-S" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=f"{fix_sha} fix\n{intro_sha} feat\n",
                    stderr="",
                )
            if "pr" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps([{
                        "number": 102,
                        "mergeCommit": {"oid": intro_sha},
                        "mergedAt": "2026-06-06T01:24:56Z",
                    }]),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                cmd, 0, stdout="", stderr="",
            )

        monkeypatch.setattr("subprocess.run", _mock_run)

        proposal = backjudge_propose(
            repo_path="/fake/home-portal",
            fix_commit=fix_sha,
        )
        reviewer_attrs = [a for a in proposal["attributions"]
                          if a["role"] == "reviewer"]
        assert len(reviewer_attrs) == 2


class TestBackjudgeConfirm:
    """backjudge_confirm() writes confirmed attributions to the DB."""

    def test_writes_confirmed_proposal(self, test_db):
        from tms.review_eval import backjudge_confirm

        proposal = {
            "repo": "home-portal",
            "introducing_pr": 102,
            "introducing_commit": (
                "e9e1cd4b067ba3baa0fa0a0bc354491e782af0f4"
            ),
            "fix_commit": (
                "ce0d29b61357a93fe0134c77e76a4d56aedbac33"
            ),
            "defect_class": "sql-syntax",
            "severity": "critical",
            "discovered_at": "2026-06-05T06:27:08+00:00",
            "discovery_source": "ci",
            "description": "DISTINCT ON syntax error",
            "fix_pr": 102,
            "attributions": [
                {"run_source": "coding_runs",
                 "run_id": "author-1",
                 "role": "author", "outcome": "shipped"},
                {"run_source": "reviewer_runs",
                 "run_id": "run-reviewer",
                 "role": "reviewer", "outcome": "missed"},
            ],
        }

        defect_id = backjudge_confirm(proposal)
        assert defect_id is not None

        with test_db() as conn:
            defects = _query_all(conn, "escaped_defects")
            assert len(defects) == 1
            attrs = _query_all(conn, "defect_attributions")
            assert len(attrs) == 2

        # Re-judgment
        proposal2 = dict(proposal)
        proposal2["attributions"] = [
            {"run_source": "reviewer_runs",
             "run_id": "run-reviewer2",
             "role": "reviewer", "outcome": "missed"},
        ]
        backjudge_confirm(proposal2, defect_id=defect_id)

        with test_db() as conn:
            defects2 = _query_all(conn, "escaped_defects")
            assert len(defects2) == 1
            attrs2 = _query_all(conn, "defect_attributions")
            assert len(attrs2) == 3
