"""Tests for lib/tms/events.py — dispatch event logging, transition
detection, and stats computation (issue #53).
"""

import json
import multiprocessing
import os
import time
from unittest.mock import patch

import pytest

# ── Phase 1: dispatch event appending ─────────────────────────────


def test_append_event_creates_file_and_writes_valid_jsonl(tmp_path, monkeypatch):
    """append_event() must create the events file if it doesn't exist
    and write a single valid JSONL record.
    """
    from tms.events import append_event

    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))

    record = {"event_type": "dispatch", "repo": "tms", "issue": 53}
    append_event(record)

    assert events_path.exists()
    lines = events_path.read_text().strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event_type"] == "dispatch"
    assert parsed["repo"] == "tms"
    assert parsed["issue"] == 53


def test_append_event_appends_not_replaces(tmp_path, monkeypatch):
    """Multiple append_event() calls must append lines, not replace."""
    from tms.events import append_event

    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))

    append_event({"n": 1})
    append_event({"n": 2})
    append_event({"n": 3})

    lines = events_path.read_text().strip().split("\n")
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"n": 1}
    assert json.loads(lines[1]) == {"n": 2}
    assert json.loads(lines[2]) == {"n": 3}


def test_append_event_concurrent_writers_no_torn_lines(tmp_path, monkeypatch):
    """N concurrent append_event calls must produce exactly N valid JSON
    records with no torn/partial lines. Uses O_APPEND which POSIX
    guarantees atomic for writes ≤ PIPE_BUF (~4KB). A single JSONL
    record is <1KB, so this test asserts correctness under fleet load.
    """
    from tms.events import append_event

    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))

    N = 30

    def writer(i):
        append_event({"writer": i, "data": "x" * 200})

    procs = [
        multiprocessing.Process(target=writer, args=(i,))
        for i in range(N)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        assert p.exitcode == 0

    # Read all lines — every one must be valid JSON
    content = events_path.read_text()
    lines = [l for l in content.strip().split("\n") if l.strip()]
    assert len(lines) == N, f"expected {N} records, got {len(lines)}"

    for i, line in enumerate(lines):
        try:
            parsed = json.loads(line)
            assert "writer" in parsed
        except json.JSONDecodeError as e:
            pytest.fail(f"line {i} is not valid JSON: {e}\nline: {line[:80]!r}")


def test_log_dispatch_event_writes_all_fields(tmp_path, monkeypatch):
    """log_dispatch_event() must write a record with all required fields."""
    from tms.events import log_dispatch_event, EVENTS_PATH

    monkeypatch.setattr("tms.events.EVENTS_PATH", str(tmp_path / "events.jsonl"))

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

    events_path = tmp_path / "events.jsonl"
    record = json.loads(events_path.read_text().strip().split("\n")[0])

    assert record["event_type"] == "dispatch"
    assert record["repo"] == "tms"
    assert record["issue"] == 53
    assert record["agent"] == "pi"
    assert record["provider"] == "minimax"
    assert record["model"] == "MiniMax-M3"
    assert record["dispatch_type"] == "feature"
    assert record["worktree"] == "/root/wt-tms-53"
    assert record["session"] == "feat-tms#53"
    assert record["aoe_id_prefix"] == "abc12345"
    assert "timestamp" in record
    # timestamp must be ISO 8601
    assert "T" in record["timestamp"]


def test_log_dispatch_event_has_event_type_discriminator(tmp_path, monkeypatch):
    """Every dispatch record must carry event_type="dispatch" from day 1
    so tms#56 (stale-marker watchdog) can extend the same log without
    a schema migration. See proposal-review finding from reviewer-claude.
    """
    from tms.events import log_dispatch_event

    monkeypatch.setattr("tms.events.EVENTS_PATH", str(tmp_path / "events.jsonl"))

    log_dispatch_event(
        repo="tms", issue=1, agent="pi", provider="", model="",
        dispatch_type="feature", worktree="/tmp/wt", session="feat-tms#1",
        aoe_id_prefix="",
    )

    record = json.loads(
        (tmp_path / "events.jsonl").read_text().strip().split("\n")[0]
    )
    assert record["event_type"] == "dispatch"


def test_log_dispatch_event_resolves_default_model(tmp_path, monkeypatch):
    """When provider/model are empty strings (default pi dispatch without
    --provider/--model flags), log_dispatch_event should resolve the
    actually-served model from pi's settings. Empty provider/model
    makes per-model stats useless — we need the real value.
    """
    from tms.events import log_dispatch_event

    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))

    # Mock the settings file with a known model
    fake_settings = {"defaultModel": "deepseek-v4-pro"}
    with patch("tms.events._resolve_default_model", return_value=("deepseek", "deepseek-v4-pro")):
        log_dispatch_event(
            repo="tms", issue=1, agent="pi", provider="", model="",
            dispatch_type="feature", worktree="/tmp/wt", session="feat-tms#1",
            aoe_id_prefix="abc12345",
        )

    record = json.loads(events_path.read_text().strip().split("\n")[0])
    assert record["provider"] == "deepseek"
    assert record["model"] == "deepseek-v4-pro"


