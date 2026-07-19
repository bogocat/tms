"""Tests for the review-trigger poller (tms#57 Phase 2).

Covers the verdict-contract consumer (parser, sha match, current-PASS
detection) and the scan orchestration (registry dedupe, draft skip,
closed/merged no-op, live-session double-dispatch guard, dispatch +
event logging).

The verdict format is the reviewer-owned contract defined in the
code-review skill (pi-dotfiles). Real-world fixture lines below are
copied from live PRs (tms#69 PASS, tms#64 FAIL, tms#62 minimal).
"""

import json

import pytest

from tms import review_poll


# ── verdict-line fixtures (copied from live PRs) ──────────────────

LIVE_PASS = "<<REVIEW-VERDICT: PASS sha=16e3ead rounds=2 panel=deepseek-v4-pro,minimax-m3,claude-sonnet>>"
LIVE_PASS_FULL = "<<REVIEW-VERDICT: PASS sha=a97975433088e47891ca839709f8e480cf460353>>"
LIVE_FAIL = "<<REVIEW-VERDICT: FAIL sha=043b8958dab4ce34eb406dd3414b37d179e36d54 p0=2 p1=4 rounds=1 panel=deepseek-v4-pro>>"


# ── parse_verdict_line ────────────────────────────────────────────

class TestParseVerdictLine:
    def test_pass_with_full_panel(self):
        v = review_poll.parse_verdict_line(LIVE_PASS)
        assert v is not None
        assert v["state"] == "PASS"
        assert v["sha"] == "16e3ead"
        assert v["rounds"] == 2
        assert v["panel"] == "deepseek-v4-pro,minimax-m3,claude-sonnet"

    def test_pass_minimal(self):
        # Older verdicts (tms#62) carried only state + sha.
        v = review_poll.parse_verdict_line(LIVE_PASS_FULL)
        assert v["state"] == "PASS"
        assert v["sha"] == "a97975433088e47891ca839709f8e480cf460353"
        assert v["rounds"] == 0
        assert v["panel"] == ""

    def test_fail_with_counts(self):
        v = review_poll.parse_verdict_line(LIVE_FAIL)
        assert v["state"] == "FAIL"
        assert v["sha"] == "043b8958dab4ce34eb406dd3414b37d179e36d54"
        assert v["p0"] == 2
        assert v["p1"] == 4
        assert v["rounds"] == 1

    def test_field_order_independent(self):
        # The contract does not fix field order; p0/p1 may precede panel.
        line = "<<REVIEW-VERDICT: FAIL sha=abcdef1 p1=3 p0=1 panel=reviewer,reviewer-m3 rounds=2>>"
        v = review_poll.parse_verdict_line(line)
        assert v["state"] == "FAIL"
        assert v["p0"] == 1
        assert v["p1"] == 3
        assert v["rounds"] == 2
        assert v["panel"] == "reviewer,reviewer-m3"

    def test_embedded_in_comment_body(self):
        # The verdict line is the LAST line of a review comment body.
        body = "## Code Review\n\nLooks good.\n\n" + LIVE_PASS + "\n"
        v = review_poll.parse_verdict_line(body)
        assert v is not None
        assert v["state"] == "PASS"
        assert v["sha"] == "16e3ead"

    def test_none_for_plain_text(self):
        assert review_poll.parse_verdict_line("## Code Review\n\nNo issues found.") is None
        assert review_poll.parse_verdict_line("") is None
        assert review_poll.parse_verdict_line(None) is None

    def test_none_for_lookalike(self):
        # A deliberate non-match: wrong marker name.
        assert review_poll.parse_verdict_line(
            "<<REVIEW-VERDICT-DRAFT: PASS sha=deadbeef>>"
        ) is None


# ── sha_matches ───────────────────────────────────────────────────

class TestShaMatches:
    def test_short_matches_full_prefix(self):
        # Verdict posted a short sha; PR head is the full 40-char oid.
        assert review_poll.sha_matches("16e3ead", "16e3ead7bb5b2303157e01817f2816004d5ae11a")

    def test_full_matches_full(self):
        full = "a97975433088e47891ca839709f8e480cf460353"
        assert review_poll.sha_matches(full, full)

    def test_full_verdict_short_head(self):
        # Verdict full sha, head reported short — reverse prefix.
        assert review_poll.sha_matches("16e3ead7bb5b2303157e01817f2816004d5ae11a", "16e3ead")

    def test_mismatch(self):
        assert not review_poll.sha_matches("16e3ead", "043b8958dab4ce34eb406dd3414b37d179e36d54")

    def test_empty(self):
        assert not review_poll.sha_matches("", "16e3ead")
        assert not review_poll.sha_matches("16e3ead", "")

    def test_case_insensitive(self):
        assert review_poll.sha_matches("16E3EAD", "16e3ead7bb5b2303157e01817f2816004d5ae11a")


