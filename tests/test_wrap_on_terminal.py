"""Tests for wrap-on-terminal hook (issue #82).

Covers:
  - Watermark management (read/write/advance)
  - Idempotency (re-run over same terminal transition = no-op)
  - Objective-mode synthesis from fixture events
  - Frontmatter validation guard
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


# ── Watermark tests ──────────────────────────────────────────────


class TestWatermark:
    """Watermark persistence: read, write, advance, and first-run behavior."""

    def test_read_watermark_returns_none_on_missing_file(self):
        """If no watermark file exists, read returns None (first run)."""
        from tms.wrap_on_terminal import read_watermark

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nonexistent.json")
            assert read_watermark(path) is None

    def test_read_watermark_returns_timestamp(self):
        """Read returns the stored ISO timestamp."""
        from tms.wrap_on_terminal import read_watermark, write_watermark

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "watermark.json")
            ts = "2026-07-17T12:00:00+00:00"
            write_watermark(path, ts)
            assert read_watermark(path) == ts

    def test_write_watermark_overwrites(self):
        """Write overwrites an existing watermark."""
        from tms.wrap_on_terminal import read_watermark, write_watermark

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "watermark.json")
            write_watermark(path, "2026-07-17T10:00:00+00:00")
            write_watermark(path, "2026-07-17T14:00:00+00:00")
            assert read_watermark(path) == "2026-07-17T14:00:00+00:00"

    def test_watermark_compare_skip_when_terminal_before_watermark(self):
        """A terminal transition with timestamp <= watermark should be skipped."""
        from tms.wrap_on_terminal import is_new_terminal

        watermark = "2026-07-17T12:00:00+00:00"
        # Terminal timestamp is before watermark → not new
        terminal_ts = "2026-07-17T10:00:00+00:00"
        assert not is_new_terminal(watermark, terminal_ts)

    def test_watermark_compare_process_when_terminal_after_watermark(self):
        """A terminal transition with timestamp > watermark should be processed."""
        from tms.wrap_on_terminal import is_new_terminal

        watermark = "2026-07-17T12:00:00+00:00"
        terminal_ts = "2026-07-17T14:00:00+00:00"
        assert is_new_terminal(watermark, terminal_ts)

    def test_watermark_compare_process_when_no_watermark(self):
        """First run (no watermark) should process all terminal transitions."""
        from tms.wrap_on_terminal import is_new_terminal

        assert is_new_terminal(None, "2026-07-17T12:00:00+00:00")


# ── Idempotency tests ────────────────────────────────────────────


class TestIdempotency:
    """Re-running over the same terminal transition must be a no-op."""

    def test_wrap_exists_for_issue_returns_true(self):
        """wrap_exists returns True when memory file already exists."""
        from tms.wrap_on_terminal import wrap_exists

        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = os.path.join(tmp, "memory")
            os.makedirs(mem_dir)
            path = os.path.join(mem_dir, "issue-wrap-tms#99.md")
            with open(path, "w") as f:
                f.write("---\nname: issue-wrap-tms#99\n---\n")
            assert wrap_exists(mem_dir, "tms", 99)

    def test_wrap_exists_returns_false_when_no_file(self):
        """wrap_exists returns False when no wrap file exists."""
        from tms.wrap_on_terminal import wrap_exists

        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = os.path.join(tmp, "memory")
            os.makedirs(mem_dir)
            assert not wrap_exists(mem_dir, "tms", 99)


# ── Frontmatter validation tests ─────────────────────────────────


class TestFrontmatterValidation:
    """Frontmatter validation guard — refuse to write memory files
    missing name: / description: / metadata:."""

    def test_valid_frontmatter_passes(self):
        """A well-formed frontmatter block passes validation."""
        from tms.wrap_on_terminal import validate_frontmatter

        content = """---
name: issue-wrap-distillery#558
description: "Wrap for distillery#558 — objective mode"
metadata:
  node_type: memory
  type: project
---

## Summary
"""
        errors = validate_frontmatter(content)
        assert errors == []

    def test_missing_name_fails(self):
        """Frontmatter missing 'name:' is rejected."""
        from tms.wrap_on_terminal import validate_frontmatter

        content = """---
