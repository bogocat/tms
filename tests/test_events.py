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
