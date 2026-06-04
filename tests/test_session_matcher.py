"""End-to-end tests for build_session_map — the 4 live-aoe session
scenarios from the issue as golden snapshots, plus the P1#1 + P1#3
regression guards.

P1#3 (the 5th P0/P1 from PR #7): an aoe session with a descriptive
title like "tms issue filing" MUST NOT auto-link to whatever issue
branch is checked out at its cwd. The matcher's rule is: aoe sessions
are linked by title; scratch sessions are linked by worktree branch;
the two paths do not cross.

P1#1: a scratch session on a worktree path (where .git is a file) must
still be linked via the worktree branch. The os.path.exists check
correctly handles both files and directories; the old os.path.isdir
silently disabled this code path for worktrees.
"""

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from tms.session_map import build_session_map


# ── Mocking helpers ───────────────────────────────────────────────


def _make_subprocess_mock(panes=None, aoe_sessions=None, registry=None,
                          branch_map=None):
    """Build a fake subprocess.run that dispatches by command.

    Args:
      panes: list of (session, cmd, cwd) tuples for tmux list-panes
      aoe_sessions: list of dicts (with 'id', 'title', 'path') for aoe
      registry: tab-separated string for `tmq list --machine`
      branch_map: dict of {cwd: branch_name} for `git -C <cwd> branch`
    """
    panes = panes or []
    aoe_sessions = aoe_sessions or []
    registry = registry or ""
    branch_map = branch_map or {}

    def fake_run(cmd, *args, **kwargs):
        # tmq list --machine
        if len(cmd) >= 3 and cmd[:3] == ['tmq', 'list', '--machine']:
            return subprocess.CompletedProcess(cmd, 0, registry, "")
        # tmux list-panes -a -F ...
        if cmd and cmd[0] == 'tmux' and 'list-panes' in cmd:
            lines = ['|'.join(p) for p in panes]
            return subprocess.CompletedProcess(
                cmd, 0, '\n'.join(lines) + ('\n' if lines else ''), "",
            )
        # aoe list --json
        if cmd[:3] == ['aoe', 'list', '--json']:
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps(aoe_sessions), "",
            )
        # git -C <cwd> branch --show-current
        if cmd[0] == 'git' and cmd[1] == '-C' and 'branch' in cmd:
            cwd = cmd[2]
            return subprocess.CompletedProcess(
                cmd, 0, branch_map.get(cwd, ''), "",
            )
        # Fallback: empty success
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return fake_run


# A minimal but realistic tmq registry for the mock
FAKE_REGISTRY = "\n".join(
    [
        "distillery\t/root/projects/distillery\tbogocat/distillery\t1",
        "home-portal\t/root/projects/home-portal\tbogocat/home-portal\t1",
        "tms\t/root/tms\tbogocat/tms\t0",
        "rms\t/root/projects/rms\tbogocat/openrms\t0",
    ]
)


def _build_with_mock(**kwargs):
    """Call build_session_map with the given mock state. Return parsed JSON."""
    import tempfile
    out = tempfile.NamedTemporaryFile(
        mode='w', suffix='.json', delete=False,
    )
    out.close()
    try:
        fake_run = _make_subprocess_mock(**kwargs)
        with patch("tms.session_map.subprocess.run", side_effect=fake_run):
            build_session_map(out.name)
        return json.loads(open(out.name).read())
    finally:
        try:
            os.unlink(out.name)
        except OSError:
            pass


# ── Scenario 1: tmq-named session ────────────────────────────────


def test_tmq_named_session_links_by_name():
    """`feat-distillery#245-oc` on /root/projects/distillery → distillery#245."""
    mapping = _build_with_mock(
        panes=[("feat-distillery#245-oc", "pi", "/root/projects/distillery")],
        registry=FAKE_REGISTRY,
    )
    assert "distillery#245" in mapping
    assert mapping["distillery#245"] == ["feat-distillery#245-oc π"]


def test_tmq_named_session_with_cc_suffix():
    """`feat-home-portal#57-cc` → home-portal#57."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mapping = _build_with_mock(
            panes=[("feat-home-portal#57-cc", "claude", "/root/projects/home-portal")],
            registry=FAKE_REGISTRY,
        )
        assert "home-portal#57" in mapping
        assert mapping["home-portal#57"] == ["feat-home-portal#57-cc cc"]


def test_tmq_fix_session():
    """`fix-tms#10` (no agent suffix) → tms#10."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mapping = _build_with_mock(
            panes=[("fix-tms#10", "pi", "/root/tms")],
            registry=FAKE_REGISTRY,
        )
        assert "tms#10" in mapping
        assert mapping["tms#10"] == ["fix-tms#10 π"]


# ── Scenario 2: aoe session with linkable title ──────────────────


def test_aoe_session_links_by_title():
    """aoe session titled "feat-tms#1" → tms#1 (joined via 8-char id prefix)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mapping = _build_with_mock(
            panes=[("aoe_feat-tms_1_abc12345", "pi", "/root/wt-tms-1")],
            aoe_sessions=[{
                "id": "abc12345-1234-1234-1234-1234567890ab",
                "title": "feat-tms#1",
                "path": "/root/wt-tms-1",
                "tool": "pi",
            }],
            registry=FAKE_REGISTRY,
        )
        assert "tms#1" in mapping
        assert mapping["tms#1"] == ["aoe_feat-tms_1_abc12345 π"]


def test_aoe_session_with_review_title():
    """aoe session titled "review-distillery#245" → distillery#245."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mapping = _build_with_mock(
            panes=[("aoe_review-distillery_245_def67890", "claude", "/root/wt-distillery-245")],
            aoe_sessions=[{
                "id": "def67890-1234-1234-1234-1234567890ab",
                "title": "review-distillery#245",
                "path": "/root/wt-distillery-245",
                "tool": "claude",
            }],
            registry=FAKE_REGISTRY,
        )
        assert "distillery#245" in mapping


