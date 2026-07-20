"""Wrap-on-terminal hook — cron poller that watches tms_review.events
for terminal transitions and dispatches an objective-mode wrap (issue #82).

Design:
  - Poller mirrors review_poll.py's systemd-timer-friendly shape.
  - Watermark is a file (~/.local/state/tmq/wrap-watermark.json) storing
    the last-processed terminal transition timestamp.
  - Cross-process locking via fcntl.flock on the watermark path prevents
    duplicate comments from overlapping cron invocations.
  - For each new terminal transition, cross-references the dispatch event
    to resolve repo+issue, then synthesizes an objective wrap from:
      * tms_review.events — the transition history for this session
      * gh — PRs and commits referencing the issue (post-filtered to
        only include PRs that actually reference the issue)
      * bogocat.llm_call_log — calls from this worktree (best-effort,
        documented in the wrap itself)
  - Output: a memory file keyed by issue at
    ~/.claude/projects/-root/memory/issue-wrap-<repo>#<issue>.md +
    a closing comment on the GitHub issue (comment posted BEFORE file
    write, so crash between them doesn't orphan a comment-less wrap).
  - Dead-letter tracking: per-item failures don't stall later items;
    a quarantine file records permanently-failing (repo, issue) pairs
    so they're skipped on future runs.

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
import fcntl
import json
import os
import re
import subprocess
import sys
import traceback

import yaml


# ── Paths ─────────────────────────────────────────────────────────

MEMORY_DIR = os.path.expanduser("~/.claude/projects/-root/memory")
WATERMARK_PATH = os.path.expanduser("~/.local/state/tmq/wrap-watermark.json")
DEAD_LETTER_PATH = os.path.expanduser("~/.local/state/tmq/wrap-dead-letter.json")
LOCK_PATH = os.path.expanduser("~/.local/state/tmq/wrap-watermark.lock")

# ── Error counters (per-run) ──────────────────────────────────────

_ERROR_COUNTS = {
    "db": 0,
    "gh": 0,
    "llm": 0,
    "write": 0,
    "comment": 0,
    "frontmatter": 0,
}


def _log_error(category, msg):
    """Log an error to stderr and increment the per-category counter."""
    _ERROR_COUNTS[category] = _ERROR_COUNTS.get(category, 0) + 1
    print(f"[wrap-on-terminal] {category}: {msg}", file=sys.stderr)


def get_error_counts():
    """Return a copy of the current error counters."""
    return dict(_ERROR_COUNTS)


# ── Locking ───────────────────────────────────────────────────────


def _acquire_lock(lock_path=None):
    """Acquire an exclusive flock on the lock file. Returns fd or None."""
    p = lock_path or LOCK_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (BlockingIOError, OSError):
        if 'fd' in dir():
            try:
                os.close(fd)
            except OSError:
                pass
        return None


def _release_lock(fd):
    """Release the flock and close the fd."""
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


# ── Dead-letter tracking ──────────────────────────────────────────


def _load_dead_letters(path=None):
    """Return the set of (repo, issue) tuples that are permanently failing."""
    p = path or DEAD_LETTER_PATH
    try:
        with open(p) as f:
            data = json.load(f)
            return {tuple(item) for item in data.get("dead", [])}
    except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError):
        return set()


def _save_dead_letters(dead_set, path=None):
    """Persist the dead-letter set."""
    import tempfile as _tmp

    p = path or DEAD_LETTER_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd_w, tmp_path = _tmp.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    try:
        with os.fdopen(fd_w, "w") as f:
            json.dump({"dead": [list(item) for item in sorted(dead_set)]}, f)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Timestamp normalization ───────────────────────────────────────


def _to_iso_string(ts):
    """Normalize a timestamp to an ISO-8601 string.

    psycopg2 returns TIMESTAMPTZ columns as datetime objects;
    sqlite3 (test shim) returns TEXT. This helper normalizes both
    to a sortable ISO string so watermark comparisons work in production.
    """
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


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
    ts_str = _to_iso_string(ts)
    fd, tmp_path = _tmp.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"last_terminal_ts": ts_str}, f)
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
    the watermark). ISO-8601 fixed-format string comparison is safe here.
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
    Also validates that these keys have non-empty values.
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
        elif fm[key] is None or (isinstance(fm[key], str) and not fm[key].strip()):
            errors.append(f"required frontmatter key '{key}' has empty value")

    return errors


# ── Terminal transition discovery ─────────────────────────────────


def _get_conn():
    """Return a database connection. Uses tms.events._get_conn pattern."""
    from tms.events import _get_conn as _events_conn
    return _events_conn()


def find_terminal_transitions_since(watermark):
    """Find terminal transitions with event_timestamp >= watermark.

    Uses >= to handle microsecond collision edge cases (P1-8).
    Returns a list of dicts, each with {aoe_id_prefix, repo, issue,
    terminal_ts}. Terminal transitions that cannot be resolved to a
    dispatch event (no repo/issue) are excluded.

    The caller is responsible for deduplicating by (repo, issue) and
    filtering out already-processed items (via dead-letter set).
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
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
                             AND t.event_timestamp >= %s
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
                for aoe_prefix, ts, repo, issue in cur.fetchall():
                    results.append({
                        "aoe_id_prefix": aoe_prefix,
                        "terminal_ts": ts,
                        "repo": repo,
                        "issue": int(issue) if issue is not None else None,
                    })
                return results
    except Exception as e:
        _log_error("db", f"find_terminal_transitions_since: {e}")
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
                        "timestamp": _to_iso_string(ts),
                        "reason": reason,
                        "blocked_class": blocked_class,
                    })
                return results
    except Exception as e:
        _log_error("db", f"collect_event_history({aoe_id_prefix}): {e}")
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
    """Run a ``gh`` subprocess, return parsed JSON (or None on error)."""
    out = _run(["gh"] + args, timeout=timeout)
    if not out:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


