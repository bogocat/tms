"""Tests for cache atomic writes + narrowed exception handling.

P0#1: race in cache writes — the old `_aoe_status_map` and `_tmq_registry`
      in bin/tms wrote to the cache file in-place, so a concurrent reader
      could see a half-written line. Fix: write to `$FILE.new.$$` then
      `mv -f` to the real path (atomic rename on the same filesystem).

      Python equivalent: `atomic_write_json(path, data)` does the same
      pattern (write to tmp, os.replace). Used by the Python cache
      writers (lib/tms/aoe_status.py, etc.) so the race is closed in
      Python too.

P0#2: bare `except: pass` — the old aoe-list JSON load used `except:`,
      which also caught `KeyboardInterrupt` / `SystemExit`, making Ctrl+C
      unresponsive during slow aoe calls. Fix: narrowed to specific
      exception types — `json.JSONDecodeError`, `ValueError`,
      `subprocess.TimeoutExpired`, `OSError`.

      `KeyboardInterrupt` and `SystemExit` MUST propagate.
"""

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest


# Path is added via pytest.ini's `pythonpath = lib`
from tms.atomic import atomic_write_json
from tms import aoe_status


# ── P0#1: atomic write semantics ─────────────────────────────────


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "cache.json"
    atomic_write_json(target, {"k": "v"})
    assert target.exists()
    assert json.loads(target.read_text()) == {"k": "v"}


