"""Event logging for fleet dispatch metrics (issue #53).

Append-only JSONL event log + transition detection + stats computation.

Public API:
  - append_event(record)       — atomic O_APPEND of one JSONL line
  - log_dispatch_event(...)    — write a dispatch event (called from tmq)
  - detect_transitions()       — poll aoe + tmux pane capture, emit transition
                                  events on status change (see detect_transitions)
  - compute_stats(since=None)  — read events.jsonl, return structured stats
  - format_stats_report(stats) — pretty-print the stats report

Event log path: ~/.local/state/tmq/events.jsonl
Last-status cache: /tmp/tmq-last-status-cache.json (ephemeral, TTL'd)

The append uses Python's open(path, 'a') which maps to O_APPEND:
POSIX guarantees atomic writes ≤ PIPE_BUF (typically 4KB). A single
JSONL record is <1KB, so concurrent appends are safe without locking.
The repo's atomic_write_* helpers (lib/tms/atomic.py, temp+os.replace)
are for full-file replacement — used only for last_status.json here.
"""

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys


# ── Paths ─────────────────────────────────────────────────────────

EVENTS_PATH = os.path.expanduser("~/.local/state/tmq/events.jsonl")
LAST_STATUS_PATH = "/tmp/tmq-last-status-cache.json"

# Regex for extracting AGENT-STATE markers from pane content.
# Matches: <<AGENT-STATE: WORKING>>, <<AGENT-STATE: BLOCKED: reason>>
# The state is captured in group(1); the optional reason in group(2).
_AGENT_STATE_RE = re.compile(
    r'<<AGENT-STATE:\s*([A-Z-]+)(?::\s*(.*?))?\s*>>'
)


# ── Core append ───────────────────────────────────────────────────

def append_event(record: dict) -> None:
    """Append one JSONL record to the events file.

    Uses open(path, 'a') for O_APPEND atomicity — safe for concurrent
    writers as long as each write() call is ≤ PIPE_BUF (records are
    always <1KB, well under the ~4KB limit). Does NOT use the repo's
    atomic_write_* helpers which do temp+os.replace (whole-file replace,
    would drop concurrent writes).

    Creates the parent directory if it doesn't exist.
    """
    os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(EVENTS_PATH, "a") as f:
        f.write(line)


# ── Model resolution ──────────────────────────────────────────────