_ISSUE_REF_RE = re.compile(
    r'(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved|ref|refs|see)\s+#?(\d+)',
    re.IGNORECASE,
)


def _pr_references_issue(pr, issue):
    """Return True if the PR title or body references the given issue number.

    Checks for common closing/fixing/referencing keywords followed by #N.
    This post-filters gh search results, which can include false positives
    from bare number matches in commit hashes or other numeric fields.
    """
    title = (pr.get("title", "") or "")
    body = (pr.get("body", "") or "")

    # Direct #N reference anywhere in title or body
    needle = f"#{issue}"
    if needle in title or needle in body:
        return True

    # Keyword reference: "Closes #N", "Fixes #N", "Refs #N", etc.
    for match in _ISSUE_REF_RE.finditer(title + "\n" + body):
        if int(match.group(1)) == issue:
            return True

    return False


def _fetch_gh_prs(gh_repo, issue):
    """Fetch PRs that reference the issue via gh search.

    Post-filters results to only include PRs that actually reference
    the issue number (P1-6). Returns a list of {number, title, state,
    url}. (mergedAt is not a valid gh-search JSON field; state carries
    MERGED.)
    """
    # NOTE: pass the repo via --repo flag and omit type:pr — embedding
    # `repo:` in the query string makes gh mangle+reject the search.
    query = f"{issue} in:title,body"
    data = _gh_json([
        "search", "prs", "--repo", gh_repo, query,
        "--json", "number,title,state,url,body",
        "--limit", "20",
    ])
    if not isinstance(data, list):
        if data is None:
            _log_error("gh", f"gh search failed for {gh_repo}#{issue}")
        return []

    # Post-filter: only PRs that genuinely reference this issue
    filtered = [pr for pr in data if _pr_references_issue(pr, issue)]
    if len(filtered) < len(data):
        _log_error("gh", (
            f"Post-filter dropped {len(data) - len(filtered)}/{len(data)} PRs "
            f"for {gh_repo}#{issue} (false positives from bare number match)"
        ))
    return filtered


def _fetch_llm_call_count(repo, issue):
    """Count llm_call_log rows from the worktree for (repo, issue).

    Routes through _get_conn() (same connection abstraction as the rest
    of the module — P1-5). Matches meta->>'encoded_cwd' against the
    pattern ``--root-wt-<repo>-<issue>--``. Returns the count
    (best-effort; also counts sibling sessions on the same worktree).
    """
    cwd_pattern = f"--root-wt-{repo}-{issue}--"
    try:
        with _get_conn() as conn:
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
    except Exception as e:
        _log_error("llm", f"_fetch_llm_call_count({repo}#{issue}): {e}")
    return 0


# ── Wrap synthesis ────────────────────────────────────────────────