def test_log_dispatch_event_no_override_when_explicit_model(tmp_path, monkeypatch):
    """When provider/model are explicitly passed (not empty), they must
    be used as-is — don't override with the resolved default.
    """
    from tms.events import log_dispatch_event

    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))

    log_dispatch_event(
        repo="tms", issue=1, agent="pi", provider="minimax", model="MiniMax-M3",
        dispatch_type="feature", worktree="/tmp/wt", session="feat-tms#1",
        aoe_id_prefix="abc12345",
    )

    record = json.loads(events_path.read_text().strip().split("\n")[0])
    assert record["provider"] == "minimax"
    assert record["model"] == "MiniMax-M3"


def test_append_event_handles_special_characters(tmp_path, monkeypatch):
    """JSONL records with special characters (newlines in values, unicode,
    quotes) must be written correctly as a single JSON line.
    """
    from tms.events import append_event

    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))

    record = {
        "event_type": "dispatch",
        "title": 'feat: add "metrics" with unicode → ✓',
        "body": "multi\nline\ncontent",
    }
    append_event(record)

    lines = events_path.read_text().strip().split("\n")
    assert len(lines) == 1, "special chars must not produce extra lines"
    parsed = json.loads(lines[0])
    assert parsed["title"] == record["title"]
    assert parsed["body"] == record["body"]


# ── Phase 2: transition detection ─────────────────────────────────


def _make_aoe_list_json(sessions):
    """Build a mock aoe list --json response."""
    return json.dumps([
        {"id": s["id"], "title": s["title"], "path": s["path"],
         "tool": s.get("tool", "pi")}
        for s in sessions
    ])


def _make_aoe_show_json(session_id, status="running"):
    """Build a mock aoe session show --json response."""
    return json.dumps({"id": session_id, "status": status})


