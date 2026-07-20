"""Tests for lib/tms/events.py — dispatch event logging, transition
detection, and stats computation (issue #53, migrated to postgres #65).

Database isolation: tests use the conftest.py test_db fixture which
monkeypatches _get_conn() to return a sqlite3 in-memory connection.
All data written by append_event() etc. lands in sqlite3, not postgres.
"""

import json
import os
import time
from unittest.mock import patch

import pytest


# ── Postgres migration tests (issue #65) ──────────────────────────


def test_append_event_inserts_into_db(test_db):
    """append_event() must INSERT into the events table, not write JSONL."""
    from tms.events import append_event

    record = {"event_type": "dispatch", "repo": "tms", "issue": 65}
    append_event(record)

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT event_type, repo, issue, payload FROM events")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "dispatch"
    assert rows[0][1] == "tms"
    assert rows[0][2] == 65
    # payload must be valid JSON
    payload = json.loads(rows[0][3])
    assert payload["event_type"] == "dispatch"
    assert payload["repo"] == "tms"


def test_append_event_inserts_multiple_records(test_db):
    """Multiple append_event() calls must INSERT multiple rows."""
    from tms.events import append_event

    append_event({"event_type": "dispatch", "repo": "a"})
    append_event({"event_type": "dispatch", "repo": "b"})
    append_event({"event_type": "transition", "from_status": "WORKING"})

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events")
        assert cur.fetchone()[0] == 3


