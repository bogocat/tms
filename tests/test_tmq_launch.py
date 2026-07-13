"""Tests for the TMS_TMQ_LAUNCH opt-in auto-attach env var (tms#12).

Background: tmq's default behavior is dispatch-then-return — the fzf flow in
`tms` returns the user to the issue list after spawning the agent, rather than
dropping them into the agent's tmux session. Auto-attach (switching the user's
tmux client into the new session) is opt-in via `TMS_TMQ_LAUNCH=1`, intended
for direct `tmq` invocations where the user wants to drop straight in.

These tests source `bin/tmq` and exercise the `_tmq_want_auto_attach` helper
that gates the opt-in, covering:
  - default (unset)           → no auto-attach (the core regression guard)
  - empty / "true" / "yes"    → no auto-attach (only the literal "1" opts in)
  - "1"                       → auto-attach

The companion invariants — that `aoe session start` always runs regardless
(agent always launches), and that attach only runs after start succeeds —
are structural and asserted by reading spawn_agent (see AC4 in the PR).
"""

import os
import pathlib
import subprocess

import pytest

TMQ = pathlib.Path(__file__).resolve().parents[1] / "bin" / "tmq"


def _auto_attach(env_value):
    """Source bin/tmq and report whether _tmq_want_auto_attach is true.

    Returns "yes" or "no". Runs in a throwaway bash subshell so the sourced
    `set -euo pipefail` and top-level repo-registry init can't leak.
    """
    env = dict(os.environ)
    # Strip any ambient TMS_TMQ_LAUNCH from the test runner's environment so
    # the (unset) case is genuinely unset, not just empty.
    if env_value is None:
        env.pop("TMS_TMQ_LAUNCH", None)
    else:
        env["TMS_TMQ_LAUNCH"] = env_value

    script = (
        f'source "{TMQ}"; '
        f'if _tmq_want_auto_attach; then echo yes; else echo no; fi'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"sourcing bin/tmq or calling _tmq_want_auto_attach failed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout.strip()


# ── AC1: default preserves dispatch-then-return ──────────────────


def test_default_does_not_auto_attach():
    """Unset TMS_TMQ_LAUNCH → no auto-attach. This is the whole point of the
    opt-in: the tms fzf flow must return to the issue list, not drop into the
    agent session."""
    assert _auto_attach(None) == "no"


# ── AC2: literal "1" opts in ──────────────────────────────────────


def test_opt_in_auto_attach():
    """TMS_TMQ_LAUNCH=1 → auto-attach (direct tmq invocations drop in)."""
    assert _auto_attach("1") == "yes"


# ── AC3: only the literal "1" opts in (robustness) ────────────────


@pytest.mark.parametrize("noise", ["", "true", "yes", "TRUE", "0", "on"])
def test_non_one_does_not_auto_attach(noise):
    """Empty string and common truthy spellings must NOT opt in. Avoids
    accidental auto-attach from env noise — only the explicit "1" is a
    deliberate opt-in."""
    assert _auto_attach(noise) == "no"
