"""Review-trigger poller for independent review (tms#57 Phase 2).

This module is the *consumer* side of the reviewer-owned verdict contract
defined in the ``code-review`` skill (pi-dotfiles): every review comment
ends with one machine-parseable line ::

    <<REVIEW-VERDICT: PASS sha=<head-sha> rounds=<n> panel=<model,...>>>
    <<REVIEW-VERDICT: FAIL sha=<head-sha> p0=<n> p1=<n> rounds=<n> panel=<model,...>>>

The poller removes the author agent from the review trigger path. A cron
timer (every 5 min) lists open PRs across the ``tmq`` repo registry, finds
those with **no verdict at all**, and dispatches ``tmq review`` for them.
The author still self-triggers review (out of scope to remove); the poller
is a safety net for PRs that never got reviewed, not a second orchestrator
that races the author's FAIL/stale-sha re-review loop.

V1 scope (operator-approved 2026-07-13): dispatch ONLY for PRs with zero
verdict comments. FAIL / stale-PASS PRs are skipped — the author owns that
loop. Widen later by editing :func:`needs_poller_review` once #53 metrics
show the poller is stable.

Public API:
  - REVIEW_VERDICT_RE            regex for the verdict line
  - parse_verdict_line(text)     -> dict | None
  - latest_verdict(comments)     -> dict | None  (most recent verdict)
  - sha_matches(verdict_sha, head_oid) -> bool
  - has_current_pass(comments, head_oid) -> bool
  - needs_poller_review(comments, head_oid) -> bool   (V1 policy)
  - list_open_prs(gh_repo)       -> list[dict]
  - live_review_sessions()       -> set[str] of "repo#pr" keys
  - scan_repos(dispatch=False, repo_filter=None) -> list[dict]

All external calls (gh, tmq, aoe, tmux) go through thin wrappers so tests
can monkeypatch them without touching the network.
"""

import json
import os
import re
import subprocess
import sys
import traceback


# ── Verdict-line parser (consumes the pi-dotfiles contract) ───────
#
# The marker is ``<<REVIEW-VERDICT: STATE ...>>``. Fields after STATE are
# key=value tokens in unspecified order: sha (hex), p0, p1, rounds, panel.
# We capture STATE + the free-form interior, then pluck each key.

REVIEW_VERDICT_RE = re.compile(
    r'<<REVIEW-VERDICT:\s*(PASS|FAIL)\b(.*?)>>',
    re.IGNORECASE,
)
_SHA_RE = re.compile(r'sha=([0-9a-fA-F]+)')
_INT_RE_TEMPLATE = r'%s=(\d+)'
_PANEL_RE = re.compile(r'panel=(.+?)(?=\s+\w+=|$)')


def _search1(pattern, text):
    m = pattern.search(text or '')
    return m.group(1) if m else ''


def _search1_int(pattern, text):
    m = pattern.search(text or '')
    return int(m.group(1)) if m else 0


def parse_verdict_line(text):
    """Parse a verdict line from text (a full comment body is fine).

    Returns ``{state, sha, p0, p1, rounds, panel}`` or ``None`` if no
    verdict marker is present. ``p0``/``p1``/``rounds`` default to 0;
    ``sha``/``panel`` default to ``''`` when absent.
    """
    m = REVIEW_VERDICT_RE.search(text or '')
    if not m:
        return None
    state = m.group(1).upper()
    rest = m.group(2)
    return {
        'state': state,
        'sha': _search1(_SHA_RE, rest),
        'p0': _search1_int(re.compile(_INT_RE_TEMPLATE % 'p0'), rest),
        'p1': _search1_int(re.compile(_INT_RE_TEMPLATE % 'p1'), rest),
        'rounds': _search1_int(re.compile(_INT_RE_TEMPLATE % 'rounds'), rest),
        'panel': _search1(_PANEL_RE, rest),
    }


def sha_matches(verdict_sha, head_oid):
    """True if the verdict sha refers to the PR head.

    The verdict sha may be short (7-char) or full (40-char), so we prefix
    -match in *either* direction. Case-insensitive (git shas are lowercase
    but defensive against uppercase).
    """
    if not verdict_sha or not head_oid:
        return False
    v = verdict_sha.lower()
    h = head_oid.lower()
    return v.startswith(h) or h.startswith(v)


def latest_verdict(comments):
    """Return the most recent verdict across a list of comment bodies.

    ``comments`` is a list of strings (comment bodies) in chronological
    order (oldest first — the order ``gh pr view --json comments`` returns).
    The most recent verdict-bearing comment wins; we scan from the end.
    """
    for body in reversed(comments):
        v = parse_verdict_line(body)
        if v is not None:
            return v
    return None