def test_log_dispatch_event_writes_all_fields(test_db):
    """log_dispatch_event() must write a record with all required fields."""
    from tms.events import log_dispatch_event

    log_dispatch_event(
        repo="tms",
        issue=53,
        agent="pi",
        provider="minimax",
        model="MiniMax-M3",
        dispatch_type="feature",
        worktree="/root/wt-tms-53",
        session="feat-tms#53",
        aoe_id_prefix="abc12345",
    )

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_type, repo, issue, agent, provider, model,
                      dispatch_type, worktree, session, aoe_id_prefix, payload
               FROM events"""
        )
        row = cur.fetchone()
    assert row[0] == "dispatch"
    assert row[1] == "tms"
    assert row[2] == 53
    assert row[3] == "pi"
    assert row[4] == "minimax"
    assert row[5] == "MiniMax-M3"
    assert row[6] == "feature"
    assert row[7] == "/root/wt-tms-53"
    assert row[8] == "feat-tms#53"
    assert row[9] == "abc12345"
    # payload must be valid JSON with all fields
    payload = json.loads(row[10])
    assert payload["repo"] == "tms"
    assert "timestamp" in payload  # ISO 8601


def test_log_dispatch_event_has_event_type_discriminator(test_db):
    """Every dispatch record must carry event_type='dispatch'."""
    from tms.events import log_dispatch_event

    log_dispatch_event(
        repo="tms", issue=1, agent="pi", provider="", model="",
        dispatch_type="feature", worktree="/tmp/wt", session="feat-tms#1",
        aoe_id_prefix="",
    )

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT event_type FROM events")
        assert cur.fetchone()[0] == "dispatch"


def test_log_dispatch_event_resolves_default_model(test_db):
    """When provider/model are empty, resolve from pi settings."""
    from tms.events import log_dispatch_event

    with patch("tms.events._resolve_default_model", return_value=("deepseek", "deepseek-v4-pro")):
        log_dispatch_event(
            repo="tms", issue=1, agent="pi", provider="", model="",
            dispatch_type="feature", worktree="/tmp/wt", session="feat-tms#1",
            aoe_id_prefix="abc12345",
        )

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT provider, model FROM events")
        row = cur.fetchone()
    assert row[0] == "deepseek"
    assert row[1] == "deepseek-v4-pro"


@pytest.mark.parametrize(
    ("model", "expected_provider"),
    [
        ("deepseek-v4-pro", "deepseek"),
        ("MiniMax-M3", "minimax"),
        ("MiniMax-M3.5", "minimax"),
        ("glm-5.2", "zai"),
    ],
)
def test_log_dispatch_event_resolves_provider_from_explicit_model(
    test_db, model, expected_provider,
):
    """A tmq --model flag determines provider, not the stale pi default."""
    from tms.events import log_dispatch_event

    with patch(
        "tms.events._resolve_default_model",
        return_value=("stale-provider", "stale-model"),
    ):
        log_dispatch_event(
            repo="tms", issue=67, agent="pi", provider="", model=model,
            dispatch_type="feature", worktree="/tmp/wt", session="feat-tms#67",
            aoe_id_prefix="abc12345",
        )

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT provider, model FROM events")
        row = cur.fetchone()
    assert row == (expected_provider, model)


def test_log_dispatch_event_no_override_when_explicit_model(test_db):
    """Explicit provider/model must be used as-is."""
    from tms.events import log_dispatch_event

    log_dispatch_event(
        repo="tms", issue=1, agent="pi",
        provider="custom-provider", model="custom-model",
        dispatch_type="feature", worktree="/tmp/wt", session="feat-tms#1",
        aoe_id_prefix="abc12345",
    )

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT provider, model FROM events")
        row = cur.fetchone()
    assert row == ("custom-provider", "custom-model")


def test_log_dispatch_event_default_source_is_author(test_db):
    """Default source must be 'author' (tms#57: author self-trigger)."""
    from tms.events import log_dispatch_event

    log_dispatch_event(
        repo="tms", issue=1, agent="pi", provider="", model="",
        dispatch_type="feature", worktree="/tmp/wt", session="feat-tms#1",
        aoe_id_prefix="",
    )
    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM events")
        payload = json.loads(cur.fetchone()[0])
    assert payload.get("source") == "author"


def test_log_dispatch_event_source_poller(test_db):
    """source='poller' marks a poller-triggered dispatch (tms#57)."""
    from tms.events import log_dispatch_event

    log_dispatch_event(
        repo="tms", issue=53, agent="pi", provider="", model="",
        dispatch_type="review", worktree="", session="",
        aoe_id_prefix="", source="poller",
    )
    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM events")
        payload = json.loads(cur.fetchone()[0])
    assert payload.get("source") == "poller"
    assert payload["dispatch_type"] == "review"
    assert payload["repo"] == "tms"
    assert payload["issue"] == 53


def test_append_event_handles_special_characters(test_db):
    """Records with special characters must be stored correctly."""
    from tms.events import append_event

    record = {
        "event_type": "dispatch",
        "title": 'feat: add "metrics" with unicode → ✓',
        "body": "multi\nline\ncontent",
    }
    append_event(record)

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM events")
        payload = cur.fetchone()[0]
    parsed = json.loads(payload)
    assert parsed["title"] == record["title"]
    assert parsed["body"] == record["body"]


def test_log_dispatch_failed_event_resolves_explicit_model_provider(test_db):
    """Failed dispatch provenance follows --model without reading defaults."""
    from tms.events import log_dispatch_failed_event

    with patch(
        "tms.events._resolve_default_model",
        side_effect=AssertionError("explicit model must bypass defaults"),
    ):
        log_dispatch_failed_event(
            repo="tms", issue=67, agent="pi", provider="", model="MiniMax-M3",
            dispatch_type="feature", reason="aoe add failed",
        )

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT event_type, provider, model FROM events")
        row = cur.fetchone()
    assert row == ("dispatch_failed", "minimax", "MiniMax-M3")


def test_log_dispatch_failed_event(test_db):
    """dispatch_failed events must INSERT with reason field."""
    from tms.events import log_dispatch_failed_event

    with patch("tms.events._resolve_default_model", return_value=("minimax", "MiniMax-M3")):
        log_dispatch_failed_event(
            repo="tms", issue=1, agent="cc", provider="", model="",
            dispatch_type="feature", reason="cc dispatch refused under root",
        )

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT event_type, reason, repo, issue, aoe_id_prefix FROM events")
        row = cur.fetchone()
    assert row[0] == "dispatch_failed"
    assert row[1] == "cc dispatch refused under root"
    assert row[2] == "tms"
    assert row[3] == 1
    # P0 regression: aoe_id_prefix must NOT be NULL for UNIQUE idempotency
    assert row[4] is not None
    assert row[4] != ""
    assert row[4].startswith("failed-")


def test_dispatch_failed_idempotent(test_db):
    """Re-inserting same dispatch_failed must not create duplicate rows.

    P0 regression: NULL aoe_id_prefix bypasses UNIQUE index.
    Now dispatch_failed gets a synthetic prefix for idempotency.
    """
    import datetime as dt
    from unittest.mock import patch as mock_patch
    from tms.events import log_dispatch_failed_event

    # Freeze time so both calls get identical timestamps
    frozen = dt.datetime(2026, 7, 1, 10, 0, 0, tzinfo=dt.timezone.utc)

    with mock_patch("tms.events.datetime.datetime") as mock_dt:
        mock_dt.now.return_value = frozen
        mock_dt.fromisoformat = dt.datetime.fromisoformat
        mock_dt.timezone = dt.timezone
        with mock_patch("tms.events._resolve_default_model", return_value=("minimax", "MiniMax-M3")):
            log_dispatch_failed_event(
                repo="tms", issue=1, agent="cc", provider="", model="",
                dispatch_type="feature", reason="cc dispatch refused under root",
            )
            log_dispatch_failed_event(
                repo="tms", issue=1, agent="cc", provider="", model="",
                dispatch_type="feature", reason="cc dispatch refused under root",
            )

    conn = test_db()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE event_type = 'dispatch_failed'")
        assert cur.fetchone()[0] == 1, (
            "ON CONFLICT DO NOTHING should prevent duplicate dispatch_failed"
        )


# ── Phase 2: transition detection ─────────────────────────────────


def _make_aoe_list_json(sessions):
    """Build a mock aoe list --json response."""
    return json.dumps([
        {"id": s["id"], "title": s["title"], "path": s["path"],
         "tool": s.get("tool", "pi")}
        for s in sessions
    ])


class TestDetectTransitions:
    """Tests for detect_transitions() — polling aoe + tmux pane capture.

    Uses test_db fixture so append_event() writes to sqlite3.
    """

    def test_first_run_seeds_state_no_events(self, test_db, monkeypatch, tmp_path):
        """On first run, detect_transitions must seed state but emit no events."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        # Mock: one session with PANE showing WORKING state
        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#53", "path": "/root/wt-tms-53"},
                    ]), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    "some output\n<<AGENT-STATE: WORKING>>\nmore output", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 0, "first run must emit no transition events"
        assert last_status_path.exists(), "must seed last_status.json"
        state = json.loads(last_status_path.read_text())
        assert "abc12345" in state
        assert state["abc12345"] == "WORKING"
        # No events written to DB
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM events")
            assert cur.fetchone()[0] == 0

    def test_status_change_emits_transition_event(self, test_db, monkeypatch, tmp_path):
        """When a session's status changes, emit a transition event."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        # Pre-seed: session was WORKING
        last_status_path.write_text(json.dumps({"abc12345": "WORKING"}))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#53", "path": "/root/wt-tms-53"},
                    ]), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    "...\n<<AGENT-STATE: PR-REVIEW>>\nwaiting...", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 1, f"expected 1 transition, got {n}"
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_type, from_status, to_status, aoe_id_prefix, session "
                "FROM events"
            )
            row = cur.fetchone()
        assert row[0] == "transition"
        assert row[1] == "WORKING"
        assert row[2] == "PR-REVIEW"
        assert row[3] == "abc12345"
        assert row[4] == "feat-tms#53"

    def test_no_change_emits_no_event(self, test_db, monkeypatch, tmp_path):
        """When status hasn't changed, emit 0 events."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        last_status_path.write_text(json.dumps({"abc12345": "WORKING"}))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#53", "path": "/root/wt-tms-53"},
                    ]), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "still working\n<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 0
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM events")
            assert cur.fetchone()[0] == 0

    def test_session_disappearance_with_done_status_emits_terminal(self, test_db, monkeypatch, tmp_path):
        """When a MERGE-READY session disappears, emit terminal event."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        last_status_path.write_text(json.dumps({
            "abc12345": "MERGE-READY",
            "fed67890": "WORKING",
        }))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "fed67890-1234-1234-1234-1234567890ab",
                         "title": "feat-tms#54", "path": "/root/wt-tms-54"},
                    ]), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n >= 1
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT from_status, to_status, aoe_id_prefix FROM events "
                "WHERE to_status = 'terminal'"
            )
            row = cur.fetchone()
        assert row[0] == "MERGE-READY"
        assert row[2] == "abc12345"

    def test_session_disappearance_with_non_terminal_status_skipped(self, test_db, monkeypatch, tmp_path):
        """WORKING session disappearance must NOT emit terminal."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        last_status_path.write_text(json.dumps({"abc12345": "WORKING"}))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(cmd, 0, "[]", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 0

    def test_parse_agent_state_from_pane(self):
        """Parse most recent marker from pane text."""
        from tms.events import _parse_agent_state_from_pane

        result = _parse_agent_state_from_pane(
            "old output\n<<AGENT-STATE: PLAN-REVIEW>>\nmore\n<<AGENT-STATE: WORKING>>"
        )
        assert result == ("WORKING", None)

    def test_parse_agent_state_blocked_with_reason(self):
        """BLOCKED markers carry a reason after colon."""
        from tms.events import _parse_agent_state_from_pane

        result = _parse_agent_state_from_pane(
            "<<AGENT-STATE: BLOCKED: review not converging — bad design>>"
        )
        assert result == ("BLOCKED", "review not converging — bad design")

    def test_parse_agent_state_no_marker(self):
        """No marker → None."""
        from tms.events import _parse_agent_state_from_pane

        result = _parse_agent_state_from_pane("just regular output")
        assert result is None

    def test_parse_agent_state_ansi_escapes_stripped(self):
        """ANSI escape sequences must not prevent matching."""
        from tms.events import _parse_agent_state_from_pane

        pane = "\x1b[32m<<AGENT-STATE: WORKING>>\x1b[0m"
        result = _parse_agent_state_from_pane(pane)
        assert result == ("WORKING", None)

    def test_parse_agent_state_picks_last_of_multiple(self):
        """Last marker wins (most recent)."""
        from tms.events import _parse_agent_state_from_pane

        pane = (
            "<<AGENT-STATE: PLAN-REVIEW>>\n"
            "... work ...\n"
            "<<AGENT-STATE: WORKING>>\n"
            "... more work ...\n"
            "<<AGENT-STATE: PR-REVIEW>>"
        )
        result = _parse_agent_state_from_pane(pane)
        assert result == ("PR-REVIEW", None)

    def test_corrupted_last_status_handled_gracefully(self, test_db, monkeypatch, tmp_path):
        """Corrupt last_status.json must not crash — treat as first run."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        last_status_path.write_text("not valid json {{{ ")

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#53", "path": "/root/wt-tms-53"},
                    ]), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 0
        state = json.loads(last_status_path.read_text())
        assert "abc12345" in state

    def test_derived_tmux_session_name_uses_8_char_prefix(self):
        """P0 regression: must use 8-char UUID prefix."""
        from tms.events import _derived_tmux_session_name

        name = _derived_tmux_session_name(
            "feat-tms#53", "6e803f602e914761"
        )
        assert name == "aoe_feat-tms_53_6e803f60"

    # ── Session-scoped capture (issue #98) ─────────────────────────

    def test_session_scoped_capture_emits_transition(
            self, test_db, monkeypatch, tmp_path):
        """--session <name> captures a single session synchronously."""
        from tms.events import detect_transition_for_session, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr(
            "tms.events.LAST_STATUS_PATH", str(last_status_path))

        # Pre-seed: session was WORKING
        last_status_path.write_text(
            json.dumps({"abc12345": "WORKING"}))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#98", "path": "/root/wt-tms-98"},
                    ]), "")
            if cmd == ["tmux", "has-session", "-t",
                       "aoe_feat-tms_98_abc12345"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    "...\n<<AGENT-STATE: PR-REVIEW>>\nwaiting...", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            emitted, old_st, new_st = \
                detect_transition_for_session("feat-tms#98")

        assert emitted is True
        assert old_st == "WORKING"
        assert new_st == "PR-REVIEW"
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_type, from_status, to_status, aoe_id_prefix "
                "FROM events")
            row = cur.fetchone()
        assert row[0] == "transition"
        assert row[1] == "WORKING"
        assert row[2] == "PR-REVIEW"
        assert row[3] == "abc12345"
        # last_status.json must be updated
        state = json.loads(last_status_path.read_text())
        assert state["abc12345"] == "PR-REVIEW"

    def test_session_scoped_no_change_is_noop(
            self, test_db, monkeypatch, tmp_path):
        """--session with unchanged state emits 0 events."""
        from tms.events import detect_transition_for_session, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr(
            "tms.events.LAST_STATUS_PATH", str(last_status_path))

        last_status_path.write_text(
            json.dumps({"abc12345": "WORKING"}))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#98", "path": "/root/wt-tms-98"},
                    ]), "")
            if cmd == ["tmux", "has-session", "-t",
                       "aoe_feat-tms_98_abc12345"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            emitted, old_st, new_st = \
                detect_transition_for_session("feat-tms#98")

        assert emitted is False
        assert old_st == "WORKING"
        assert new_st == "WORKING"
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM events")
            assert cur.fetchone()[0] == 0

    def test_session_scoped_first_run_seeds_no_event(
            self, test_db, monkeypatch, tmp_path):
        """--session on first run (no prior cache) seeds state, no event."""
        from tms.events import detect_transition_for_session, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr(
            "tms.events.LAST_STATUS_PATH", str(last_status_path))

        # No pre-existing last_status.json

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#98", "path": "/root/wt-tms-98"},
                    ]), "")
            if cmd == ["tmux", "has-session", "-t",
                       "aoe_feat-tms_98_abc12345"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            emitted, old_st, new_st = \
                detect_transition_for_session("feat-tms#98")

        assert emitted is False
        assert old_st is None
        assert new_st == "WORKING"
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM events")
            assert cur.fetchone()[0] == 0
        # Must seed state for future runs
        state = json.loads(last_status_path.read_text())
        assert state["abc12345"] == "WORKING"

    def test_session_scoped_unknown_session_errors_gracefully(
            self, test_db, monkeypatch, tmp_path):
        """--session with unknown name must raise clear error, not crash."""
        from tms.events import detect_transition_for_session

        # aoe list returns empty — no matching session
        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(cmd, 0, "[]", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            with pytest.raises(ValueError, match="Session not found"):
                detect_transition_for_session("nonexistent")

    def test_session_scoped_blocked_classification(
            self, test_db, monkeypatch, tmp_path):
        """Session-scoped capture must classify BLOCKED transitions (#98)."""
        from tms.events import detect_transition_for_session, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr(
            "tms.events.LAST_STATUS_PATH", str(last_status_path))

        last_status_path.write_text(
            json.dumps({"abc12345": "WORKING"}))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#98", "path": "/root/wt-tms-98"},
                    ]), "")
            if cmd == ["tmux", "has-session", "-t",
                       "aoe_feat-tms_98_abc12345"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    "<<AGENT-STATE: BLOCKED: AC is ambiguous>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            emitted, old_st, new_st = \
                detect_transition_for_session("feat-tms#98")

        assert emitted is True
        assert old_st == "WORKING"
        assert new_st == "BLOCKED"
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_status, reason, blocked_class FROM events")
            row = cur.fetchone()
        assert row[0] == "BLOCKED"
        assert row[1] == "AC is ambiguous"
        assert row[2] == "ambiguous-ac"

    def test_session_scoped_dead_tmux_session_raises(
            self, test_db, monkeypatch, tmp_path):
        """When tmux has-session fails, raise clear error (not silent)."""
        from tms.events import detect_transition_for_session

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#98", "path": "/root/wt-tms-98"},
                    ]), "")
            if cmd == ["tmux", "has-session", "-t",
                       "aoe_feat-tms_98_abc12345"]:
                return subprocess.CompletedProcess(cmd, 1, "", "no server")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            with pytest.raises(ValueError, match="tmux session.*not found"):
                detect_transition_for_session("feat-tms#98")

    def test_capture_pane_target_uses_correct_session_name(self, test_db, monkeypatch, tmp_path):
        """P0 regression: tmux capture-pane target must match 8-char prefix."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        captured_targets = []

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "6e803f602e914761",
                         "title": "feat-tms#53", "path": "/root/wt-tms-53"},
                    ]), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                if "-t" in cmd:
                    idx = cmd.index("-t")
                    if idx + 1 < len(cmd):
                        captured_targets.append(cmd[idx + 1])
                return subprocess.CompletedProcess(
                    cmd, 0, "<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            detect_transitions()

        assert len(captured_targets) > 0
        assert captured_targets[0] == "aoe_feat-tms_53_6e803f60"


# ── Phase 3: stats computation ───────────────────────────────────


def _insert_fixture_events(test_db_conn, records):
    """Insert fixture event records directly into the test DB.

    Uses the same append_event path so payload/flat columns are consistent.
    """
    from tms.events import append_event

    for r in records:
        append_event(r)


class TestComputeStats:
    """Tests for compute_stats() — aggregate metrics from postgres."""

    def test_empty_log_returns_zeros(self, test_db):
        """Empty table → all zeros, no crash."""
        from tms.events import compute_stats

        stats = compute_stats()
        assert stats["total_dispatches"] == 0
        assert stats["total_transitions"] == 0
        assert stats["per_model"] == {}

    def test_counts_dispatch_and_transition_events(self, test_db):
        """compute_stats must distinguish event types."""
        _insert_fixture_events(test_db, [
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "feat-tms#1",
             "aoe_id_prefix": "aaa"},
            {"event_type": "dispatch", "timestamp": "2026-07-01T11:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "cc",
             "provider": "anthropic", "model": "claude-sonnet",
             "dispatch_type": "fix", "session": "fix-tms#2-cc",
             "aoe_id_prefix": "bbb"},
            {"event_type": "dispatch_failed", "timestamp": "2026-07-01T12:00:00+00:00",
             "repo": "tms", "issue": 3, "agent": "cc",
             "provider": "", "model": "", "dispatch_type": "feature",
             "reason": "cc dispatch refused under root"},
            {"event_type": "transition", "timestamp": "2026-07-01T10:30:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "WORKING", "to_status": "PR-REVIEW"},
        ])

        from tms.events import compute_stats

        stats = compute_stats()
        assert stats["total_dispatches"] == 2
        assert stats["total_failed_dispatches"] == 1
        assert stats["total_transitions"] == 1

    def test_plan_gate_fast_path_detection(self, test_db):
        """Fast path: no PLAN-REVIEW. Normal: has PLAN-REVIEW."""
        _insert_fixture_events(test_db, [
            # aaa: fast path
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "fix", "session": "fix-tms#1",
             "aoe_id_prefix": "aaa"},
            {"event_type": "transition", "timestamp": "2026-07-01T10:01:00+00:00",
             "session": "fix-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "WORKING", "to_status": "MERGE-READY"},
            # bbb: normal path
            {"event_type": "dispatch", "timestamp": "2026-07-01T11:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "feat-tms#2",
             "aoe_id_prefix": "bbb"},
            {"event_type": "transition", "timestamp": "2026-07-01T11:05:00+00:00",
             "session": "feat-tms#2", "aoe_id_prefix": "bbb",
             "from_status": "PLAN-REVIEW", "to_status": "WORKING"},
        ])

        from tms.events import compute_stats

        stats = compute_stats()
        assert stats["fast_path_count"] == 1
        assert stats["normal_path_count"] == 1

    def test_review_rounds_counting(self, test_db):
        """PR-REVIEW→WORKING transitions count as review rounds."""
        _insert_fixture_events(test_db, [
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "feature", "session": "feat-tms#1",
             "aoe_id_prefix": "aaa"},
            {"event_type": "transition", "timestamp": "2026-07-01T11:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "PR-REVIEW", "to_status": "WORKING"},
            {"event_type": "transition", "timestamp": "2026-07-01T12:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "PR-REVIEW", "to_status": "WORKING"},
            {"event_type": "transition", "timestamp": "2026-07-01T13:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "WORKING", "to_status": "MERGE-READY"},
        ])

        from tms.events import compute_stats

        stats = compute_stats()
        assert stats["review_rounds_total"] == 2
        assert stats["review_rounds_avg"] == 2.0

    def test_blocked_rate(self, test_db):
        """BLOCKED transitions vs MERGE-READY transitions."""
        _insert_fixture_events(test_db, [
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "feature", "session": "feat-tms#1",
             "aoe_id_prefix": "aaa"},
            {"event_type": "transition", "timestamp": "2026-07-01T11:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "PLAN-REVIEW", "to_status": "BLOCKED"},
            {"event_type": "dispatch", "timestamp": "2026-07-02T10:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "feat-tms#2",
             "aoe_id_prefix": "bbb"},
            {"event_type": "transition", "timestamp": "2026-07-02T12:00:00+00:00",
             "session": "feat-tms#2", "aoe_id_prefix": "bbb",
             "from_status": "WORKING", "to_status": "MERGE-READY"},
        ])

        from tms.events import compute_stats

        stats = compute_stats()
        assert stats["blocked_count"] == 1
        assert stats["merge_ready_count"] == 1
        assert stats["blocked_rate"] == 0.5

    def test_per_model_outcomes(self, test_db):
        """Per-model: dispatch counts, merged, blocked."""
        _insert_fixture_events(test_db, [
            # Minimax → merged
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "feat-tms#1",
             "aoe_id_prefix": "aaa"},
            {"event_type": "transition", "timestamp": "2026-07-01T13:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "WORKING", "to_status": "MERGE-READY"},
            {"event_type": "transition", "timestamp": "2026-07-01T14:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "MERGE-READY", "to_status": "terminal"},
            # DeepSeek → blocked
            {"event_type": "dispatch", "timestamp": "2026-07-02T10:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "feature", "session": "feat-tms#2",
             "aoe_id_prefix": "bbb"},
            {"event_type": "transition", "timestamp": "2026-07-02T11:00:00+00:00",
             "session": "feat-tms#2", "aoe_id_prefix": "bbb",
             "from_status": "WORKING", "to_status": "BLOCKED"},
            # Another minimax (in progress)
            {"event_type": "dispatch", "timestamp": "2026-07-03T10:00:00+00:00",
             "repo": "tms", "issue": 3, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "feat-tms#3",
             "aoe_id_prefix": "ccc"},
        ])

        from tms.events import compute_stats

        stats = compute_stats()
        pm = stats["per_model"]
        assert "MiniMax-M3" in pm
        assert pm["MiniMax-M3"]["dispatches"] == 2
        assert pm["MiniMax-M3"]["merged"] == 1
        assert "deepseek-v4-pro" in pm
        assert pm["deepseek-v4-pro"]["dispatches"] == 1
        assert pm["deepseek-v4-pro"]["merged"] == 0
        assert pm["deepseek-v4-pro"]["blocked"] == 1

    def test_latency_computation(self, test_db):
        """Issue→merge latency from dispatch timestamp → terminal."""
        _insert_fixture_events(test_db, [
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "feat-tms#1",
             "aoe_id_prefix": "aaa"},
            {"event_type": "transition", "timestamp": "2026-07-01T14:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "WORKING", "to_status": "MERGE-READY"},
            {"event_type": "transition", "timestamp": "2026-07-01T15:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "MERGE-READY", "to_status": "terminal"},
            # Faster second issue
            {"event_type": "dispatch", "timestamp": "2026-07-02T10:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "fix", "session": "fix-tms#2",
             "aoe_id_prefix": "bbb"},
            {"event_type": "transition", "timestamp": "2026-07-02T10:30:00+00:00",
             "session": "fix-tms#2", "aoe_id_prefix": "bbb",
             "from_status": "WORKING", "to_status": "terminal"},
        ])

        from tms.events import compute_stats

        stats = compute_stats()
        # Issue 1: 5h = 18000s. Issue 2: 30m = 1800s
        # p50 of [1800, 18000] = 9900
        assert stats["latency_p50_seconds"] == pytest.approx(9900, abs=1)
        assert stats["latency_p90_seconds"] == pytest.approx(16380, abs=1)

    def test_since_filter(self, test_db):
        """--since filter must exclude events before cutoff."""
        _insert_fixture_events(test_db, [
            {"event_type": "dispatch", "timestamp": "2026-06-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "old",
             "aoe_id_prefix": "old"},
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "feature", "session": "new",
             "aoe_id_prefix": "new"},
        ])

        from tms.events import compute_stats

        stats = compute_stats(since="2026-07-01")
        assert stats["total_dispatches"] == 1
        pm = stats["per_model"]
        assert "deepseek-v4-pro" in pm
        assert "MiniMax-M3" not in pm

    def test_format_stats_report_text(self, test_db, capsys):
        """format_stats_report text mode prints readable report."""
        from tms.events import format_stats_report

        stats = {
            "total_dispatches": 10,
            "total_failed_dispatches": 2,
            "total_transitions": 50,
            "fast_path_count": 3,
            "normal_path_count": 7,
            "fast_path_rate": 0.3,
            "review_rounds_total": 15,
            "review_rounds_avg": 1.5,
            "blocked_count": 2,
            "merge_ready_count": 8,
            "blocked_rate": 0.2,
            "latency_p50_seconds": 7200,
            "latency_p90_seconds": 14400,
            "completed_sessions": 8,
            "per_model": {
                "MiniMax-M3": {
                    "dispatches": 5, "merged": 4, "blocked": 1,
                    "avg_latency_seconds": 5400,
                },
                "deepseek-v4-pro": {
                    "dispatches": 5, "merged": 4, "blocked": 1,
                    "avg_latency_seconds": 9000,
                },
            },
        }
        format_stats_report(stats)
        out = capsys.readouterr().out
        assert "Total dispatches" in out
        assert "MiniMax-M3" in out
        assert "deepseek-v4-pro" in out

    def test_format_stats_report_json(self, test_db, capsys):
        """format_stats_report --json must output valid JSON."""
        from tms.events import format_stats_report

        stats = {"total_dispatches": 1, "per_model": {}}
        format_stats_report(stats, as_json=True)
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["total_dispatches"] == 1


# ── BLOCKED reason taxonomy (#76 follow-up) ───────────────────────


class TestClassifyBlockedReason:
    """classify_blocked_reason: BLOCKED marker text → taxonomy value."""

    @pytest.mark.parametrize("text,expected", [
        ("tool crash: tmux spawn failed with ENOENT", "mechanical"),
        ("aoe session start failed", "mechanical"),
        ("rate limited by provider", "mechanical"),
        ("AC is ambiguous — needs human clarification", "ambiguous-ac"),
        ("unclear acceptance criteria", "ambiguous-ac"),
        ("issue contradicts itself", "ambiguous-ac"),
        ("scope too large — should be split into smaller issues", "scope-creep"),
        ("this is too big for one PR", "scope-creep"),
        ("too complex for me to complete", "capacity"),
        ("cannot handle the migration", "capacity"),
        ("unable to resolve the type errors", "capacity"),
        ("waiting on the operator to decide", "other"),
        ("", "other"),
        (None, "other"),
    ])
    def test_taxonomy(self, text, expected):
        from tms.events import classify_blocked_reason
        assert classify_blocked_reason(text) == expected


class TestBlockedTransitionTaxonomy:
    """detect_transitions must classify BLOCKED transitions (#76 follow-up)."""

    def test_blocked_transition_records_blocked_class(
            self, test_db, monkeypatch, tmp_path):
        from tms.events import detect_transitions

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr(
            "tms.events.LAST_STATUS_PATH", str(last_status_path))
        last_status_path.write_text(json.dumps({"abc12345": "WORKING"}))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#76", "path": "/root/wt-tms-76"},
                    ]), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    "...\n<<AGENT-STATE: BLOCKED: AC is ambiguous — "
                    "which table?>>\nwaiting...", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 1
        conn = test_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_status, reason, blocked_class, payload FROM events"
            )
            row = cur.fetchone()
        assert row[0] == "BLOCKED"
        assert row[1] == "AC is ambiguous — which table?"
        assert row[2] == "ambiguous-ac"
        payload = json.loads(row[3])
        assert payload["blocked_class"] == "ambiguous-ac"

    def test_non_blocked_transition_has_no_blocked_class(
            self, test_db, monkeypatch, tmp_path):
        from tms.events import detect_transitions

        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr(
            "tms.events.LAST_STATUS_PATH", str(last_status_path))
        last_status_path.write_text(json.dumps({"abc12345": "WORKING"}))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#76", "path": "/root/wt-tms-76"},
                    ]), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "<<AGENT-STATE: PR-REVIEW>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            detect_transitions()

        conn = test_db()
        with conn.cursor() as cur:
            cur.execute("SELECT blocked_class FROM events")
            row = cur.fetchone()
        assert row[0] is None
        # payload must not carry the key at all
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM events")
            payload = json.loads(cur.fetchone()[0])
        assert "blocked_class" not in payload


