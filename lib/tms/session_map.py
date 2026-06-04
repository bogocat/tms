"""Session → issue mapping for tms.

Extracted from bin/tms:_issue_session_map (PR #7 → issue #8 refactor).
The bash wrapper at bin/tms:_issue_session_map now invokes
`python3 -m tms.session_map <out_path>` and reads the JSON written there.

Public API:
  - parse_issue_branch(branch)         → (type, num) | None
  - parse_issue_title(title)          → (type, repo, num) | None
  - parse_tmq_session_name(name)      → (type, repo, num) | None
  - detect_repo_from_path(path)        → short | ''
  - build_session_map(out_path)        → writes JSON mapping to out_path

The build_session_map function closes the following P0/P1 regressions
from PR #7's review:
  - P1#1: uses os.path.exists (not isdir) for the .git check, so
          worktrees (where .git is a file) aren't silently skipped.
  - P1#2: uses re.search (not re.match) for the /wt-<short>-<num>
          worktree convention, so the path doesn't need to start with
          /wt- to match.
  - P1#3: aoe sessions are linked by TITLE only, not by falling back
          to the worktree branch. A descriptive title like "tms issue
          filing" must NOT auto-link to whatever issue branch is
          checked out at the session's cwd.
"""

import json
import os
import re
import subprocess


# Icons for known agent commands. Mirrors the bash `detect_agent` in bin/tms.
ICONS = {
    'pi': 'π',
    'claude': 'cc',
    'opencode': 'oc',
    'python3': 'py',
    'bash': 'sh',
    'zsh': 'sh',
}


def _run(cmd, timeout=5):
    """Run a subprocess, return stripped stdout. Empty string on error.

    P0#2 fix: narrow exception types. Bare `except: pass` swallowed
    KeyboardInterrupt / SystemExit, breaking Ctrl+C.
    """
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ''


def parse_issue_branch(branch):
    """Parse a (feat|fix|chore|review)/issue-<num>-<slug> branch.

    Returns (type, num) tuple, or None for non-issue branches.
    """
    m = re.match(r'(feat|fix|chore|review)/issue-(\d+)', branch or '')
    if not m:
        return None
    return (m.group(1), m.group(2))


def parse_issue_title(title):
    """Parse an aoe title like "feat-home-portal#5" → (type, repo, num).

    Returns (type, repo, num) tuple, or None for titles that don't match
    the (feat|fix|chore|review)-<repo>#<num> pattern. Descriptive titles
    like "tms issue filing" (no `#<num>`) return None — they MUST NOT
    auto-link to whatever issue branch is checked out in the cwd.
    """
    m = re.match(r'(feat|fix|chore|review)-(.+?)#(\d+)$', title or '')
    if not m:
        return None
    return (m.group(1), m.group(2), m.group(3))


def parse_tmq_session_name(name):
    """Parse a tmq session name like "feat-distillery#245-oc".

    Returns (type, repo, num) tuple, or None for non-tmq names. The
    optional -oc / -cc agent suffix is allowed but not part of the
    returned tuple (the matcher's key is `repo#num`).
    """
    m = re.match(r'(feat|fix|chore|review)-(.+?)#(\d+)(-cc|-oc)?$', name or '')
    if not m:
        return None
    return (m.group(1), m.group(2), m.group(3))