# ── latest_verdict ────────────────────────────────────────────────

class TestLatestVerdict:
    def test_returns_none_when_no_verdict(self):
        assert review_poll.latest_verdict(["## Code Review\n\nLooks good."]) is None
        assert review_poll.latest_verdict([]) is None

    def test_single_verdict(self):
        v = review_poll.latest_verdict(["body\n" + LIVE_PASS])
        assert v["state"] == "PASS"
        assert v["sha"] == "16e3ead"

    def test_newest_verdict_wins(self):
        # Comments are chronological (oldest first). A FAIL then a PASS
        # (second review round) → the most recent PASS is authoritative.
        comments = [
            "round 1\n" + LIVE_FAIL,
            "round 2\n" + LIVE_PASS,
        ]
        v = review_poll.latest_verdict(comments)
        assert v["state"] == "PASS"
        assert v["sha"] == "16e3ead"

    def test_verdict_in_middle_then_none_after(self):
        comments = ["round 1\n" + LIVE_PASS, "a follow-up comment with no verdict"]
        v = review_poll.latest_verdict(comments)
        assert v["state"] == "PASS"

    def test_multiple_bodies_last_with_verdict(self):
        # The most recent verdict-bearing comment wins regardless of position.
        comments = ["noise", "noise", LIVE_FAIL, "noise"]
        v = review_poll.latest_verdict(comments)
        assert v["state"] == "FAIL"


# ── has_current_pass ──────────────────────────────────────────────

class TestHasCurrentPass:
    HEAD = "16e3ead7bb5b2303157e01817f2816004d5ae11a"

    def test_pass_matching_sha(self):
        assert review_poll.has_current_pass([LIVE_PASS], self.HEAD)

    def test_fail_verdict_is_not_current_pass(self):
        fail_for_head = "<<REVIEW-VERDICT: FAIL sha=" + self.HEAD + " p0=1 rounds=1 panel=x>>"
        assert not review_poll.has_current_pass([fail_for_head], self.HEAD)

    def test_pass_wrong_sha_is_stale(self):
        # PASS but for an older commit (code pushed after review).
        stale = "<<REVIEW-VERDICT: PASS sha=deadbeefdeadbeef rounds=1 panel=x>>"
        assert not review_poll.has_current_pass([stale], self.HEAD)

    def test_no_verdict(self):
        assert not review_poll.has_current_pass(["no verdict here"], self.HEAD)


# ── needs_poller_review (V1 policy: no verdict at all) ────────────

class TestNeedsPollerReview:
    """V1 scope (operator-approved 2026-07-13): the poller dispatches only
    for PRs with ZERO verdict comments. FAIL / stale-PASS PRs are skipped —
    the author agent owns that re-review loop (avoids racing the author's
    own self-trigger)."""

    HEAD = "16e3ead7bb5b2303157e01817f2816004d5ae11a"

    def test_no_verdict_needs_review(self):
        assert review_poll.needs_poller_review(["no verdict here"], self.HEAD)

    def test_pass_verdict_skipped(self):
        assert not review_poll.needs_poller_review([LIVE_PASS], self.HEAD)

    def test_fail_verdict_skipped(self):
        # Author owns the fix→re-review loop, not the poller.
        assert not review_poll.needs_poller_review([LIVE_FAIL], self.HEAD)

    def test_stale_pass_skipped(self):
        stale = "<<REVIEW-VERDICT: PASS sha=deadbeefdeadbeef rounds=1 panel=x>>"
        assert not review_poll.needs_poller_review([stale], self.HEAD)

    def test_empty_comments_needs_review(self):
        assert review_poll.needs_poller_review([], self.HEAD)


# ── _repo_registry dedupe ─────────────────────────────────────────