def has_current_pass(comments, head_oid):
    """True iff the latest verdict is PASS for the current head sha.

    A PASS whose sha does not match the head is stale (code was pushed
    after the review) and is NOT a current pass.
    """
    v = latest_verdict(comments)
    return v is not None and v['state'] == 'PASS' and sha_matches(v['sha'], head_oid)


def needs_poller_review(comments, head_oid):
    """V1 policy: the poller dispatches only for PRs with NO verdict at all.

    Rationale (operator-approved 2026-07-13): a FAIL verdict or a stale
    PASS means the author agent is (or should be) driving the fix → re-review
    loop; the poller dispatching there would race the author's own
    self-trigger. The poller is a safety net for *never-reviewed* PRs.

    ``head_oid`` is accepted for API symmetry and V2 widening; V1 ignores
    it (any verdict, current or not, suppresses the poller).
    """
    return latest_verdict(comments) is None


def _classify_skip(comments, head_oid):
    """Human-readable skip reason for a PR that has a verdict (V1)."""
    v = latest_verdict(comments)
    if v is None:
        return None
    if v['state'] == 'FAIL':
        return 'skip_fail'
    if sha_matches(v['sha'], head_oid):
        return 'skip_current_pass'
    return 'skip_stale_pass'


# ── External-call wrappers (thin, monkeypatchable) ────────────────

