"""Wrap-on-terminal hook — cron poller that watches tms_review.events
for terminal transitions and dispatches an objective-mode wrap (issue #82).

Design:
  - Poller mirrors review_poll.py's systemd-timer-friendly shape.
  - Watermark is a file (~/.local/state/tmq/wrap-watermark.json) storing
    the last-processed terminal transition timestamp.
  - For each new terminal transition, cross-references the dispatch event
    to resolve repo+issue, then synthesizes an objective wrap from:
      * tms_review.events — the transition history for this session
      * gh — PRs and commits referencing the issue
      * bogocat.llm_call_log — calls from this worktree (best-effort,
        documented in the wrap itself)
  - Output: a memory file keyed by issue at
    ~/.claude/projects/-root/memory/issue-wrap-<repo>#<issue>.md +
    a closing comment on the GitHub issue.

Public API:
  - read_watermark(path) -> str | None
  - write_watermark(path, ts) -> None
  - is_new_terminal(watermark, terminal_ts) -> bool
  - wrap_exists(memory_dir, repo, issue) -> bool
  - validate_frontmatter(content) -> list[str]
  - find_terminal_transitions_since(watermark) -> list[dict]
  - collect_event_history(aoe_id_prefix) -> list[dict]
  - synthesize_wrap_content(...) -> str
  - scan_and_wrap(dispatch=False) -> list[dict]
"""

import datetime
import json
import os
import re
import subprocess
import sys
import yaml


# ── Paths ─────────────────────────────────────────────────────────

MEMORY_DIR = os.path.expanduser("~/.claude/projects/-root/memory")
WATERMARK_PATH = os.path.expanduser("~/.local/state/tmq/wrap-watermark.json")

# ── Watermark persistence ─────────────────────────────────────────


def read_watermark(path=None):
    """Read the watermark timestamp from a JSON file.

    Returns the stored ISO timestamp string, or None if the file
    does not exist or is malformed.
    """
    p = path or WATERMARK_PATH
    try:
        with open(p) as f:
            data = json.load(f)
            return data.get("last_terminal_ts")
    except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError):
        return None


def write_watermark(path, ts):
    """Write the watermark timestamp to a JSON file atomically.

    Uses temp + os.replace for atomicity (same pattern as atomic.py).
    """
    import tempfile as _tmp

    p = path or WATERMARK_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp_path = _tmp.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"last_terminal_ts": ts}, f)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_new_terminal(watermark, terminal_ts):
    """Return True if terminal_ts is after the watermark (or no watermark).

    A terminal transition that is equal to the watermark is NOT new
    (idempotent: we've already processed transitions up to and including
    the watermark).
    """
    if watermark is None:
        return True
    return terminal_ts > watermark


# ── Idempotency ───────────────────────────────────────────────────


def wrap_exists(memory_dir, repo, issue):
    """Return True if a wrap file already exists for (repo, issue)."""
    filename = f"issue-wrap-{repo}#{issue}.md"
    path = os.path.join(memory_dir, filename)
    return os.path.isfile(path)


# ── Frontmatter validation ────────────────────────────────────────

_REQUIRED_FRONTMATTER_KEYS = ("name", "description", "metadata")


def validate_frontmatter(content):
    """Validate that a memory file has the required YAML frontmatter.

    Returns a list of error strings. Empty list = valid.

    Required keys: name, description, metadata.
    """
    errors = []

    # Must start with YAML frontmatter delimiter
    if not content.startswith("---"):
        errors.append("missing frontmatter: content must start with '---'")
        return errors

    # Extract frontmatter between the first two '---' lines
    lines = content.splitlines()
    if lines[0].strip() != "---":
        errors.append("missing frontmatter: content must start with '---'")
        return errors

    # Find closing '---'
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        errors.append(
            "unclosed frontmatter: no closing '---' found"
        )
        return errors

    fm_text = "\n".join(lines[1:end_idx])

    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        errors.append(f"invalid YAML in frontmatter: {e}")
        return errors

    if not isinstance(fm, dict):
        errors.append("frontmatter must be a YAML mapping")
        return errors

    for key in _REQUIRED_FRONTMATTER_KEYS:
        if key not in fm:
            errors.append(f"missing required frontmatter key: '{key}'")

    return errors