class TestRepoRegistryDedupe:
    def test_dedupe_by_gh_repo(self, monkeypatch):
        # deploy and distillery both map to bogocat/distillery.
        # The poller now prefers the worktree=1 entry (distillery) over
        # deploy (worktree=0) so live-session keys match correctly.
        monkeypatch.setattr(
            review_poll, "_run_tmq_list_machine",
            lambda: "deploy\t/root/deploy/distillery\tbogocat/distillery\t0\n"
                    "distillery\t/root/projects/distillery\tbogocat/distillery\t1\n"
                    "tms\t/root/tms\tbogocat/tms\t1\n",
        )
        repos = review_poll._repo_registry()
        gh_repos = [gh for (_short, gh) in repos]
        assert gh_repos.count("bogocat/distillery") == 1
        assert "bogocat/tms" in gh_repos
        # The retained entry for distillery must be 'distillery' (worktree=1),
        # not 'deploy'.
        short_names = {short for (short, gh) in repos if gh == "bogocat/distillery"}
        assert short_names == {"distillery"}

    def test_filter_by_short_name(self, monkeypatch):
        monkeypatch.setattr(
            review_poll, "_run_tmq_list_machine",
            lambda: "tms\t/root/tms\tbogocat/tms\t1\n"
                    "distillery\t/root/projects/distillery\tbogocat/distillery\t1\n",
        )
        repos = review_poll._repo_registry(repo_filter="tms")
        assert repos == [("tms", "bogocat/tms")]


# ── list_open_prs (closed/merged excluded by construction) ────────

class TestListOpenPrs:
    def test_uses_state_open(self, monkeypatch):
        captured = {}

        def fake_gh(args, timeout=15):
            # _gh_json is monkeypatched to return already-parsed data.
            captured["args"] = args
            return [{"number": 57, "headRefOid": "abc123", "isDraft": False}]

        monkeypatch.setattr(review_poll, "_gh_json", fake_gh)
        prs = review_poll.list_open_prs("bogocat/tms")
        # The --state flag MUST be open (AC4: closed/merged no-op).
        assert "--state" in captured["args"]
        assert "open" in captured["args"]
        assert len(prs) == 1
        assert prs[0]["number"] == 57

    def test_empty_repo(self, monkeypatch):
        monkeypatch.setattr(review_poll, "_gh_json", lambda *a, **k: [])
        assert review_poll.list_open_prs("bogocat/empty") == []


# ── live_review_sessions (double-dispatch guard) ──────────────────

class TestLiveReviewSessions:
    def test_detects_review_sessions(self, monkeypatch):
        # aoe session titled review-tms#57 + a feat session → only the
        # review session counts.
        monkeypatch.setattr(
            review_poll, "_run_aoe_list_json",
            lambda: [
                {"id": "abcdef1234567890", "title": "review-tms#57", "path": "/root/wt-tms-57", "tool": "pi"},
                {"id": "fedcba9876543210", "title": "feat-tms#57", "path": "/root/wt-tms-57", "tool": "pi"},
            ],
        )
        monkeypatch.setattr(review_poll, "_tmux_session_names", lambda: [])
        live = review_poll.live_review_sessions()
        assert "tms#57" in live
        # feat sessions must NOT block a review dispatch (different loop).
        assert live == {"tms#57"}

    def test_tmq_named_session_also_detected(self, monkeypatch):
        # Fallback tmux session named review-distillery#547-oc.
        monkeypatch.setattr(review_poll, "_run_aoe_list_json", lambda: [])
        monkeypatch.setattr(review_poll, "_tmux_session_names", lambda: ["review-distillery#547-oc"])
        live = review_poll.live_review_sessions()
        assert "distillery#547" in live

    def test_no_sessions(self, monkeypatch):
        monkeypatch.setattr(review_poll, "_run_aoe_list_json", lambda: [])
        monkeypatch.setattr(review_poll, "_tmux_session_names", lambda: [])
        assert review_poll.live_review_sessions() == set()

    def test_descriptive_titles_ignored(self, monkeypatch):
        # A session titled "tms issue filing" must not be treated as a
        # review session (mirrors session_map.py P1#3 fix).
        monkeypatch.setattr(
            review_poll, "_run_aoe_list_json",
            lambda: [{"id": "12345678abcdef00", "title": "tms issue filing", "path": "/root/tms", "tool": "pi"}],
        )
        monkeypatch.setattr(review_poll, "_tmux_session_names", lambda: [])
        assert review_poll.live_review_sessions() == set()


