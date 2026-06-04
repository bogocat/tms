"""Tests for detect_repo_from_path (P1#1 + P1#2 regression guards).

P1#1: in git worktrees, `.git` is a *file* (containing `gitdir: ...`),
      not a directory. The old `os.path.isdir(.git)` check returned
      False for worktrees, silently disabling the matcher for them.
      Fix: use `os.path.exists` (works for both files and dirs).

P1#2: the worktree-convention regex must use `re.search`, not
      `re.match`. The old code anchored at position 0 with
      `re.match(r'/wt-...', path)`, but the path may be
      `/root/wt-distillery-108` (not starting with `/wt-`). The
      `re.match` call returned None, so the convention never fired.
      Fix: `re.search(r'/wt-...', path)`.

These tests cover the matcher against the P0/P1 scenarios from
PR #7's multi-model review.
"""

import os
import json
import subprocess
from unittest.mock import patch

import pytest

from tms.session_map import detect_repo_from_path


# A minimal but realistic tmq registry for the mocked subprocess call.
# Fields: short<TAB>local_path<TAB>gh_repo<TAB>needs_worktree
FAKE_REGISTRY = "\n".join(
    [
        "distillery\t/root/projects/distillery\tbogocat/distillery\t1",
        "home-portal\t/root/projects/home-portal\tbogocat/home-portal\t1",
        "tms\t/root/tms\tbogocat/tms\t0",
        "rms\t/root/projects/rms\tbogocat/openrms\t0",
    ]
)


@pytest.fixture
def fake_tmq_registry():
    """Patch subprocess.run to return our fake registry for tmq list."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["tmq", "list", "--machine"]:
            # Return a CompletedProcess-like object
            cp = subprocess.CompletedProcess(
                cmd, 0, stdout=FAKE_REGISTRY, stderr=""
            )
            return cp
        return real_run(cmd, *args, **kwargs)

    with patch("tms.session_map.subprocess.run", side_effect=fake_run):
        yield FAKE_REGISTRY


# ── P1#1: worktree .git is a file, not a directory ───────────────


def test_worktree_file_structure_contract(tmp_path):
    """The git worktree contract: .git is a FILE, not a directory.

    Documents the contract that the matcher relies on. The P1#1
    production regression is caught by
    `test_scratch_session_on_worktree_p1_regression` in
    `test_session_matcher.py` — this test only asserts the
    filesystem invariant.
    """
    fake_wt = tmp_path / "wt-distillery-108"
    fake_wt.mkdir()
    (fake_wt / ".git").write_text("gitdir: /tmp/fake\n")

    dot_git = fake_wt / ".git"
    # The fix: os.path.exists works for both file and dir
    assert os.path.exists(dot_git)
    # The contract: in worktrees, .git is a FILE
    assert os.path.isfile(dot_git)
    # The bug: os.path.isdir returns False for the worktree case
    assert not os.path.isdir(dot_git)


def test_scratch_session_on_fake_worktree_p1_regression(tmp_path):
    """P1#1 production regression: a scratch session on a worktree path
    (where .git is a FILE) must still be linked via the worktree branch.

    The old matcher used `os.path.isdir(cwd + '/.git')` which returned
    False for worktrees, silently disabling this code path. The fix
    uses `os.path.exists`, which works for both files and directories.

    This test calls the production `build_session_map` against a fake
    worktree created in tmp_path — so it runs in any environment (no
    dependency on the real /root/wt-tms-8 path).
    """
    import json
    from unittest.mock import patch
    from tms.session_map import build_session_map

    fake_wt = tmp_path / "wt-distillery-99"
    fake_wt.mkdir()
    (fake_wt / ".git").write_text("gitdir: /tmp/fake\n")

    panes = [("c5", "pi", str(fake_wt))]
    panes_str = '\n'.join('|'.join(p) for p in panes)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["tmq", "list", "--machine"]:
            return subprocess.CompletedProcess(cmd, 0, FAKE_REGISTRY, "")
        if cmd and cmd[0] == "tmux" and "list-panes" in cmd:
            return subprocess.CompletedProcess(cmd, 0, panes_str, "")
        if cmd[0] == "git" and cmd[1] == "-C" and "branch" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "feat/issue-99-test", "")
        return real_run(cmd, *args, **kwargs)

    out = tmp_path / "map.json"
    with patch("tms.session_map.subprocess.run", side_effect=fake_run):
        build_session_map(str(out))
    mapping = json.loads(out.read_text())

    assert "distillery#99" in mapping, (
        f"scratch session on a worktree-style path was not linked "
        f"(P1#1 regression — isdir check skipped the session); "
        f"mapping={mapping}"
    )


# ── P1#2: worktree convention uses re.search, not re.match ───────


def test_worktree_convention_matches_at_any_path_position(fake_tmq_registry):
    """`/root/wt-distillery-108` MUST resolve to `distillery`.

    P1#2 regression: the old code used `re.match(r'/wt-...')` which
    anchors at position 0. The path starts with `/root`, not `/wt-`,
    so re.match returned None. Fix: `re.search` finds the pattern
    anywhere in the path.
    """
    assert detect_repo_from_path("/root/wt-distillery-108") == "distillery"


def test_worktree_convention_matches_with_trailing_slash(fake_tmq_registry):
    """`/root/wt-home-portal-57/subdir/` also resolves via the convention."""
    assert (
        detect_repo_from_path("/root/wt-home-portal-57/subdir/") == "home-portal"
    )


def test_worktree_convention_ignores_unknown_shortname(fake_tmq_registry):
    """The worktree shortname must be a KNOWN repo — `wt-random-1` → ''.

    Prevents misidentifying random paths like `/var/log/wt-foo-99.log`
    as worktrees.
    """
    assert detect_repo_from_path("/root/wt-notarepo-1") == ""


def test_worktree_convention_requires_issue_number(fake_tmq_registry):
    """The worktree pattern requires a `-<num>` suffix: `wt-distillery` → ''.

    (Distinguishes from `wt-distillery-108` which has the trailing number.)
    """
    assert detect_repo_from_path("/root/wt-distillery") == ""


# ── Longest-prefix match (the canonical path) ─────────────────────


def test_exact_path_match(fake_tmq_registry):
    assert detect_repo_from_path("/root/projects/distillery") == "distillery"


def test_subpath_match(fake_tmq_registry):
    assert (
        detect_repo_from_path("/root/projects/distillery/scripts/foo.py")
        == "distillery"
    )


def test_subpath_match_for_tms(fake_tmq_registry):
    # /root/tms is the actual worktree for this repo
    assert detect_repo_from_path("/root/tms") == "tms"


# ── No match / edge cases ────────────────────────────────────────


def test_returns_empty_for_unknown_path(fake_tmq_registry):
    assert detect_repo_from_path("/var/log/something") == ""


def test_returns_empty_for_empty_path(fake_tmq_registry):
    assert detect_repo_from_path("") == ""


def test_returns_empty_when_tmq_subprocess_fails():
    """If `tmq list --machine` fails, we get no rows → no detection."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["tmq", "list", "--machine"]:
            cp = subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="tmq not found"
            )
            return cp
        return real_run(cmd, *args, **kwargs)

    with patch("tms.session_map.subprocess.run", side_effect=fake_run):
        assert detect_repo_from_path("/root/projects/distillery") == ""
