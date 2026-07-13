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


# ── AC4 (structural): attach is additive, never replaces start ────
#
# These are static source-grep assertions, not behavioral tests. They lock in
# the load-bearing invariant that `aoe session start` always launches the
# agent and `aoe session attach` only runs AFTER start succeeds — so the
# opt-in path can never regress to the earlier 59cf300 design that swapped
# start->attach (where attach-alone might not spawn the tmux process).


def _enclosing_if_line(lines, cmd_prefix):
    """Return the text of the `if ...` line that immediately encloses the
    first line beginning (after indent) with `cmd_prefix`.

    Walks backward line-by-line from the command, tracking `if`/`fi` nesting
    so an intervening closed `if ... fi` block can't masquerade as the
    enclosing gate. The first un-closed `if ` line reached (at depth 0) is
    the command's own gate.
    """
    cmd_i = next(
        (i for i, ln in enumerate(lines) if ln.lstrip().startswith(cmd_prefix)),
        None,
    )
    if cmd_i is None:
        raise AssertionError(f"no line starting with `{cmd_prefix}` found")

    # Walk backward, tracking nesting: a `fi` at the start of a line means
    # we've dipped into a *more deeply nested* block (skip past its matching
    # `if`); an un-closed `if` is our enclosing gate.
    depth = 0
    for j in range(cmd_i - 1, -1, -1):
        stripped = lines[j].lstrip()
        if stripped.startswith("fi"):
            depth += 1
        elif stripped.startswith("if "):
            if depth == 0:
                return lines[j]
            depth -= 1
    raise AssertionError(f"no enclosing `if` found for `{cmd_prefix}` call")


def test_ac4_start_is_unconditional():
    """`aoe session start` must run whenever add_ok, independent of the
    auto-attach flag. Asserts the start call's own gate references only
    add_ok (not the auto-attach flag), so start can never become opt-in."""
    gate = _enclosing_if_line(TMQ.read_text().splitlines(), "aoe session start")
    assert "add_ok" in gate, (
        f"start must be gated on add_ok; gate was {gate!r}"
    )
    assert "_tmq_want_auto_attach" not in gate, (
        "start must NOT be gated on the auto-attach flag (that would make "
        f"launching the agent opt-in); gate was {gate!r}"
    )


def test_ac4_attach_is_gated_on_start_ok():
    """`aoe session attach` must only run when start_ok AND the env flag is
    set — proving attach is strictly additive (after start), never a
    replacement for start. Guards against the 59cf300 regression where
    attach replaced start in opt-in mode."""
    gate = _enclosing_if_line(TMQ.read_text().splitlines(), "aoe session attach")
    assert "start_ok" in gate, (
        "attach must be gated on start_ok (additive ordering — start always "
        f"runs first); gate was {gate!r}"
    )
    assert "_tmq_want_auto_attach" in gate, (
        "attach must be gated on _tmq_want_auto_attach (opt-in); "
        f"gate was {gate!r}"
    )

