"""Dispatch-outcomes writer (tms#119).

Populates ``tms_review.dispatch_outcomes`` (migration 004), the mutable
companion table to the append-only events log. Outcomes change over
time (open -> merged/closed_unmerged), so they are UPSERTed here keyed
by ``aoe_id_prefix`` instead of being appended to ``tms_review.events``
(which would break the tms#53 append-only contract).

For each ``dispatch`` event in the since-window that does not already
have a terminal outcome, the job resolves the outcome from GitHub:

- ``dispatch_type=review`` — the event's ``issue`` is a PR number.
  PR state MERGED -> ``merged``, CLOSED -> ``closed_unmerged``,
  OPEN -> ``open``. (derived_via ``gh_pr_state``)
- otherwise — closing-PR references for the dispatched issue
  (GraphQL ``closedByPullRequestsReferences``; the installed gh has
  no ``gh issue view --json`` field for this):
  a MERGED closing PR -> ``merged``; issue CLOSED with only
  unmerged closing PRs, or closed as not-planned with none ->
  ``closed_unmerged``; issue CLOSED as completed with no closing
  PRs at all -> ``unknown`` (could be a direct-to-main completion —
  never fabricated as merged); issue OPEN -> ``open``.
  (derived_via ``gh_closing_prs``)

gh/network errors skip the row — prior state is never clobbered
with ``unknown`` on a transient failure.

Bounded and idempotent (safe to cron every 15 min):

- only dispatches from the last N days (default 30),
- rows already terminal (``merged``/``closed_unmerged``) are skipped
  before any GitHub call,
- one GitHub call per (repo, issue, is_review) group, even when the
  same issue was dispatched multiple times.

Repo short-name -> gh org/repo mapping is derived from
``tmq list --machine`` (TSV: short, path, gh, needs_worktree) — the
single source of truth — with ``wrap_on_terminal.REPO_TO_GH`` as a
fallback for short names no longer in the live registry. One tmq call
per sync run; no file cache needed at this frequency.

CLI:
    python3 -m tms.outcomes sync [--since DAYS] [--dry-run]
"""

import datetime
import json
import subprocess
import sys

import psycopg2

# Delegate DB access to tms.events so the tests' sqlite shim
# (conftest test_db patches events._get_conn) covers this module too.
from tms import events as _events
from tms.wrap_on_terminal import REPO_TO_GH as _REPO_TO_GH_FALLBACK

# Outcomes that never change again — skipped without a GitHub call.
TERMINAL_OUTCOMES = frozenset({"merged", "closed_unmerged"})

# 'unknown' (issue closed-as-completed, no PR link) is not terminal — a PR
# link can appear retroactively — but re-querying it every 15-min cron run
# forever is unbounded API cost (P1). Re-check at most once per this window.
UNKNOWN_RECHECK_HOURS = 24

DEFAULT_SINCE_DAYS = 30


# ── Subprocess / gh helpers (monkeypatched in tests) ──────────────

