"""Tests for bin/tmq's tmq_node22_prefix helper.

Background: panes spawned from cron or the long-lived aoe daemon can
inherit a PATH where /usr/bin/node (distro v20) precedes nvm. pi is a
node script (`#!/usr/bin/env node`) whose bundled undici crashes on
Node 20 at startup (webidl.util.markAsUncloneable), which killed every
cron-dispatched review session seconds after spawn on 2026-07-29.

tmq_node22_prefix prints an inline env prefix pinning the newest nvm
node >= 22 (e.g. `PATH='/root/.nvm/versions/node/v22.19.0/bin':"$PATH" `)
so the pane is deterministic regardless of inherited PATH ordering.

These tests source bin/tmq in a throwaway bash subshell (same pattern as
test_tmq_launch.py) with NVM_DIR pointed at a tmp_path fixture.
"""

import os
import pathlib
import subprocess

TMQ = pathlib.Path(__file__).resolve().parents[1] / "bin" / "tmq"


def _mk_node(nvm_dir: pathlib.Path, version: str, executable: bool = True):
    bindir = nvm_dir / "versions" / "node" / version / "bin"
    bindir.mkdir(parents=True)
    node = bindir / "node"
    node.write_text("#!/bin/sh\n")
    if executable:
        node.chmod(0o755)


def _prefix(env_overrides: dict) -> str:
    env = dict(os.environ)
    env.pop("NVM_DIR", None)
    env.update(env_overrides)
    result = subprocess.run(
        ["bash", "-c", f'source "{TMQ}"; tmq_node22_prefix'],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        f"sourcing bin/tmq or calling tmq_node22_prefix failed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


def test_picks_newest_node_ge_22(tmp_path):
    _mk_node(tmp_path, "v20.19.2")
    _mk_node(tmp_path, "v22.15.0")
    _mk_node(tmp_path, "v22.19.0")
    out = _prefix({"NVM_DIR": str(tmp_path)})
    assert out == f"PATH='{tmp_path}/versions/node/v22.19.0/bin':\"$PATH\" "


def test_ignores_node_below_22(tmp_path):
    _mk_node(tmp_path, "v18.20.4")
    _mk_node(tmp_path, "v20.19.2")
    assert _prefix({"NVM_DIR": str(tmp_path)}) == ""


def test_handles_future_majors(tmp_path):
    _mk_node(tmp_path, "v22.19.0")
    _mk_node(tmp_path, "v24.1.0")
    out = _prefix({"NVM_DIR": str(tmp_path)})
    assert "v24.1.0" in out


def test_missing_nvm_dir_is_empty_prefix(tmp_path):
    assert _prefix({"NVM_DIR": str(tmp_path / "nonexistent")}) == ""


def test_non_executable_node_skipped(tmp_path):
    _mk_node(tmp_path, "v22.19.0", executable=False)
    assert _prefix({"NVM_DIR": str(tmp_path)}) == ""


def test_unset_nvm_dir_falls_back_to_home(tmp_path):
    # With NVM_DIR unset the helper looks under $HOME/.nvm.
    _mk_node(tmp_path / ".nvm", "v22.19.0")
    env = dict(os.environ)
    env.pop("NVM_DIR", None)
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        ["bash", "-c", f'source "{TMQ}"; tmq_node22_prefix'],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert "v22.19.0" in result.stdout
