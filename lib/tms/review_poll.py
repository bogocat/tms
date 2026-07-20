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

Staleness filter (tms#91, 2026-07-19): PRs whose ``updatedAt`` is older
than 14 days are skipped as ``skip_stale``. This prevents the 3/run cap
from burning dispatches on dead PRs that will never reach MERGE-READY.
PRs with the ``keep-warm`` label are exempt from the staleness filter.

Public API:
  - REVIEW_VERDICT_RE            regex for the verdict line
  - parse_verdict_line(text)     -> dict | None
  - latest_verdict(comments)     -> dict | None  (most recent verdict)
  - sha_matches(verdict_sha, head_oid) -> bool
  - has_current_pass(comments, head_oid) -> bool
  - needs_poller_review(comments, head_oid) -> bool   (V1 policy)
  - is_pr_stale(updated_at, days=14) -> bool   (tms#91)
  - list_open_prs(gh_repo)       -> list[dict]
  - live_review_sessions()       -> set[str] of "repo#pr" keys
  - scan_repos(dispatch=False, repo_filter=None) -> list[dict]

All external calls (gh, tmq, aoe, tmux) go through thin wrappers so tests
can monkeypatch them without touching the network.
"""

import datetime
import json
import os
import re
import subprocess
import sys
import traceback

from tms.diff_shape import classify_diff
from tms.review_eval import _get_conn


# ── Panel entry parsing (tms#96) ─────────────────────────────────

# Panel entries can be in two formats:
#   agent(model) — e.g. reviewer(deepseek-v4-pro)
#   bare model   — e.g. deepseek-v4-pro
# They are comma-separated, may have whitespace.
# Entries with (max-sub) or (other annotations) in parens have the
# annotation stripped; bare model entries are used as both agent and model.

def _split_panel_entries(panel_text):
    """Split a panel field value on commas, respecting parenthesized groups.

    ``reviewer(deepseek-v4-pro),claude-sonnet(max-sub)`` →
    ``['reviewer(deepseek-v4-pro)', 'claude-sonnet(max-sub)']``
    """
    if not panel_text:
        return []
    entries = []
    depth = 0
    current = []
    for ch in panel_text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            entries.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    remaining = ''.join(current).strip()
    if remaining:
        entries.append(remaining)
    return entries


_AGENT_MODEL_RE = re.compile(
    r'^([^(]+)\((.+)\)$'
)


def _parse_panel_entries(panel_text):
    """Parse a panel field value into a list of (reviewer_agent, model) tuples.

    Handles:
        ``reviewer(deepseek-v4-pro),reviewer-m3(MiniMax-M3)``
        ``deepseek-v4-pro,minimax-m3``
        ``claude-sonnet(max-sub)``
        ``reviewer-fast(claude-sonnet(max-sub))``
        Mixed formats in the same panel.

    Returns an empty list for empty or None panel_text.
    """
    if not panel_text or not panel_text.strip():
        return []
    entries = []
    # Known subscription/pricing annotations (not part of model names).
    # Strip these before attempting agent(model) extraction.
    _ANNOTATION_RE = re.compile(r'\((?:max-sub|max|pro|lite|sub)\)')

    for entry in _split_panel_entries(panel_text):
        # Strip known annotation parens only, not model-name parens.
        cleaned = _ANNOTATION_RE.sub('', entry).strip()
        if not cleaned:
            continue
        m = _AGENT_MODEL_RE.match(cleaned)
        if m:
            agent = m.group(1).strip()
            model = m.group(2).strip()
            entries.append((agent, model))
        else:
            # Bare model name (no parens at all, or all parens were
            # annotations and got stripped).
            entries.append((cleaned, cleaned))
    return entries


# ── Provider inference (tms#96) ──────────────────────────────────

_PROVIDER_MAP = {
    'deepseek': ['deepseek'],
    'minimax': ['minimax'],
    'anthropic': ['claude', 'anthropic'],
}


def _model_to_provider(model):
    """Infer the provider name from a model identifier.

    Uses case-insensitive substring matching against known provider
    patterns. Falls back to ``'unknown'``.
    """
    if not model:
        return 'unknown'
    lower = model.lower()
    for provider, keywords in _PROVIDER_MAP.items():
        for kw in keywords:
            if kw in lower:
                return provider
    return 'unknown'


# ── Verdict-to-rows conversion (tms#96) ──────────────────────────

def _verdict_to_rows(repo, pr_number, verdict):
    """Convert a parsed verdict dict into a list of reviewer_runs row dicts.

    One row per reviewer in the panel. Verdicts without a panel field
    (empty or missing reviewer list) return an empty list — we can't
    create rows without knowing who reviewed.
    """
    panel_text = verdict.get('panel', '')
    entries = _parse_panel_entries(panel_text)
    if not entries:
        return []
    rows = []
    for agent, model in entries:
        rows.append({
            'repo': repo,
            'pr_number': pr_number,
            'review_round': verdict.get('rounds', 0),
            'reviewer_agent': agent,
            'model': model,
            'provider_used': _model_to_provider(model),
            'diff_sha_reviewed': verdict.get('sha', ''),
            'p0': verdict.get('p0', 0),
            'p1': verdict.get('p1', 0),
            'p2': verdict.get('p2', 0),
            'wall_time_ms': None,
            'findings': None,
            'input_tokens': None,
            'output_tokens': None,
            'specialist_composition': [],
        })
    return rows


# ── Verdict capture (tms#96) ─────────────────────────────────────

# -- Verdict-line parser (consumes the pi-dotfiles contract) ───────
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


def is_pr_stale(updated_at, days=14):
    """True if ``updated_at`` is older than ``days`` days.

    ``updated_at`` is an ISO 8601 timestamp string (e.g. from
    ``gh pr list --json updatedAt``). Returns False (fail-open) if
    ``updated_at`` is None, empty, or unparseable — a missing
    timestamp must not block a legitimate dispatch.

    The comparison uses calendar dates (``.date()``) for an exclusive
    threshold: PRs last touched on the same calendar date as the cutoff
    are NOT stale. This intentionally gives ~24h of threshold fuzz
    rather than a strict N×24h wall clock.

    Note: ``updatedAt`` updates on any activity (pushes, comments, labels,
    bot actions) — not just code pushes. This is deliberate: a PR receiving
    comments or bot attention is not truly abandoned. For a tighter
    "code-only" staleness signal, ``pushedAt`` is available from the same
    ``gh pr list`` JSON response.
    """
    if not updated_at:
        return False
    try:
        ts = datetime.datetime.fromisoformat(
            str(updated_at).replace('Z', '+00:00')
        )
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        # Compare on date boundaries for an exclusive threshold:
        # exactly ``days`` days ago (same calendar date) is NOT stale.
        return ts.date() < cutoff.date()
    except (ValueError, TypeError, OverflowError):
        return False


_KEEP_WARM_LABEL = 'keep-warm'


def _has_keep_warm_label(labels):
    """True if the PR's labels include ``keep-warm`` (case-insensitive).

    Defensive against ``name: null`` in label dicts — ``dict.get(key, default)``
    returns the default only when the key is *missing*, not when the key is
    present with a ``None`` value. Using ``or ''`` after ``get`` handles both.
    """
    if not labels:
        return False
    return any(
        (str(l.get('name') or '') if isinstance(l, dict) else str(l or '')).lower()
        == _KEEP_WARM_LABEL
        for l in labels
    )


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

def _capture_verdict(repo, pr_number, verdict):
    """Insert reviewer_runs rows for a single verdict, with dedup.

    Dedup key: (repo, pr_number, diff_sha_reviewed, reviewer_agent).
    If any row for the same (repo, pr_number, sha, reviewer) already
    exists, that row is skipped. Returns the number of rows inserted.
    """
    rows = _verdict_to_rows(repo, pr_number, verdict)
    if not rows:
        return 0
    inserted = 0
    with _get_conn() as conn:
        with conn.cursor() as cur:
            for row in rows:
                # Dedup: skip if this exact (repo, pr, sha, agent) exists.
                cur.execute(
                    """SELECT 1 FROM tms_review.reviewer_runs
                       WHERE repo = %s AND pr_number = %s
                         AND diff_sha_reviewed = %s
                         AND reviewer_agent = %s
                       LIMIT 1""",
                    (row['repo'], row['pr_number'],
                     row['diff_sha_reviewed'], row['reviewer_agent']),
                )
                if cur.fetchone():
                    continue
                import uuid
                import json as _json
                cur.execute(
                    """INSERT INTO tms_review.reviewer_runs
                       (run_id, repo, pr_number, review_round,
                        reviewer_agent, model, provider_used,
                        diff_sha_reviewed, p0, p1, p2, wall_time_ms,
                        findings, input_tokens, output_tokens,
                        specialist_composition)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(uuid.uuid4()),
                        row['repo'], row['pr_number'], row['review_round'],
                        row['reviewer_agent'], row['model'],
                        row['provider_used'], row['diff_sha_reviewed'],
                        row['p0'], row['p1'], row['p2'],
                        row['wall_time_ms'],
                        _json.dumps(row['findings']) if row['findings'] is not None else None,
                        row['input_tokens'], row['output_tokens'],
                        _json.dumps(row['specialist_composition']),
                    ),
                )
                inserted += 1
        conn.commit()
    return inserted


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

    Also fetches ``updatedAt`` (for staleness filter, tms#91) and ``labels``
    (for keep-warm exemption).
    """
    data = _gh_json([
        'pr', 'list', '--repo', gh_repo, '--state', 'open',
        '--json', 'number,headRefOid,isDraft,updatedAt,labels', '--limit', '100',
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


def _fetch_pr_reviews(gh_repo, number):
    """Fetch PR review bodies (GH Reviews API, not issue comments).

    Returns a list of review body strings. Returns empty list on error.
    This is the dual-surface complement to ``_pr_comment_bodies`` — some
    verdicts (e.g. tms#88) are posted as PR reviews rather than issue
    comments and would be systematically missed without this path.
    """
    data = _gh_json([
        'api', f'repos/{gh_repo}/pulls/{number}/reviews',
    ])
    if not isinstance(data, list):
        return []
    return [r.get('body', '') for r in data if r.get('body')]


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


# ── Diff-shape classifier integration (#94) ───────────────────────

def _fetch_pr_diff(gh_repo, pr_number):
    """Fetch the diff for a PR via ``gh pr diff``.

    Returns the diff text as a string, or empty string on failure.
    The caller treats empty string as fail-open (no specialist signals).
    """
    out = _run(['gh', 'pr', 'diff', str(pr_number), '--repo', gh_repo],
               timeout=30)
    if not out:
        print(f'WARNING: gh pr diff {gh_repo}#{pr_number} returned empty '
              f'(timeout, auth error, or rate limit)', file=sys.stderr)
    return out if out else ''


def _scan_prs_for_verdicts(gh_repo, state='merged', since=None):
    """Scan merged/closed PRs for verdict markers in both comments
    and reviews. Yields dicts with {number, headRefOid, verdicts}
    where ``verdicts`` is a list of parsed verdict dicts.

    For each PR that has at least one verdict marker (in either
    comments or reviews), yields its metadata and ALL parsed verdicts.
    PRs without any verdict marker are silently skipped.

    Args:
        gh_repo: GitHub repo (bogocat/tms, etc.)
        state: PR state filter (merged, closed, all)
        since: ISO date string for --search merged:>=DATE filter
    """
    # Build gh pr list args
    list_args = [
        'pr', 'list', '--repo', gh_repo, '--state', state,
        '--json', 'number,headRefOid,mergedAt', '--limit', '100',
    ]
    if since:
        list_args[4:4] = ['--search', f'merged:>={since}']
    prs = _gh_json(list_args)
    if not isinstance(prs, list):
        return
    for pr in prs:
        number = pr.get('number')
        if not number:
            continue
        # Fetch both comments and reviews
        comments, _ = _pr_comment_bodies(gh_repo, number)
        review_bodies = _fetch_pr_reviews(gh_repo, number)
        all_bodies = comments + review_bodies
        # Parse verdicts from all bodies
        verdicts = []
        for body in all_bodies:
            v = parse_verdict_line(body)
            if v is not None:
                verdicts.append(v)
        if verdicts:
            yield {
                'number': number,
                'headRefOid': pr.get('headRefOid', ''),
                'verdicts': verdicts,
            }


def capture_verdicts(repo_filter=None, backfill=False, since=None):
    """Scan the repo registry and capture verdict markers into
    ``tms_review.reviewer_runs``.

    In normal mode (``backfill=False``), scans open PRs for verdicts
    and captures them. In backfill mode (``backfill=True``), scans
    merged PRs since ``since``.

    Returns a list of result dicts:
    ``{repo, gh_repo, pr, verdicts_found, rows_inserted}``.
    """
    results = []
    state = 'merged' if backfill else 'open'
    for short, gh_repo in _repo_registry(repo_filter=repo_filter):
        for pr_info in _scan_prs_for_verdicts(gh_repo, state=state,
                                               since=since):
            number = pr_info['number']
            total_inserted = 0
            for verdict in pr_info['verdicts']:
                total_inserted += _capture_verdict(short, number, verdict)
            results.append({
                'repo': short,
                'gh_repo': gh_repo,
                'pr': number,
                'verdicts_found': len(pr_info['verdicts']),
                'rows_inserted': total_inserted,
            })
    return results


def _classify_pr(gh_repo, pr_number):
    """Fetch a PR's diff and classify it for specialist signals.

    Returns a set of specialist signal strings (subset of
    ``SPECIALIST_SIGNALS``). Returns empty set on fetch failure
    (fail-open: generalist-only panel).
    """
    diff_text = _fetch_pr_diff(gh_repo, pr_number)
    if not diff_text:
        return set()
    return classify_diff(diff_text)


# ── Dispatch ──────────────────────────────────────────────────────

# Per-run dispatch-failure tracking to break retry loops (#77).
# If a PR fails to dispatch in the current cron run, skip it for the
# rest of the run — the poller will retry on the next 5-min cycle.
_DISPATCH_FAILED_THIS_RUN = set()
# Per-day dispatch-failure counter (keyed by "repo#pr"). After
# MAX_FAILURES_PER_DAY consecutive failures, the poller stops retrying
# until the next calendar day. Prevents the 195-event storm seen on
# 2026-07-14 where 3 PRs were retried 36x/hour for 5 hours.
_DISPATCH_FAILURES_TODAY = {}
_MAX_FAILURES_PER_DAY = 6  # ~30 min of 5-min cycles


def _dispatch_review(repo, pr, specialists=None):
    """Spawn ``tmq review <repo> <pr>`` for a PR needing review.

    Sets ``TMQ_NO_LOG=1`` so tmq does not log its own dispatch event — the
    poller logs the dispatch itself (with ``source='poller'``) so #53 stats
    can split author-self-triggered from poller-triggered reviews.

    When ``specialists`` is provided (non-empty list of specialist signal
    names), sets ``TMQ_SPECIALIST`` in the environment so the review session
    can include specialist reviewer personas (#94).

    Returns ``True`` if tmq launched successfully (exit 0), ``False`` on
    failure (review P0: failed dispatches must not be counted as successful).
    """
    key = f'{repo}#{pr}'

    # Circuit breaker: don't retry a PR that's already failed this run
    # or has exceeded the daily failure budget (#77).
    if key in _DISPATCH_FAILED_THIS_RUN:
        return False
    today = datetime.date.today().isoformat()
    fail_key = f'{today}:{key}'
    if _DISPATCH_FAILURES_TODAY.get(fail_key, 0) >= _MAX_FAILURES_PER_DAY:
        return False

    env = dict(os.environ)
    env['TMQ_NO_LOG'] = '1'
    if specialists:
        env['TMQ_SPECIALIST'] = ','.join(sorted(specialists))
    try:
        r = subprocess.run(
            ['tmq', 'review', repo, str(pr)],
            env=env, timeout=60,
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return True
        # Capture failure reason from stderr for diagnostics (#77)
        fail_reason = 'tmq exit ' + str(r.returncode)
        stderr_tail = (r.stderr or '').strip()
        if stderr_tail:
            # Take last 3 non-empty lines of stderr
            lines = [l for l in stderr_tail.splitlines() if l.strip()]
            fail_reason += ': ' + '; '.join(lines[-3:])
        print(f'WARNING: tmq review {key} failed: {fail_reason}',
              file=sys.stderr)
        _DISPATCH_FAILED_THIS_RUN.add(key)
        _DISPATCH_FAILURES_TODAY[fail_key] = \
            _DISPATCH_FAILURES_TODAY.get(fail_key, 0) + 1
        return False
    except subprocess.TimeoutExpired:
        print(f'WARNING: tmq review {key} timed out after 60s',
              file=sys.stderr)
        _DISPATCH_FAILED_THIS_RUN.add(key)
        _DISPATCH_FAILURES_TODAY[fail_key] = \
            _DISPATCH_FAILURES_TODAY.get(fail_key, 0) + 1
        return False
    except OSError as e:
        print(f'WARNING: tmq review {key} spawn failed: {e}',
              file=sys.stderr)
        _DISPATCH_FAILED_THIS_RUN.add(key)
        _DISPATCH_FAILURES_TODAY[fail_key] = \
            _DISPATCH_FAILURES_TODAY.get(fail_key, 0) + 1
        return False


def _result(repo, gh_repo, pr, status, head=None, specialists=None):
    return {
        'repo': repo, 'gh_repo': gh_repo, 'pr': pr,
        'status': status, 'head': head,
        'specialists': sorted(specialists) if specialists else [],
    }


def scan_repos(dispatch=False, repo_filter=None, max_dispatch=3):
    """Scan the repo registry for PRs needing an independent review.

    For each open, non-draft PR with no verdict at all (V1), no live
    review session, and updated within the last 14 days (tms#91 staleness
    filter): record a candidate. When ``dispatch`` is True, spawn
    ``tmq review`` for it (after an orphan re-check) and log a dispatch
    event (``source='poller'``) to the #53 event log.

    PRs labelled ``keep-warm`` are exempt from the staleness filter.

    ``max_dispatch`` caps dispatches per run so a large backlog (e.g. the
    first run against old PRs) does not spawn an unbounded burst of review
    sessions. Overflow PRs are reported as ``would_dispatch`` and picked up
    on subsequent runs. Default 3 (spreads a 36-PR backlog over ~1h at a
    5-min cadence). ``0`` is a scan-only kill switch.

    Returns a list of result dicts: ``{repo, gh_repo, pr, status, head}``
    where status is one of: ``would_dispatch``, ``dispatched``,
    ``skip_draft``, ``skip_stale``, ``skip_current_pass``, ``skip_fail``,
    ``skip_stale_pass``, ``skip_live_session``, ``skip_closed``,
    ``skip_gh_error``, ``skip_dispatch_failed``, ``skip_verdict``.
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

            # Staleness filter (tms#91): skip PRs with no activity in >14 days
            # unless labelled keep-warm. Checked before verdict/comment fetch
            # so stale PRs don't waste a gh API call for fetching comments.
            if is_pr_stale(pr.get('updatedAt')) and not _has_keep_warm_label(
                pr.get('labels', [])
            ):
                results.append(_result(short, gh_repo, number, 'skip_stale', head))
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

            # Classify diff for specialist signals (#94). Fetch diff for
            # all viable candidates (even if we won't dispatch this run)
            # so scan results always carry specialist composition.
            specialists = _classify_pr(gh_repo, number)

            if not can_dispatch:
                results.append(_result(short, gh_repo, number, 'would_dispatch', head,
                                       specialists=specialists))
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

            if _dispatch_review(short, number, specialists=specialists):
                _log_poller_dispatch(short, number)
                dispatched_count += 1
                results.append(_result(short, gh_repo, number, 'dispatched', head,
                                       specialists=specialists))
            else:
                results.append(_result(short, gh_repo, number, 'skip_dispatch_failed', head,
                                       specialists=specialists))

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
             'skip_stale',
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


def _main_scan_reviews(args):
    """CLI handler for ``scan-reviews`` subcommand."""
    import sys

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


def _format_capture_report(results):
    """Pretty-print the verdict capture results."""
    if not results:
        print("No verdict markers found.")
        return
    total_verdicts = sum(r['verdicts_found'] for r in results)
    total_rows = sum(r['rows_inserted'] for r in results)
    print(f"Captured {total_verdicts} verdict(s) → {total_rows} reviewer_runs row(s):")
    for r in results:
        print(f"  {r['gh_repo']}#{r['pr']}: "
              f"{r['verdicts_found']} verdict(s) → "
              f"{r['rows_inserted']} row(s)")


def _main_capture_verdicts(args):
    """CLI handler for ``capture-verdicts`` subcommand."""
    import sys

    if '--help' in args or '-h' in args:
        print("usage: tms review_poll capture-verdicts "
              "[--backfill] [--since DATE] [--repo SHORT]")
        print()
        print("  Scan PR comments and reviews for <<REVIEW-VERDICT>> markers")
        print("  and capture them into tms_review.reviewer_runs.")
        print()
        print("  --backfill   Scan merged PRs (instead of open)")
        print("  --since DATE  Only PRs merged on/after DATE (ISO format)")
        print("  --repo SHORT  Restrict scan to one registered repo")
        sys.exit(0)

    backfill = '--backfill' in args
    repo_filter = None
    since = None

    i = 1
    while i < len(args):
        if args[i] == '--repo' and i + 1 < len(args):
            repo_filter = args[i + 1]
            i += 2
        elif args[i] == '--since' and i + 1 < len(args):
            since = args[i + 1]
            i += 2
        else:
            i += 1

    results = capture_verdicts(repo_filter=repo_filter, backfill=backfill,
                               since=since)
    _format_capture_report(results)


def main():
    """Entry point for ``python3 -m tms.review_poll``.

      scan-reviews [--dispatch] [--repo <short>] [--max-dispatch <n>]
      capture-verdicts [--backfill] [--since DATE] [--repo SHORT]
    """
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print("usage: python3 -m tms.review_poll "
              "<scan-reviews|capture-verdicts> [...]")
        sys.exit(0)

    subcmd = args[0]
    if subcmd not in ('scan-reviews', 'capture-verdicts'):
        print(f"unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)

    if subcmd == 'scan-reviews':
        _main_scan_reviews(args)
    elif subcmd == 'capture-verdicts':
        _main_capture_verdicts(args)


if __name__ == '__main__':
    main()