def _run(cmd, timeout=15):
    """Run a subprocess, return stripped stdout. Empty string on error."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            return ""
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _gh_graphql(query, variables=None):
    """Run ``gh api graphql``, return the parsed ``data`` dict or None.

    ``variables`` are passed as ``-F`` flags so identifiers are never
    string-interpolated into the query (P1: a quote in an org/repo name
    would break the query syntax and silently return None).
    """
    argv = ["gh", "api", "graphql"]
    for key, val in (variables or {}).items():
        argv += ["-F", f"{key}={val}"]
    argv += ["-f", f"query={query}"]
    out = _run(argv, timeout=30)
    if not out:
        return None
    try:
        return json.loads(out).get("data")
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None


# ── Repo registry ─────────────────────────────────────────────────

def load_registry():
    """Map repo short name -> gh org/repo.

    Derived from ``tmq list --machine`` (single source of truth; see
    review_poll._repo_registry for the parsing precedent). Falls back
    to wrap_on_terminal.REPO_TO_GH for short names that are no longer
    registered (old events can reference retired repos) — reusing the
    existing table, not adding another hardcoded registry copy.
    """
    registry = dict(_REPO_TO_GH_FALLBACK)
    for line in (_run(["tmq", "list", "--machine"], timeout=5) or "") \
            .splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        short, _path, gh = parts[0], parts[1], parts[2]
        if short and gh:
            registry[short] = gh
    return registry


def _split_gh_repo(gh_repo):
    """Split ``org/repo`` -> (org, repo), or None if malformed."""
    if not gh_repo or "/" not in gh_repo:
        return None
    owner, _, name = gh_repo.partition("/")
    if not owner or not name:
        return None
    return owner, name


# ── Outcome resolution ────────────────────────────────────────────

def resolve_issue_outcome(gh_repo, issue):
    """Resolve outcome for a non-review dispatch (issue number).

    Returns (outcome, derived_via) or None when GitHub could not be
    queried (caller skips the row — never clobber prior state).
    """
    split = _split_gh_repo(gh_repo)
    if split is None:
        return None
    owner, name = split
    query = (
        "query($owner:String!,$name:String!,$number:Int!)"
        "{repository(owner:$owner,name:$name){issue(number:$number){"
        "state stateReason "
        "closedByPullRequestsReferences(first:20,includeClosedPrs:true)"
        "{nodes{number state}}}}}"
    )
    data = _gh_graphql(
        query, {"owner": owner, "name": name, "number": int(issue)})
    if not data:
        return None
    issue_node = (data.get("repository") or {}).get("issue")
    if not issue_node:
        return None

    refs = ((issue_node.get("closedByPullRequestsReferences") or {})
            .get("nodes") or [])
    pr_states = {(n or {}).get("state") for n in refs}

    if "MERGED" in pr_states:
        return ("merged", "gh_closing_prs")
    if issue_node.get("state") == "OPEN":
        return ("open", "gh_closing_prs")
    # Round-4 P1: a full first page (== the first:20 cap) with no merged
    # PR cannot prove closed_unmerged — the merged ref may be beyond the
    # page. closed_unmerged is terminal and would never self-correct;
    # 'unknown' is rechecked (24h window) and can.
    if len(refs) >= 20 and issue_node.get("stateReason") != "NOT_PLANNED":
        return ("unknown", "gh_closing_prs_capped")
    # Issue CLOSED with no merged closing PR:
    if refs or issue_node.get("stateReason") == "NOT_PLANNED":
        return ("closed_unmerged", "gh_closing_prs")
    # Closed as completed with no closing-PR link at all — could be a
    # direct-to-main completion or a manual close. Never fabricate.
    return ("unknown", "gh_closing_prs")


def resolve_pr_outcome(gh_repo, pr_number):
    """Resolve outcome for a review dispatch (issue field is a PR number).

    Returns (outcome, derived_via) or None when GitHub could not be
    queried.
    """
    split = _split_gh_repo(gh_repo)
    if split is None:
        return None
    owner, name = split
    query = (
        "query($owner:String!,$name:String!,$number:Int!)"
        "{repository(owner:$owner,name:$name){pullRequest(number:$number){"
        "state}}}"
    )
    data = _gh_graphql(
        query, {"owner": owner, "name": name, "number": int(pr_number)})
    if not data:
        return None
    pr_node = (data.get("repository") or {}).get("pullRequest")
    if not pr_node:
        return None
    state = pr_node.get("state")
    if state == "MERGED":
        return ("merged", "gh_pr_state")
    if state == "CLOSED":
        return ("closed_unmerged", "gh_pr_state")
    if state == "OPEN":
        return ("open", "gh_pr_state")
    return None


# ── DB access ─────────────────────────────────────────────────────

def _load_existing_outcomes():
    """Return {aoe_id_prefix: (outcome, derived_at)} for all rows.

    Returns None on DB error (round-4 P1: the earlier {} fallback made
    the sync treat every row — including terminal ones — as unsettled,
    re-resolving them and bumping derived_at; weaker than the
    preservation guarantee the tests promise). The caller aborts the
    sync and surfaces load_failed instead.
    """
    existing = {}
    try:
        with _events._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT aoe_id_prefix, outcome, derived_at "
                    "FROM tms_review.dispatch_outcomes"
                )
                for row in cur.fetchall():
                    existing[row[0]] = (row[1], row[2])
    except (psycopg2.OperationalError, psycopg2.DatabaseError, OSError) as exc:
        print(f"  warn: could not load existing outcomes: {exc}",
              file=sys.stderr)
        return None
    return existing


def _upsert_outcome(aoe_id_prefix, repo, issue, outcome, derived_via):
    """UPSERT one outcome row keyed by aoe_id_prefix.

    ON CONFLICT updates outcome/derived_via/derived_at and preserves
    created_at (works on both postgres and the sqlite test shim).
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with _events._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO tms_review.dispatch_outcomes
                       (aoe_id_prefix, repo, issue, outcome,
                        derived_via, derived_at, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (aoe_id_prefix) DO UPDATE SET
                           outcome = EXCLUDED.outcome,
                           derived_via = EXCLUDED.derived_via,
                           derived_at = EXCLUDED.derived_at,
                           repo = EXCLUDED.repo,
                           issue = EXCLUDED.issue""",
                    (aoe_id_prefix, repo, int(issue), outcome,
                     derived_via, now, now),
                )
            conn.commit()
    except (psycopg2.OperationalError, psycopg2.DatabaseError, OSError) as exc:
        print(f"  warn: outcome upsert failed for {aoe_id_prefix}: {exc}",
              file=sys.stderr)
        return False
    return True


# ── Sync ──────────────────────────────────────────────────────────

def sync_outcomes(since_days=DEFAULT_SINCE_DAYS, dry_run=False):
    """Resolve and upsert outcomes for recent non-terminal dispatches.

    Returns a summary dict: {checked, resolved, written, skipped_terminal,
    skipped_unresolved}.
    """
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc).date()
        - datetime.timedelta(days=since_days)
    ).isoformat()
    events = _events._read_events_from_db(since=cutoff)
    dispatches = []
    for e in events:
        if (e.get("event_type") != "dispatch" or not e.get("aoe_id_prefix")
                or not e.get("repo") or e.get("issue") is None):
            continue
        # Round-4 P1: a single corrupt issue value must skip the row,
        # not ValueError-abort the whole sync.
        try:
            e = dict(e, issue=int(e["issue"]))
        except (ValueError, TypeError):
            print(f"  warn: corrupt issue value {e.get('issue')!r} "
                  f"({e.get('repo')}, {e.get('aoe_id_prefix')}) — skipped",
                  file=sys.stderr)
            continue
        dispatches.append(e)

    existing = _load_existing_outcomes()
    if existing is None:
        # Round-4 P1: abort rather than re-resolve terminal rows blind.
        print("  error: aborting sync — existing outcomes unreadable",
              file=sys.stderr)
        return {"checked": 0, "resolved": 0, "written": 0,
                "skipped_terminal": 0, "skipped_unresolved": 0,
                "load_failed": True}

    # Group by (repo, issue, is_review) so re-dispatches of the same
    # issue cost one GitHub call, then fan the outcome out to every
    # non-terminal aoe_id_prefix in the group.
    groups = {}  # (repo, issue, is_review) -> [aoe_id_prefix, ...]
    for d in dispatches:
        is_review = d.get("dispatch_type") == "review"
        key = (d["repo"], d["issue"], is_review)
        prefixes = groups.setdefault(key, [])
        if d["aoe_id_prefix"] not in prefixes:
            prefixes.append(d["aoe_id_prefix"])

    registry = load_registry() if groups else {}

    summary = {
        "checked": 0,
        "resolved": 0,
        "written": 0,
        "skipped_terminal": 0,
        "skipped_unresolved": 0,
    }

    now_dt = datetime.datetime.now(datetime.timezone.utc)

    def _settled(prefix):
        """Terminal, or a fresh 'unknown' inside its re-check window."""
        prior = existing.get(prefix)
        if prior is None:
            return False
        outcome, derived_at = prior
        if outcome in TERMINAL_OUTCOMES:
            return True
        if outcome == "unknown" and derived_at:
            try:
                age = now_dt - datetime.datetime.fromisoformat(str(derived_at))
                return age < datetime.timedelta(hours=UNKNOWN_RECHECK_HOURS)
            except (ValueError, TypeError):
                return False
        return False

    for (repo, issue, is_review), prefixes in sorted(groups.items()):
        pending = [p for p in prefixes if not _settled(p)]
        summary["skipped_terminal"] += len(prefixes) - len(pending)
        if not pending:
            continue
        summary["checked"] += 1

        gh_repo = registry.get(repo)
        if not gh_repo:
            summary["skipped_unresolved"] += 1
            print(f"  skip {repo}#{issue}: no gh mapping for '{repo}'",
                  file=sys.stderr)
            continue

        if is_review:
            resolved = resolve_pr_outcome(gh_repo, issue)
        else:
            resolved = resolve_issue_outcome(gh_repo, issue)
        if resolved is None:
            summary["skipped_unresolved"] += 1
            print(f"  skip {repo}#{issue}: gh resolution failed",
                  file=sys.stderr)
            continue

        outcome, derived_via = resolved
        summary["resolved"] += 1
        for prefix in pending:
            prior = existing.get(prefix)
            prior_outcome = prior[0] if prior else "-"
            if dry_run:
                print(f"  [dry-run] {repo}#{issue} ({prefix}): "
                      f"{prior_outcome} -> {outcome} "
                      f"[{derived_via}]")
            elif _upsert_outcome(prefix, repo, issue, outcome, derived_via):
                summary["written"] += 1

    return summary


# ── CLI ───────────────────────────────────────────────────────────

def main():
    """Entry point for ``python3 -m tms.outcomes sync``."""
    if len(sys.argv) < 2 or sys.argv[1] != "sync":
        print("usage: python3 -m tms.outcomes sync [--since DAYS] "
              "[--dry-run]", file=sys.stderr)
        sys.exit(1)

    since_days = DEFAULT_SINCE_DAYS
    dry_run = False
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--since" and i + 1 < len(args):
            try:
                since_days = int(args[i + 1])
            except ValueError:
                print(f"--since expects an integer day count, "
                      f"got: {args[i + 1]}", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    summary = sync_outcomes(since_days=since_days, dry_run=dry_run)
    mode = "dry-run" if dry_run else "sync"
    print(
        f"Outcomes {mode}: {summary['checked']} group(s) checked, "
        f"{summary['resolved']} resolved, {summary['written']} row(s) "
        f"written, {summary['skipped_terminal']} already terminal, "
        f"{summary['skipped_unresolved']} unresolved."
    )


if __name__ == "__main__":
    main()