def _resolve_default_model():
    """Resolve the actually-served model from pi's settings file.

    When tmq dispatches pi without --provider/--model flags, the agent
    uses the default from ~/.pi/agent/settings.json. We resolve this
    at event-write time so the dispatch record carries the real model,
    not an empty string. Returns (provider, model) tuple.
    """
    settings_path = os.path.expanduser("~/.pi/agent/settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ("", "")

    model = settings.get("defaultModel", "")
    if not model:
        return ("", "")

    # Map model → provider from the known fleet configuration.
    # See institutional memory: data-driven-model-selection.md.
    MODEL_TO_PROVIDER = {
        "deepseek-v4-pro": "deepseek",
        "MiniMax-M3": "minimax",
        "MiniMax-M3.5": "minimax",
        "glm-5.2": "zai",
    }
    provider = MODEL_TO_PROVIDER.get(model, "unknown")
    return (provider, model)


# ── Dispatch events ───────────────────────────────────────────────

def log_dispatch_event(
    repo: str,
    issue: int,
    agent: str,
    provider: str,
    model: str,
    dispatch_type: str,
    worktree: str,
    session: str,
    aoe_id_prefix: str = "",
) -> None:
    """Write a dispatch event record. Called by bin/tmq after spawn.

    If provider/model are empty (default pi dispatch), resolves the
    actually-served model from ~/.pi/agent/settings.json so per-model
    stats are meaningful from day 1.
    """
    if not provider or not model:
        resolved_provider, resolved_model = _resolve_default_model()
        if resolved_model:
            provider = provider or resolved_provider
            model = model or resolved_model

    record = {
        "event_type": "dispatch",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "repo": repo,
        "issue": issue,
        "agent": agent,
        "provider": provider,
        "model": model,
        "dispatch_type": dispatch_type,
        "worktree": worktree,
        "session": session,
        "aoe_id_prefix": aoe_id_prefix,
    }
    append_event(record)


def log_dispatch_failed_event(
    repo: str,
    issue: int,
    agent: str,
    provider: str,
    model: str,
    dispatch_type: str,
    reason: str,
) -> None:
    """Write a dispatch_failed event when tmq spawn fails.

    Distinguishes "never started" from "started and broke" so the
    BLOCKED-rate denominator accounts for all dispatch attempts.
    See proposal-review finding: "dispatch_failed events for tmq
    spawn failures."
    """
    if not provider or not model:
        resolved_provider, resolved_model = _resolve_default_model()
        if resolved_model:
            provider = provider or resolved_provider
            model = model or resolved_model

    record = {
        "event_type": "dispatch_failed",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "repo": repo,
        "issue": issue,
        "agent": agent,
        "provider": provider,
        "model": model,
        "dispatch_type": dispatch_type,
        "reason": reason,
    }
    append_event(record)


# ── Transition detection ──────────────────────────────────────────
# Phase 2: poll aoe + tmux pane capture, detect state transitions.
# These are stubs until Phase 2 implementation.


def _run(cmd, timeout=5):
    """Run a subprocess, return stripped stdout. Empty string on error."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _parse_agent_state_from_pane(pane_text: str):
    """Extract the most recent AGENT-STATE marker from pane content.

    Returns (state, reason) tuple, or None if no marker found.
    The most recent marker (last match) wins — per AGENTS.md contract.
    """
    matches = _AGENT_STATE_RE.findall(pane_text)
    if not matches:
        return None
    state, reason = matches[-1]
    return (state, reason.strip() if reason else None)


def detect_transitions():
    """Poll aoe + tmux pane capture, detect state transitions.

    For each session in aoe list --json:
      1. Run tmux capture-pane to get recent output
      2. Parse the most recent <<AGENT-STATE: ...>> marker
      3. Compare against stored last_status.json (keyed by aoe_id_prefix)
      4. On change: append a transition event
      5. On first run (no last_status file): seed state, emit no events

    Sessions that were in last_status but are no longer in aoe list:
      - If last status was DONE or MERGE-READY: emit terminal event
      - Otherwise: skip (don't count transient disappearance as terminal)

    Returns the number of transition events emitted.
    """
    # Phase 2 stub — implemented in a follow-up commit
    return 0


# ── Stats computation ─────────────────────────────────────────────
# Phase 3: read events.jsonl, compute aggregate stats.
# These are stubs until Phase 3 implementation.


def compute_stats(since=None):
    """Read events.jsonl and compute aggregate dispatch metrics.

    Args:
        since: optional ISO date string (YYYY-MM-DD) to filter events.

    Returns a dict with:
      - total_dispatches, total_sessions
      - issue_latency_p50, issue_latency_p90
      - review_rounds_avg
      - blocked_rate, merge_ready_rate
      - fast_path_rate
      - per_model: {model: {dispatches, merges, blocked, avg_latency}}
    """
    # Phase 3 stub — implemented in a follow-up commit
    return {
        "total_dispatches": 0,
        "total_sessions": 0,
        "per_model": {},
    }


def format_stats_report(stats, as_json=False):
    """Pretty-print the stats report.

    If as_json=True, output as JSON for machine consumption.
    """
    if as_json:
        print(json.dumps(stats, indent=2, default=str))
    else:
        print("(no data — stats computation is Phase 3)")


# ── CLI entry point ───────────────────────────────────────────────

def main():
    """Entry point for `python3 -m tms.events <subcommand>`.

    Subcommands:
      dispatch <repo> <issue> <agent> <provider> <model> <type>
              <worktree> <session> [aoe_id_prefix]
          Append a dispatch event record.

      transitions
          Run detect_transitions() once (poll aoe + tmux panes).

      stats [--since YYYY-MM-DD] [--json]
          Compute and print the stats report.
    """
    if len(sys.argv) < 2:
        print("usage: python3 -m tms.events <dispatch|transitions|stats> [...]",
              file=sys.stderr)
        sys.exit(1)

    subcmd = sys.argv[1]

    if subcmd == "dispatch":
        if len(sys.argv) < 9:
            print("usage: python3 -m tms.events dispatch <repo> <issue> "
                  "<agent> <provider> <model> <type> <worktree> <session> "
                  "[aoe_id_prefix]", file=sys.stderr)
            sys.exit(1)
        repo = sys.argv[2]
        issue = int(sys.argv[3])
        agent = sys.argv[4]
        provider = sys.argv[5]
        model = sys.argv[6]
        dispatch_type = sys.argv[7]
        worktree = sys.argv[8]
        session = sys.argv[9]
        aoe_id_prefix = sys.argv[10] if len(sys.argv) > 10 else ""
        log_dispatch_event(
            repo=repo, issue=issue, agent=agent,
            provider=provider, model=model,
            dispatch_type=dispatch_type, worktree=worktree,
            session=session, aoe_id_prefix=aoe_id_prefix,
        )

    elif subcmd == "dispatch-failed":
        if len(sys.argv) < 8:
            print("usage: python3 -m tms.events dispatch-failed <repo> <issue> "
                  "<agent> <provider> <model> <type> <reason>", file=sys.stderr)
            sys.exit(1)
        repo = sys.argv[2]
        issue = int(sys.argv[3])
        agent = sys.argv[4]
        provider = sys.argv[5]
        model = sys.argv[6]
        dispatch_type = sys.argv[7]
        reason = sys.argv[8] if len(sys.argv) > 8 else "unknown"
        log_dispatch_failed_event(
            repo=repo, issue=issue, agent=agent,
            provider=provider, model=model,
            dispatch_type=dispatch_type, reason=reason,
        )

    elif subcmd == "transitions":
        n = detect_transitions()
        print(f"Emitted {n} transition event(s).")

    elif subcmd == "stats":
        since = None
        as_json = False
        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--since" and i + 1 < len(args):
                since = args[i + 1]
                i += 2
            elif args[i] == "--json":
                as_json = True
                i += 1
            else:
                i += 1
        stats = compute_stats(since=since)
        format_stats_report(stats, as_json=as_json)

    else:
        print(f"unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
