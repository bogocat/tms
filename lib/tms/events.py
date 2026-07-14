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
import math
import os
import pathlib
import re
import subprocess
import sys
import uuid

import psycopg2


# ── Paths ─────────────────────────────────────────────────────────

EVENTS_PATH = os.path.expanduser("~/.local/state/tmq/events.jsonl")
LAST_STATUS_PATH = "/tmp/tmq-last-status-cache.json"

# ── Database connection ───────────────────────────────────────────

_DB_CONF_PATH = os.path.expanduser("~/.config/bogocat/db.conf")


def _read_db_config():
    """Parse db.conf into a dict {host, dbname, user, password}.

    Format: space-separated key=value pairs on one line.
    """
    with open(_DB_CONF_PATH) as f:
        raw = f.read().strip()
    config = {}
    for token in raw.split():
        if "=" in token:
            key, val = token.split("=", 1)
            config[key.strip()] = val.strip()
    return config


def _get_conn():
    """Return a database connection.

    In production: psycopg2 to unified postgres via db.conf DSN.
    In tests: monkeypatched to sqlite3 in-memory.
    """
    cfg = _read_db_config()
    return psycopg2.connect(
        host=cfg["host"],
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
    )

# Regex for extracting AGENT-STATE markers from pane content.
# Matches: <<AGENT-STATE: WORKING>>, <<AGENT-STATE: BLOCKED: reason>>
# The state is captured in group(1); the optional reason in group(2).
_AGENT_STATE_RE = re.compile(
    r'<<AGENT-STATE:\s*([A-Z-]+)(?::\s*(.*?))?\s*>>'
)


# ── Core append ───────────────────────────────────────────────────

def append_event(record: dict) -> None:
    """INSERT one record into the tms_review.events table.

    Replaces the JSONL append with a postgres INSERT (tms#65).
    The payload column stores the canonical full JSON record for
    forward compatibility; flat columns are denormalized query indices.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    event_ts = record.get("timestamp") or now
    event_type = record.get("event_type", "")
    payload = json.dumps(record, ensure_ascii=False)

    # Derive aoe_id_prefix: for dispatch_failed events (no aoe session),
    # generate a synthetic deterministic prefix so the composite UNIQUE
    # index catches duplicates. Same input → same prefix.
    aoe_prefix = record.get("aoe_id_prefix") or ""
    if not aoe_prefix and event_type == "dispatch_failed":
        aoe_prefix = (
            f"failed-{record.get('repo','')}-"
            f"{record.get('issue','')}-{event_ts[:19]}"
        )

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO tms_review.events
                   (id, created_at, event_type, event_timestamp,
                    repo, issue, agent, provider, model,
                    dispatch_type, worktree, session,
                    aoe_id_prefix, reason, from_status, to_status,
                    payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (event_type, aoe_id_prefix, event_timestamp)
                   DO NOTHING""",
                (
                    str(uuid.uuid4()),
                    now,
                    event_type,
                    event_ts,
                    record.get("repo"),
                    record.get("issue"),
                    record.get("agent"),
                    record.get("provider"),
                    record.get("model"),
                    record.get("dispatch_type"),
                    record.get("worktree"),
                    record.get("session"),
                    aoe_prefix,
                    record.get("reason"),
                    record.get("from_status"),
                    record.get("to_status"),
                    payload,
                ),
            )
        conn.commit()


# ── Model resolution ──────────────────────────────────────────────

# Map model → provider from the known fleet configuration.
# See institutional memory: data-driven-model-selection.md.
MODEL_TO_PROVIDER = {
    "deepseek-v4-pro": "deepseek",
    "MiniMax-M3": "minimax",
    "MiniMax-M3.5": "minimax",
    "glm-5.2": "zai",
}


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

    provider = MODEL_TO_PROVIDER.get(model, "unknown")
    return (provider, model)


