"""Tests for worktree pooling — tmq wt claim/status/release (tms#126).

Pooling replaces the always-create-fresh worktree pattern with a
reusable pool: idle worktrees are reset (git fetch + switch) instead
of recreated, keeping node_modules warm. Claimed detection is
derived from live tmux pane cwd; dirty detection from git status.

Tests follow the established patterns for bin/tmq:
  - Structural: grep source for function/block existence
  - Behavioral: source bin/tmq in a throwaway bash subshell
"""

import json
import os
import pathlib
import subprocess
import tempfile

import pytest

TMQ = pathlib.Path(__file__).resolve().parents[1] / "bin" / "tmq"


# ── Helpers ──────────────────────────────────────────────────────

def _tmq_src():
    return TMQ.read_text()


def _source_tmq_and_run(tmp_path, function_call, env_overrides=None):
    """Source bin/tmq in a subshell and call a function."""
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    script = f'source "{TMQ}"; {function_call}'
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    return r.stdout, r.stderr, r.returncode


def _make_test_repo(path):
    """Create a minimal git repo with a main branch at `path`.

    tms#137: this sequence (git init + empty 'test'-authored 'init'
    commit) IS the clobber signature seen on the real repo family, so
    the fixture refuses any target outside a throwaway temp directory —
    it must be impossible to point at a real checkout.
    """
    resolved = pathlib.Path(path).resolve()
    tmp_root = pathlib.Path(tempfile.gettempdir()).resolve()
    if tmp_root != resolved and tmp_root not in resolved.parents:
        raise RuntimeError(
            f"_make_test_repo refuses non-temp target: {resolved} "
            f"(must be under {tmp_root})"
        )
    path.mkdir(parents=True, exist_ok=True)
    git_env = dict(os.environ, GIT_AUTHOR_NAME="test",
                   GIT_AUTHOR_EMAIL="test@test.com",
                   GIT_COMMITTER_NAME="test",
                   GIT_COMMITTER_EMAIL="test@test.com")
    subprocess.run(["git", "-C", str(path), "init", "-b", "main"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "--allow-empty",
                    "-m", "init"], capture_output=True, env=git_env)


def test_make_test_repo_refuses_non_temp_target():
    # tms#137 AC-2: the fixture must be un-runnable against real repos.
    with pytest.raises(RuntimeError, match="refuses non-temp"):
        _make_test_repo(pathlib.Path("/root/definitely-not-a-temp-dir"))


# ── AC: wt subcommand dispatch exists ────────────────────────────

def test_wt_subcommand_in_main_dispatch():
    """tmq wt must be a recognized subcommand in the main() dispatch."""
    src = _tmq_src()
    assert 'wt)' in src, "bin/tmq has no 'wt' subcommand dispatch"


def test_wt_case_arm_dispatches_to_handlers():
    """The wt case arm must dispatch to status/claim/release handlers."""
    src = _tmq_src()
    assert 'wt_status' in src, "no wt_status function reference"
    assert 'wt_claim' in src, "no wt_claim function reference"
    assert 'wt_release' in src, "no wt_release function reference"


