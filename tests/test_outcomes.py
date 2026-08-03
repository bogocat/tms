"""Tests for the dispatch-outcomes writer (tms#119).

Covers registry parsing, GitHub outcome resolution (mocked gh), the
sync upsert path, terminal-row skipping, dry-run, since-window
bounding, and the end-to-end handoff to compute_stats_by_class (#112).
"""

import datetime
import json

import pytest

from tms import events as events_mod
from tms import outcomes


def _iso_days_ago(days):
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=days)
    ).isoformat()


def _dispatch(repo, issue, aoe, dispatch_type="feature", days_ago=1):
    """Append one dispatch event via the real append path."""
    events_mod.append_event({
        "event_type": "dispatch",
        "timestamp": _iso_days_ago(days_ago),
        "repo": repo, "issue": issue, "agent": "pi",
        "provider": "minimax", "model": "MiniMax-M3",
        "dispatch_type": dispatch_type,
        "worktree": f"/root/wt-{repo}-{issue}",
        "session": f"feat-{repo}#{issue}",
        "aoe_id_prefix": aoe,
    })


def _rows(test_db):
    conn = test_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT aoe_id_prefix, repo, issue, outcome, derived_via, "
        "derived_at, created_at FROM dispatch_outcomes "
        "ORDER BY aoe_id_prefix")
    return cur.fetchall()


@pytest.fixture
def fixed_registry(monkeypatch):
    """Bypass the tmq subprocess with a static short→gh mapping."""
    monkeypatch.setattr(
        outcomes, "load_registry",
        lambda: {"tms": "bogocat/tms",
                 "distillery": "bogocat/distillery"})


# ── Registry parsing ──────────────────────────────────────────────

class TestLoadRegistry:

    def test_parses_tmq_tsv(self, monkeypatch):
        tsv = ("tms\t/root/tms\tbogocat/tms\t1\n"
               "deploy\t/root/projects/distillery\tbogocat/distillery\t0\n"
               "distillery\t/root/projects/distillery\tbogocat/distillery\t1")

        def fake_run(cmd, timeout=15):
            assert cmd == ["tmq", "list", "--machine"]
            return tsv

        monkeypatch.setattr(outcomes, "_run", fake_run)
        reg = outcomes.load_registry()
        assert reg["tms"] == "bogocat/tms"
        # Both shorts map to the same gh repo (short→gh direction).
        assert reg["deploy"] == "bogocat/distillery"
        assert reg["distillery"] == "bogocat/distillery"

    def test_falls_back_to_repo_to_gh_when_tmq_empty(self, monkeypatch):
        monkeypatch.setattr(outcomes, "_run", lambda cmd, timeout=15: "")
        reg = outcomes.load_registry()
        # Retired/unregistered shorts still resolve via the
        # wrap_on_terminal.REPO_TO_GH fallback table.
        assert reg["tms"] == "bogocat/tms"
        assert reg["rms"] == "bogocat/openrms"

    def test_tmq_wins_over_fallback(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_run",
            lambda cmd, timeout=15: "tms\t/root/tms\tbogocat/tms-new\t1")
        reg = outcomes.load_registry()
        assert reg["tms"] == "bogocat/tms-new"

    def test_skips_malformed_rows(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_run",
            lambda cmd, timeout=15: "short-only\nnoise\t\t\t\n"
                                    "ok\t/p\tbogocat/ok\t1")
        reg = outcomes.load_registry()
        assert reg["ok"] == "bogocat/ok"
        assert "short-only" not in reg


# ── Issue outcome resolution (mocked gh GraphQL) ──────────────────

def _issue_data(state, state_reason=None, pr_states=()):
    return {"repository": {"issue": {
        "state": state,
        "stateReason": state_reason,
        "closedByPullRequestsReferences": {
            "nodes": [{"number": 900 + i, "state": s}
                      for i, s in enumerate(pr_states)],
        },
    }}}