def _resolve_dispatch_model(provider: str, model: str):
    """Resolve event provenance from explicit flags, then pi defaults.

    An explicit model determines its missing provider from the fleet map.
    Defaults are consulted only when the invocation supplies no model.
    """
    if model:
        return (provider or MODEL_TO_PROVIDER.get(model, "unknown"), model)

    resolved_provider, resolved_model = _resolve_default_model()
    return (provider or resolved_provider, resolved_model)


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
    provider, model = _resolve_dispatch_model(provider, model)

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
    provider, model = _resolve_dispatch_model(provider, model)

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
    import json as _json

    # 1. Fetch current aoe sessions
    try:
        r = subprocess.run(
            ["aoe", "list", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return 0
        aoe_sessions = _json.loads(r.stdout)
    except (subprocess.TimeoutExpired, OSError, _json.JSONDecodeError, ValueError):
        return 0

    # 2. Load last-known status (may not exist yet)
    last_status = {}
    try:
        if os.path.exists(LAST_STATUS_PATH):
            with open(LAST_STATUS_PATH) as f:
                last_status = _json.load(f)
    except (_json.JSONDecodeError, OSError):
        last_status = {}  # corrupted → treat as first run

    is_first_run = len(last_status) == 0

    # 3. Build current status map: aoe_id_prefix[:8] → agent_state
    current_status = {}
    session_titles = {}  # aoe_id_prefix → session title (for event logging)
    for s in aoe_sessions:
        sid = s.get("id", "")
        if len(sid) < 8:
            continue
        id_prefix = sid[:8]
        title = s.get("title", "")
        session_titles[id_prefix] = title

        # Derive tmux session name from aoe title + id.
        # aoe session names follow the pattern:
        #   aoe_<title-with-underscores>_<uuid>
        # Example: title "feat-tms#53" → tmux name "aoe_feat-tms_53_abc12345..."
        tmux_name = _derived_tmux_session_name(title, sid)
        if not tmux_name:
            continue

        # Capture pane content and parse AGENT-STATE marker
        pane_text = _run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p", "-S", "-200"],
            timeout=3,
        )
        parsed = _parse_agent_state_from_pane(pane_text)
        if parsed is not None:
            current_status[id_prefix] = parsed[0]

    # 4. Compare and emit transition events
    emitted = 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for id_prefix, new_state in current_status.items():
        old_state = last_status.get(id_prefix)
        if old_state is not None and old_state != new_state:
            # State changed — emit transition
            record = {
                "event_type": "transition",
                "timestamp": now,
                "session": session_titles.get(id_prefix, ""),
                "aoe_id_prefix": id_prefix,
                "from_status": old_state,
                "to_status": new_state,
            }
            append_event(record)
            emitted += 1

    # 5. Detect disappeared sessions (in last_status but not current)
    for id_prefix, old_state in last_status.items():
        if id_prefix in current_status:
            continue
        # Only emit terminal for terminal-like states
        if old_state in ("DONE", "MERGE-READY"):
            record = {
                "event_type": "transition",
                "timestamp": now,
                "session": "",  # session is gone, title unknown
                "aoe_id_prefix": id_prefix,
                "from_status": old_state,
                "to_status": "terminal",
            }
            append_event(record)
            emitted += 1

    # 6. Write updated last_status (atomic via atomic_write_json)
    from tms.atomic import atomic_write_json

    # Merge: keep entries for disappeared sessions that we didn't mark
    # as terminal (they may come back), and update with current status
    merged = {}
    for id_prefix, old_state in last_status.items():
        if id_prefix in current_status:
            merged[id_prefix] = current_status[id_prefix]
        elif old_state in ("DONE", "MERGE-READY"):
            continue  # terminal — don't carry forward
        else:
            merged[id_prefix] = old_state  # keep, may come back
    for id_prefix, new_state in current_status.items():
        merged[id_prefix] = new_state

    atomic_write_json(LAST_STATUS_PATH, merged)

    return emitted


def _derived_tmux_session_name(title, aoe_id):
    """Derive the likely tmux session name from an aoe title + id.

    aoe constructs tmux session names as:
        aoe_<sanitized-title>_<8-char-uuid-prefix>
    where the title has '#' → '_' and spaces → '_', and the UUID
    is truncated to 8 characters.
    Example: title="feat-tms#53", id="abc12345fedc..." →
             "aoe_feat-tms_53_abc12345"

    Returns the derived name or None if derivation is impossible.
    """
    if not title or not aoe_id:
        return None
    # Sanitize the title the same way aoe does: # → _, space → _
    sanitized = title.replace("#", "_").replace(" ", "_")
    return f"aoe_{sanitized}_{aoe_id[:8]}"


# ── Stats computation ─────────────────────────────────────────────
# Reads from tms_review.events via _read_events_from_db().
# The rest of the computation is identical to the JSONL version —
# only the data source changed (tms#65).


def _read_events_from_db(since=None):
    """Read events from tms_review.events as dicts (mirrors JSONL format).

    Deserializes payload column back to dicts. The events are ordered
    by event_timestamp for deterministic stats (replaces JSONL append order).

    Args:
        since: optional ISO date string (YYYY-MM-DD) to filter events.
    """
    events = []
    cutoff = None
    if since:
        cutoff = since if "T" in since else since + "T00:00:00+00:00"

    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                if cutoff:
                    cur.execute(
                        """SELECT payload
                           FROM tms_review.events
                           WHERE event_timestamp >= %s
                           ORDER BY event_timestamp, id""",
                        (cutoff,),
                    )
                else:
                    cur.execute(
                        """SELECT payload
                           FROM tms_review.events
                           ORDER BY event_timestamp, id"""
                    )
                for (payload,) in cur.fetchall():
                    try:
                        events.append(json.loads(payload))
                    except (json.JSONDecodeError, ValueError):
                        continue
    except (psycopg2.OperationalError, psycopg2.DatabaseError, OSError):
        pass

    return events


def compute_stats(since=None):
    """Read events from postgres and compute aggregate dispatch metrics.

    Args:
        since: optional ISO date string (YYYY-MM-DD) to filter events.

    Returns a dict with:
      - total_dispatches, total_failed_dispatches, total_transitions
      - fast_path_count, normal_path_count, fast_path_rate
      - review_rounds_total, review_rounds_avg
      - blocked_count, merge_ready_count, blocked_rate
      - latency_p50_seconds, latency_p90_seconds
      - completed_sessions
      - per_model: {model: {dispatches, merged, blocked, avg_latency_seconds}}
    """
    # Read all events from postgres (replaces JSONL file read)
    events = _read_events_from_db(since)

    # Partition by event type
    dispatches = [e for e in events if e.get("event_type") == "dispatch"]
    failed = [e for e in events if e.get("event_type") == "dispatch_failed"]
    transitions = [e for e in events if e.get("event_type") == "transition"]

    # Per-model dispatch counts
    per_model = {}
    for d in dispatches:
        model = d.get("model", "")
        if not model:
            model = d.get("agent", "unknown")
        if model not in per_model:
            per_model[model] = {
                "dispatches": 0, "merged": 0, "blocked": 0,
                "avg_latency_seconds": 0,
            }
        per_model[model]["dispatches"] += 1

    # Group transitions by aoe_id_prefix
    transitions_by_session = {}
    for t in transitions:
        sid = t.get("aoe_id_prefix", "")
        if not sid:
            continue
        transitions_by_session.setdefault(sid, []).append(t)

    # Compute per-session metrics
    review_rounds_total = 0
    sessions_with_reviews = 0
    blocked_count = 0
    merge_ready_count = 0
    fast_path_count = 0
    normal_path_count = 0
    latencies = []
    completed_sessions = 0

    # Build dispatch lookup: aoe_id_prefix → dispatch record
    dispatch_by_session = {}
    for d in dispatches:
        sid = d.get("aoe_id_prefix", "")
        if sid:
            dispatch_by_session[sid] = d

    for sid, ts in transitions_by_session.items():
        # Review rounds: count PR-REVIEW→WORKING transitions
        rounds = sum(
            1 for t in ts
            if t.get("from_status") == "PR-REVIEW"
            and t.get("to_status") == "WORKING"
        )
        if rounds > 0:
            review_rounds_total += rounds
            sessions_with_reviews += 1

        # BLOCKED detection
        for t in ts:
            if t.get("to_status") == "BLOCKED":
                blocked_count += 1
                # Tag the model
                disp = dispatch_by_session.get(sid, {})
                model = disp.get("model", disp.get("agent", ""))
                if model and model in per_model:
                    per_model[model]["blocked"] = \
                        per_model[model].get("blocked", 0) + 1
            if t.get("to_status") == "MERGE-READY":
                merge_ready_count += 1

        # Plan-gate: does this session have a PLAN-REVIEW marker?
        # The marker appears as from_status when the agent transitions
        # OUT of PLAN-REVIEW (PLAN-REVIEW→WORKING).
        has_plan_review = any(
            t.get("from_status") == "PLAN-REVIEW" for t in ts
        )
        # Fast path = dispatch exists AND first transition is NOT
        # to PLAN-REVIEW (agent went straight to WORKING)
        if has_plan_review:
            normal_path_count += 1
        else:
            # Only count if there's at least one transition (agent ran)
            if len(ts) > 0:
                fast_path_count += 1

        # Latency: dispatch→terminal or dispatch→DONE/MERGE-READY
        disp = dispatch_by_session.get(sid)
        if disp:
            disp_ts = disp.get("timestamp", "")
            # Find terminal event
            terminal_t = None
            for t in ts:
                if t.get("to_status") in ("terminal", "DONE"):
                    terminal_t = t
                    break
            if not terminal_t:
                # Fall back to MERGE-READY as completion
                for t in ts:
                    if t.get("to_status") == "MERGE-READY":
                        terminal_t = t
                        break

            if terminal_t and disp_ts:
                try:
                    d_start = datetime.datetime.fromisoformat(disp_ts)
                    d_end = datetime.datetime.fromisoformat(
                        terminal_t.get("timestamp", "")
                    )
                    latency = (d_end - d_start).total_seconds()
                    if latency >= 0:
                        latencies.append(latency)
                        completed_sessions += 1
                        # Per-model latency
                        model = disp.get("model", disp.get("agent", ""))
                        if model and model in per_model:
                            pm = per_model[model]
                            n = pm.get("merged", 0)
                            old_avg = pm.get("avg_latency_seconds", 0)
                            pm["avg_latency_seconds"] = (
                                (old_avg * n + latency) / (n + 1)
                            )
                            pm["merged"] = n + 1
                except (ValueError, OverflowError):
                    pass

    # Compute aggregate stats
    review_rounds_avg = (
        review_rounds_total / sessions_with_reviews
        if sessions_with_reviews > 0 else 0.0
    )
    blocked_rate = (
        blocked_count / (blocked_count + merge_ready_count)
        if (blocked_count + merge_ready_count) > 0 else 0.0
    )
    fast_path_rate = (
        fast_path_count / (fast_path_count + normal_path_count)
        if (fast_path_count + normal_path_count) > 0 else 0.0
    )

    # Percentile latencies
    latencies.sort()
    latency_p50 = _percentile(latencies, 50)
    latency_p90 = _percentile(latencies, 90)

    return {
        "total_dispatches": len(dispatches),
        "total_failed_dispatches": len(failed),
        "total_transitions": len(transitions),
        "fast_path_count": fast_path_count,
        "normal_path_count": normal_path_count,
        "fast_path_rate": round(fast_path_rate, 3),
        "review_rounds_total": review_rounds_total,
        "review_rounds_avg": round(review_rounds_avg, 2),
        "blocked_count": blocked_count,
        "merge_ready_count": merge_ready_count,
        "blocked_rate": round(blocked_rate, 3),
        "latency_p50_seconds": latency_p50,
        "latency_p90_seconds": latency_p90,
        "completed_sessions": completed_sessions,
        "per_model": per_model,
    }


def _percentile(sorted_values, pct):
    """Compute the pct-th percentile from a sorted list.

    Uses linear interpolation (same as numpy.percentile with
    method='linear'). Returns 0 for empty lists.
    """
    if not sorted_values:
        return 0
    n = len(sorted_values)
    # Fractional rank: (pct/100) * (n-1) for 0-indexed interpolation
    rank = (pct / 100.0) * (n - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_values[lo]
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def format_stats_report(stats, as_json=False):
    """Pretty-print the stats report.

    If as_json=True, output as JSON for machine consumption.
    """
    if as_json:
        print(json.dumps(stats, indent=2, default=str))
        return

    def _hms(seconds):
        if seconds == 0:
            return "—"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"

    print("=== Fleet Dispatch Metrics ===")
    print()
    print(f"  Total dispatches:       {stats['total_dispatches']}")
    print(f"  Failed dispatches:      {stats['total_failed_dispatches']}")
    print(f"  Total transitions:      {stats['total_transitions']}")
    print()
    print(f"  Completed sessions:     {stats['completed_sessions']}")
    print(f"  Issue latency (p50):    {_hms(stats['latency_p50_seconds'])}")
    print(f"  Issue latency (p90):    {_hms(stats['latency_p90_seconds'])}")
    print()
    print(f"  Plan-gate fast path:    {stats['fast_path_count']} "
          f"({stats['fast_path_rate']:.0%})")
    print(f"  Plan-gate normal:       {stats['normal_path_count']}")
    print()
    print(f"  Review rounds (avg):    {stats['review_rounds_avg']}")
    print(f"  Review rounds (total):  {stats['review_rounds_total']}")
    print()
    print(f"  BLOCKED count:          {stats['blocked_count']}")
    print(f"  MERGE-READY count:      {stats['merge_ready_count']}")
    print(f"  BLOCKED rate:           {stats['blocked_rate']:.1%}")
    print()

    pm = stats.get("per_model", {})
    if pm:
        print("  Per-model breakdown:")
        print(f"  {'Model':<24} {'Disp':>5} {'Merged':>7} {'Blocked':>8} {'Avg Lat':>8}")
        print(f"  {'─'*24} {'─'*5} {'─'*7} {'─'*8} {'─'*8}")
        for model, mstats in sorted(pm.items()):
            print(
                f"  {model:<24} "
                f"{mstats['dispatches']:>5} "
                f"{mstats.get('merged', 0):>7} "
                f"{mstats.get('blocked', 0):>8} "
                f"{_hms(mstats.get('avg_latency_seconds', 0)):>8}"
            )


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
        if len(sys.argv) < 10:
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