def test_wt_no_args_prints_usage():
    """`tmq wt` with no subcommand must print usage (non-zero exit)."""
    r = subprocess.run(
        ["bash", str(TMQ), "wt"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0, "tmq wt with no args must exit non-zero"
    output = r.stdout + r.stderr
    assert 'usage' in output.lower() or 'claim' in output.lower(), (
        f"tmq wt usage missing: {output!r}"
    )


# ── AC: Pool helpers exist ───────────────────────────────────────

def test_pool_file_function_exists():
    src = _tmq_src()
    assert '_pool_file' in src, "no _pool_file function"


def test_pool_load_function_exists():
    src = _tmq_src()
    assert '_pool_load' in src, "no _pool_load function"


def test_pool_save_function_exists():
    src = _tmq_src()
    assert '_pool_save' in src, "no _pool_save function"


def test_wt_is_claimed_function_exists():
    src = _tmq_src()
    assert '_wt_is_claimed' in src, "no _wt_is_claimed function"


def test_wt_is_dirty_function_exists():
    src = _tmq_src()
    assert '_wt_is_dirty' in src, "no _wt_is_dirty function"


def test_pool_lock_function_exists():
    src = _tmq_src()
    assert '_pool_lock' in src, "no _pool_lock function"
    assert 'flock' in src, "_pool_lock must use flock for advisory locking"


def test_pool_unlock_function_exists():
    src = _tmq_src()
    assert '_pool_unlock' in src, "no _pool_unlock function"


def test_wt_install_deps_function_exists():
    src = _tmq_src()
    assert '_wt_install_deps' in src, "no _wt_install_deps function"


def test_wt_reset_accepts_two_args_only():
    """_wt_reset should take only (wt_path, branch) after dead param removal."""
    src = _tmq_src()
    # repo_path ($3) should not appear in the function signature
    m = __import__('re').search(r'_wt_reset\(\)\s*\{.*?^\}', src, __import__('re').S | __import__('re').M)
    assert m, "_wt_reset function not found"
    body = m.group(0)
    assert 'repo_path' not in body, "_wt_reset still references unused repo_path param"


# ── Behavioral: wt_status ───────────────────────────────────────

def test_wt_status_empty_pool(tmp_path):
    """wt_status shows '(pool empty)' when the pool file is empty."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    (pool_dir / "pool.json").write_text("[]")

    env = dict(os.environ, HOME=str(tmp_path / "root"))
    stdout, stderr, rc = _source_tmq_and_run(
        tmp_path,
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; wt_status',
        env_overrides=env,
    )
    assert rc == 0, f"wt_status failed: {stderr}"
    assert "pool empty" in stdout.lower(), f"missing empty pool message: {stdout}"


def test_wt_status_shows_idle_worktree(tmp_path):
    """wt_status shows an idle worktree with correct columns."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    wt_path = tmp_path / "wt-distillery-108"
    wt_path.mkdir()
    (wt_path / ".git").write_text("gitdir: /tmp/fake\n")
    pool = json.dumps([
        {"repo": "distillery", "path": str(wt_path),
         "last_used": "2026-07-30T01:00:00Z"}
    ])
    (pool_dir / "pool.json").write_text(pool)

    env = dict(os.environ, HOME=str(tmp_path / "root"))
    stdout, stderr, rc = _source_tmq_and_run(
        tmp_path,
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; '
        '_wt_is_claimed() { return 1; }; '
        '_wt_is_dirty() { return 1; }; '
        'wt_status',
        env_overrides=env,
    )
    assert rc == 0, f"wt_status failed: {stderr}"
    assert "PATH" in stdout, f"missing PATH column: {stdout}"
    assert "STATUS" in stdout, f"missing STATUS column: {stdout}"
    assert "BRANCH" in stdout, f"missing BRANCH column: {stdout}"
    assert "REPO" in stdout, f"missing REPO column: {stdout}"
    assert "DIRTY" in stdout, f"missing DIRTY column: {stdout}"
    assert "idle" in stdout.lower(), f"worktree not idle: {stdout}"


def test_wt_status_shows_claimed_worktree(tmp_path):
    """wt_status shows 'claimed' when a worktree cwd matches a tmux pane."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    wt_path = tmp_path / "wt-distillery-108"
    wt_path.mkdir()
    (wt_path / ".git").write_text("gitdir: /tmp/fake\n")
    pool = json.dumps([
        {"repo": "distillery", "path": str(wt_path),
         "last_used": "2026-07-30T01:00:00Z"}
    ])
    (pool_dir / "pool.json").write_text(pool)

    env = dict(os.environ, HOME=str(tmp_path / "root"))
    stdout, stderr, rc = _source_tmq_and_run(
        tmp_path,
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; '
        '_wt_is_claimed() { return 0; }; '
        '_wt_is_dirty() { return 1; }; '
        'wt_status',
        env_overrides=env,
    )
    assert rc == 0, f"wt_status failed: {stderr}"
    assert "claimed" in stdout.lower(), f"worktree not claimed: {stdout}"


def test_wt_status_shows_dirty_worktree(tmp_path):
    """wt_status shows 'YES' in DIRTY column for uncommitted changes."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    wt_path = tmp_path / "wt-distillery-108"
    wt_path.mkdir()
    (wt_path / ".git").write_text("gitdir: /tmp/fake\n")
    pool = json.dumps([
        {"repo": "distillery", "path": str(wt_path),
         "last_used": "2026-07-30T01:00:00Z"}
    ])
    (pool_dir / "pool.json").write_text(pool)

    env = dict(os.environ, HOME=str(tmp_path / "root"))
    stdout, stderr, rc = _source_tmq_and_run(
        tmp_path,
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; '
        '_wt_is_claimed() { return 1; }; '
        '_wt_is_dirty() { return 0; }; '
        'wt_status',
        env_overrides=env,
    )
    assert rc == 0, f"wt_status failed: {stderr}"
    assert "YES" in stdout, f"dirty flag not shown: {stdout}"


# ── Behavioral: wt_claim ────────────────────────────────────────

def test_wt_claim_creates_new_worktree_when_pool_empty(tmp_path):
    """wt_claim creates a fresh worktree when no idle ones exist."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    (pool_dir / "pool.json").write_text("[]")
    wt_root = tmp_path / "root"

    # Create a fake main repo
    main_repo = wt_root / "projects" / "distillery"
    _make_test_repo(main_repo)

    env = dict(os.environ, HOME=str(wt_root))
    script = (
        f'source "{TMQ}"; '
        f'_wt_root() {{ echo "{wt_root}"; }}; '
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; '
        f'REPO_PATH[distillery]="{main_repo}"; '
        f'REPO_GH[distillery]="bogocat/distillery"; '
        f'REPO_WORKTREE[distillery]=1; '
        f'wt_claim distillery'
    )
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert r.returncode == 0, f"wt_claim failed: {r.stderr}"
    claimed_path = r.stdout.strip()
    assert claimed_path, "wt_claim printed nothing"
    assert os.path.isdir(claimed_path), f"claimed path not a dir: {claimed_path}"

    # Verify pool was updated
    pool = json.loads((pool_dir / "pool.json").read_text())
    assert len(pool) == 1, f"pool should have 1 entry, got {len(pool)}"
    assert pool[0]["repo"] == "distillery"
    assert pool[0]["path"] == claimed_path


def test_wt_claim_skips_dirty_worktree(tmp_path):
    """wt_claim must not reuse a worktree with uncommitted changes."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    wt_root = tmp_path / "root"

    dirty_wt = wt_root / "wt-distillery-99"
    dirty_wt.mkdir(parents=True)
    (dirty_wt / ".git").write_text("gitdir: /tmp/fake\n")

    pool = json.dumps([
        {"repo": "distillery", "path": str(dirty_wt),
         "last_used": "2026-07-30T01:00:00Z"}
    ])
    (pool_dir / "pool.json").write_text(pool)

    main_repo = wt_root / "projects" / "distillery"
    _make_test_repo(main_repo)

    env = dict(os.environ, HOME=str(wt_root))
    script = (
        f'source "{TMQ}"; '
        f'_wt_root() {{ echo "{wt_root}"; }}; '
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; '
        f'_wt_is_claimed() {{ return 1; }}; '
        f'_wt_is_dirty() {{ [[ "$1" == "{dirty_wt}" ]] && return 0 || return 1; }}; '
        f'REPO_PATH[distillery]="{main_repo}"; '
        f'REPO_GH[distillery]="bogocat/distillery"; '
        f'REPO_WORKTREE[distillery]=1; '
        f'wt_claim distillery'
    )
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert r.returncode == 0, f"wt_claim failed: {r.stderr}"
    assert "dirty" in r.stderr.lower(), (
        f"should log skipping dirty worktree: stderr={r.stderr!r}"
    )
    claimed_path = r.stdout.strip()
    assert claimed_path != str(dirty_wt), f"claimed dirty worktree: {claimed_path}"


def test_wt_claim_skips_claimed_worktree(tmp_path):
    """wt_claim must not reuse a worktree that is in use (tmux cwd match)."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    wt_root = tmp_path / "root"

    claimed_wt = wt_root / "wt-distillery-99"
    claimed_wt.mkdir(parents=True)
    (claimed_wt / ".git").write_text("gitdir: /tmp/fake\n")

    pool = json.dumps([
        {"repo": "distillery", "path": str(claimed_wt),
         "last_used": "2026-07-30T01:00:00Z"}
    ])
    (pool_dir / "pool.json").write_text(pool)

    main_repo = wt_root / "projects" / "distillery"
    _make_test_repo(main_repo)

    env = dict(os.environ, HOME=str(wt_root))
    script = (
        f'source "{TMQ}"; '
        f'_wt_root() {{ echo "{wt_root}"; }}; '
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; '
        f'_wt_is_claimed() {{ [[ "$1" == "{claimed_wt}" ]] && return 0 || return 1; }}; '
        f'_wt_is_dirty() {{ return 1; }}; '
        f'REPO_PATH[distillery]="{main_repo}"; '
        f'REPO_GH[distillery]="bogocat/distillery"; '
        f'REPO_WORKTREE[distillery]=1; '
        f'wt_claim distillery'
    )
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert r.returncode == 0, f"wt_claim failed: {r.stderr}"
    claimed_path = r.stdout.strip()
    assert claimed_path != str(claimed_wt), (
        f"claimed an in-use worktree: {claimed_path}"
    )


# ── Behavioral: wt_release ──────────────────────────────────────

def test_wt_release_returns_to_pool(tmp_path):
    """wt_release returns a worktree to the idle pool (does not remove it)."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    wt_path = tmp_path / "root" / "wt-distillery-108"
    wt_path.mkdir(parents=True)
    (wt_path / ".git").write_text("gitdir: /tmp/fake\n")

    pool = json.dumps([
        {"repo": "distillery", "path": str(wt_path),
         "last_used": "2026-07-30T01:00:00Z"}
    ])
    (pool_dir / "pool.json").write_text(pool)

    env = dict(os.environ, HOME=str(tmp_path / "root"))
    script = (
        f'source "{TMQ}"; '
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; '
        f'wt_release "{wt_path}"'
    )
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert r.returncode == 0, f"wt_release failed: {r.stderr}"
    assert "Released" in r.stdout, f"missing Released confirmation: {r.stdout}"

    # Pool should still contain the entry (returned to idle, not removed)
    pool_after = json.loads((pool_dir / "pool.json").read_text())
    assert len(pool_after) == 1, f"pool should still have entry after release: {pool_after}"
    assert pool_after[0]["path"] == str(wt_path)


def test_wt_release_unknown_path_fails(tmp_path):
    """wt_release on an unregistered path must fail."""
    pool_dir = tmp_path / "root" / ".tmq"
    pool_dir.mkdir(parents=True)
    (pool_dir / "pool.json").write_text("[]")

    env = dict(os.environ, HOME=str(tmp_path / "root"))
    script = (
        f'source "{TMQ}"; '
        f'_pool_file() {{ echo "{pool_dir / "pool.json"}"; }}; '
        f'wt_release "/nonexistent/path"'
    )
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert r.returncode != 0, "wt_release with unknown path must exit non-zero"


# ── AC: create_worktree wires pool reuse ────────────────────────

def test_create_worktree_feature_checks_pool():
    """create_worktree for feature type must query the pool for idle worktrees."""
    src = _tmq_src()
    # The feature section of create_worktree must reference _pool_load
    # to check for idle worktrees before falling through to fresh creation.
    m = __import__('re').search(
        r'create_worktree\(\).*?^}', src, __import__('re').S | __import__('re').M
    )
    assert m, "create_worktree function not found"
    body = m.group(0)
    assert '_pool_load' in body, (
        "create_worktree feature section must query pool (_pool_load)"
    )
    assert '_wt_is_claimed' in body, (
        "create_worktree must check claimed status when reusing pool worktrees"
    )
    assert '_wt_is_dirty' in body, (
        "create_worktree must check dirty status when reusing pool worktrees"
    )
    assert 'Reusing pooled worktree' in body, (
        "create_worktree must log 'Reusing pooled worktree' on pool reuse"
    )


def test_create_worktree_registers_new_worktrees_in_pool():
    """Fresh worktrees created by create_worktree must be registered in pool."""
    src = _tmq_src()
    m = __import__('re').search(
        r'create_worktree\(\).*?^}', src, __import__('re').S | __import__('re').M
    )
    assert m, "create_worktree function not found"
    body = m.group(0)
    assert '_pool_save' in body, (
        "create_worktree must register new worktrees in pool (_pool_save)"
    )


def test_create_worktree_skips_dirty_pool_worktrees():
    """Dirty pool worktrees must be skipped (logged, not reused)."""
    src = _tmq_src()
    m = __import__('re').search(
        r'create_worktree\(\).*?^}', src, __import__('re').S | __import__('re').M
    )
    assert m, "create_worktree function not found"
    body = m.group(0)
    assert 'skipping dirty' in body.lower() or 'dirty worktree' in body.lower(), (
        "create_worktree must log when skipping dirty pool worktrees"
    )
