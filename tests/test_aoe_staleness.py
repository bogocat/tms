"""Tests for the aoe staleness watchdog (issue #56).

The watchdog tracks when each aoe session's <<AGENT-STATE: ...>> marker
last changed. If a session reports `running` but its marker hasn't
changed in > TMS_STALE_THRESHOLD_MINUTES (default 7), the status is
emitted as `stale:Nm` instead of `running`.

Tests use monkeypatch on subprocess.run and time.time to avoid touching
real tmux panes or aoe daemons.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

from tms.aoe_status import (
    FORMAT_TABLE,
    STALE_THRESHOLD_MINUTES,
    _load_staleness_state,
    _save_staleness_state,
    _prune_staleness_state,
    _capture_marker,
    _compute_status,
    build_status_map,
    format_status,
    log_staleness_event,
)


# ── helpers ───────────────────────────────────────────────────────


def _state_file_path():
    return os.path.join(tempfile.gettempdir(), 'tms-aoe-staleness.json')


# ── FORMAT_TABLE staleness entry ──────────────────────────────────


def test_format_table_has_stale_entry():
    """FORMAT_TABLE must include a 'stale' entry with a 4-char label."""
    assert 'stale' in FORMAT_TABLE, "FORMAT_TABLE missing 'stale' entry"
    label, color = FORMAT_TABLE['stale']
    assert len(label) == 4, f"stale label {label!r} is {len(label)} chars, expected 4"
    assert label == 'stal', f"stale label should be 'stal', got {label!r}"


def test_stale_color_is_magenta_bold():
    """Stale status should be visually distinct — magenta bold (35;1)."""
    _, color = FORMAT_TABLE['stale']
    assert color == '35;1', f"stale color should be '35;1', got {color!r}"


def test_format_status_stale_prefix():
    """format_status('stale:12m') should return the stale label/color."""
    label, color = format_status('stale:12m')
    assert label == 'stal'
    assert color == '35;1'


def test_format_status_stale_zero_minutes():
    """format_status('stale:0m') should also use the stale entry."""
    label, color = format_status('stale:0m')
    assert label == 'stal'
    assert color == '35;1'


# ── state file I/O ────────────────────────────────────────────────


def test_load_staleness_state_empty(tmp_path):
    """Loading from a non-existent file returns an empty dict."""
    path = tmp_path / 'nonexistent.json'
    state = _load_staleness_state(str(path))
    assert state == {}


def test_load_staleness_state_corrupt(tmp_path):
    """Loading corrupt JSON returns an empty dict (resilient)."""
    path = tmp_path / 'corrupt.json'
    path.write_text('not json')
    state = _load_staleness_state(str(path))
    assert state == {}


def test_save_and_load_staleness_state_roundtrip(tmp_path):
    """Saving state and loading it back preserves all data."""
    path = str(tmp_path / 'state.json')
    state = {
        'feat-tms#56': {
            'last_marker': '<<AGENT-STATE: WORKING>>',
            'last_marker_at': 1752000000.0,
            'last_seen_at': 1752000060.0,
        },
    }
    _save_staleness_state(path, state)
    loaded = _load_staleness_state(path)
    assert loaded == state


def test_prune_staleness_state_removes_old_entries(tmp_path):
    """Entries not seen in > 24 hours are pruned."""
    path = str(tmp_path / 'state.json')
    now = 1752000000.0
    state = {
        'recent': {
            'last_marker': 'WORKING',
            'last_marker_at': now - 3600,  # 1 hour ago
            'last_seen_at': now - 60,
        },
        'stale_entry': {
            'last_marker': 'WORKING',
            'last_marker_at': now - 100000,  # > 24 hours ago
            'last_seen_at': now - 100000,
        },
    }
    _save_staleness_state(path, state)
    pruned = _prune_staleness_state(path, now=now)
    assert 'recent' in pruned
    assert 'stale_entry' not in pruned


# ── marker capture ────────────────────────────────────────────────


def test_capture_marker_finds_working(monkeypatch):
    """When pane has <<AGENT-STATE: WORKING>>, capture_marker finds it."""
    pane_lines = '\n'.join([
        'some output',
        '<<AGENT-STATE: WORKING>>',
        'more output',
    ])

    def _mock_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout=pane_lines, stderr='')
        return result

    monkeypatch.setattr(subprocess, 'run', _mock_run)
    marker = _capture_marker('session_name', lines=50)
    assert marker == '<<AGENT-STATE: WORKING>>'


def test_capture_marker_returns_empty_when_no_marker(monkeypatch):
    """When pane has no AGENT-STATE marker, returns empty string."""
    def _mock_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout='just output\nno marker here\n', stderr='')
        return result

    monkeypatch.setattr(subprocess, 'run', _mock_run)
    marker = _capture_marker('session_name', lines=50)
    assert marker == ''


def test_capture_marker_returns_most_recent_when_multiple(monkeypatch):
    """When multiple markers are in the scrollback window, returns the LAST (most recent).

    tmux capture-pane prints oldest-to-newest top-to-bottom. If an agent
    transitions PLAN-REVIEW → WORKING within the capture window, both
    markers are visible. _capture_marker must return the most recent one
    so the staleness timer resets correctly on the transition.
    """
    pane_lines = '\n'.join([
        '<<AGENT-STATE: PLAN-REVIEW>>',   # older — older in scrollback
        'some output',
        '<<AGENT-STATE: WORKING>>',       # newer — should be the match
        'more recent output',
    ])

    def _mock_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout=pane_lines, stderr='')
        return result

    monkeypatch.setattr(subprocess, 'run', _mock_run)
    marker = _capture_marker('session_name', lines=50)
    assert marker == '<<AGENT-STATE: WORKING>>', (
        f"expected most recent WORKING marker, got {marker!r}"
    )


def test_capture_marker_handles_tmux_failure(monkeypatch):
    """When tmux capture-pane fails, returns empty string gracefully."""
    def _mock_run(cmd, **kwargs):
        if cmd[0] == 'tmux':
            raise OSError('tmux not found')
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

    monkeypatch.setattr(subprocess, 'run', _mock_run)
    marker = _capture_marker('session_name', lines=50)
    assert marker == ''


# ── status computation (the core logic) ────────────────────────────


def test_compute_status_new_session_not_stale(monkeypatch):
    """A session seen for the first time is never stale — initialized at now."""
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)

    state = {}
    result = _compute_status(
        state, 'feat-tms#56', 'running', '<<AGENT-STATE: WORKING>>',
        threshold_minutes=7, now=now,
    )
    # New session: last_marker_at = now → not stale
    assert result['status'] == 'running'
    assert result['state']['last_marker'] == '<<AGENT-STATE: WORKING>>'
    assert result['state']['last_marker_at'] == now


def test_compute_status_marker_changed_resets_timer(monkeypatch):
    """When marker changes, last_marker_at resets to now."""
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)

    state = {
        'feat-tms#56': {
            'last_marker': '<<AGENT-STATE: PLAN-REVIEW>>',
            'last_marker_at': now - 600,  # 10 min ago
            'last_seen_at': now - 10,
        },
    }
    result = _compute_status(
        state, 'feat-tms#56', 'running', '<<AGENT-STATE: WORKING>>',
        threshold_minutes=7, now=now,
    )
    assert result['status'] == 'running'
    assert result['state']['last_marker'] == '<<AGENT-STATE: WORKING>>'
    assert result['state']['last_marker_at'] == now


def test_compute_status_marker_unchanged_within_threshold(monkeypatch):
    """When marker is unchanged but still within threshold, not stale."""
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)

    state = {
        'feat-tms#56': {
            'last_marker': '<<AGENT-STATE: WORKING>>',
            'last_marker_at': now - 300,  # 5 min ago
            'last_seen_at': now - 10,
        },
    }
    result = _compute_status(
        state, 'feat-tms#56', 'running', '<<AGENT-STATE: WORKING>>',
        threshold_minutes=7, now=now,
    )
    assert result['status'] == 'running'


def test_compute_status_marker_unchanged_at_exact_threshold(monkeypatch):
    """When elapsed exactly equals threshold, session is stale (inclusive)."""
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)

    state = {
        'feat-tms#56': {
            'last_marker': '<<AGENT-STATE: WORKING>>',
            'last_marker_at': now - 420,  # exactly 7 min ago
            'last_seen_at': now - 10,
        },
    }
    result = _compute_status(
        state, 'feat-tms#56', 'running', '<<AGENT-STATE: WORKING>>',
        threshold_minutes=7, now=now,
    )
    assert result['status'] == 'stale:7m'


def test_compute_status_marker_unchanged_past_threshold(monkeypatch):
    """When marker is unchanged past threshold, emit stale:Nm."""
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)

    state = {
        'feat-tms#56': {
            'last_marker': '<<AGENT-STATE: WORKING>>',
            'last_marker_at': now - 540,  # 9 min ago
            'last_seen_at': now - 10,
        },
    }
    result = _compute_status(
        state, 'feat-tms#56', 'running', '<<AGENT-STATE: WORKING>>',
        threshold_minutes=7, now=now,
    )
    assert result['status'] == 'stale:9m'


def test_compute_status_no_marker_uses_last_seen_proxy(monkeypatch):
    """When no marker is found but session was seen recently with one,
    use last_seen_at as proxy for staleness detection."""
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)

    # Session was running with a marker, now the marker isn't visible
    # but it's been > threshold since the marker changed AND we've been
    # seeing it without a marker for a while
    state = {
        'feat-tms#56': {
            'last_marker': '<<AGENT-STATE: WORKING>>',
            'last_marker_at': now - 600,   # 10 min ago (past threshold)
            'last_seen_at': now - 120,     # last seen 2 min ago
        },
    }
    result = _compute_status(
        state, 'feat-tms#56', 'running', '',  # no marker found now
        threshold_minutes=7, now=now,
    )
    # Marker last changed 10 min ago (> 7 min threshold)
    assert result['status'] == 'stale:10m'


def test_compute_status_non_running_skipped(monkeypatch):
    """Sessions not in 'running' state skip staleness check entirely."""
    state = {}
    result = _compute_status(
        state, 'feat-tms#56', 'idle', '<<AGENT-STATE: WORKING>>',
        threshold_minutes=7, now=1752000000.0,
    )
    assert result['status'] == 'idle'
    assert result['state'] is None  # state not tracked for non-running


def test_compute_status_threshold_from_env(monkeypatch):
    """Threshold is read from TMS_STALE_THRESHOLD_MINUTES env var."""
    monkeypatch.setenv('TMS_STALE_THRESHOLD_MINUTES', '3')
    assert STALE_THRESHOLD_MINUTES() == 3


def test_compute_status_threshold_default(monkeypatch):
    """Default threshold is 7 when env var is not set."""
    monkeypatch.delenv('TMS_STALE_THRESHOLD_MINUTES', raising=False)
    assert STALE_THRESHOLD_MINUTES() == 7


# ── staleness event logging ───────────────────────────────────────


def test_log_staleness_event_emits_json_to_stderr(capsys):
    """log_staleness_event writes structured JSON to stderr."""
    log_staleness_event('feat-tms#56', 12, now=1752000000.0)
    captured = capsys.readouterr()
    event = json.loads(captured.err.strip())
    assert event['type'] == 'staleness'
    assert event['session'] == 'feat-tms#56'
    assert event['stale_minutes'] == 12
    assert event['timestamp'] == 1752000000.0
    assert 'threshold_minutes' in event


def test_log_staleness_event_includes_threshold(capsys):
    """The event includes the configured threshold."""
    log_staleness_event('feat-tms#56', 8, threshold_minutes=5, now=1752000000.0)
    captured = capsys.readouterr()
    event = json.loads(captured.err.strip())
    assert event['threshold_minutes'] == 5


def test_build_status_map_dedup_staleness_events(tmp_path, monkeypatch):
    """Staleness events only fire once on transition, not on every refresh."""
    out_path = str(tmp_path / 'status.tsv')
    state_path = str(tmp_path / 'staleness.json')
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)

    # Pre-populate: session has been in same marker for 10 min (past 7-min threshold)
    # and we've already emitted a staleness event for 7m.
    _save_staleness_state(state_path, {
        'feat-tms#56': {
            'last_marker': '<<AGENT-STATE: WORKING>>',
            'last_marker_at': now - 600,  # 10 min ago
            'last_seen_at': now - 10,
            'last_emitted_stale_minutes': 7,  # already logged at 7m
        },
    })

    sessions = [
        {'id': 'abc123def456', 'title': 'feat-tms#56', 'status': 'running'},
    ]
    pane_lines = '<<AGENT-STATE: WORKING>>\n'

    import io
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()

    try:
        def _mock_run(cmd, **kwargs):
            result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')
            if cmd[0] == 'aoe' and cmd[1] == 'list':
                result.stdout = json.dumps(sessions)
            elif cmd[0] == 'aoe' and cmd[1] == 'session' and cmd[2] == 'show':
                result.stdout = json.dumps(sessions[0])
            elif cmd[0] == 'tmux' and cmd[1] == 'capture-pane':
                result.stdout = pane_lines
            return result

        monkeypatch.setattr(subprocess, 'run', _mock_run)
        monkeypatch.setattr('tms.aoe_status._STALENESS_STATE_PATH', state_path)

        # First call: stale for 10m but last logged at 7m — should emit
        build_status_map(out_path)
        events1 = sys.stderr.getvalue()

        # Reset stderr
        sys.stderr = io.StringIO()

        # Second call: same state, same stale_minutes (10m), same last_emitted
        # (the state file was updated to last_emitted_stale_minutes=10 by first call)
        build_status_map(out_path)
        events2 = sys.stderr.getvalue()

        # First call emits (10m != 7m)
        assert len(events1.strip().split('\n')) == 1, (
            f"first call should emit one event, got: {events1!r}"
        )
        # Second call should NOT emit (10m == 10m, same as last emitted)
        assert events2.strip() == '', (
            f"second call should not emit event, got: {events2!r}"
        )
    finally:
        sys.stderr = old_stderr


# ── integration: build_status_map with staleness ──────────────────


def test_build_status_map_emits_stale_for_stuck_session(tmp_path, monkeypatch):
    """End-to-end: a session running > threshold with same marker emits stale."""
    out_path = str(tmp_path / 'status.tsv')
    state_path = str(tmp_path / 'staleness.json')
    # Pre-populate state: session seen 10 min ago with WORKING marker
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)
    _save_staleness_state(state_path, {
        'feat-tms#56': {
            'last_marker': '<<AGENT-STATE: WORKING>>',
            'last_marker_at': now - 600,  # 10 min ago
            'last_seen_at': now - 10,
        },
    })

    sessions = [
        {'id': 'abc123def456', 'title': 'feat-tms#56', 'status': 'running'},
    ]
    pane_lines = '<<AGENT-STATE: WORKING>>\nsome output\n'

    def _mock_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')
        if cmd[0] == 'aoe' and cmd[1] == 'list':
            result.stdout = json.dumps(sessions)
        elif cmd[0] == 'aoe' and cmd[1] == 'session' and cmd[2] == 'show':
            for s in sessions:
                if s['title'] == cmd[3]:
                    result.stdout = json.dumps(s)
                    break
            else:
                result.returncode = 1
        elif cmd[0] == 'tmux' and cmd[1] == 'capture-pane':
            result.stdout = pane_lines
        return result

    monkeypatch.setattr(subprocess, 'run', _mock_run)
    monkeypatch.setattr('tms.aoe_status._STALENESS_STATE_PATH', state_path)

    build_status_map(out_path)

    lines = open(out_path).read().strip().split('\n')
    assert len(lines) >= 1
    assert 'feat-tms#56\tstale:10m' in lines[0]


def test_build_status_map_emits_running_for_fresh_session(tmp_path, monkeypatch):
    """End-to-end: a fresh session with a marker should emit 'running'."""
    out_path = str(tmp_path / 'status.tsv')
    state_path = str(tmp_path / 'staleness.json')
    now = 1752000000.0
    monkeypatch.setattr(time, 'time', lambda: now)

    sessions = [
        {'id': 'abc123def456', 'title': 'feat-tms#56', 'status': 'running'},
    ]
    pane_lines = '<<AGENT-STATE: WORKING>>\n'

    def _mock_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')
        if cmd[0] == 'aoe' and cmd[1] == 'list':
            result.stdout = json.dumps(sessions)
        elif cmd[0] == 'aoe' and cmd[1] == 'session' and cmd[2] == 'show':
            result.stdout = json.dumps(sessions[0])
        elif cmd[0] == 'tmux' and cmd[1] == 'capture-pane':
            result.stdout = pane_lines
        return result

    monkeypatch.setattr(subprocess, 'run', _mock_run)
    monkeypatch.setattr('tms.aoe_status._STALENESS_STATE_PATH', state_path)

    build_status_map(out_path)

    lines = open(out_path).read().strip().split('\n')
    assert 'feat-tms#56\trunning' in lines[0]


def test_build_status_map_passes_through_non_running(tmp_path, monkeypatch):
    """Non-running sessions are passed through unchanged (no staleness check)."""
    out_path = str(tmp_path / 'status.tsv')
    state_path = str(tmp_path / 'staleness.json')
    monkeypatch.setattr(time, 'time', lambda: 1752000000.0)

    sessions = [
        {'id': 'abc123def456', 'title': 'feat-tms#56', 'status': 'idle'},
        {'id': 'def789ghi012', 'title': 'feat-home-portal#255', 'status': 'error'},
    ]

    def _mock_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')
        if cmd[0] == 'aoe' and cmd[1] == 'list':
            result.stdout = json.dumps(sessions)
        elif cmd[0] == 'aoe' and cmd[1] == 'session' and cmd[2] == 'show':
            for s in sessions:
                if s['title'] == cmd[3]:
                    result.stdout = json.dumps(s)
                    break
        return result

    monkeypatch.setattr(subprocess, 'run', _mock_run)
    monkeypatch.setattr('tms.aoe_status._STALENESS_STATE_PATH', state_path)

    build_status_map(out_path)

    content = open(out_path).read()
    assert 'feat-tms#56\tidle' in content
    assert 'feat-home-portal#255\terror' in content
