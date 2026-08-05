"""Tests for the git-trace wrapper (tms#137 AC-1).

Behavioral: copy scripts/git-trace-wrapper.sh into a temp dir with its
TRACE_LOG pointed at a temp file, run real git through it, and assert on
the trace. The concurrency test pins the single-write append contract —
records from parallel traced invocations must never interleave.
"""

import pathlib
import re
import subprocess
import sys

import pytest

WRAPPER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "git-trace-wrapper.sh"
)

RECORD_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    r"\tcwd=.*\tchain=.*\ttmux=.*\tparent=.*\targv=git .+$"
)


@pytest.fixture
def traced_git(tmp_path):
    """Install the wrapper as `git` in tmp_path, tracing to tmp_path/trace.log."""
    log = tmp_path / "trace.log"
    src = WRAPPER.read_text()
    src = src.replace(
        "TRACE_LOG=/var/log/git-trace.log", f"TRACE_LOG={log}"
    )
    git = tmp_path / "git"
    git.write_text(src)
    git.chmod(0o755)
    return git, log


def test_traced_subcommand_logs_one_complete_record(traced_git, tmp_path):
    git, log = traced_git
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([str(git), "-C", str(repo), "init", "-b", "main"],
                   capture_output=True, check=True)
    lines = [l for l in log.read_text().splitlines() if l]
    assert len(lines) == 1
    assert RECORD_RE.match(lines[0]), lines[0]
    assert lines[0].endswith(f"argv=git -C {repo} init -b main")


def test_untraced_subcommand_logs_nothing(traced_git, tmp_path):
    git, log = traced_git
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([str(git), "-C", str(repo), "init", "-b", "main", "-q"],
                   capture_output=True, check=True)
    before = log.read_text()
    subprocess.run([str(git), "-C", str(repo), "status"],
                   capture_output=True, check=True)
    subprocess.run([str(git), "--version"], capture_output=True, check=True)
    assert log.read_text() == before


def test_exit_code_passthrough(traced_git):
    git, _ = traced_git
    r = subprocess.run([str(git), "definitely-not-a-subcommand"],
                       capture_output=True)
    assert r.returncode != 0


def test_unwritable_log_does_not_break_git(traced_git, tmp_path):
    git, _ = traced_git
    src = git.read_text().replace(
        f"TRACE_LOG={tmp_path / 'trace.log'}", "TRACE_LOG=/proc/version"
    )
    git.write_text(src)
    repo = tmp_path / "repo"
    repo.mkdir()
    r = subprocess.run([str(git), "-C", str(repo), "init", "-b", "main"],
                       capture_output=True)
    assert r.returncode == 0


def test_concurrent_traced_writes_do_not_interleave(traced_git, tmp_path):
    """P1 from PR #144 round 1: each record must land as a single write.

    40 parallel traced invocations, each with a unique argv marker; every
    non-empty log line must be one complete record and every marker must
    appear exactly once.
    """
    git, log = traced_git
    n = 40
    procs = []
    for i in range(n):
        repo = tmp_path / f"repo{i}"
        repo.mkdir()
        procs.append(subprocess.Popen(
            [str(git), "-C", str(repo), "init", "-b", f"marker-{i:03d}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    for p in procs:
        assert p.wait() == 0
    lines = [l for l in log.read_text().splitlines() if l]
    assert len(lines) == n
    for line in lines:
        assert RECORD_RE.match(line), f"corrupt/interleaved record: {line!r}"
    markers = sorted(
        m for line in lines
        for m in re.findall(r"marker-(\d{3})$", line)
    )
    assert markers == [f"{i:03d}" for i in range(n)]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
