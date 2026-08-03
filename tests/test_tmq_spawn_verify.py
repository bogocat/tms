"""Behavioral tests for spawn_agent's post-add verification (tms#117).

Background: `aoe add` for an existing (title, path) prints "Session already
exists with same title and path" and exits 0 — a silent no-op that keeps the
OLD stored command. `aoe session start` then revives a stale agent (wrong
provider/model, deleted prompt file). Observed live on feat-openagent#66
(2026-07-20): a re-dispatch with --provider deepseek ran MiniMax for ~45min.

These tests source bin/tmq and drive spawn_agent against a fake `aoe`
binary (PATH-prefixed) whose session store is a directory of files — one
per title, each holding the stored cmd-override. The fake reproduces the
real duplicate-exit-0 behavior and can simulate a broken purge and a
corrupted store.

Covered:
  - fresh dispatch               -> add + verify + start, command matches
  - stale registration           -> purge + re-add, NEW command installed (the AC repro)
  - purge ineffective            -> fail loud, never `session start`
  - stored-command mismatch      -> fail loud, never `session start`
"""

import os
import pathlib
import stat
import subprocess

import pytest

TMQ = pathlib.Path(__file__).resolve().parents[1] / "bin" / "tmq"

FAKE_AOE = r"""#!/usr/bin/env bash
# Fake aoe for spawn_agent tests (tms#117).
# Session store: $FAKE_AOE_STATE/<title> holds the stored cmd-override.
set -euo pipefail
log() { printf '%s\n' "$*" >> "$FAKE_AOE_LOG"; }
cmd=$1; shift
case "$cmd" in
  add)
    log "add $*"
    title=""; override=""
    while (($#)); do
      case "$1" in
        -t) title=$2; shift 2 ;;
        --cmd-override) override=$2; shift 2 ;;
        *) shift ;;
      esac
    done
    state="$FAKE_AOE_STATE/$title"
    if [[ -e "$state" ]]; then
      # Real aoe 1.13.0 behavior: duplicate (title, path) is a no-op that
      # keeps the old command and EXITS 0.
      echo "Session already exists with same title and path: $title"
      exit 0
    fi
    if [[ "${FAKE_AOE_STORE_WRONG:-}" == "1" ]]; then
      printf '%s' "CORRUPTED $override" > "$state"
    else
      printf '%s' "$override" > "$state"
    fi
    echo "Added session $title"
    ;;
  rm)
    log "rm $*"
    title=""; purge=0
    while (($#)); do
      case "$1" in
        --purge) purge=1; shift ;;
        *) title=$1; shift ;;
      esac
    done
    if [[ "${FAKE_AOE_PURGE_BROKEN:-}" == "1" ]]; then
      exit 0   # claims success, deletes nothing
    fi
    if (( purge )); then rm -f "$FAKE_AOE_STATE/$title"; fi
    ;;
  remove)
    log "remove $*"
    ;;
  session)
    sub=$1; shift
    case "$sub" in
      show)
        title=$1
        log "session show $title"
        state="$FAKE_AOE_STATE/$title"
        [[ -e "$state" ]] || exit 1
        python3 -c 'import json,sys;print(json.dumps({"id":"deadbeef12345678","title":sys.argv[1],"path":"/tmp","tool":"pi","command":open(sys.argv[2]).read(),"status":"idle"}))' "$title" "$state"
        ;;
      start)
        log "session start $*"
        ;;
      *)
        log "session $sub $*"
        ;;
    esac
    ;;
  *)
    log "$cmd $*"
    ;;
esac
"""

SESSION = "feat-fakerepo#1"
NEW_MODEL = "deepseek-v4-pro"
OLD_CMD = (
    "PI_DISPATCH_AUTOAPPROVE=1 pi --provider minimax --model MiniMax-M3 "
    f"@/tmp/tmq-prompt-{SESSION}.txt; echo; echo '--- PI DONE ---'; exec bash"
)


@pytest.fixture
def fake_aoe(tmp_path):
    """Install the fake aoe on PATH; yield (env, state_dir, log_path)."""
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()
    aoe = bin_dir / "aoe"
    aoe.write_text(FAKE_AOE)
    aoe.chmod(aoe.stat().st_mode | stat.S_IEXEC)
    log_path = tmp_path / "aoe-calls.log"
    log_path.touch()
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_AOE_STATE"] = str(state_dir)
    env["FAKE_AOE_LOG"] = str(log_path)
    env.pop("TMQ_NO_LOG", None)
    return env, state_dir, log_path


def _spawn(env, tmp_path, provider="deepseek", model=NEW_MODEL):
    """Source bin/tmq and run spawn_agent with an empty dispatch_repo
    (skips event logging — no DB in tests)."""
    script = (
        f'source "{TMQ}"; '
        f'PI_PROVIDER="{provider}"; PI_MODEL="{model}"; '
        f'spawn_agent "{SESSION}" "{tmp_path}" "test prompt" pi ""'
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=env, capture_output=True, text=True, timeout=30,
    )


def _expected_cmd(provider="deepseek", model=NEW_MODEL):
    return (
        f"PI_DISPATCH_AUTOAPPROVE=1 pi --provider {provider} --model {model} "
        f"@/tmp/tmq-prompt-{SESSION}.txt; echo; echo '--- PI DONE ---'; exec bash"
    )


def test_fresh_dispatch_installs_and_starts(fake_aoe, tmp_path):
    env, state_dir, log_path = fake_aoe
    result = _spawn(env, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (state_dir / SESSION).read_text() == _expected_cmd()
    assert "session start" in log_path.read_text()


def test_stale_registration_purged_and_new_command_installed(fake_aoe, tmp_path):
    """The tms#117 AC repro: dispatch, stop, re-dispatch with a different
    --model; the session command must contain the NEW model."""
    env, state_dir, log_path = fake_aoe
    (state_dir / SESSION).write_text(OLD_CMD)

    result = _spawn(env, tmp_path, provider="deepseek", model=NEW_MODEL)
    assert result.returncode == 0, result.stdout + result.stderr

    installed = (state_dir / SESSION).read_text()
    assert NEW_MODEL in installed, "re-dispatch kept the stale command"
    assert "MiniMax-M3" not in installed
    log = log_path.read_text()
    assert "rm --purge" in log or "--purge" in log, "stale record was never purged"
    assert "session start" in log


def test_purge_ineffective_fails_loud_and_never_starts(fake_aoe, tmp_path):
    env, state_dir, log_path = fake_aoe
    (state_dir / SESSION).write_text(OLD_CMD)
    env["FAKE_AOE_PURGE_BROKEN"] = "1"

    result = _spawn(env, tmp_path)
    assert result.returncode != 0, "persistent duplicate must fail the dispatch"
    assert "still reports an existing session" in result.stdout + result.stderr
    assert "session start" not in log_path.read_text(), (
        "stale session must never be started"
    )
    # The stale command is untouched — nothing ran with it.
    assert (state_dir / SESSION).read_text() == OLD_CMD


def test_stored_command_mismatch_fails_loud_and_never_starts(fake_aoe, tmp_path):
    env, state_dir, log_path = fake_aoe
    env["FAKE_AOE_STORE_WRONG"] = "1"

    result = _spawn(env, tmp_path)
    assert result.returncode != 0, "verification must reject a mismatched stored command"
    assert "DIFFERENT command" in result.stdout + result.stderr
    assert "session start" not in log_path.read_text()