# ── scan_repos orchestration ──────────────────────────────────────

class TestScanRepos:
    HEAD = "16e3ead7bb5b2303157e01817f2816004d5ae11a"

    def _setup_single_repo(self, monkeypatch, prs, comments_by_pr, live=None):
        """Wire a single repo (tms) into the scan with controllable PRs."""
        monkeypatch.setattr(
            review_poll, "_run_tmq_list_machine",
            lambda: "tms\t/root/tms\tbogocat/tms\t1\n",
        )
        monkeypatch.setattr(review_poll, "list_open_prs", lambda gh: prs)
        monkeypatch.setattr(review_poll, "_pr_comment_bodies", lambda gh, num: (comments_by_pr.get(num, []), True))
        monkeypatch.setattr(review_poll, "live_review_sessions", lambda: set(live or []))
        # Default: PRs are still open. The orphan-guard test overrides this.
        monkeypatch.setattr(review_poll, "_pr_still_open", lambda gh, num: True)

    def test_dry_run_finds_pr_needing_review(self, monkeypatch):
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert len(results) == 1
        assert results[0]["status"] == "would_dispatch"
        assert results[0]["repo"] == "tms"
        assert results[0]["pr"] == 57

    def test_dry_run_skips_pr_with_pass(self, monkeypatch):
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: [LIVE_PASS]},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "skip_current_pass"

    def test_dry_run_skips_pr_with_fail(self, monkeypatch):
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: [LIVE_FAIL]},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "skip_fail"

    def test_dry_run_skips_draft(self, monkeypatch):
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": True}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "skip_draft"

    def test_skip_live_review_session(self, monkeypatch):
        # AC4: do not double-dispatch while a review session is live.
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: []},
            live=["tms#57"],
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "skip_live_session"

    def test_dispatch_calls_tmq_review_and_logs_event(self, monkeypatch, test_db):
        dispatched = []
        monkeypatch.setattr(review_poll, "_dispatch_review",
                            lambda repo, pr: dispatched.append((repo, pr)) or True)
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=True)
        assert results[0]["status"] == "dispatched"
        assert dispatched == [("tms", 57)]
        # AC5: the dispatch is logged to the #53 event log.
        conn = test_db()
        rows = conn.cursor().execute(
            "SELECT event_type, repo, issue, dispatch_type FROM events"
        ).fetchall()
        assert any(r[0] == "dispatch" and r[1] == "tms" and r[2] == 57
                   and r[3] == "review" for r in rows)

    def test_dispatch_skipped_when_live_session(self, monkeypatch, test_db):
        # Even in dispatch mode, a live review session prevents dispatch.
        dispatched = []
        monkeypatch.setattr(review_poll, "_dispatch_review",
                            lambda repo, pr: dispatched.append((repo, pr)) or True)
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: []},
            live=["tms#57"],
        )
        results = review_poll.scan_repos(dispatch=True)
        assert results[0]["status"] == "skip_live_session"
        assert dispatched == []

    def test_orphan_guard_closed_before_dispatch(self, monkeypatch, test_db):
        # PR was open at scan time but closed between scan and dispatch.
        # _pr_still_open returns False → no dispatch, status skip_closed.
        dispatched = []
        monkeypatch.setattr(review_poll, "_dispatch_review",
                            lambda repo, pr: dispatched.append((repo, pr)) or True)
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: []},
        )
        # Override the helper's default (open) AFTER setup so this simulates
        # a close between scan and dispatch.
        monkeypatch.setattr(review_poll, "_pr_still_open", lambda gh, num: False)
        results = review_poll.scan_repos(dispatch=True)
        assert results[0]["status"] == "skip_closed"
        assert dispatched == []

    def test_repo_filter_restricts_scan(self, monkeypatch):
        monkeypatch.setattr(
            review_poll, "_run_tmq_list_machine",
            lambda: "tms\t/root/tms\tbogocat/tms\t1\n"
                    "distillery\t/root/projects/distillery\tbogocat/distillery\t1\n",
        )
        seen_repos = []

        def fake_list_open_prs(gh):
            seen_repos.append(gh)
            return []

        monkeypatch.setattr(review_poll, "list_open_prs", fake_list_open_prs)
        review_poll.scan_repos(dispatch=False, repo_filter="tms")
        assert seen_repos == ["bogocat/tms"]

    def test_max_dispatch_caps_dispatches_per_run(self, monkeypatch, test_db):
        # Safety: the poller must not dispatch an unbounded burst on the
        # first run against a large backlog. ``max_dispatch`` caps the
        # number of dispatches per scan; the rest stay would_dispatch.
        dispatched = []
        monkeypatch.setattr(review_poll, "_dispatch_review",
                            lambda repo, pr: dispatched.append((repo, pr)) or True)
        self._setup_single_repo(monkeypatch,
            prs=[
                {"number": 10, "headRefOid": "aaa", "isDraft": False},
                {"number": 11, "headRefOid": "bbb", "isDraft": False},
                {"number": 12, "headRefOid": "ccc", "isDraft": False},
                {"number": 13, "headRefOid": "ddd", "isDraft": False},
            ],
            comments_by_pr={},  # no verdicts → all need review
        )
        results = review_poll.scan_repos(dispatch=True, max_dispatch=2)
        assert len(dispatched) == 2
        statuses = [r["status"] for r in results]
        assert statuses.count("dispatched") == 2
        # The overflow stays would_dispatch (not lost — next run picks them up).
        assert statuses.count("would_dispatch") == 2

    def test_max_dispatch_zero_disables_dispatch(self, monkeypatch, test_db):
        # max_dispatch=0 → scan-only even in dispatch mode (operational kill switch).
        monkeypatch.setattr(
            review_poll, "_dispatch_review",
            lambda repo, pr: pytest.fail("should not dispatch when max_dispatch=0"),
        )
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 10, "headRefOid": "aaa", "isDraft": False}],
            comments_by_pr={},
        )
        results = review_poll.scan_repos(dispatch=True, max_dispatch=0)
        assert results[0]["status"] == "would_dispatch"

    def test_gh_error_skips_comment_fetch_failure(self, monkeypatch):
        # gh error must not be treated as affirmative no-verdict evidence.
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={},
        )
        monkeypatch.setattr(review_poll, "_pr_comment_bodies",
                            lambda gh, num: ([], False))
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "skip_gh_error"

    def test_dispatch_failure_reported_not_counted(self, monkeypatch, test_db):
        # A failed dispatch must not be counted as dispatched (P0.2).
        monkeypatch.setattr(review_poll, "_dispatch_review",
                            lambda repo, pr: False)
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=True, max_dispatch=2)
        assert results[0]["status"] == "skip_dispatch_failed"
        conn = test_db()
        rows = conn.cursor().execute(
            "SELECT count(*) FROM events"
        ).fetchall()
        assert rows[0][0] == 0  # no event logged on failure

    def test_live_session_recheck_after_snapshot(self, monkeypatch, test_db):
        # P0.3: a review session starting after the initial snapshot
        # must be caught by the pre-dispatch re-check.
        calls = []
        monkeypatch.setattr(review_poll, "_dispatch_review",
                            lambda repo, pr: True)
        monkeypatch.setattr(review_poll, "_log_poller_dispatch",
                            lambda repo, issue: None)
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: []},
        )
        def multi_phase_live():
            calls.append(1)
            return {"tms#57"} if len(calls) >= 2 else set()
        monkeypatch.setattr(review_poll, "live_review_sessions", multi_phase_live)
        results = review_poll.scan_repos(dispatch=True)
        assert results[0]["status"] == "skip_live_session"
        assert len(calls) >= 2  # re-check fired

    def test_source_poller_in_payload(self, monkeypatch, test_db):
        # Dispatch events from the poller must carry source='poller' (AC5).
        import json
        monkeypatch.setattr(review_poll, "_dispatch_review",
                            lambda repo, pr: True)
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: []},
        )
        review_poll.scan_repos(dispatch=True)
        conn = test_db()
        rows = conn.cursor().execute(
            "SELECT payload FROM events"
        ).fetchall()
        payload = json.loads(rows[0][0])
        assert payload.get("source") == "poller"

    def test_stale_pass_skip_in_scan(self, monkeypatch):
        # A stale PASS (sha != head) must be classified as skip_stale_pass.
        stale = "<<REVIEW-VERDICT: PASS sha=deadbeefdeadbeef rounds=1 panel=x>>"
        self._setup_single_repo(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False}],
            comments_by_pr={57: [stale]},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "skip_stale_pass"