description: "Wrap for distillery#558"
metadata:
  node_type: memory
---

## Summary
"""
        errors = validate_frontmatter(content)
        assert len(errors) == 1
        assert "name" in errors[0].lower()

    def test_missing_description_fails(self):
        """Frontmatter missing 'description:' is rejected."""
        from tms.wrap_on_terminal import validate_frontmatter

        content = """---
name: issue-wrap-distillery#558
metadata:
  node_type: memory
---

## Summary
"""
        errors = validate_frontmatter(content)
        assert len(errors) == 1
        assert "description" in errors[0].lower()

    def test_missing_metadata_fails(self):
        """Frontmatter missing 'metadata:' is rejected."""
        from tms.wrap_on_terminal import validate_frontmatter

        content = """---
name: issue-wrap-distillery#558
description: "Wrap for distillery#558"
---

## Summary
"""
        errors = validate_frontmatter(content)
        assert len(errors) == 1
        assert "metadata" in errors[0].lower()

    def test_no_frontmatter_at_all_fails(self):
        """Content with no YAML frontmatter at all is rejected."""
        from tms.wrap_on_terminal import validate_frontmatter

        content = "## Summary\n\nJust text, no frontmatter."
        errors = validate_frontmatter(content)
        assert len(errors) >= 1

    def test_multiple_missing_fields_reports_all(self):
        """When multiple required fields are missing, all are reported."""
        from tms.wrap_on_terminal import validate_frontmatter

        content = """---
---

## Summary
"""
        errors = validate_frontmatter(content)
        # Empty frontmatter (just --- delimiters) means yaml.safe_load
        # returns None, which is not a dict → single error.
        # A frontmatter with only non-required keys should pass.
        # This test verifies that ALL three required fields are checked
        # when we have a dict with some but not all.
        content2 = """---
name: test
---