# Map of repo short names to full gh org/repo. Mirrors the tmq
# registry. Keep in sync with tmq list --machine output.
# Test: test_repo_to_gh_matches_tmq_registry
REPO_TO_GH = {
    "tms": "bogocat/tms",
    "distillery": "bogocat/distillery",
    "deploy": "bogocat/distillery",
    "home-portal": "bogocat/home-portal",
    "garmin-doctor": "bogocat/garmin-doctor",
    "tower-fleet": "bogocat/tower-fleet",
    "pi-dotfiles": "bogocat/pi-dotfiles",
    "tmq": "bogocat/tmq",
    "rms": "bogocat/openrms",
    "tcg": "bogocat/tcg",
    "brainlearn": "bogocat/brainlearn",
    "subtitleai": "bogocat/subtitlesv2",
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


def _markdown_escape(text):
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
        pr_title = _markdown_escape(pr.get("title", ""))
        state = pr.get("state", "?")
        merged = "merged" if str(state).upper() == "MERGED" or pr.get("mergedAt") else state
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

    # P1-1: Cross-process lock — prevent overlapping cron invocations
    # from racing on wrap_exists() and posting duplicate comments.
    lock_fd = None
    if dispatch and not dry_run:
        lock_fd = _acquire_lock()
        if lock_fd is None:
            print("[wrap-on-terminal] Lock held by another process, "
                  "skipping this run.", file=sys.stderr)
            return [_result("", 0, "skip_locked")]

    try:
        results = _scan_and_wrap_locked(
            dispatch=dispatch, memory_dir=mem, watermark_path=wp,
            dry_run=dry_run,
        )
    finally:
        _release_lock(lock_fd)

    return results


def _scan_and_wrap_locked(dispatch, memory_dir, watermark_path, dry_run):
    """Core scan logic — assumes lock is already held."""
    mem = memory_dir
    wp = watermark_path

    watermark = read_watermark(wp)
    dead_letters = _load_dead_letters()
    terminals = find_terminal_transitions_since(watermark)
    results = []

    if not terminals:
        return results

    # P1-7: Group terminals by (repo, issue), pick the latest session
    # for each issue so the wrap reflects the most recent attempt.
    by_issue = {}
    for term in terminals:
        key = (term["repo"], term["issue"])
        ts = _to_iso_string(term["terminal_ts"])
        if key not in by_issue or ts > by_issue[key][0]:
            by_issue[key] = (ts, term)

    # Sort by terminal_ts so watermark advances in order
    sorted_issues = sorted(by_issue.items(), key=lambda kv: kv[1][0])

    # Watermark: advance past successfully handled items. Per-item
    # failures don't stall later items (P1-4 fix: continue, don't break).
    wrapped_ts = watermark
    new_dead = set()

    for (repo, issue), (ts, term) in sorted_issues:
        aoe_prefix = term["aoe_id_prefix"]

        # P1-4: Skip dead-letter items (known-bad from previous runs)
        if (repo, issue) in dead_letters:
            results.append(_result(repo, issue, "skip_dead_letter"))
            if ts > (wrapped_ts or ""):
                wrapped_ts = ts
            continue

        # Idempotency: skip if a wrap already exists
        if wrap_exists(mem, repo, issue):
            results.append(_result(repo, issue, "skip_exists"))
            if ts > (wrapped_ts or ""):
                wrapped_ts = ts
            continue

        # Dry-run and non-dispatch both collect data and synthesize
        collect = dispatch or dry_run
        if not collect:
            results.append(_result(repo, issue, "would_wrap",
                                   {"terminal_ts": ts}))
            if ts > (wrapped_ts or ""):
                wrapped_ts = ts
            continue

        # ── Collect objective data ────────────────────────────
        transitions = collect_event_history(aoe_prefix)
        gh_repo = _resolve_gh_repo(repo)
        gh_prs = _fetch_gh_prs(gh_repo, issue)
        llm_count = _fetch_llm_call_count(repo, issue)

        # ── Synthesize and validate ───────────────────────────
        content = synthesize_wrap_content(
            repo, issue, transitions, gh_prs, llm_count,
        )
        errors = validate_frontmatter(content)
        if errors:
            _log_error("frontmatter",
                       f"{repo}#{issue}: {', '.join(errors)}")
            results.append(_result(repo, issue, "skip_frontmatter_invalid",
                                   {"errors": errors}))
            new_dead.add((repo, issue))
            # Advance watermark past poison pill so it doesn't
            # stall later items (P1-4)
            if ts > (wrapped_ts or ""):
                wrapped_ts = ts
            continue

        if dry_run:
            results.append(_result(repo, issue, "dry_run",
                                   {"content_len": len(content)}))
            continue

        # ── Post comment BEFORE writing file ──────────────────
        # P1-2: If the process crashes between file write and
        # comment post, the wrap file exists but the comment is
        # missing forever. Reversing the order means:
        #   - Comment succeeds, file goes down → crash still leaves
        #     a comment (idempotent re-post is harmless on re-run
        #     since wrap_exists() catches the file)
        #   - Comment fails → don't write file → retry next run
        filename = f"issue-wrap-{repo}#{issue}.md"
        comment_ok = _post_issue_comment(gh_repo, issue, filename,
                                         transitions, gh_prs, llm_count)
        if not comment_ok:
            _log_error("comment", f"{repo}#{issue}: gh issue comment failed")
            # Don't advance watermark — retry next run
            # But also don't dead-letter this; comments can fail transiently
            continue

        # ── Write memory file ─────────────────────────────────
        import tempfile as _tmp2
        try:
            os.makedirs(mem, exist_ok=True)
            filepath = os.path.join(mem, filename)
            fd2, tmp_path2 = _tmp2.mkstemp(
                dir=mem, prefix=f".tmp-issue-wrap-{repo}#{issue}-",
                suffix=".md",
            )
            try:
                with os.fdopen(fd2, "w") as f:
                    f.write(content)
                os.replace(tmp_path2, filepath)
            except Exception:
                try:
                    os.unlink(tmp_path2)
                except OSError:
                    pass
                raise
        except OSError as e:
            _log_error("write", f"{repo}#{issue}: {e}")
            results.append(_result(repo, issue, "skip_write_error",
                                   {"error": str(e)}))
            # Don't advance watermark; retry next run
            continue

        results.append(_result(
            repo, issue, "wrapped",
            {"terminal_ts": ts, "comment_ok": comment_ok},
        ))
        if ts > (wrapped_ts or ""):
            wrapped_ts = ts

    # Persist dead letters (P1-4: so future runs skip known-bad items)
    if new_dead:
        dead_letters |= new_dead
        _save_dead_letters(dead_letters)

    # Advance watermark (only on dispatch, not dry-run)
    if dispatch and not dry_run and wrapped_ts and wrapped_ts != watermark:
        write_watermark(wp, wrapped_ts)

    return results


def _post_issue_comment(gh_repo, issue, wrap_filename, transitions,
                        gh_prs, llm_count):
    """Post a closing comment on the GitHub issue.

    Returns True on success, False on failure.
    """
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
        "-- SELECT * FROM tms_review.events WHERE to_status='terminal'",
        "",
        "-- LLM calls from this worktree",
        "-- SELECT COUNT(*) FROM bogocat.llm_call_log",
        "-- WHERE meta->>'encoded_cwd' = '--root-wt-<repo>-<issue>--'",
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
        ok = r.returncode == 0
        if not ok:
            _log_error("comment",
                       f"{gh_repo}#{issue}: gh exit {r.returncode}: "
                       f"{(r.stderr or '').strip()[:200]}")
        return ok
    except subprocess.TimeoutExpired:
        _log_error("comment", f"{gh_repo}#{issue}: gh timed out after 30s")
        return False
    except OSError as e:
        _log_error("comment", f"{gh_repo}#{issue}: gh spawn failed: {e}")
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
        elif r["status"] == "skip_dead_letter":
            extra = " (in dead-letter — previous failures)"
        elif r["status"] == "skip_frontmatter_invalid":
            errs = r.get("errors", [])
            extra = f" (frontmatter errors: {', '.join(errs)})"
        elif r["status"] == "skip_locked":
            extra = " (lock held by another process)"
        elif r["status"] == "wrapped":
            extra = f" (comment={'ok' if r.get('comment_ok') else 'failed'})"
        print(f"  [{r['status']}] {r['repo']}#{r['issue']}{extra}")

    # Surface error counts in summary (P1-3)
    err_counts = get_error_counts()
    total_errs = sum(err_counts.values())
    if total_errs > 0:
        err_detail = ", ".join(
            f"{cat}={n}" for cat, n in sorted(err_counts.items()) if n > 0
        )
        print()
        print(f"Errors ({total_errs} total): {err_detail}")

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