# ── is_pr_stale (staleness filter, tms#91) ───────────────────────

class TestIsPrStale:
    def test_recent_pr_not_stale(self):
        # A PR updated 1 hour ago is not stale.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        recent = (now - datetime.timedelta(hours=1)).isoformat()
        assert not review_poll.is_pr_stale(recent, days=14)

    def test_old_pr_is_stale(self):
        # A PR updated 15 days ago IS stale at 14-day threshold.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        old = (now - datetime.timedelta(days=15)).isoformat()
        assert review_poll.is_pr_stale(old, days=14)

    def test_exactly_at_threshold_not_stale(self):
        # A PR updated exactly 14 days ago is NOT stale (threshold is >14).
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        at_threshold = (now - datetime.timedelta(days=14)).isoformat()
        assert not review_poll.is_pr_stale(at_threshold, days=14)

    def test_none_or_empty_updated_at(self):
        # Missing updatedAt is treated as not stale (fail-open).
        assert not review_poll.is_pr_stale(None, days=14)
        assert not review_poll.is_pr_stale("", days=14)

    def test_custom_threshold(self):
        # The days parameter is configurable.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        old_7 = (now - datetime.timedelta(days=8)).isoformat()
        assert review_poll.is_pr_stale(old_7, days=7)
        assert not review_poll.is_pr_stale(old_7, days=14)