# ── Terminal transition discovery ─────────────────────────────────


def _get_conn():
    """Return a database connection. Uses tms.events._get_conn pattern."""
    from tms.events import _get_conn as _events_conn
    return _events_conn()


def find_terminal_transitions_since(watermark):
    """Find terminal transitions with event_timestamp > watermark.

    Returns a list of dicts, each with {aoe_id_prefix, repo, issue,
    terminal_ts, event_history}. Terminal transitions that cannot be
    resolved to a dispatch event (no repo/issue) are excluded — we
    cannot synthesize a wrap without knowing which issue this is.
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                # Find terminal transitions with matching dispatch events.
                # Dedupe in Python since the JOIN may produce multiple rows
                # when the same aoe_id_prefix has multiple dispatch events.
                if watermark:
                    cur.execute(
                        """SELECT t.aoe_id_prefix, t.event_timestamp,
                                  d.repo, d.issue
                           FROM tms_review.events t
                           JOIN tms_review.events d
                             ON t.aoe_id_prefix = d.aoe_id_prefix
                            AND d.event_type = 'dispatch'
                           WHERE t.event_type = 'transition'
                             AND t.to_status = 'terminal'
                             AND t.event_timestamp > %s
                             AND d.repo IS NOT NULL
                             AND d.issue IS NOT NULL
                           ORDER BY t.event_timestamp""",
                        (watermark,),
                    )
                else:
                    cur.execute(
                        """SELECT t.aoe_id_prefix, t.event_timestamp,
                                  d.repo, d.issue
                           FROM tms_review.events t
                           JOIN tms_review.events d
                             ON t.aoe_id_prefix = d.aoe_id_prefix
                            AND d.event_type = 'dispatch'
                           WHERE t.event_type = 'transition'
                             AND t.to_status = 'terminal'
                             AND d.repo IS NOT NULL
                             AND d.issue IS NOT NULL
                           ORDER BY t.event_timestamp"""
                    )

                results = []
                seen = set()
                for aoe_prefix, ts, repo, issue in cur.fetchall():
                    key = (aoe_prefix, ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        "aoe_id_prefix": aoe_prefix,
                        "terminal_ts": ts,
                        "repo": repo,
                        "issue": int(issue) if issue is not None else None,
                    })
                return results
    except Exception:
        return []


def collect_event_history(aoe_id_prefix):
    """Return all transition events for a given aoe_id_prefix.

    Ordered by event_timestamp ascending.
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT from_status, to_status, event_timestamp,
                              reason, blocked_class
                       FROM tms_review.events
                       WHERE event_type = 'transition'
                         AND aoe_id_prefix = %s
                       ORDER BY event_timestamp""",
                    (aoe_id_prefix,),
                )
                results = []
                for from_s, to_s, ts, reason, blocked_class in cur.fetchall():
                    results.append({
                        "from_status": from_s,
                        "to_status": to_s,
                        "timestamp": ts,
                        "reason": reason,
                        "blocked_class": blocked_class,
                    })
                return results
    except Exception:
        return []


# ── External data collection ──────────────────────────────────────


def _run(cmd, timeout=15):
    """Run a subprocess, return stripped stdout. Empty string on error."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _gh_json(args, timeout=15):
    """Run a ``gh`` subcommand, return parsed JSON (or None on error)."""
    out = _run(["gh"] + args, timeout=timeout)
    if not out:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def _fetch_gh_prs(gh_repo, issue):
    """Fetch PRs that reference the issue via gh search.

    Returns a list of {number, title, state, mergedAt, url}.
    """
    query = f"repo:{gh_repo} {issue} in:title,body type:pr"
    data = _gh_json([
        "search", "prs", query,
        "--json", "number,title,state,mergedAt,url",
        "--limit", "20",
    ])
    if not isinstance(data, list):
        return []
    return data