class TestResolveIssueOutcome:

    def test_merged_closing_pr(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_gh_graphql",
            lambda q, v=None: _issue_data("CLOSED", "COMPLETED", ["MERGED"]))
        assert outcomes.resolve_issue_outcome("bogocat/tms", 112) == \
            ("merged", "gh_closing_prs")

    def test_merged_wins_even_with_closed_siblings(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_gh_graphql",
            lambda q, v=None: _issue_data("CLOSED", "COMPLETED",
                                  ["CLOSED", "MERGED"]))
        assert outcomes.resolve_issue_outcome("bogocat/tms", 1)[0] == "merged"

    def test_open_issue_no_pr(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_gh_graphql", lambda q, v=None: _issue_data("OPEN"))
        assert outcomes.resolve_issue_outcome("bogocat/tms", 2) == \
            ("open", "gh_closing_prs")

    def test_open_issue_with_open_pr_still_open(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_gh_graphql",
            lambda q, v=None: _issue_data("OPEN", None, ["OPEN"]))
        assert outcomes.resolve_issue_outcome("bogocat/tms", 3)[0] == "open"

    def test_closed_with_unmerged_pr(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_gh_graphql",
            lambda q, v=None: _issue_data("CLOSED", "COMPLETED", ["CLOSED"]))
        assert outcomes.resolve_issue_outcome("bogocat/tms", 4) == \
            ("closed_unmerged", "gh_closing_prs")

    def test_closed_not_planned_no_pr(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_gh_graphql",
            lambda q, v=None: _issue_data("CLOSED", "NOT_PLANNED"))
        assert outcomes.resolve_issue_outcome("bogocat/tms", 5)[0] == \
            "closed_unmerged"

    def test_closed_completed_no_pr_is_unknown(self, monkeypatch):
        """Direct-to-main completion cannot be distinguished from a
        manual close — never fabricate 'merged'."""
        monkeypatch.setattr(
            outcomes, "_gh_graphql",
            lambda q, v=None: _issue_data("CLOSED", "COMPLETED"))
        assert outcomes.resolve_issue_outcome("bogocat/tms", 6)[0] == \
            "unknown"

    def test_gh_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(outcomes, "_gh_graphql", lambda q, v=None: None)
        assert outcomes.resolve_issue_outcome("bogocat/tms", 7) is None

    def test_missing_issue_node_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_gh_graphql",
            lambda q, v=None: {"repository": {"issue": None}})
        assert outcomes.resolve_issue_outcome("bogocat/tms", 8) is None

    def test_malformed_gh_repo_returns_none(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            outcomes, "_gh_graphql", lambda q, v=None: called.append(q))
        assert outcomes.resolve_issue_outcome("no-slash", 9) is None
        assert called == []


# ── PR outcome resolution (review dispatches) ─────────────────────

def _pr_data(state):
    return {"repository": {"pullRequest": {"state": state}}}


class TestResolvePrOutcome:

    @pytest.mark.parametrize("state,expected", [
        ("MERGED", "merged"),
        ("CLOSED", "closed_unmerged"),
        ("OPEN", "open"),
    ])
    def test_state_mapping(self, monkeypatch, state, expected):
        monkeypatch.setattr(
            outcomes, "_gh_graphql", lambda q, v=None: _pr_data(state))
        assert outcomes.resolve_pr_outcome("bogocat/tms", 113) == \
            (expected, "gh_pr_state")

    def test_gh_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(outcomes, "_gh_graphql", lambda q, v=None: None)
        assert outcomes.resolve_pr_outcome("bogocat/tms", 113) is None

    def test_missing_pr_node_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            outcomes, "_gh_graphql",
            lambda q, v=None: {"repository": {"pullRequest": None}})
        assert outcomes.resolve_pr_outcome("bogocat/tms", 113) is None


# ── sync_outcomes ─────────────────────────────────────────────────