def _run(cmd, timeout=15):
    """Run a subprocess, return stripped stdout. Empty string on error."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ''


def _run_tmq_list_machine():
    """Return the raw stdout of ``tmq list --machine`` (tab-separated rows)."""
    return _run(['tmq', 'list', '--machine'], timeout=5)


def _repo_registry(repo_filter=None):
    """Parse ``tmq list --machine`` into ``[(short, gh_repo), ...]``.

    Dedupes by gh_repo (``deploy`` and ``distillery`` both map to
    ``bogocat/distillery``). Prefers entries with ``needs_worktree=1``
    (isolated worktree) so the poller's live-session key matches the
    author agent's session name — if the poller used ``deploy#547`` as
    the key, it wouldn't detect a ``distillery#547`` review session.
    Optionally filters to a single short name.
    """
    candidates = {}  # gh_repo -> list of (short, wt_flag)
    for line in (_run_tmq_list_machine() or '').splitlines():
        parts = line.split('\t')
        if len(parts) < 4:
            continue
        short, _path, gh, wt = parts[0], parts[1], parts[2], parts[3]
        if not short or not gh:
            continue
        if repo_filter and short != repo_filter:
            continue
        wt_flag = int(wt) if (wt or '').isdigit() else 0
        candidates.setdefault(gh, []).append((short, wt_flag))

    rows = []
    for gh, entries in candidates.items():
        # Prefer worktree=1 entry (isolated), else first.
        entries.sort(key=lambda e: (e[1] != 1, 0))
        short = entries[0][0]
        rows.append((short, gh))
    return rows


def _gh_json(args, timeout=15):
    """Run a ``gh`` subcommand, return parsed JSON (or None on error)."""
    out = _run(['gh'] + args, timeout=timeout)
    if not out:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def list_open_prs(gh_repo):
    """List open, non-draft PRs for a repo.

    Uses ``--state open`` so closed/merged PRs are excluded by construction
    (AC4 first half). Drafts are returned here (with ``isDraft=True``) and
    filtered by the caller, so the scan can report them as ``skip_draft``.
    """
    data = _gh_json([
        'pr', 'list', '--repo', gh_repo, '--state', 'open',
        '--json', 'number,headRefOid,isDraft', '--limit', '100',
    ])
    if not isinstance(data, list):
        return []
    return data


def _pr_comment_bodies(gh_repo, number):
    """Return PR comment bodies (chronological, oldest first).

    Returns ``(bodies, ok)`` so the caller can distinguish \"no comments\"
    from \"failed to fetch comments\" — a gh error must not be treated as
    affirmative evidence of no-verdict (review P0).
    """
    data = _gh_json([
        'pr', 'view', str(number), '--repo', gh_repo,
        '--json', 'comments',
    ])
    if not isinstance(data, dict):
        return [], False
    return [c.get('body', '') for c in data.get('comments', [])], True


def _pr_still_open(gh_repo, number):
    """Re-check a PR is still open right before dispatching (orphan guard).

    A PR may close/merge between the scan and the dispatch. Returns True if
    still open. Defaults to True on gh failure (fail-open: a transient gh
    error should not suppress a legitimate dispatch — the review agent
    itself no-ops on a closed/merged PR).
    """
    data = _gh_json([
        'pr', 'view', str(number), '--repo', gh_repo,
        '--json', 'state',
    ])
    if not isinstance(data, dict):
        return True
    return data.get('state') == 'OPEN'


def _run_aoe_list_json():
    """Return parsed ``aoe list --json`` (list of session dicts)."""
    out = _run(['aoe', 'list', '--json'], timeout=5)
    if not out:
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _tmux_session_names():
    """Return current tmux session names (fallback path for live sessions)."""
    out = _run(['tmux', 'list-sessions', '-F', '#{session_name}'], timeout=3)
    return [s for s in out.splitlines() if s]


def live_review_sessions():
    """Return the set of ``"repo#pr"`` keys with a live review session.

    Sources (both checked, either suffices to block a dispatch):
      - aoe sessions whose title parses as ``review-<repo>#<pr>``
      - tmux sessions named ``review-<repo>#<pr>[-cc|-oc]`` (fallback spawn)

    feat/fix/chore sessions do NOT count (different loop). Descriptive
    titles (e.g. "tms issue filing") are ignored — mirrors the
    session_map.py P1#3 fix: never auto-link a title to an issue.
    """
    from tms.session_map import parse_issue_title, parse_tmq_session_name

    live = set()
    for s in _run_aoe_list_json():
        title = s.get('title', '') or ''
        parsed = parse_issue_title(title)
        if parsed is None:
            continue
        _type, repo, num = parsed
        if _type == 'review':
            live.add(f'{repo}#{num}')

    for name in _tmux_session_names():
        parsed = parse_tmq_session_name(name)
        if parsed is None:
            continue
        _type, repo, num = parsed
        if _type == 'review':
            live.add(f'{repo}#{num}')

    return live


# ── Dispatch ──────────────────────────────────────────────────────

def _dispatch_review(repo, pr):
    """Spawn ``tmq review <repo> <pr>`` for a PR needing review.

    Sets ``TMQ_NO_LOG=1`` so tmq does not log its own dispatch event — the
    poller logs the dispatch itself (with ``source='poller'``) so #53 stats
    can split author-self-triggered from poller-triggered reviews.

    Returns ``True`` if tmq launched successfully (exit 0), ``False`` on
    failure (review P0: failed dispatches must not be counted as successful).
    """
    env = dict(os.environ)
    env['TMQ_NO_LOG'] = '1'
    try:
        r = subprocess.run(
            ['tmq', 'review', repo, str(pr)],
            env=env, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        print(f'WARNING: tmq review {repo}#{pr} timed out after 60s',
              file=sys.stderr)
        return False
    except OSError as e:
        print(f'WARNING: tmq review {repo}#{pr} spawn failed: {e}',
              file=sys.stderr)
        return False


def _result(repo, gh_repo, pr, status, head=None):
    return {
        'repo': repo, 'gh_repo': gh_repo, 'pr': pr,
        'status': status, 'head': head,
    }


def scan_repos(dispatch=False, repo_filter=None, max_dispatch=3):
    """Scan the repo registry for PRs needing an independent review.

    For each open, non-draft PR with no verdict at all (V1) and no live
    review session: record a candidate. When ``dispatch`` is True, spawn
    ``tmq review`` for it (after an orphan re-check) and log a dispatch
    event (``source='poller'``) to the #53 event log.

    ``max_dispatch`` caps dispatches per run so a large backlog (e.g. the
    first run against old PRs) does not spawn an unbounded burst of review
    sessions. Overflow PRs are reported as ``would_dispatch`` and picked up
    on subsequent runs. Default 3 (spreads a 36-PR backlog over ~1h at a
    5-min cadence). ``0`` is a scan-only kill switch.

    Returns a list of result dicts: ``{repo, gh_repo, pr, status, head}``
    where status is one of: ``would_dispatch``, ``dispatched``,
    ``skip_draft``, ``skip_current_pass``, ``skip_fail``, ``skip_stale_pass``,
    ``skip_live_session``, ``skip_closed``, ``skip_gh_error``,
    ``skip_dispatch_failed``, ``skip_verdict``.
    """
    live = live_review_sessions()
    results = []
    dispatched_count = 0

    for short, gh_repo in _repo_registry(repo_filter=repo_filter):
        for pr in list_open_prs(gh_repo):
            number = pr.get('number')
            head = pr.get('headRefOid', '') or ''
            if not number:
                continue

            if pr.get('isDraft'):
                results.append(_result(short, gh_repo, number, 'skip_draft', head))
                continue

            comments, ok = _pr_comment_bodies(gh_repo, number)
            if not ok:
                results.append(_result(short, gh_repo, number, 'skip_gh_error', head))
                continue

            # V1: skip any PR that already has a verdict (author owns the
            # FAIL/stale-sha re-review loop; poller is for never-reviewed PRs).
            if not needs_poller_review(comments, head):
                skip = _classify_skip(comments, head) or 'skip_verdict'
                results.append(_result(short, gh_repo, number, skip, head))
                continue

            key = f'{short}#{number}'
            if key in live:
                results.append(_result(short, gh_repo, number, 'skip_live_session', head))
                continue

            # Rate-limit: once the per-run cap is hit, remaining candidates
            # are reported but not dispatched (next run picks them up).
            can_dispatch = dispatch and dispatched_count < max_dispatch

            if not can_dispatch:
                results.append(_result(short, gh_repo, number, 'would_dispatch', head))
                continue

            # Re-check live sessions immediately before dispatch
            # (review P0: the snapshot at scan start is stale by the time we
            # reach a candidate — another cron or author may have started a
            # review in the meantime).
            if f'{short}#{number}' in live_review_sessions():
                results.append(_result(short, gh_repo, number, 'skip_live_session', head))
                continue

            # Orphan guard: PR may have closed between scan and dispatch.
            if not _pr_still_open(gh_repo, number):
                results.append(_result(short, gh_repo, number, 'skip_closed', head))
                continue

            if _dispatch_review(short, number):
                _log_poller_dispatch(short, number)
                dispatched_count += 1
                results.append(_result(short, gh_repo, number, 'dispatched', head))
            else:
                results.append(_result(short, gh_repo, number, 'skip_dispatch_failed', head))

    return results


def _log_poller_dispatch(repo, issue):
    """Log a poller-triggered review dispatch to the #53 event log.

    ``source='poller'`` rides the payload column (no schema change) so
    stats can later split self-triggered vs poller-triggered reviews.
    """
    try:
        from tms.events import log_dispatch_event
        log_dispatch_event(
            repo=repo, issue=issue, agent='pi', provider='', model='',
            dispatch_type='review', worktree='', session='',
            aoe_id_prefix='', source='poller',
        )
    except Exception:
        # Event logging must never break a dispatch loop, but it must
        # not silently swallow persistent failures either (review P1).
        print('WARNING: poller dispatch event logging failed:',
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


# ── CLI ───────────────────────────────────────────────────────────

def _format_report(results):
    """Pretty-print the scan results. One line per PR."""
    if not results:
        print("No open PRs need review.")
        return

    counts = {}
    for r in results:
        counts[r['status']] = counts.get(r['status'], 0) + 1

    by_status = {}
    for r in results:
        by_status.setdefault(r['status'], []).append(r)

    # Show dispatch candidates first, then skips.
    order = ['dispatched', 'would_dispatch',
             'skip_live_session', 'skip_closed',
             'skip_gh_error', 'skip_dispatch_failed',
             'skip_draft', 'skip_fail', 'skip_stale_pass',
             'skip_current_pass', 'skip_verdict']
    for status in order:
        rows = by_status.get(status, [])
        if not rows:
            continue
        for r in rows:
            print(f"  [{status}] {r['gh_repo']}#{r['pr']}")

    print()
    print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def main():
    """Entry point for ``python3 -m tms.review_poll``.

      scan-reviews [--dispatch] [--repo <short>] [--max-dispatch <n>]
    """
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print("usage: python3 -m tms.review_poll scan-reviews "
              "[--dispatch] [--repo <short>] [--max-dispatch <n>]")
        sys.exit(0)

    subcmd = args[0]
    if subcmd != 'scan-reviews':
        print(f"unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)

    # Check for --help/-h anywhere in the remaining args (not just position 0)
    if '--help' in args or '-h' in args:
        print("usage: tms events scan-reviews "
              "[--dispatch] [--repo <short>] [--max-dispatch <n>]")
        print()
        print("  Scan open PRs across the repo registry for those lacking a")
        print("  reviewer verdict (tms#57). Dry run by default; --dispatch")
        print("  spawns tmq review for never-reviewed PRs.")
        print()
        print("  --dispatch        Spawn tmq review for PRs needing review")
        print("  --max-dispatch N  Cap dispatches per run (default 3; 0 = scan-only)")
        print("  --repo SHORT       Restrict scan to one registered repo")
        sys.exit(0)

    dispatch = '--dispatch' in args
    repo_filter = None
    max_dispatch = 3

    i = 1
    while i < len(args):
        if args[i] == '--repo' and i + 1 < len(args):
            repo_filter = args[i + 1]
            i += 2
        elif args[i] == '--max-dispatch' and i + 1 < len(args):
            try:
                max_dispatch = int(args[i + 1])
            except ValueError:
                print(f"invalid --max-dispatch value: {args[i + 1]}", file=sys.stderr)
                sys.exit(1)
            i += 2
        else:
            i += 1

    results = scan_repos(dispatch=dispatch, repo_filter=repo_filter,
                         max_dispatch=max_dispatch)
    _format_report(results)


if __name__ == '__main__':
    main()