def detect_repo_from_path(path):
    """Resolve a worktree/cwd path to a registered repo shortname.

    Longest-prefix match against `tmq list --machine` rows first, so a
    worktree at `/root/projects/distillery/scripts/foo` matches
    `distillery` (not a shorter registered path). Falls back to the
    `/root/wt-<short>-<num>` worktree convention (P1#2 fix: uses
    `re.search`, not `re.match`, because the path doesn't necessarily
    start with `/wt-`).

    Returns the shortname or '' for no match.
    """
    if not path:
        return ''

    rows = []
    try:
        r = subprocess.run(
            ['tmq', 'list', '--machine'],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            rows = r.stdout.splitlines()
    except (subprocess.TimeoutExpired, OSError):
        rows = []

    best, bestlen = '', 0
    known_shorts = set()
    for row in rows:
        parts = row.split('\t')
        if len(parts) < 2:
            continue
        short, p = parts[0], parts[1]
        if not short:
            continue
        known_shorts.add(short)
        if not p:
            continue
        if path == p or path.startswith(p + '/'):
            if len(p) > bestlen:
                best, bestlen = short, len(p)
    if best:
        return best

    # Worktree convention: /wt-<short>-<num> (P1#2: re.search, not re.match)
    wm = re.search(r'/wt-([a-z0-9-]+)-\d+(/|$)', path)
    if wm and wm.group(1) in known_shorts:
        return wm.group(1)

    return ''


def build_session_map(out_path):
    """Build the session → issue-key mapping and write it as JSON to out_path.

    Iterates tmux sessions and groups them under their issue key
    (repo#num). Three session types are recognized:
      - tmq session names: feat-<repo>#<num>[-oc|-cc] — linked directly
      - aoe session names: aoe_<title>_<uuid> — linked by the aoe title
        (P1#3 fix: NEVER fall back to the worktree branch — descriptive
        titles like "tms issue filing" must not auto-link)
      - scratch session names: c<n>/o<n>/p<n> — linked by the worktree
        branch (parse_issue_branch) + path (detect_repo_from_path)

    All other session names are ignored. Sessions without a resolvable
    cwd/.git are ignored (P1#1 fix: os.path.exists, not isdir).
    """
    # 1. Pull all panes (session, command, cwd) in a single tmux call.
    panes_raw = _run([
        'tmux', 'list-panes', '-a', '-F',
        '#{session_name}|#{pane_current_command}|#{pane_current_path}',
    ])
    panes = []
    for line in panes_raw.split('\n'):
        if '|' in line:
            parts = line.split('|', 2)
            panes.append({'session': parts[0], 'cmd': parts[1], 'cwd': parts[2]})

    # First pane per session wins (a multi-pane session is rare)
    ses_info = {}
    for p in panes:
        if p['session'] not in ses_info:
            ses_info[p['session']] = (p['cmd'], p['cwd'])

    # 2. Pull aoe sessions for the id-prefix → title join.
    aoe_by_id = {}
    try:
        r = subprocess.run(
            ['aoe', 'list', '--json'],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            for s in json.loads(r.stdout):
                sid = s.get('id', '')
                if len(sid) >= 8:
                    aoe_by_id[sid[:8]] = {
                        'title': s.get('title', ''),
                        'path':  s.get('path', ''),
                        'tool':  s.get('tool', ''),
                    }
    except (json.JSONDecodeError, ValueError, subprocess.TimeoutExpired, OSError):
        pass

    # 3. Walk sessions and build the mapping.
    mapping = {}
    for session, (cmd, cwd) in ses_info.items():
        icon = ICONS.get(cmd, cmd[:3] if cmd else '?')

        # tmq sessions: feat|fix|chore|review-<repo>#<num>[-cc|-oc]
        parsed_tmq = parse_tmq_session_name(session)
        if parsed_tmq is not None:
            _type, repo_name, num = parsed_tmq
            key = f'{repo_name}#{num}'
            mapping.setdefault(key, []).append(f'{session} {icon}')
            continue

        # aoe sessions: aoe_<title>_<uuid> — join key is the 8-char id
        # prefix. For aoe sessions, the title is the source of truth.
        # We do NOT fall back to the worktree branch: a session titled
        # "tms issue filing" on /root/tms should not auto-link to
        # whatever feature branch the user happens to have checked out
        # there. (P1#3 fix)
        if session.startswith('aoe_'):
            um = re.match(r'^aoe_.+_([0-9a-f]{6,})$', session)
            if um:
                uuid_prefix = um.group(1)[:8]
                info = aoe_by_id.get(uuid_prefix, {})
                parsed_title = parse_issue_title(info.get('title', ''))
                if parsed_title is not None:
                    _type, repo_name, num = parsed_title
                    mapping.setdefault(
                        f'{repo_name}#{num}', [],
                    ).append(f'{session} {icon}')
            continue

        # scratch: c<n>, o<n>, p<n>
        m = re.match(r'^([cop])(\d+)$', session)
        if not m:
            continue
        # P1#1 fix: os.path.exists, not os.path.isdir. In git worktrees
        # `.git` is a FILE (containing `gitdir: ...`), not a directory.
        if not cwd or not os.path.exists(os.path.join(cwd, '.git')):
            continue
        branch = _run(['git', '-C', cwd, 'branch', '--show-current'])
        parsed_branch = parse_issue_branch(branch)
        if parsed_branch is None:
            continue
        _type, num = parsed_branch
        repo_name = detect_repo_from_path(cwd)
        if not repo_name:
            continue
        key = f'{repo_name}#{num}'
        mapping.setdefault(key, []).append(f'{session} {icon}')

    with open(out_path, 'w') as f:
        json.dump(mapping, f)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: python3 -m tms.session_map <out_path>', file=sys.stderr)
        sys.exit(1)
    build_session_map(sys.argv[1])