class TestDetectTransitions:
    """Tests for detect_transitions() — polling aoe + tmux pane capture."""

    def test_first_run_seeds_state_no_events(self, tmp_path, monkeypatch):
        """On first run (no last_status.json), detect_transitions must
        seed the state file with current statuses but emit NO transition
        events. Spurious 'unknown→running' transitions on first poll
        would permanently corrupt the stats.
        """
        from tms.events import detect_transitions, LAST_STATUS_PATH

        events_path = tmp_path / "events.jsonl"
        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))
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
            if cmd[:2] == ["aoe", "session"] and cmd[2] == "show":
                return subprocess.CompletedProcess(
                    cmd, 0, _make_aoe_show_json("abc12345"), "")
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
        # No events written
        assert not events_path.exists() or events_path.read_text().strip() == ""

    def test_status_change_emits_transition_event(self, tmp_path, monkeypatch):
        """When a session's AGENT-STATE marker changes between polls,
        detect_transitions must emit a transition event.
        """
        from tms.events import detect_transitions, LAST_STATUS_PATH

        events_path = tmp_path / "events.jsonl"
        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        # Pre-seed: session was WORKING
        last_status_path.write_text(json.dumps({"abc12345": "WORKING"}))

        call_count = [0]

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "abc12345-fedc-fedc-fedc-fedcba987654",
                         "title": "feat-tms#53", "path": "/root/wt-tms-53"},
                    ]), "")
            if cmd[:2] == ["aoe", "session"] and cmd[2] == "show":
                return subprocess.CompletedProcess(
                    cmd, 0, _make_aoe_show_json("abc12345"), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                call_count[0] += 1
                # Now the pane shows PR-REVIEW
                return subprocess.CompletedProcess(
                    cmd, 0,
                    "...\n<<AGENT-STATE: PR-REVIEW>>\nwaiting...", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 1, f"expected 1 transition, got {n}"
        # Verify event was written
        lines = events_path.read_text().strip().split("\n")
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["event_type"] == "transition"
        assert event["from_status"] == "WORKING"
        assert event["to_status"] == "PR-REVIEW"
        assert event["aoe_id_prefix"] == "abc12345"
        assert event["session"] == "feat-tms#53"

    def test_no_change_emits_no_event(self, tmp_path, monkeypatch):
        """When the status hasn't changed, detect_transitions must emit 0 events."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        events_path = tmp_path / "events.jsonl"
        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))
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
            if cmd[:2] == ["aoe", "session"] and cmd[2] == "show":
                return subprocess.CompletedProcess(
                    cmd, 0, _make_aoe_show_json("abc12345"), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "still working\n<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 0
        # No events file written (or empty)
        content = events_path.read_text() if events_path.exists() else ""
        assert content.strip() == ""

    def test_session_disappearance_with_done_status_emits_terminal(self, tmp_path, monkeypatch):
        """When a session that was DONE disappears from aoe list, emit a
        terminal event. This is the common MERGE-READY→merge→cleanup path.
        """
        from tms.events import detect_transitions, LAST_STATUS_PATH

        events_path = tmp_path / "events.jsonl"
        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))
        monkeypatch.setattr("tms.events.LAST_STATUS_PATH", str(last_status_path))

        # Pre-seed: session was MERGE-READY
        last_status_path.write_text(json.dumps({
            "abc12345": "MERGE-READY",
            "fed67890": "WORKING",
        }))

        def fake_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[:3] == ["aoe", "list", "--json"]:
                # Only the WORKING session is still alive;
                # MERGE-READY session has been cleaned up
                return subprocess.CompletedProcess(
                    cmd, 0,
                    _make_aoe_list_json([
                        {"id": "fed67890-1234-1234-1234-1234567890ab",
                         "title": "feat-tms#54", "path": "/root/wt-tms-54"},
                    ]), "")
            if cmd[:2] == ["aoe", "session"] and cmd[2] == "show":
                return subprocess.CompletedProcess(
                    cmd, 0, _make_aoe_show_json("fed67890"), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n >= 1, "should emit at least the terminal event"
        lines = events_path.read_text().strip().split("\n")
        terminal_events = [
            json.loads(l) for l in lines
            if json.loads(l).get("to_status") == "terminal"
        ]
        assert len(terminal_events) == 1
        assert terminal_events[0]["from_status"] == "MERGE-READY"
        assert terminal_events[0]["aoe_id_prefix"] == "abc12345"

    def test_session_disappearance_with_non_terminal_status_skipped(self, tmp_path, monkeypatch):
        """When a WORKING session disappears (crash, not merge), do NOT
        emit a terminal event — that would inflate the terminal count.
        """
        from tms.events import detect_transitions, LAST_STATUS_PATH

        events_path = tmp_path / "events.jsonl"
        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))
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

        assert n == 0, "non-terminal disappearance must emit 0 events"

    def test_parse_agent_state_from_pane(self):
        """_parse_agent_state_from_pane must extract the most recent marker."""
        from tms.events import _parse_agent_state_from_pane

        result = _parse_agent_state_from_pane(
            "old output\n<<AGENT-STATE: PLAN-REVIEW>>\nmore\n<<AGENT-STATE: WORKING>>"
        )
        assert result == ("WORKING", None)

    def test_parse_agent_state_blocked_with_reason(self):
        """BLOCKED markers carry a reason after the colon."""
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
        """ANSI escape sequences around the marker must not prevent matching.
        Agents often color their output; the regex must work through it.
        """
        from tms.events import _parse_agent_state_from_pane

        # Simulated colored output
        pane = "\x1b[32m<<AGENT-STATE: WORKING>>\x1b[0m"
        result = _parse_agent_state_from_pane(pane)
        assert result == ("WORKING", None)

    def test_parse_agent_state_picks_last_of_multiple(self):
        """When multiple markers exist, the LAST one wins (most recent)."""
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

    def test_corrupted_last_status_handled_gracefully(self, tmp_path, monkeypatch):
        """Corrupt last_status.json must not crash — treat as first run."""
        from tms.events import detect_transitions, LAST_STATUS_PATH

        events_path = tmp_path / "events.jsonl"
        last_status_path = tmp_path / "last_status.json"
        monkeypatch.setattr("tms.events.EVENTS_PATH", str(events_path))
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
            if cmd[:2] == ["aoe", "session"] and cmd[2] == "show":
                return subprocess.CompletedProcess(
                    cmd, 0, _make_aoe_show_json("abc12345"), "")
            if cmd[0] == "tmux" and "capture-pane" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, "<<AGENT-STATE: WORKING>>", "")
            import subprocess as sp
            return sp.CompletedProcess(cmd, 0, "", "")

        with patch("tms.events.subprocess.run", side_effect=fake_run):
            n = detect_transitions()

        assert n == 0, "corrupted state file must not produce spurious events"
        # Should have re-written valid JSON
        state = json.loads(last_status_path.read_text())
        assert "abc12345" in state


# ── Phase 3: stats computation ───────────────────────────────────


def _write_fixture_events(tmp_path, records):
    """Write fixture event records to a temp events.jsonl."""
    path = tmp_path / "events.jsonl"
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return str(path)


class TestComputeStats:
    """Tests for compute_stats() — aggregate metrics from events.jsonl."""

    def test_empty_log_returns_zeros(self, tmp_path, monkeypatch):
        """Empty events file → all zeros, no crash."""
        from tms.events import compute_stats

        events_path = _write_fixture_events(tmp_path, [])
        monkeypatch.setattr("tms.events.EVENTS_PATH", events_path)

        stats = compute_stats()
        assert stats["total_dispatches"] == 0
        assert stats["total_transitions"] == 0
        assert stats["per_model"] == {}

    def test_counts_dispatch_and_transition_events(self, tmp_path, monkeypatch):
        """compute_stats must distinguish dispatch/transition/failed events."""
        from tms.events import compute_stats

        events_path = _write_fixture_events(tmp_path, [
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
        monkeypatch.setattr("tms.events.EVENTS_PATH", events_path)

        stats = compute_stats()
        assert stats["total_dispatches"] == 2  # successful only
        assert stats["total_failed_dispatches"] == 1
        assert stats["total_transitions"] == 1

    def test_plan_gate_fast_path_detection(self, tmp_path, monkeypatch):
        """Fast-path: dispatch→WORKING without intervening PLAN-REVIEW.
        Normal path: dispatch→PLAN-REVIEW→WORKING.
        """
        from tms.events import compute_stats

        # Session aaa: fast path (fix type, no PLAN-REVIEW)
        # Session bbb: normal path (PLAN-REVIEW→WORKING)
        events_path = _write_fixture_events(tmp_path, [
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "fix", "session": "fix-tms#1",
             "aoe_id_prefix": "aaa"},
            {"event_type": "transition", "timestamp": "2026-07-01T10:01:00+00:00",
             "session": "fix-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "WORKING", "to_status": "MERGE-READY"},
            {"event_type": "dispatch", "timestamp": "2026-07-01T11:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "feat-tms#2",
             "aoe_id_prefix": "bbb"},
            {"event_type": "transition", "timestamp": "2026-07-01T11:05:00+00:00",
             "session": "feat-tms#2", "aoe_id_prefix": "bbb",
             "from_status": "PLAN-REVIEW", "to_status": "WORKING"},
        ])
        monkeypatch.setattr("tms.events.EVENTS_PATH", events_path)

        stats = compute_stats()
        # aaa: fix type → auto fast-path (no PLAN-REVIEW seen before WORKING)
        # bbb: PLAN-REVIEW→WORKING → normal path
        assert stats["fast_path_count"] == 1
        assert stats["normal_path_count"] == 1

    def test_review_rounds_counting(self, tmp_path, monkeypatch):
        """PR-REVIEW→WORKING transitions count as review rounds."""
        from tms.events import compute_stats

        events_path = _write_fixture_events(tmp_path, [
            {"event_type": "dispatch", "timestamp": "2026-07-01T10:00:00+00:00",
             "repo": "tms", "issue": 1, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "feature", "session": "feat-tms#1",
             "aoe_id_prefix": "aaa"},
            # Round 1
            {"event_type": "transition", "timestamp": "2026-07-01T11:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "PR-REVIEW", "to_status": "WORKING"},
            # Round 2
            {"event_type": "transition", "timestamp": "2026-07-01T12:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "PR-REVIEW", "to_status": "WORKING"},
            # Done
            {"event_type": "transition", "timestamp": "2026-07-01T13:00:00+00:00",
             "session": "feat-tms#1", "aoe_id_prefix": "aaa",
             "from_status": "WORKING", "to_status": "MERGE-READY"},
        ])
        monkeypatch.setattr("tms.events.EVENTS_PATH", events_path)

        stats = compute_stats()
        assert stats["review_rounds_total"] == 2
        assert stats["review_rounds_avg"] == 2.0  # 1 session, 2 rounds

    def test_blocked_rate(self, tmp_path, monkeypatch):
        """BLOCKED transitions vs MERGE-READY transitions."""
        from tms.events import compute_stats

        events_path = _write_fixture_events(tmp_path, [
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
        monkeypatch.setattr("tms.events.EVENTS_PATH", events_path)

        stats = compute_stats()
        assert stats["blocked_count"] == 1
        assert stats["merge_ready_count"] == 1
        # blocked_rate = blocked / (blocked + merge_ready) = 0.5
        assert stats["blocked_rate"] == 0.5

    def test_per_model_outcomes(self, tmp_path, monkeypatch):
        """Per-model stats: dispatch counts, terminal outcomes."""
        from tms.events import compute_stats

        events_path = _write_fixture_events(tmp_path, [
            # Minimax dispatch + merge
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
            # DeepSeek dispatch + blocked
            {"event_type": "dispatch", "timestamp": "2026-07-02T10:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "feature", "session": "feat-tms#2",
             "aoe_id_prefix": "bbb"},
            {"event_type": "transition", "timestamp": "2026-07-02T11:00:00+00:00",
             "session": "feat-tms#2", "aoe_id_prefix": "bbb",
             "from_status": "WORKING", "to_status": "BLOCKED"},
            # Another minimax dispatch (still in progress, no terminal)
            {"event_type": "dispatch", "timestamp": "2026-07-03T10:00:00+00:00",
             "repo": "tms", "issue": 3, "agent": "pi",
             "provider": "minimax", "model": "MiniMax-M3",
             "dispatch_type": "feature", "session": "feat-tms#3",
             "aoe_id_prefix": "ccc"},
        ])
        monkeypatch.setattr("tms.events.EVENTS_PATH", events_path)

        stats = compute_stats()
        pm = stats["per_model"]

        # Minimax: 2 dispatches, 1 merged, 1 in-progress
        assert "MiniMax-M3" in pm
        assert pm["MiniMax-M3"]["dispatches"] == 2
        assert pm["MiniMax-M3"]["merged"] == 1

        # DeepSeek: 1 dispatch, 0 merged, 1 blocked
        assert "deepseek-v4-pro" in pm
        assert pm["deepseek-v4-pro"]["dispatches"] == 1
        assert pm["deepseek-v4-pro"]["merged"] == 0
        assert pm["deepseek-v4-pro"]["blocked"] == 1

    def test_latency_computation(self, tmp_path, monkeypatch):
        """Issue→merge latency: dispatch timestamp → terminal/DONE timestamp."""
        from tms.events import compute_stats

        events_path = _write_fixture_events(tmp_path, [
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
            # Second issue, faster
            {"event_type": "dispatch", "timestamp": "2026-07-02T10:00:00+00:00",
             "repo": "tms", "issue": 2, "agent": "pi",
             "provider": "deepseek", "model": "deepseek-v4-pro",
             "dispatch_type": "fix", "session": "fix-tms#2",
             "aoe_id_prefix": "bbb"},
            {"event_type": "transition", "timestamp": "2026-07-02T10:30:00+00:00",
             "session": "fix-tms#2", "aoe_id_prefix": "bbb",
             "from_status": "WORKING", "to_status": "terminal"},
        ])
        monkeypatch.setattr("tms.events.EVENTS_PATH", events_path)

        stats = compute_stats()
        # Issue 1: 10:00→15:00 = 5 hours = 18000 seconds
        # Issue 2: 10:00→10:30 = 0.5 hours = 1800 seconds
        # p50 of [1800, 18000] = (1800+18000)/2 = 9900
        assert stats["latency_p50_seconds"] == pytest.approx(9900, abs=1)
        # p90 of 2 values: linear interp → 16380
        assert stats["latency_p90_seconds"] == pytest.approx(16380, abs=1)

    def test_since_filter(self, tmp_path, monkeypatch):
        """--since filter must exclude events before the cutoff date."""
        from tms.events import compute_stats

        events_path = _write_fixture_events(tmp_path, [
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
        monkeypatch.setattr("tms.events.EVENTS_PATH", events_path)

        stats = compute_stats(since="2026-07-01")
        assert stats["total_dispatches"] == 1
        pm = stats["per_model"]
        assert "deepseek-v4-pro" in pm
        assert "MiniMax-M3" not in pm

    def test_format_stats_report_text(self, tmp_path, monkeypatch, capsys):
        """format_stats_report in text mode must print a readable report."""
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

    def test_format_stats_report_json(self, tmp_path, monkeypatch, capsys):
        """format_stats_report --json must output valid JSON."""
        from tms.events import format_stats_report

        stats = {"total_dispatches": 1, "per_model": {}}
        format_stats_report(stats, as_json=True)
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["total_dispatches"] == 1