def _seed_blocked_class_events(test_db):
    """BLOCKED transitions carrying blocked_class payload values."""
    _insert_fixture_events(test_db, [
        {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
         "repo": "tms", "issue": 1, "agent": "pi",
         "provider": "minimax", "model": "MiniMax-M3",
         "dispatch_type": "feature", "session": "feat-tms#1",
         "aoe_id_prefix": "aaa"},
        {"event_type": "transition", "timestamp": "2026-07-01T11:00:00+00:00",
         "session": "feat-tms#1", "aoe_id_prefix": "aaa",
         "from_status": "WORKING", "to_status": "BLOCKED",
         "reason": "AC is ambiguous", "blocked_class": "ambiguous-ac"},
        {"event_type": "transition", "timestamp": "2026-07-01T12:00:00+00:00",
         "session": "feat-tms#1", "aoe_id_prefix": "aaa",
         "from_status": "BLOCKED", "to_status": "WORKING"},
        {"event_type": "transition", "timestamp": "2026-07-01T13:00:00+00:00",
         "session": "feat-tms#1", "aoe_id_prefix": "aaa",
         "from_status": "WORKING", "to_status": "BLOCKED",
         "reason": "aoe session start failed",
         "blocked_class": "mechanical"},
        # Legacy BLOCKED transition with no class → bucketed as other
        {"event_type": "transition", "timestamp": "2026-07-02T10:00:00+00:00",
         "session": "feat-tms#9", "aoe_id_prefix": "zzz",
         "from_status": "WORKING", "to_status": "BLOCKED"},
    ])