class TestSyncOutcomes:

    def test_writes_merged_row(self, test_db, monkeypatch, fixed_registry):
        _dispatch("tms", 100, "aaa11111")
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: ("merged", "gh_closing_prs"))

        summary = outcomes.sync_outcomes()

        rows = _rows(test_db)
        assert len(rows) == 1
        aoe, repo, issue, outcome, via, derived_at, created_at = rows[0]
        assert (aoe, repo, issue, outcome, via) == \
            ("aaa11111", "tms", 100, "merged", "gh_closing_prs")
        assert derived_at and created_at
        assert summary["written"] == 1
        assert summary["resolved"] == 1

    def test_upsert_updates_open_to_merged(self, test_db, monkeypatch,
                                           fixed_registry):
        """Re-sync flips open→merged, updates derived_at, preserves
        created_at (the UPSERT contract)."""
        _dispatch("tms", 100, "aaa11111")
        conn = test_db()
        conn.cursor().execute(
            "INSERT INTO dispatch_outcomes "
            "(aoe_id_prefix, repo, issue, outcome, derived_via, "
            " derived_at, created_at) "
            "VALUES ('aaa11111', 'tms', 100, 'open', 'gh_closing_prs', "
            "        '2020-01-01T00:00:00+00:00', "
            "        '2020-01-01T00:00:00+00:00')")
        conn.commit()

        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: ("merged", "gh_closing_prs"))
        outcomes.sync_outcomes()

        rows = _rows(test_db)
        assert len(rows) == 1
        _, _, _, outcome, _, derived_at, created_at = rows[0]
        assert outcome == "merged"
        assert created_at == "2020-01-01T00:00:00+00:00"
        assert derived_at != "2020-01-01T00:00:00+00:00"

    def test_terminal_rows_skip_github(self, test_db, monkeypatch,
                                       fixed_registry):
        """merged/closed_unmerged rows never trigger a gh call."""
        _dispatch("tms", 100, "aaa11111")
        _dispatch("tms", 101, "bbb22222")
        conn = test_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dispatch_outcomes VALUES "
            "('aaa11111', 'tms', 100, 'merged', 'gh_closing_prs', "
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
        cur.execute(
            "INSERT INTO dispatch_outcomes VALUES "
            "('bbb22222', 'tms', 101, 'closed_unmerged', 'gh_closing_prs', "
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
        conn.commit()

        def _boom(gh, n):
            raise AssertionError("gh must not be called for terminal rows")

        monkeypatch.setattr(outcomes, "resolve_issue_outcome", _boom)
        summary = outcomes.sync_outcomes()
        assert summary["checked"] == 0
        assert summary["skipped_terminal"] == 2

    def test_non_terminal_open_rechecked(self, test_db, monkeypatch,
                                         fixed_registry):
        _dispatch("tms", 100, "aaa11111")
        conn = test_db()
        conn.cursor().execute(
            "INSERT INTO dispatch_outcomes VALUES "
            "('aaa11111', 'tms', 100, 'open', 'gh_closing_prs', "
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
        conn.commit()

        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: ("open", "gh_closing_prs"))
        summary = outcomes.sync_outcomes()
        assert summary["checked"] == 1
        assert summary["written"] == 1

    def test_dry_run_writes_nothing(self, test_db, monkeypatch,
                                    fixed_registry, capsys):
        _dispatch("tms", 100, "aaa11111")
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: ("merged", "gh_closing_prs"))

        summary = outcomes.sync_outcomes(dry_run=True)

        assert _rows(test_db) == []
        assert summary["written"] == 0
        assert summary["resolved"] == 1
        assert "dry-run" in capsys.readouterr().out

    def test_since_window_bounds_lookups(self, test_db, monkeypatch,
                                         fixed_registry):
        _dispatch("tms", 100, "aaa11111", days_ago=60)

        def _boom(gh, n):
            raise AssertionError("dispatch outside window must be skipped")

        monkeypatch.setattr(outcomes, "resolve_issue_outcome", _boom)
        summary = outcomes.sync_outcomes(since_days=30)
        assert summary["checked"] == 0
        assert _rows(test_db) == []

    def test_review_dispatch_uses_pr_resolution(self, test_db, monkeypatch,
                                                fixed_registry):
        """dispatch_type=review: the issue field is a PR number."""
        _dispatch("tms", 113, "ccc33333", dispatch_type="review")

        def _no_issue_path(gh, n):
            raise AssertionError("review dispatch must use the PR path")

        monkeypatch.setattr(outcomes, "resolve_issue_outcome",
                            _no_issue_path)
        monkeypatch.setattr(
            outcomes, "resolve_pr_outcome",
            lambda gh, n: ("merged", "gh_pr_state"))
        outcomes.sync_outcomes()

        rows = _rows(test_db)
        assert rows[0][3] == "merged"
        assert rows[0][4] == "gh_pr_state"

    def test_redispatch_one_gh_call_two_rows(self, test_db, monkeypatch,
                                             fixed_registry):
        """Two dispatches of the same issue share one resolution."""
        _dispatch("tms", 100, "aaa11111", days_ago=2)
        _dispatch("tms", 100, "bbb22222", days_ago=1)
        calls = []

        def _resolve(gh, n):
            calls.append((gh, n))
            return ("merged", "gh_closing_prs")

        monkeypatch.setattr(outcomes, "resolve_issue_outcome", _resolve)
        outcomes.sync_outcomes()

        assert calls == [("bogocat/tms", 100)]
        assert len(_rows(test_db)) == 2

    def test_unresolved_skipped_without_write(self, test_db, monkeypatch,
                                              fixed_registry):
        """gh failure never clobbers prior state with 'unknown'."""
        _dispatch("tms", 100, "aaa11111")
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome", lambda gh, n: None)
        summary = outcomes.sync_outcomes()
        assert summary["skipped_unresolved"] == 1
        assert _rows(test_db) == []

    def test_gh_failure_preserves_prior_terminal_state(
            self, test_db, monkeypatch, fixed_registry):
        """The actual preservation guarantee (review P1-5): a prior
        'merged' row survives a failed re-sync untouched — outcome AND
        created_at."""
        _dispatch("tms", 100, "aaa11111")
        conn = test_db()
        conn.cursor().execute(
            "INSERT INTO dispatch_outcomes "
            "(aoe_id_prefix, repo, issue, outcome, derived_via, "
            " derived_at, created_at) "
            "VALUES ('aaa11111', 'tms', 100, 'merged', 'gh_closing_prs', "
            "        '2026-07-30T00:00:00+00:00', '2026-07-29T00:00:00+00:00')")
        conn.commit()
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome", lambda gh, n: None)

        summary = outcomes.sync_outcomes()

        # Terminal row is skipped before any GitHub call, so nothing is
        # even attempted — and the row is byte-identical afterwards.
        assert summary["skipped_terminal"] == 1
        rows = _rows(test_db)
        assert len(rows) == 1
        assert rows[0][3] == "merged"
        assert rows[0][6] == "2026-07-29T00:00:00+00:00"

    def test_fresh_unknown_not_rechecked_within_window(
            self, test_db, monkeypatch, fixed_registry):
        """P1-2: an 'unknown' derived <24h ago is not re-queried."""
        _dispatch("tms", 100, "aaa11111")
        import datetime as _dt
        fresh = _dt.datetime.now(_dt.timezone.utc).isoformat()
        conn = test_db()
        conn.cursor().execute(
            "INSERT INTO dispatch_outcomes "
            "(aoe_id_prefix, repo, issue, outcome, derived_via, "
            " derived_at, created_at) "
            f"VALUES ('aaa11111', 'tms', 100, 'unknown', 'gh_closing_prs', "
            f"        '{fresh}', '{fresh}')")
        conn.commit()
        calls = []
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: calls.append((gh, n)) or ("unknown", "gh_closing_prs"))

        outcomes.sync_outcomes()
        assert calls == [], "fresh unknown must not trigger a GitHub call"

    def test_stale_unknown_rechecked_after_window(
            self, test_db, monkeypatch, fixed_registry):
        """P1-2: an 'unknown' older than the window IS re-queried."""
        _dispatch("tms", 100, "aaa11111")
        conn = test_db()
        conn.cursor().execute(
            "INSERT INTO dispatch_outcomes "
            "(aoe_id_prefix, repo, issue, outcome, derived_via, "
            " derived_at, created_at) "
            "VALUES ('aaa11111', 'tms', 100, 'unknown', 'gh_closing_prs', "
            "        '2026-07-30T00:00:00+00:00', '2026-07-30T00:00:00+00:00')")
        conn.commit()
        calls = []
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: calls.append((gh, n)) or ("merged", "gh_closing_prs"))

        outcomes.sync_outcomes()
        assert calls == [("bogocat/tms", 100)]
        assert _rows(test_db)[0][3] == "merged"

    def test_corrupt_issue_value_skipped_not_fatal(
            self, test_db, monkeypatch, fixed_registry):
        """Round-4 P1-2: one corrupt event must not abort the sync."""
        _dispatch("tms", 100, "aaa11111")
        conn = test_db()
        conn.cursor().execute(
            "UPDATE events SET issue = NULL, payload = payload "
            "WHERE aoe_id_prefix = 'zzz99999'")
        conn.commit()
        import tms.events as ev
        real_read = ev._read_events_from_db

        def poisoned(since=None):
            rows = real_read(since=since)
            rows.append({"event_type": "dispatch", "aoe_id_prefix": "bad00001",
                         "repo": "tms", "issue": "abc",
                         "dispatch_type": "feature"})
            return rows
        monkeypatch.setattr(ev, "_read_events_from_db", poisoned)
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: ("merged", "gh_closing_prs"))

        summary = outcomes.sync_outcomes()
        assert summary["written"] == 1, "good row must still be processed"
        assert all(r[0] != "bad00001" for r in _rows(test_db))

    def test_load_failure_aborts_sync(self, test_db, monkeypatch,
                                      fixed_registry):
        """Round-4 P1-1: unreadable outcomes table aborts the sync
        (never re-resolves terminal rows blind)."""
        _dispatch("tms", 100, "aaa11111")
        monkeypatch.setattr(
            outcomes, "_load_existing_outcomes", lambda: None)
        calls = []
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: calls.append(1) or ("merged", "x"))

        summary = outcomes.sync_outcomes()
        assert summary.get("load_failed") is True
        assert calls == [], "no GitHub calls after a failed load"
        assert _rows(test_db) == []

    def test_full_closing_pr_page_degrades_to_unknown(
            self, test_db, monkeypatch):
        """Round-4 P1-3: 20 closing-PR refs (== page cap) with no merged
        PR cannot prove closed_unmerged — degrade to re-checkable
        unknown."""
        data = {"repository": {"issue": {
            "state": "CLOSED", "stateReason": "COMPLETED",
            "closedByPullRequestsReferences": {
                "nodes": [{"number": i, "state": "CLOSED"}
                          for i in range(20)]}}}}
        monkeypatch.setattr(
            outcomes, "_gh_graphql", lambda q, v=None: data)
        assert outcomes.resolve_issue_outcome("bogocat/tms", 1) == (
            "unknown", "gh_closing_prs_capped")

    def test_under_cap_closed_page_still_closed_unmerged(
            self, test_db, monkeypatch):
        data = {"repository": {"issue": {
            "state": "CLOSED", "stateReason": "COMPLETED",
            "closedByPullRequestsReferences": {
                "nodes": [{"number": 1, "state": "CLOSED"}]}}}}
        monkeypatch.setattr(
            outcomes, "_gh_graphql", lambda q, v=None: data)
        assert outcomes.resolve_issue_outcome("bogocat/tms", 1) == (
            "closed_unmerged", "gh_closing_prs")

    def test_unmapped_repo_skipped(self, test_db, monkeypatch):
        _dispatch("mystery-repo", 1, "aaa11111")
        monkeypatch.setattr(outcomes, "load_registry", lambda: {})
        summary = outcomes.sync_outcomes()
        assert summary["skipped_unresolved"] == 1
        assert _rows(test_db) == []

    def test_dispatch_without_aoe_prefix_skipped(self, test_db, monkeypatch,
                                                 fixed_registry):
        _dispatch("tms", 100, "")
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: ("merged", "gh_closing_prs"))
        summary = outcomes.sync_outcomes()
        assert summary["checked"] == 0
        assert _rows(test_db) == []

    def test_check_constraint_matches_migration_004(self, test_db):
        """The sqlite shim enforces the migration 004 outcome enum."""
        import sqlite3
        conn = test_db()
        with pytest.raises(sqlite3.IntegrityError):
            conn.cursor().execute(
                "INSERT INTO dispatch_outcomes VALUES "
                "('zzz', 'tms', 1, 'bogus-state', NULL, 't', 't')")