# ── scan_repos staleness skip (tms#91) ────────────────────────────

class TestScanReposStaleness:
    HEAD = "16e3ead7bb5b2303157e01817f2816004d5ae11a"

    def _setup(self, monkeypatch, prs, comments_by_pr, live=None):
        monkeypatch.setattr(
            review_poll, "_run_tmq_list_machine",
            lambda: "tms\t/root/tms\tbogocat/tms\t1\n",
        )
        monkeypatch.setattr(review_poll, "list_open_prs", lambda gh: prs)
        monkeypatch.setattr(review_poll, "_pr_comment_bodies",
                            lambda gh, num: (comments_by_pr.get(num, []), True))
        monkeypatch.setattr(review_poll, "live_review_sessions",
                            lambda: set(live or []))
        monkeypatch.setattr(review_poll, "_pr_still_open",
                            lambda gh, num: True)

    def test_skip_stale_pr_no_verdict(self, monkeypatch):
        # A PR with no verdict but updated >14 days ago → skip_stale.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_date = (now - datetime.timedelta(days=20)).isoformat()
        self._setup(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False,
                   "updatedAt": stale_date}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "skip_stale"

    def test_fresh_pr_not_skipped(self, monkeypatch):
        # A PR with no verdict updated recently → would_dispatch (not stale).
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        fresh_date = (now - datetime.timedelta(hours=2)).isoformat()
        self._setup(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False,
                   "updatedAt": fresh_date}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "would_dispatch"

    def test_stale_checked_before_verdict(self, monkeypatch):
        # Staleness check runs BEFORE verdict check — a stale PR is
        # skip_stale even if it also has a verdict.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_date = (now - datetime.timedelta(days=30)).isoformat()
        self._setup(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False,
                   "updatedAt": stale_date}],
            comments_by_pr={57: [
                "<<REVIEW-VERDICT: FAIL sha=abc p0=2 rounds=1 panel=x>>"
            ]},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "skip_stale"

    def test_missing_updated_at_fail_open(self, monkeypatch):
        # A PR without updatedAt in the JSON is treated as NOT stale
        # (fail-open: don't starve new PRs because of a gh API quirk).
        self._setup(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False,
                   "updatedAt": None}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "would_dispatch"

    def test_keep_warm_label_exempts_from_staleness(self, monkeypatch):
        # A stale PR labelled keep-warm is NOT skipped — exemption.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_date = (now - datetime.timedelta(days=30)).isoformat()
        self._setup(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False,
                   "updatedAt": stale_date,
                   "labels": [{"name": "keep-warm"}]}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "would_dispatch"

    def test_keep_warm_case_insensitive(self, monkeypatch):
        # Label matching is case-insensitive.
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_date = (now - datetime.timedelta(days=30)).isoformat()
        self._setup(monkeypatch,
            prs=[{"number": 57, "headRefOid": self.HEAD, "isDraft": False,
                   "updatedAt": stale_date,
                   "labels": [{"name": "KEEP-WARM"}]}],
            comments_by_pr={57: []},
        )
        results = review_poll.scan_repos(dispatch=False)
        assert results[0]["status"] == "would_dispatch"