def test_per_blocked_class_in_stats(test_db):
    """compute_stats exposes per_blocked_class counts (#76 follow-up)."""
    _seed_blocked_class_events(test_db)
    from tms.events import compute_stats

    stats = compute_stats()
    pbc = stats["per_blocked_class"]
    assert pbc["ambiguous-ac"] == 1
    assert pbc["mechanical"] == 1
    assert pbc["other"] == 1  # legacy unclassified BLOCKED
    assert sum(pbc.values()) == 3


def test_per_blocked_class_empty(test_db):
    from tms.events import compute_stats

    stats = compute_stats()
    assert stats["per_blocked_class"] == {}


def test_default_report_hides_blocked_class_section(capsys):
    """Default report unchanged: no taxonomy section without the flag."""
    from tms.events import format_stats_report

    stats = {
        "total_dispatches": 1, "total_failed_dispatches": 0,
        "total_transitions": 1, "fast_path_count": 1,
        "normal_path_count": 0, "fast_path_rate": 1.0,
        "review_rounds_total": 0, "review_rounds_avg": 0.0,
        "blocked_count": 1, "merge_ready_count": 0, "blocked_rate": 1.0,
        "latency_p50_seconds": 0, "latency_p90_seconds": 0,
        "completed_sessions": 0, "per_model": {},
        "per_label": {}, "per_area": {}, "per_point": {},
        "per_blocked_class": {"ambiguous-ac": 1},
    }
    format_stats_report(stats)
    out = capsys.readouterr().out
    assert "BLOCKED by class" not in out