# ── End-to-end: writer feeds --by-class (#112) ────────────────────

class TestFeedsStatsByClass:

    def test_stats_by_class_sees_synced_outcomes(self, test_db, monkeypatch,
                                                 fixed_registry):
        """The whole point: after sync, stats --by-class reports real
        merged/pass_rate numbers instead of structural zero."""
        _dispatch("tms", 100, "aaa11111")
        _dispatch("tms", 101, "bbb22222")

        def _resolve(gh, n):
            return (("merged", "gh_closing_prs") if n == 100
                    else ("open", "gh_closing_prs"))

        monkeypatch.setattr(outcomes, "resolve_issue_outcome", _resolve)
        outcomes.sync_outcomes()

        stats = events_mod.compute_stats_by_class()
        tms_feat = [r for r in stats
                    if r["repo"] == "tms"
                    and r["dispatch_type"] == "feature"][0]
        assert tms_feat["dispatches"] == 2
        assert tms_feat["merged"] == 1
        assert tms_feat["pass_rate"] == pytest.approx(0.5)


# ── CLI ───────────────────────────────────────────────────────────

class TestMain:

    def test_sync_subcommand_prints_summary(self, test_db, monkeypatch,
                                            fixed_registry, capsys):
        import sys as _sys
        _dispatch("tms", 100, "aaa11111")
        monkeypatch.setattr(
            outcomes, "resolve_issue_outcome",
            lambda gh, n: ("merged", "gh_closing_prs"))
        monkeypatch.setattr(_sys, "argv", ["tms.outcomes", "sync"])
        outcomes.main()
        out = capsys.readouterr().out
        assert "1 row(s) written" in out
        assert len(_rows(test_db)) == 1

    def test_since_flag_parsed(self, test_db, monkeypatch, fixed_registry):
        import sys as _sys
        _dispatch("tms", 100, "aaa11111", days_ago=10)
        seen = {}

        def _fake_sync(since_days=30, dry_run=False):
            seen.update(since_days=since_days, dry_run=dry_run)
            return {"checked": 0, "resolved": 0, "written": 0,
                    "skipped_terminal": 0, "skipped_unresolved": 0}

        monkeypatch.setattr(outcomes, "sync_outcomes", _fake_sync)
        monkeypatch.setattr(
            _sys, "argv",
            ["tms.outcomes", "sync", "--since", "7", "--dry-run"])
        outcomes.main()
        assert seen == {"since_days": 7, "dry_run": True}

    def test_unknown_subcommand_exits(self, monkeypatch):
        import sys as _sys
        monkeypatch.setattr(_sys, "argv", ["tms.outcomes", "frobnicate"])
        with pytest.raises(SystemExit):
            outcomes.main()

    def test_bad_since_value_exits(self, monkeypatch):
        import sys as _sys
        monkeypatch.setattr(
            _sys, "argv", ["tms.outcomes", "sync", "--since", "soon"])
        with pytest.raises(SystemExit):
            outcomes.main()