def _fetch_llm_call_count(repo, issue):
    """Count llm_call_log rows from the worktree for (repo, issue).

    Matches meta->>'encoded_cwd' against the pattern
    ``--root-wt-<repo>-<issue>--``. Returns the count (best-effort;
    also counts sibling sessions on the same worktree).
    """
    cwd_pattern = f"--root-wt-{repo}-{issue}--"
    try:
        from tms.events import _read_db_config
        import psycopg2

        cfg = _read_db_config()
        conn = psycopg2.connect(
            host=cfg["host"],
            dbname=cfg["dbname"],
            user=cfg["user"],
            password=cfg["password"],
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*)
                       FROM bogocat.llm_call_log
                       WHERE meta->>'encoded_cwd' = %s""",
                    (cwd_pattern,),
                )
                row = cur.fetchone()
                if row:
                    return int(row[0])
    except Exception:
        pass
    return 0


# ── Wrap synthesis ────────────────────────────────────────────────

# Map of repo short names to full gh org/repo. Mirrors the tmq
# registry. Keep in sync with tmq list --machine output.
REPO_TO_GH = {
    "tms": "bogocat/tms",
    "distillery": "bogocat/distillery",
    "deploy": "bogocat/distillery",
    "home-portal": "bogocat/home-portal",
    "garmin-doctor": "bogocat/garmin-doctor",
    "tower-fleet": "bogocat/tower-fleet",
    "pi-dotfiles": "bogocat/pi-dotfiles",
    "tmq": "bogocat/tmq",
    "rms": "bogocat/rms",
    "tcg": "bogocat/tcg",
    "brainlearn": "bogocat/brainlearn",
    "subtitleai": "bogocat/subtitleai",
    "music-control": "bogocat/music-control",
    "notes-app": "bogocat/notes-app",
    "replyflow": "bogocat/replyflow",
    "vault-platform": "bogocat/vault-platform",
    "ai-providers": "bogocat/ai-providers",
    "palimpsest": "bogocat/palimpsest",
    "scripts": "bogocat/scripts",
    "openagent": "bogocat/openagent",
    "trip-planner": "bogocat/trip-planner",
}


def _makdown_escape(text):
    """Escape pipe characters for markdown tables."""
    return (text or "").replace("|", "\\|")


def synthesize_wrap_content(repo, issue, transitions, gh_prs, llm_call_count):
    """Produce a complete memory file as a string.

    Args:
        repo: short repo name (e.g. "distillery")
        issue: issue number
        transitions: list of transition dicts from collect_event_history
        gh_prs: list of PR dicts from _fetch_gh_prs
        llm_call_count: int, approximate count from llm_call_log

    Returns a string with YAML frontmatter + markdown body.
    """
    today = datetime.date.today().isoformat()
    title = f"issue-wrap-{repo}#{issue}"

    # Count states
    states_seen = set()
    for t in transitions:
        states_seen.add(t.get("from_status", ""))
        states_seen.add(t.get("to_status", ""))

    blocked_reasons = [
        t.get("reason") for t in transitions
        if t.get("to_status") == "BLOCKED" and t.get("reason")
    ]

    # Build transition timeline
    timeline_lines = []
    for t in transitions:
        ts = (t.get("timestamp", "") or "")[:19]
        from_s = t.get("from_status", "?")
        to_s = t.get("to_status", "?")
        reason = t.get("reason", "")
        extra = f": {reason}" if reason else ""
        timeline_lines.append(f"- `{ts}` {from_s} → {to_s}{extra}")

    # Build PR list
    pr_lines = []
    for pr in (gh_prs or []):
        num = pr.get("number", "?")
        pr_title = _makdown_escape(pr.get("title", ""))
        state = pr.get("state", "?")
        merged = "merged" if pr.get("mergedAt") else state
        pr_lines.append(f"- PR #{num} ({merged}) — {pr_title}")

    if not pr_lines:
        pr_lines.append("- (no PRs found referencing this issue)")

    lines = [
        "---",
        f"name: {title}",
        f"description: \"Objective wrap for {repo}#{issue} — terminal transition detected {today}\"",
        "metadata:",
        "  node_type: memory",
        "  type: project",
        "  source: wrap-on-terminal",
        f"  repo: {repo}",
        f"  issue: {issue}",
        "---",
        "",
        f"# Objective wrap: {repo}#{issue}",
        "",
        "*Auto-generated by wrap-on-terminal hook (issue #82). Built from objective",
        "sources only — no conversation data. llm_call_log counts are best-effort",
        "(calls from this worktree, may include sibling sessions).*",
        "",
        "## Session timeline",
        "",
    ]
    if timeline_lines:
        lines.extend(timeline_lines)
    else:
        lines.append("- (no transitions recorded)")
    lines.append("")
    lines.append("## GitHub PRs")
    lines.append("")
    lines.extend(pr_lines)
    lines.append("")
    lines.append("## Approximate LLM calls")
    lines.append("")
    lines.append(
        f"{llm_call_count} calls from worktree `--root-wt-{repo}-{issue}--`"
    )
    lines.append(
        f"(via `bogocat.llm_call_log.meta->>'encoded_cwd'`)."
    )
    lines.append("")
    lines.append("## Blocked reasons")
    lines.append("")
    if blocked_reasons:
        for r in blocked_reasons:
            lines.append(f"- {r}")
    else:
        lines.append("- (no BLOCKED states)")
    lines.append("")
    lines.append("## Raw data queries")
    lines.append("")
    lines.append("```sql")
    lines.append("-- Transition history")
    lines.append("SELECT from_status, to_status, event_timestamp, reason")
    lines.append("FROM tms_review.events")
    lines.append("WHERE event_type = 'transition'")
    lines.append("  AND aoe_id_prefix = '<prefix>'")
    lines.append("ORDER BY event_timestamp;")
    lines.append("")
    lines.append("-- LLM calls from this worktree")
    lines.append("SELECT COUNT(*)")
    lines.append("FROM bogocat.llm_call_log")
    lines.append(
        f"WHERE meta->>'encoded_cwd' = '--root-wt-{repo}-{issue}--';"
    )
    lines.append("```")
    lines.append("")
    content = "\n".join(lines)

    return content


def _resolve_gh_repo(repo):
    """Resolve a short repo name to org/repo for gh."""
    return REPO_TO_GH.get(repo, f"bogocat/{repo}")


# ── Main scan loop ────────────────────────────────────────────────


def _result(repo, issue, status, extra=None):
    r = {"repo": repo, "issue": issue, "status": status}
    if extra:
        r.update(extra)
    return r


def scan_and_wrap(dispatch=False, memory_dir=None, watermark_path=None,
                  dry_run=False):
    """Scan for new terminal transitions and dispatch objective wraps.

    Returns a list of result dicts.
    """
    mem = memory_dir or MEMORY_DIR
    wp = watermark_path or WATERMARK_PATH

    watermark = read_watermark(wp)
    terminals = find_terminal_transitions_since(watermark)
    results = []

    if not terminals:
        return results

    # Track the latest terminal_ts across all processed transitions.
    # We advance the watermark to the max of all processed terminal_ts
    # at the end, so a partial run (crash mid-loop) re-processes anything
    # that wasn't successfully wrapped.
    max_ts = watermark
    seen_issues = set()  # dedupe within a single scan run

    for term in terminals:
        repo = term["repo"]
        issue = term["issue"]
        aoe_prefix = term["aoe_id_prefix"]
        ts = term["terminal_ts"]

        # Deduplicate within run: same issue may appear from multiple
        # terminal transitions (re-dispatch after FAIL).
        issue_key = (repo, issue)
        if issue_key in seen_issues:
            if ts > (max_ts or ""):
                max_ts = ts
            continue
        seen_issues.add(issue_key)

        # Idempotency: skip if a wrap already exists
        if wrap_exists(mem, repo, issue):
            results.append(_result(repo, issue, "skip_exists"))
            if ts > (max_ts or ""):
                max_ts = ts
            continue

        if not dispatch:
            results.append(_result(repo, issue, "would_wrap",
                                   {"terminal_ts": ts}))
            if ts > (max_ts or ""):
                max_ts = ts
            continue

        # Collect data
        transitions = collect_event_history(aoe_prefix)
        gh_repo = _resolve_gh_repo(repo)
        gh_prs = _fetch_gh_prs(gh_repo, issue)
        llm_count = _fetch_llm_call_count(repo, issue)

        # Synthesize wrap
        content = synthesize_wrap_content(
            repo, issue, transitions, gh_prs, llm_count,
        )

        # Frontmatter validation guard
        errors = validate_frontmatter(content)
        if errors:
            results.append(_result(repo, issue, "skip_frontmatter_invalid",
                                   {"errors": errors}))
            if ts > (max_ts or ""):
                max_ts = ts
            continue

        if dry_run:
            results.append(_result(repo, issue, "dry_run",
                                   {"content_len": len(content)}))
            if ts > (max_ts or ""):
                max_ts = ts
            continue

        # Write memory file
        try:
            os.makedirs(mem, exist_ok=True)
            filename = f"issue-wrap-{repo}#{issue}.md"
            filepath = os.path.join(mem, filename)
            with open(filepath, "w") as f:
                f.write(content)
        except OSError as e:
            results.append(_result(repo, issue, "skip_write_error",
                                   {"error": str(e)}))
            if ts > (max_ts or ""):
                max_ts = ts
            continue

        # Post issue comment
        comment_ok = _post_issue_comment(gh_repo, issue, filename, transitions,
                                         gh_prs, llm_count)

        results.append(_result(
            repo, issue, "wrapped",
            {"terminal_ts": ts, "comment_ok": comment_ok},
        ))

        if ts > (max_ts or ""):
            max_ts = ts

    # Advance watermark
    if dispatch and not dry_run and max_ts and max_ts != watermark:
        write_watermark(wp, max_ts)

    return results


def _post_issue_comment(gh_repo, issue, wrap_filename, transitions,
                        gh_prs, llm_count):
    """Post a closing comment on the GitHub issue.

    Returns True on success, False on failure.
    """
    mem_path = os.path.join(MEMORY_DIR, wrap_filename)
    pr_list = ", ".join(
        f"#{p['number']}" for p in (gh_prs or [])
    ) or "none"

    timeline_summary = []
    for t in (transitions or []):
        ts = (t.get("timestamp", "") or "")[:19]
        from_s = t.get("from_status", "?")
        to_s = t.get("to_status", "?")
        timeline_summary.append(f"{ts}: {from_s} → {to_s}")

    body_parts = [
        "## 🤖 Objective wrap (wrap-on-terminal)",
        "",
        f"**Memory file:** `{wrap_filename}`",
        "",
        "### What shipped",
        f"- PRs referencing this issue: {pr_list}",
        f"- Approximate LLM calls from this worktree: {llm_count}",
        "",
        "### Transition timeline",
    ]
    for line in (timeline_summary or ["- (none)"]):
        body_parts.append(f"- {line}")

    body_parts.extend([
        "",
        "### Evidence queries",
        "```sql",
        "-- Terminal transition",
        f"-- SELECT * FROM tms_review.events WHERE to_status='terminal'",
        "",
        "-- LLM calls from this worktree",
        f"-- SELECT COUNT(*) FROM bogocat.llm_call_log",
        f"-- WHERE meta->>'encoded_cwd' = '--root-wt-<repo>-<issue>--'",
        "```",
        "",
        "---",
        "*Auto-generated by [wrap-on-terminal](https://github.com/bogocat/tms/issues/82). "
        "Built from objective sources only.*",
    ])

    body = "\n".join(body_parts)

    try:
        r = subprocess.run(
            ["gh", "issue", "comment", str(issue),
             "--repo", gh_repo,
             "--body", body],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# ── CLI ───────────────────────────────────────────────────────────


def _format_report(results):
    """Pretty-print the scan results."""
    if not results:
        print("No new terminal transitions need wrapping.")
        return

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    for r in results:
        extra = ""
        if r["status"] == "skip_exists":
            extra = " (wrap already exists)"
        elif r["status"] == "skip_frontmatter_invalid":
            errs = r.get("errors", [])
            extra = f" (frontmatter errors: {', '.join(errs)})"
        elif r["status"] == "wrapped":
            extra = f" (comment={'ok' if r.get('comment_ok') else 'failed'})"
        print(f"  [{r['status']}] {r['repo']}#{r['issue']}{extra}")

    print()
    print("Summary: " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items())
    ))


def main():
    """Entry point for ``python3 -m tms.wrap_on_terminal``.

      scan-wraps [--dispatch] [--dry-run]
    """
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("usage: python3 -m tms.wrap_on_terminal scan-wraps "
              "[--dispatch] [--dry-run]")
        sys.exit(0)

    subcmd = args[0]
    if subcmd != "scan-wraps":
        print(f"unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)

    dispatch = "--dispatch" in args
    dry_run = "--dry-run" in args

    results = scan_and_wrap(dispatch=dispatch, dry_run=dry_run)
    _format_report(results)


if __name__ == "__main__":
    main()