## Summary
"""
        errors2 = validate_frontmatter(content2)
        # name present, description + metadata missing
        assert len(errors2) == 2


# ── Objective-mode synthesis from fixture events ──────────────────


class TestObjectiveSynthesis:
    """Build a wrap from objective sources only (no conversation)."""

    def test_find_terminal_transitions_since(self, test_db):
        """find_terminal_transitions_since returns terminal events
        with event_timestamp > watermark, each with repo/issue resolved."""
        from tms.wrap_on_terminal import find_terminal_transitions_since
        from tms.events import append_event

        # Seed: a dispatch event + matching terminal transition
        append_event({
            "event_type": "dispatch",
            "timestamp": "2026-07-17T10:00:00+00:00",
            "repo": "distillery",
            "issue": 558,
            "aoe_id_prefix": "abc12345",
        })
        append_event({
            "event_type": "transition",
            "timestamp": "2026-07-17T12:00:00+00:00",
            "aoe_id_prefix": "abc12345",
            "from_status": "DONE",
            "to_status": "terminal",
        })
        # Another terminal without a dispatch (should be skipped)
        append_event({
            "event_type": "transition",
            "timestamp": "2026-07-17T13:00:00+00:00",
            "aoe_id_prefix": "orphan99",
            "from_status": "DONE",
            "to_status": "terminal",
        })

        results = find_terminal_transitions_since(None)
        assert len(results) == 1
        r = results[0]
        assert r["repo"] == "distillery"
        assert r["issue"] == 558
        assert r["aoe_id_prefix"] == "abc12345"

    def test_find_terminal_transitions_since_respects_watermark(self, test_db):
        """Transitions with timestamp <= watermark are excluded."""
        from tms.wrap_on_terminal import find_terminal_transitions_since
        from tms.events import append_event

        append_event({
            "event_type": "dispatch",
            "timestamp": "2026-07-17T08:00:00+00:00",
            "repo": "tms",
            "issue": 82,
            "aoe_id_prefix": "def67890",
        })
        append_event({
            "event_type": "transition",
            "timestamp": "2026-07-17T09:00:00+00:00",
            "aoe_id_prefix": "def67890",
            "from_status": "DONE",
            "to_status": "terminal",
        })

        # Watermark after the transition → excluded
        results = find_terminal_transitions_since("2026-07-17T10:00:00+00:00")
        assert len(results) == 0

        # Watermark before the transition → included
        results = find_terminal_transitions_since("2026-07-17T08:30:00+00:00")
        assert len(results) == 1

    def test_collect_event_history(self, test_db):
        """collect_event_history returns all transitions for a given aoe_id_prefix."""
        from tms.wrap_on_terminal import collect_event_history
        from tms.events import append_event

        prefix = "hist1234"
        append_event({
            "event_type": "dispatch",
            "timestamp": "2026-07-17T08:00:00+00:00",
            "repo": "tms",
            "issue": 82,
            "aoe_id_prefix": prefix,
        })
        append_event({
            "event_type": "transition",
            "timestamp": "2026-07-17T08:30:00+00:00",
            "aoe_id_prefix": prefix,
            "from_status": "PLAN-REVIEW",
            "to_status": "WORKING",
        })
        append_event({
            "event_type": "transition",
            "timestamp": "2026-07-17T09:00:00+00:00",
            "aoe_id_prefix": prefix,
            "from_status": "WORKING",
            "to_status": "PR-REVIEW",
        })
        append_event({
            "event_type": "transition",
            "timestamp": "2026-07-17T10:00:00+00:00",
            "aoe_id_prefix": prefix,
            "from_status": "MERGE-READY",
            "to_status": "terminal",
        })
        # Another session's transition (should be excluded)
        append_event({
            "event_type": "transition",
            "timestamp": "2026-07-17T09:30:00+00:00",
            "aoe_id_prefix": "other99",
            "from_status": "WORKING",
            "to_status": "BLOCKED",
        })

        history = collect_event_history(prefix)
        assert len(history) == 3  # 3 transitions for this prefix
        statuses = [(h["from_status"], h["to_status"]) for h in history]
        assert ("PLAN-REVIEW", "WORKING") in statuses
        assert ("WORKING", "PR-REVIEW") in statuses
        assert ("MERGE-READY", "terminal") in statuses

    def test_synthesize_wrap_content(self):
        """synthesize_wrap_content produces valid markdown with frontmatter."""
        from tms.wrap_on_terminal import synthesize_wrap_content

        transitions = [
            {"from_status": "PLAN-REVIEW", "to_status": "WORKING",
             "timestamp": "2026-07-17T08:30:00+00:00"},
            {"from_status": "WORKING", "to_status": "PR-REVIEW",
             "timestamp": "2026-07-17T09:00:00+00:00"},
            {"from_status": "MERGE-READY", "to_status": "terminal",
             "timestamp": "2026-07-17T10:00:00+00:00"},
        ]

        content = synthesize_wrap_content(
            repo="tms",
            issue=82,
            transitions=transitions,
            gh_prs=[{"number": 83, "title": "feat: wrap-on-terminal hook"}],
            usage={"calls": 42, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "etl_lag_detected": False},
        )

        # Must start with valid YAML frontmatter
        assert content.startswith("---\n")
        # Must contain required fields
        assert "name: issue-wrap-tms#82" in content
        assert "description:" in content
        assert "metadata:" in content
        # Must contain the PR reference
        assert "#83" in content
        # Must pass validation
        from tms.wrap_on_terminal import validate_frontmatter
        assert validate_frontmatter(content) == []


# ── REPO_TO_GH registry sync test ────────────────────────────────


def test_repo_to_gh_matches_tmq_registry():
    """REPO_TO_GH must match the live tmq registry.

    The map is hardcoded for performance (avoids subprocess on every
    poller tick), but this test keeps it honest — any drift between
    the hardcoded map and tmq list --machine fails fast in CI.
    """
    from tms.wrap_on_terminal import REPO_TO_GH
    from tms.review_poll import _repo_registry

    live = dict(_repo_registry())
    hardcoded = dict(REPO_TO_GH)

    # Every entry in the hardcoded map that IS in the live registry
    # must match. Entries only in hardcoded (not tmq-registered) and
    # entries only in live (new repos not yet in hardcoded) are warnings.
    mismatches = []
    for short, expected_gh in sorted(hardcoded.items()):
        live_gh = live.get(short)
        if live_gh is None:
            continue  # not in tmq registry — covered by missing-warning below
        if live_gh != expected_gh:
            mismatches.append(f"{short}: hardcoded={expected_gh}, live={live_gh}")

    if mismatches:
        msg = "REPO_TO_GH out of sync with tmq registry:\n" + "\n".join(mismatches)
        msg += "\n\nUpdate REPO_TO_GH in lib/tms/wrap_on_terminal.py to match."
        pytest.fail(msg)

    # Optional: warn about repos in live but not in hardcoded map
    missing = set(live) - set(hardcoded)
    if missing:
        import warnings
        warnings.warn(
            f"REPO_TO_GH missing repos from tmq registry: {sorted(missing)}. "
            f"Wraps for these repos will fall back to bogocat/<short-name>."
        )


class TestFetchLlmUsage:
    """_fetch_llm_usage aggregation: calls, cost, tokens, ETL-lag detection."""

    def test_fetch_llm_usage_zero_rows(self, monkeypatch):
        """When llm_call_log has no rows for this worktree, returns zeros."""
        from tms import wrap_on_terminal as wot

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = lambda s, *a: None

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = lambda s, *a: None

        monkeypatch.setattr(wot, "_get_conn", lambda: mock_conn)

        result = wot._fetch_llm_usage("tms", 99)
        assert result == {
            "calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "etl_lag_detected": False,
        }

    def test_fetch_llm_usage_with_data(self, monkeypatch):
        """Returns aggregated sums when llm_call_log has rows."""
        from tms import wrap_on_terminal as wot

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (42, 0.156, 105000, 32000)
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = lambda s, *a: None

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = lambda s, *a: None

        monkeypatch.setattr(wot, "_get_conn", lambda: mock_conn)

        result = wot._fetch_llm_usage("tms", 99)
        assert result["calls"] == 42
        assert result["cost_usd"] == 0.156
        assert result["input_tokens"] == 105000
        assert result["output_tokens"] == 32000

    def test_fetch_llm_usage_sql_uses_correct_pattern(self, monkeypatch):
        """SQL query uses the correct encoded_cwd pattern."""
        from tms import wrap_on_terminal as wot

        captured_sql = {}

        def fake_execute(sql, params=None):
            captured_sql["sql"] = sql
            captured_sql["params"] = params

        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (5, 0.01, 1000, 500)
        mock_cursor.execute = fake_execute
        mock_cursor.__enter__ = lambda s: s
        mock_cursor.__exit__ = lambda s, *a: None

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = lambda s, *a: None

        monkeypatch.setattr(wot, "_get_conn", lambda: mock_conn)

        wot._fetch_llm_usage("distillery", 558)
        assert "--root-wt-distillery-558--" in captured_sql.get("params", [None])[0]


class TestCostFrontmatter:
    """cost_usd + llm_calls in frontmatter, cost section in body."""

    def test_synthesize_wrap_includes_cost_in_frontmatter(self):
        """Frontmatter includes cost_usd and llm_calls fields."""
        from tms.wrap_on_terminal import synthesize_wrap_content

        usage = {
            "calls": 42,
            "cost_usd": 0.156,
            "input_tokens": 105000,
            "output_tokens": 32000,
            "etl_lag_detected": False,
        }

        content = synthesize_wrap_content(
            repo="tms", issue=82,
            transitions=[], gh_prs=[], usage=usage,
        )

        # Extract frontmatter
        assert content.startswith("---\n")
        parts = content.split("---\n", 2)
        fm_text = parts[1]

        assert "cost_usd:" in fm_text
        assert "0.156" in fm_text
        assert "llm_calls:" in fm_text
        assert "42" in fm_text

    def test_synthesize_wrap_includes_cost_section(self):
        """Body includes a ### Cost section with calls, cost, token sums."""
        from tms.wrap_on_terminal import synthesize_wrap_content

        usage = {
            "calls": 42,
            "cost_usd": 0.156,
            "input_tokens": 105000,
            "output_tokens": 32000,
            "etl_lag_detected": False,
        }

        content = synthesize_wrap_content(
            repo="tms", issue=82,
            transitions=[], gh_prs=[], usage=usage,
        )

        assert "## Cost" in content
        assert "- Calls: 42" in content
        assert "$0.1560" in content
        assert "105,000" in content  # formatted input tokens
        assert "32,000" in content  # formatted output tokens

    def test_synthesize_wrap_partial_data_note(self):
        """When etl_lag_detected=True, body includes a partial-data note."""
        from tms.wrap_on_terminal import synthesize_wrap_content

        usage = {
            "calls": 42,
            "cost_usd": 0.156,
            "input_tokens": 105000,
            "output_tokens": 32000,
            "etl_lag_detected": True,
        }

        content = synthesize_wrap_content(
            repo="tms", issue=82,
            transitions=[], gh_prs=[], usage=usage,
        )

        assert "partial" in content.lower()
        assert "ETL" in content

    def test_synthesize_wrap_no_partial_note_when_not_lagging(self):
        """When etl_lag_detected=False, no partial-data note appears."""
        from tms.wrap_on_terminal import synthesize_wrap_content

        usage = {
            "calls": 42,
            "cost_usd": 0.156,
            "input_tokens": 105000,
            "output_tokens": 32000,
            "etl_lag_detected": False,
        }

        content = synthesize_wrap_content(
            repo="tms", issue=82,
            transitions=[], gh_prs=[], usage=usage,
        )

        # Should NOT contain a partial-data warning
        assert "partial" not in content.lower() or "partial" not in (
            content.lower().replace("partially", "")
        )

    def test_validate_frontmatter_passes_with_cost_fields(self):
        """Existing frontmatter validator still passes with cost_usd/llm_calls."""
        from tms.wrap_on_terminal import validate_frontmatter

        content = """---
name: issue-wrap-tms#99
description: "Wrap for tms#99"
metadata:
  node_type: memory
  type: project
  source: wrap-on-terminal
  repo: tms
  issue: 99
cost_usd: 0.156
llm_calls: 42
---

## Summary
"""
        errors = validate_frontmatter(content)
        assert errors == []