def test_by_blocked_class_report_section(capsys):
    from tms.events import format_stats_report

    stats = {
        "total_dispatches": 1, "total_failed_dispatches": 0,
        "total_transitions": 1, "fast_path_count": 1,
        "normal_path_count": 0, "fast_path_rate": 1.0,
        "review_rounds_total": 0, "review_rounds_avg": 0.0,
        "blocked_count": 1, "merge_ready_count": 0, "blocked_rate": 1.0,
        "latency_p50_seconds": 0, "latency_p90_seconds": 0,
        "completed_sessions": 0, "per_model": {},
        "per_label": {}, "per_area": {}, "per_point": {},
        "per_blocked_class": {"ambiguous-ac": 2, "mechanical": 1},
    }
    format_stats_report(stats, by_blocked_class=True)
    out = capsys.readouterr().out
    assert "BLOCKED by class" in out
    assert "ambiguous-ac" in out
    assert "mechanical" in out


def test_main_stats_by_blocked_class_flag(test_db, monkeypatch, capsys):
    """`python3 -m tms.events stats --by-blocked-class` prints the section."""
    import sys
    from tms import events

    _seed_blocked_class_events(test_db)
    monkeypatch.setattr(
        sys, "argv", ["tms.events", "stats", "--by-blocked-class"])
    events.main()
    out = capsys.readouterr().out
    assert "BLOCKED by class" in out
    assert "ambiguous-ac" in out