def test_build_session_map_uses_atomic_write(tmp_path, monkeypatch):
    """`build_session_map` must use atomic_write_json, not direct open()+write.

    A regression to direct `open(out_path, 'w')` + `json.dump` would
    re-open the P0#1 cache-race on the session map path. The
    aoe_status module already does this; this test pins the session
    map module to the same contract.
    """
    from unittest.mock import patch
    from tms.session_map import build_session_map

    panes_str = ""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["tmq", "list", "--machine"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd and cmd[0] == "tmux" and "list-panes" in cmd:
            return subprocess.CompletedProcess(cmd, 0, panes_str, "")
        if cmd[0] == "aoe" and cmd[1] == "list":
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        return real_run(cmd, *args, **kwargs)

    with patch("tms.session_map.subprocess.run", side_effect=fake_run), \
         patch("tms.session_map.atomic_write_json") as mock_atomic:
        out = tmp_path / "map.json"
        build_session_map(str(out))
        mock_atomic.assert_called_once()
        # The first positional arg must be the path
        args, _kwargs = mock_atomic.call_args
        assert args[0] == str(out)


def test_atomic_write_overwrites_existing(tmp_path):
    target = tmp_path / "cache.json"
    target.write_text('{"old": true}')
    atomic_write_json(target, {"new": True})
    assert json.loads(target.read_text()) == {"new": True}


def test_atomic_write_no_tmp_files_left_behind(tmp_path):
    """The tmp file must be renamed away (not left in the dir)."""
    target = tmp_path / "cache.json"
    atomic_write_json(target, {"x": 1})
    siblings = [p.name for p in tmp_path.iterdir()]
    assert siblings == ["cache.json"], f"unexpected files: {siblings}"


def test_no_partial_read_during_write(tmp_path):
    """A reader opening the file during a write must see either the old
    or new content, never a torn write. This is the property the bash
    `tmp + mv -f` pattern provides; atomic_write_json must match it.
    """
    target = tmp_path / "cache.json"
    target.write_text(json.dumps({"v": 0}))

    # Spawn a reader that polls the file 100x during the write window.
    # The write is large enough that the reader will see at least one
    # mid-write state if atomicity is broken.
    big = {"v": "x" * 100_000}
    observations = []

    def reader():
        for _ in range(200):
            try:
                content = target.read_text()
                if content:
                    parsed = json.loads(content)  # must parse cleanly
                    observations.append(parsed)
            except (json.JSONDecodeError, FileNotFoundError, ValueError):
                # File not found or partial write — both indicate a race
                observations.append("PARTIAL")
            time.sleep(0.001)

    def writer():
        atomic_write_json(target, big)

    import threading

    t_reader = threading.Thread(target=reader)
    t_writer = threading.Thread(target=writer)
    t_reader.start()
    t_writer.start()
    t_reader.join()
    t_writer.join()

    # Every observation must be a valid dict (the old, the new, or the
    # in-flight same data). NO partial / unparseable entries.
    for obs in observations:
        assert obs != "PARTIAL", "reader saw a torn write"
        assert isinstance(obs, dict)


def test_concurrent_writers_all_succeed(tmp_path):
    """N writers racing on the same path must all complete; final file
    is valid JSON and matches one of the writers' data.
    """
    target = tmp_path / "race.json"

    def writer(i):
        atomic_write_json(target, {"writer": i})

    procs = [
        multiprocessing.Process(target=writer, args=(i,))
        for i in range(20)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        assert p.exitcode == 0, f"writer exited with {p.exitcode}"

    # Final file is valid JSON, value is one of the writers
    final = json.loads(target.read_text())
    assert "writer" in final
    assert 0 <= final["writer"] < 20


# ── P0#2: narrowed exception handling ────────────────────────────


def _build_aoe_status_map_with(monkeypatch, stdout: str = "", returncode: int = 0):
    """Build a real aoe_status_map file from a mocked aoe CLI.

    The module calls `aoe list --json` (once) and then `aoe session show
    <title> --json` (per session). We stub both via subprocess.run.
    """
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        # aoe list --json → return our fake session list
        if cmd[:3] == ["aoe", "list", "--json"]:
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")
        # aoe session show <title> --json → return a fake status
        if cmd[:2] == ["aoe", "session"] and cmd[2] == "show":
            return subprocess.CompletedProcess(
                cmd, 0, '{"status": "running"}', "",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("tms.aoe_status.subprocess.run", fake_run)


def test_aoe_invalid_json_is_skipped(tmp_path, monkeypatch):
    """Invalid JSON from aoe must be skipped (no crash) and the cache
    must still be written as a single-newline empty file (issue #21:
    no stale data is allowed to survive a parse failure)."""
    out = tmp_path / "map.tsv"
    out.write_text("stale_session\trunning\n")
    _build_aoe_status_map_with(monkeypatch, stdout="not json {", returncode=0)
    # Should exit cleanly, replacing the stale cache with empty
    aoe_status.build_status_map(str(out))
    assert out.exists()
    assert out.read_text() == "\n", (
        f"expected single-newline empty file; got {out.read_text()!r}"
    )


def test_aoe_empty_list_writes_empty_file(tmp_path, monkeypatch):
    """aoe list returns [] — must write a single-newline empty file
    so any stale cache is replaced (not left in place).

    Regression for issue #21: PR #8's extraction skipped the write
    when no rows were collected, leaving the previous cache file
    stale. The original bash heredoc always wrote (a 0-byte file);
    the new module must match — a single '\\n' is the minimal
    'valid empty TSV' shape and matches what 'no rows' looks like
    under the same writer (the normal path emits '\\n'.join(lines)+'\\n').
    The pre-populated stale data below proves the file is REPLACED,
    not appended or kept.
    """
    out = tmp_path / "map.tsv"
    out.write_text("stale_session\trunning\n")
    _build_aoe_status_map_with(monkeypatch, stdout="[]", returncode=0)
    aoe_status.build_status_map(str(out))
    assert out.exists(), "expected file to be written (even if empty)"
    assert out.read_text() == "\n", (
        f"expected single-newline empty file; got {out.read_text()!r}"
    )


def test_aoe_sessions_with_empty_titles_writes_empty_file(tmp_path, monkeypatch):
    """aoe list returns rows with empty titles — same as no rows: the
    loop drops them, the file is written as empty (single newline).
    Pre-populated stale data is replaced, proving no leakage.
    """
    out = tmp_path / "map.tsv"
    out.write_text("stale_session\trunning\n")
    _build_aoe_status_map_with(
        monkeypatch,
        stdout=json.dumps([{"id": "abc", "title": ""}]),
        returncode=0,
    )
    aoe_status.build_status_map(str(out))
    assert out.exists()
    assert out.read_text() == "\n", (
        f"expected single-newline empty file; got {out.read_text()!r}"
    )


def test_aoe_all_session_shows_fail_writes_empty_file(tmp_path, monkeypatch):
    """aoe list returns N sessions but every `aoe session show` fails
    (non-zero exit). No rows survive; the file is written as empty
    (single newline). Stale data is replaced.

    This is the second half of issue #21: even if `aoe list` is
    healthy, when all per-session lookups fail we should not show
    last-known-good statuses as if they were current.
    """
    out = tmp_path / "map.tsv"
    out.write_text("stale_session\trunning\n")
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["aoe", "list", "--json"]:
            return subprocess.CompletedProcess(
                cmd, 0,
                json.dumps([{"id": "1", "title": "a"},
                            {"id": "2", "title": "b"}]),
                "",
            )
        if cmd[:2] == ["aoe", "session"] and cmd[2] == "show":
            # All session shows fail — returncode != 0, no stdout
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("tms.aoe_status.subprocess.run", fake_run)
    aoe_status.build_status_map(str(out))
    assert out.exists()
    assert out.read_text() == "\n", (
        f"expected single-newline empty file; got {out.read_text()!r}"
    )


def test_aoe_nonzero_exit_is_skipped(tmp_path, monkeypatch):
    """Non-zero exit from `aoe list --json` is treated as 'no aoe' —
    but the cache is still WRITTEN as empty (single newline) so any
    prior stale file is replaced. Issue #21: leaving stale data
    behind is the bug; writing an empty file matches the original
    heredoc semantics.
    """
    out = tmp_path / "map.tsv"
    out.write_text("stale_session\trunning\n")
    _build_aoe_status_map_with(monkeypatch, stdout="", returncode=1)
    # Should not crash
    aoe_status.build_status_map(str(out))
    assert out.exists(), "expected file to be written (even if empty)"
    assert out.read_text() == "\n", (
        f"expected single-newline empty file; got {out.read_text()!r}"
    )


def test_aoe_timeout_is_swallowed(tmp_path, monkeypatch):
    """A hung aoe daemon (subprocess.TimeoutExpired) must not crash tms."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["aoe", "list", "--json"]:
            raise subprocess.TimeoutExpired(cmd, 3)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("tms.aoe_status.subprocess.run", fake_run)
    out = tmp_path / "map.tsv"
    # Should not raise
    aoe_status.build_status_map(str(out))


def test_aoe_oserror_is_swallowed(tmp_path, monkeypatch):
    """`aoe` not installed (FileNotFoundError) must not crash tms."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["aoe", "list", "--json"]:
            raise FileNotFoundError(2, "No such file", "aoe")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("tms.aoe_status.subprocess.run", fake_run)
    out = tmp_path / "map.tsv"
    # Should not raise
    aoe_status.build_status_map(str(out))


def test_keyboard_interrupt_propagates(tmp_path, monkeypatch):
    """KeyboardInterrupt MUST propagate (the old bare `except:` swallowed it).

    Regression for P0#2: a bare `except: pass` catches BaseException
    including KeyboardInterrupt, so Ctrl+C during a slow aoe call was
    silently ignored.
    """
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["aoe", "list", "--json"]:
            raise KeyboardInterrupt()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("tms.aoe_status.subprocess.run", fake_run)
    out = tmp_path / "map.tsv"
    with pytest.raises(KeyboardInterrupt):
        aoe_status.build_status_map(str(out))


def test_system_exit_propagates(tmp_path, monkeypatch):
    """SystemExit MUST also propagate (same bare-except class of bug)."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["aoe", "list", "--json"]:
            raise SystemExit(1)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("tms.aoe_status.subprocess.run", fake_run)
    out = tmp_path / "map.tsv"
    with pytest.raises(SystemExit):
        aoe_status.build_status_map(str(out))