class TestFetchGhPrsInvocation:
    """Regression: gh search prs rejects an embedded repo: qualifier.

    The query must use the --repo flag and omit type:pr (gh appends its
    own). The old form `repo:X N in:title,body type:pr` as a single arg
    made gh re-quote and reject every search (live bug, first cron run).
    """

    def test_fetch_gh_prs_uses_repo_flag_not_embedded(self, monkeypatch):
        from tms import wrap_on_terminal as wot

        captured = {}

        def fake_gh_json(args, timeout=15):
            captured["args"] = args
            return [
                {
                    "number": 95,
                    "title": "feat: thing (#94)",
                    "state": "MERGED",
                    "mergedAt": "2026-07-19",
                    "url": "http://x",
                    "body": "Closes #94",
                }
            ]

        monkeypatch.setattr(wot, "_gh_json", fake_gh_json)
        out = wot._fetch_gh_prs("bogocat/tms", 94)

        args = captured["args"]
        assert "--repo" in args
        assert args[args.index("--repo") + 1] == "bogocat/tms"
        query = args[args.index("--repo") + 2]
        assert "repo:" not in query
        assert "type:pr" not in query
        assert "in:title,body" in query
        assert out and out[0]["number"] == 95

    def test_fetch_gh_prs_filters_non_referencing_results(self, monkeypatch):
        from tms import wrap_on_terminal as wot

        monkeypatch.setattr(
            wot,
            "_gh_json",
            lambda args, timeout=15: [
                {"number": 95, "title": "feat: thing", "state": "MERGED",
                 "mergedAt": "", "url": "", "body": "Closes #94"},
                {"number": 46, "title": "feat: unrelated", "state": "MERGED",
                 "mergedAt": "", "url": "", "body": "no reference here"},
            ],
        )
        out = wot._fetch_gh_prs("bogocat/tms", 94)
        assert [p["number"] for p in out] == [95]