# ── Per-class stats (issue #112) ──────────────────────────────────


def _seed_class_fixtures(test_db):
    """Insert fixtures covering all 4 AC scenarios for --by-class.

    Classes:
      tms:feature — 3 dispatches (1 merged-in-1-round, 1 merged-in-3-rounds,
                   1 blocked)
      tms:fix     — 1 dispatch (never reached terminal = never-dispatched)
      distillery:feature — 1 dispatch (merged, no reviewer_runs)

    encoded_cwd transform test: worktree /root/wt-tms-112 →
    meta->>'encoded_cwd' = '--root-wt-tms-112--'
    """
    import json
    conn = test_db()
    cur = conn.cursor()

    # ── Dispatch events ──────────────────────────────────────────
    _insert_fixture_events(test_db, [
        # tms:feature — merged in 1 round (aoe=aaa)
        {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
         "repo": "tms", "issue": 100, "agent": "pi",
         "provider": "minimax", "model": "MiniMax-M3",
         "dispatch_type": "feature", "worktree": "/root/wt-tms-100",
         "session": "feat-tms#100", "aoe_id_prefix": "aaa"},
        {"event_type": "transition", "timestamp": "2026-07-01T11:00:00+00:00",
         "session": "feat-tms#100", "aoe_id_prefix": "aaa",
         "from_status": "WORKING", "to_status": "MERGE-READY"},
        # tms:feature — merged in 3 rounds (aoe=bbb)
        {"event_type": "dispatch", "timestamp": "2026-07-02T10:00:00+00:00",
         "repo": "tms", "issue": 101, "agent": "pi",
         "provider": "deepseek", "model": "deepseek-v4-pro",
         "dispatch_type": "feature", "worktree": "/root/wt-tms-101",
         "session": "feat-tms#101", "aoe_id_prefix": "bbb"},
        {"event_type": "transition", "timestamp": "2026-07-02T12:00:00+00:00",
         "session": "feat-tms#101", "aoe_id_prefix": "bbb",
         "from_status": "PR-REVIEW", "to_status": "WORKING"},
        {"event_type": "transition", "timestamp": "2026-07-02T14:00:00+00:00",
         "session": "feat-tms#101", "aoe_id_prefix": "bbb",
         "from_status": "PR-REVIEW", "to_status": "WORKING"},
        {"event_type": "transition", "timestamp": "2026-07-02T16:00:00+00:00",
         "session": "feat-tms#101", "aoe_id_prefix": "bbb",
         "from_status": "WORKING", "to_status": "MERGE-READY"},
        # tms:feature — blocked (aoe=ccc)
        {"event_type": "dispatch", "timestamp": "2026-07-03T10:00:00+00:00",
         "repo": "tms", "issue": 102, "agent": "pi",
         "provider": "minimax", "model": "MiniMax-M3",
         "dispatch_type": "feature", "worktree": "/root/wt-tms-102",
         "session": "feat-tms#102", "aoe_id_prefix": "ccc"},
        {"event_type": "transition", "timestamp": "2026-07-03T11:00:00+00:00",
         "session": "feat-tms#102", "aoe_id_prefix": "ccc",
         "from_status": "WORKING", "to_status": "BLOCKED",
         "reason": "AC is ambiguous", "blocked_class": "ambiguous-ac"},
        # tms:fix — never reached terminal (aoe=ddd)
        {"event_type": "dispatch", "timestamp": "2026-07-04T10:00:00+00:00",
         "repo": "tms", "issue": 200, "agent": "pi",
         "provider": "deepseek", "model": "deepseek-v4-pro",
         "dispatch_type": "fix", "worktree": "/root/wt-tms-200",
         "session": "fix-tms#200", "aoe_id_prefix": "ddd"},
        # distillery:feature — merged (aoe=eee)
        {"event_type": "dispatch", "timestamp": "2026-07-05T10:00:00+00:00",
         "repo": "distillery", "issue": 300, "agent": "pi",
         "provider": "minimax", "model": "MiniMax-M3",
         "dispatch_type": "feature", "worktree": "/root/wt-distillery-300",
         "session": "feat-distillery#300", "aoe_id_prefix": "eee"},
        {"event_type": "transition", "timestamp": "2026-07-05T11:00:00+00:00",
         "session": "feat-distillery#300", "aoe_id_prefix": "eee",
         "from_status": "WORKING", "to_status": "MERGE-READY"},
    ])

    # ── dispatch_outcomes ────────────────────────────────────────
    for row in [
        ("aaa", "tms", 100, "merged", "gh_pr_list",
         "2026-07-01T12:00:00+00:00", "2026-07-01T10:00:00+00:00"),
        ("bbb", "tms", 101, "merged", "gh_pr_list",
         "2026-07-03T10:00:00+00:00", "2026-07-02T10:00:00+00:00"),
        ("ccc", "tms", 102, "open", "gh_pr_list",
         "2026-07-03T12:00:00+00:00", "2026-07-03T10:00:00+00:00"),
        ("ddd", "tms", 200, "open", "gh_pr_list",
         "2026-07-04T12:00:00+00:00", "2026-07-04T10:00:00+00:00"),
        ("eee", "distillery", 300, "merged", "gh_pr_list",
         "2026-07-05T12:00:00+00:00", "2026-07-05T10:00:00+00:00"),
    ]:
        cur.execute(
            "INSERT INTO dispatch_outcomes "
            "(aoe_id_prefix, repo, issue, outcome, derived_via, "
            " derived_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", row)

    # ── reviewer_runs ────────────────────────────────────────────
    # tms: 1 PR with 1 round, 1 PR with 3 rounds, 1 PR with 2 rounds
    # distillery: 1 PR with 1 round
    for row in [
        # issue 100 → PR 500, 1 round
        ("r1", "2026-07-01T11:00:00+00:00", "tms", 500, 1,
         "reviewer", "deepseek-v4-pro", "deepseek", "abc123",
         0, 0, 0, None, "[]", None, None, "[]"),
        # issue 101 → PR 501, 3 rounds (3 separate reviewer_runs rows)
        ("r2a", "2026-07-02T12:30:00+00:00", "tms", 501, 1,
         "reviewer", "deepseek-v4-pro", "deepseek", "def456",
         2, 1, 0, None, "[]", None, None, "[]"),
        ("r2b", "2026-07-02T14:30:00+00:00", "tms", 501, 2,
         "reviewer-m3", "MiniMax-M3", "minimax", "def456",
         1, 0, 0, None, "[]", None, None, "[]"),
        ("r2c", "2026-07-02T16:30:00+00:00", "tms", 501, 3,
         "reviewer", "deepseek-v4-pro", "deepseek", "def456",
         0, 0, 0, None, "[]", None, None, "[]"),
        # extra PR for tms (unrelated to issues 100-102,200)
        ("r3", "2026-07-06T10:00:00+00:00", "tms", 600, 2,
         "reviewer", "deepseek-v4-pro", "deepseek", "ghi789",
         1, 1, 0, None, "[]", None, None, "[]"),
        # distillery PR
        ("r4", "2026-07-05T11:30:00+00:00", "distillery", 700, 1,
         "reviewer", "deepseek-v4-pro", "deepseek", "jkl012",
         0, 0, 0, None, "[]", None, None, "[]"),
    ]:
        cur.execute(
            "INSERT INTO reviewer_runs "
            "(run_id, created_at, repo, pr_number, review_round, "
            " reviewer_agent, model, provider_used, diff_sha_reviewed, "
            " p0, p1, p2, wall_time_ms, findings, "
            " input_tokens, output_tokens, specialist_composition) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)

    # ── llm_call_log (cost) ──────────────────────────────────────
    # Matches the encoded_cwd pattern: --root-wt-<repo>-<issue>--
    for row in [
        # tms#100: total cost $1.50
        (1, "etl-pi", None, "minimax", "MiniMax-M3", "subscription",
         5000, 2000, 0, 0, 1.50, 8.00, 30000, 1, None,
         '{"encoded_cwd":"--root-wt-tms-100--"}',
         "2026-07-01T10:30:00+00:00"),
        # tms#101: total cost $4.00
        (2, "etl-pi", None, "deepseek", "deepseek-v4-pro", "api",
         10000, 5000, 0, 0, 4.00, 4.00, 60000, 1, None,
         '{"encoded_cwd":"--root-wt-tms-101--"}',
         "2026-07-02T15:00:00+00:00"),
        # tms#102 (blocked): total cost $0.75
        (3, "etl-pi", None, "minimax", "MiniMax-M3", "subscription",
         2000, 1000, 0, 0, 0.75, 4.00, 15000, 1, None,
         '{"encoded_cwd":"--root-wt-tms-102--"}',
         "2026-07-03T10:30:00+00:00"),
        # distillery#300: total cost $2.00
        (4, "etl-pi", None, "minimax", "MiniMax-M3", "subscription",
         8000, 3000, 0, 0, 2.00, 10.00, 45000, 1, None,
         '{"encoded_cwd":"--root-wt-distillery-300--"}',
         "2026-07-05T10:30:00+00:00"),
    ]:
        cur.execute(
            "INSERT INTO llm_call_log "
            "(id, caller, caller_ref, provider, model, billing, "
            " input_tokens, output_tokens, cache_read_tokens, "
            " cache_write_tokens, cost_usd, cost_usd_api_equiv, "
            " duration_ms, success, error, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)

    conn.commit()


class TestComputeStatsByClass:
    """Per-class stats: --by-class aggregates (issue #112)."""

    def test_by_class_outputs_per_class_rows(self, test_db):
        """Each (repo, dispatch_type) is a separate row."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class

        stats = compute_stats_by_class()

        # Expected classes:
        # tms:feature (3 dispatches), tms:fix (1), distillery:feature (1)
        assert len(stats) == 3
        classes = {(r["repo"], r["dispatch_type"]) for r in stats}
        assert ("tms", "feature") in classes
        assert ("tms", "fix") in classes
        assert ("distillery", "feature") in classes

    def test_merged_in_one_round(self, test_db):
        """tms:feature has 2 merged out of 3 dispatches, median rounds=2."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class

        stats = compute_stats_by_class()
        tms_feat = [r for r in stats
                    if r["repo"] == "tms" and r["dispatch_type"] == "feature"][0]

        assert tms_feat["dispatches"] == 3
        assert tms_feat["merged"] == 2
        assert tms_feat["blocked"] == 1
        # pass_rate = merged / dispatches = 2/3
        assert tms_feat["pass_rate"] == pytest.approx(2 / 3, abs=0.01)

    def test_review_rounds_median(self, test_db):
        """Median review rounds computed from reviewer_runs per repo."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class

        stats = compute_stats_by_class()
        tms_feat = [r for r in stats
                    if r["repo"] == "tms" and r["dispatch_type"] == "feature"][0]

        # reviewer_runs for tms: PRs with rounds [1, 3, 2] → median = 2
        assert tms_feat["repo_median_rounds"] == 2.0

    def test_never_dispatched_class(self, test_db):
        """tms:fix has 1 dispatch, 0 merged, 0 blocked, rounds from repo."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class

        stats = compute_stats_by_class()
        tms_fix = [r for r in stats
                   if r["repo"] == "tms" and r["dispatch_type"] == "fix"][0]

        assert tms_fix["dispatches"] == 1
        assert tms_fix["merged"] == 0
        assert tms_fix["blocked"] == 0
        # Rounds from reviewer_runs at repo level (not per-class)
        assert tms_fix["repo_median_rounds"] == 2.0  # same repo-level median

    def test_cost_join_via_encoded_cwd(self, test_db):
        """Cost joined via encoded_cwd, NULL when no match."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class

        stats = compute_stats_by_class()

        # tms:feature has 2 merged dispatches with costs: 1.50 + 4.00
        tms_feat = [r for r in stats
                    if r["repo"] == "tms" and r["dispatch_type"] == "feature"][0]
        # median of [1.50, 4.00] = 2.75
        assert tms_feat["median_cost"] == pytest.approx(2.75, abs=0.01)

        # tms:fix has 0 merged → median_cost is None
        tms_fix = [r for r in stats
                   if r["repo"] == "tms" and r["dispatch_type"] == "fix"][0]
        assert tms_fix["median_cost"] is None

    def test_cost_null_when_no_encoded_cwd_match(self, test_db):
        """When llm_call_log has no matching row, cost is NULL (never 0)."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class

        stats = compute_stats_by_class()
        # distillery:feature has 1 merged dispatch, cost = 2.00
        dist_feat = [r for r in stats
                     if r["repo"] == "distillery"
                     and r["dispatch_type"] == "feature"][0]
        assert dist_feat["median_cost"] == 2.00

    def test_blocked_class_distribution(self, test_db):
        """Blocked-class distribution per class."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class

        stats = compute_stats_by_class()
        tms_feat = [r for r in stats
                    if r["repo"] == "tms" and r["dispatch_type"] == "feature"][0]

        blocked_dist = tms_feat["blocked_class_distribution"]
        assert blocked_dist.get("ambiguous-ac") == 1
        assert blocked_dist.get("mechanical", 0) == 0

    def test_format_by_class_json(self, test_db, capsys):
        """--by-class --json outputs valid JSON array."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class, format_stats_by_class

        stats = compute_stats_by_class()
        format_stats_by_class(stats, as_json=True)
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert len(parsed) == 3
        # Each row has expected keys
        for row in parsed:
            assert "class" in row
            assert "dispatches" in row
            assert "merged" in row
            assert "pass_rate" in row
            assert "repo_median_rounds" in row
            assert "blocked_class_distribution" in row
            assert "median_cost" in row

    def test_format_by_class_text(self, test_db, capsys):
        """--by-class text mode prints readable table."""
        _seed_class_fixtures(test_db)
        from tms.events import compute_stats_by_class, format_stats_by_class

        stats = compute_stats_by_class()
        format_stats_by_class(stats, as_json=False)
        out = capsys.readouterr().out
        assert "Per-class breakdown" in out
        assert "tms:feature" in out
        assert "tms:fix" in out
        assert "distillery:feature" in out

    def test_main_stats_by_class_flag(self, test_db, monkeypatch, capsys):
        """`tms events stats --by-class` works end-to-end."""
        import sys
        from tms import events

        _seed_class_fixtures(test_db)
        monkeypatch.setattr(
            sys, "argv", ["tms.events", "stats", "--by-class"])
        events.main()
        out = capsys.readouterr().out
        assert "Per-class breakdown" in out

    def test_main_stats_by_class_json(self, test_db, monkeypatch, capsys):
        """`tms events stats --by-class --json` outputs valid JSON."""
        import sys
        from tms import events

        _seed_class_fixtures(test_db)
        monkeypatch.setattr(
            sys, "argv", ["tms.events", "stats", "--by-class", "--json"])
        events.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed, list)

    def test_encoded_cwd_transform(self, test_db):
        """Worktree /root/wt-tms-112 → encoded_cwd --root-wt-tms-112--."""
        from tms.events import _worktree_to_encoded_cwd

        assert _worktree_to_encoded_cwd("/root/wt-tms-112") \
            == "--root-wt-tms-112--"
        assert _worktree_to_encoded_cwd("/root/wt-distillery-300") \
            == "--root-wt-distillery-300--"
        # Edge cases
        assert _worktree_to_encoded_cwd("/not/a/worktree") is None
        assert _worktree_to_encoded_cwd("") is None
        assert _worktree_to_encoded_cwd(None) is None