# ── Scenario 3: scratch session on worktree branch ──────────────


def test_scratch_session_links_by_worktree_branch(tmp_path):
    """`c5` on a main checkout (where .git is a dir) with branch feat/issue-245-foo."""
    # Set up a fake main checkout in tmp_path: .git is a DIRECTORY.
    # The cwd uses the /wt-<short>-<num> convention so
    # detect_repo_from_path resolves it (no real-filesystem dependency).
    real_distillery = tmp_path / "wt-distillery-245"
    real_distillery.mkdir(parents=True)
    (real_distillery / ".git").mkdir()  # directory, not file

    mapping = _build_with_mock(
        panes=[("c5", "pi", str(real_distillery))],
        registry=FAKE_REGISTRY,
        branch_map={str(real_distillery): "feat/issue-245-foo"},
    )
    assert "distillery#245" in mapping
    assert mapping["distillery#245"] == ["c5 π"]


def test_scratch_session_on_worktree_p1_regression(tmp_path):
    """P1#1 regression: scratch session on a worktree (where .git is a FILE).

    Creates a fake worktree in tmp_path with .git as a file (the
    worktree contract). The old os.path.isdir check returned False
    for worktrees, silently disabling this code path. The
    os.path.exists fix lets the matcher find the .git and parse
    the branch.
    """
    real_wt = tmp_path / "wt-distillery-99"
    real_wt.mkdir()
    (real_wt / ".git").write_text("gitdir: /tmp/fake\n")  # FILE, not dir

    mapping = _build_with_mock(
        panes=[("c5", "pi", str(real_wt))],
        registry=FAKE_REGISTRY,
        branch_map={str(real_wt): "feat/issue-99-foo"},
    )
    assert "distillery#99" in mapping
    assert mapping["distillery#99"] == ["c5 π"]


def test_scratch_session_with_no_issue_branch_is_ignored():
    """`c5` on main (not a feat/issue branch) → no link."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        real_distillery = "/root/projects/distillery"
        mapping = _build_with_mock(
            panes=[("c5", "pi", real_distillery)],
            registry=FAKE_REGISTRY,
            branch_map={real_distillery: "main"},
        )
        assert mapping == {}


# ── Scenario 4 (P1#3): aoe session with descriptive title ───────


def test_aoe_session_with_descriptive_title_does_not_link(tmp_path):
    """P1#3 regression: aoe session titled "tms issue filing" MUST NOT link.

    Setup: a session titled "tms issue filing" sits in /root/tms where
    the user happens to have checked out `feat/issue-1-post-migration-polish`.
    The matcher must NOT auto-link the session to tms#1 — that would be
    a false positive. The descriptive title has no #<num>, so the
    title-based path returns None, and the matcher does NOT fall back
    to the worktree branch for aoe sessions.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        real_tms = "/root/tms"
        # The matcher will try to detect_repo_from_path(/root/tms) for
        # any cross-link, but for aoe sessions it doesn't even get there.
        mapping = _build_with_mock(
            panes=[("aoe_tms_issue_filing_abc12345", "pi", real_tms)],
            aoe_sessions=[{
                "id": "abc12345-1234-1234-1234-1234567890ab",
                "title": "tms issue filing",   # descriptive, no #num
                "path": real_tms,
                "tool": "pi",
            }],
            registry=FAKE_REGISTRY,
            branch_map={real_tms: "feat/issue-1-post-migration-polish"},
        )
        # The mapping must be empty — no auto-link to tms#1
        assert mapping == {}, (
            f"false positive: descriptive-titled aoe session linked to "
            f"{list(mapping)} (P1#3 regression — must not auto-link)"
        )


# ── Scenario 5: missing cwd / unknown session ────────────────────


def test_scratch_session_with_no_cwd_is_ignored():
    """A scratch session whose pane has no cwd → no link."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mapping = _build_with_mock(
            panes=[("c5", "pi", "")],  # empty cwd
            registry=FAKE_REGISTRY,
        )
        assert mapping == {}


def test_session_without_recognized_pattern_is_ignored():
    """Random session name (not tmq / aoe / scratch) → no link."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mapping = _build_with_mock(
            panes=[("mysession", "bash", "/tmp")],
            registry=FAKE_REGISTRY,
        )
        assert mapping == {}


# ── Edge case: multiple sessions map to the same issue ──────────


def test_multiple_sessions_collapse_under_same_issue_key():
    """Two sessions on the same issue should appear in the same list."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mapping = _build_with_mock(
            panes=[
                ("feat-distillery#245", "pi", "/root/projects/distillery"),
                ("aoe_feat-distillery_245_fed98765", "claude", "/root/wt-distillery-245"),
            ],
            aoe_sessions=[{
                "id": "fed98765-1234-1234-1234-1234567890ab",
                "title": "feat-distillery#245",
                "path": "/root/wt-distillery-245",
                "tool": "claude",
            }],
            registry=FAKE_REGISTRY,
        )
        assert "distillery#245" in mapping
        # Both sessions appear (order may vary, so sort before compare)
        assert sorted(mapping["distillery#245"]) == sorted([
            "feat-distillery#245 π",
            "aoe_feat-distillery_245_fed98765 cc",
        ])
