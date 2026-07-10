"""aoe status mapping — the Python side of the ST column.

Extracted from bin/tms:_aoe_status_map (PR #7 → issue #8 refactor).
The bash wrapper at bin/tms:_aoe_status_map now invokes
`python3 -m tms.aoe_status <out_path>` and reads the TSV written there.

Output format: title<TAB>status, one per line.
Status values: running | waiting | idle | error | stopped | unknown | stale:Nm

The P0#2 fix (narrowed exceptions) lives here: a bare `except: pass`
in the original heredoc silently swallowed KeyboardInterrupt / SystemExit,
making Ctrl+C unresponsive during slow aoe calls. We catch only the
specific exception types that indicate 'aoe is broken or missing' and
let everything else propagate.

FORMAT_TABLE / format_status — the source of truth for the ST column
label and ANSI color. The bash `_format_status` and `_status_color`
case statements in bin/tms must be kept in sync; see emit_bash_format_fn
for a regeneration helper (used by the test, not yet by bin/tms).

Staleness watchdog (issue #56):
  - `_STALENESS_STATE_PATH`: persistent state file tracking per-session
    marker changes
  - `_capture_marker(session)`: capture last N lines of tmux pane, grep
    for <<AGENT-STATE:
  - `_compute_status(state, title, aoe_status, marker, ...)`: decide if
    a running session is stale
  - `log_staleness_event(title, minutes, ...)`: structured JSON to stderr
    for the #53 event log when it lands
"""

import json
import os
import re
import subprocess
import sys
import time

from tms.atomic import atomic_write_json, atomic_write_text


# Path for the persistent staleness tracker state file.
_STALENESS_STATE_PATH = '/tmp/tms-aoe-staleness.json'

# Default staleness threshold in minutes. Override via
# TMS_STALE_THRESHOLD_MINUTES env var.
_DEFAULT_STALE_THRESHOLD = 7

# Marker pattern: <<AGENT-STATE: ...>>
_MARKER_RE = re.compile(r'<<AGENT-STATE:\s*(\S[^>]*?)\s*>>')

# State entries not seen in this many seconds are pruned.
_PRUNE_AGE_SECONDS = 24 * 3600  # 24 hours


# Status → (4-char label, ANSI color code) per bin/tms:_format_status + _status_color.
# The compact label is padded to 4 chars for column alignment.
FORMAT_TABLE = {
    'running':  ('run ',  '32;1'),   # green bold
    'waiting':  ('wait',  '33;1'),   # yellow bold (actionable)
    'idle':     ('idle',  '2'),      # dim
    'error':    ('err ',  '31;1'),   # red bold
    'stopped':  ('stop',  '31;2'),   # red dim
    'stale':    ('stal',  '35;1'),   # magenta bold (stale watchdog #56)
}


def format_status(name):
    """Return (label, color_code) for a given aoe status string.

    Unknown statuses return ('—   ', '2') (em-dash placeholder, dim).
    Stale statuses (stale:Nm) are matched by prefix.
    Used by the ST column in `tms ls` / `tms issues`.
    """
    if name in FORMAT_TABLE:
        return FORMAT_TABLE[name]
    # Stale prefix match: stale:7m, stale:12m, etc.
    if isinstance(name, str) and name.startswith('stale:'):
        return FORMAT_TABLE['stale']
    return ('—   ', '2')


def emit_bash_format_fn():
    """Return bash source for `_format_status` and `_status_color`.

    Lets bin/tms (eventually) `eval` the output of this function so the
    bash case statements are generated from the same source of truth as
    the Python table. Out of scope for the initial extraction; the test
    uses this to assert the bash function would round-trip cleanly.
    """
    lines = [
        '_format_status() {',
        '    local s=$1',
        '    case "$s" in',
    ]
    for status, (label, _color) in FORMAT_TABLE.items():
        # The stale entry uses a wildcard prefix so stale:7m, stale:12m,
        # etc. all match the same branch.
        key = 'stale:*)' if status == 'stale' else f'{status})'
        lines.append(f'        {key} printf \'{label}\'  ;;')
    lines += [
        '        *)        printf \'—   \'  ;;',
        '    esac',
        '}',
        '',
        '_status_color() {',
        '    local s=$1',
        '    case "$s" in',
    ]
    for status, (_label, color) in FORMAT_TABLE.items():
        key = 'stale:*)' if status == 'stale' else f'{status})'
        lines.append(f'        {key} echo \'{color}\' ;;')
    lines += [
        '        *)        echo \'2\' ;;',
        '    esac',
        '}',
    ]
    return '\n'.join(lines) + '\n'


def STALE_THRESHOLD_MINUTES():
    """Return the staleness threshold in minutes.

    Reads TMS_STALE_THRESHOLD_MINUTES env var; defaults to 7.
    Must be a callable (not a module-level constant) so tests can
    monkeypatch the env var per test.
    """
    try:
        return int(os.environ.get('TMS_STALE_THRESHOLD_MINUTES',
                                  _DEFAULT_STALE_THRESHOLD))
    except (ValueError, TypeError):
        return _DEFAULT_STALE_THRESHOLD


def _load_staleness_state(path=None):
    """Load the persistent staleness state from path.

    Returns {} if the file doesn't exist or is corrupt JSON.
    """
    if path is None:
        path = _STALENESS_STATE_PATH
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return {}


def _save_staleness_state(path, state):
    """Atomically write the staleness state to path."""
    atomic_write_json(path, state)


def _prune_staleness_state(path=None, now=None):
    """Load state, prune entries not seen in > 24 hours, save, return pruned state."""
    if path is None:
        path = _STALENESS_STATE_PATH
    if now is None:
        now = time.time()
    state = _load_staleness_state(path)
    cutoff = now - _PRUNE_AGE_SECONDS
    pruned = {
        k: v for k, v in state.items()
        if v.get('last_seen_at', 0) >= cutoff
    }
    if len(pruned) != len(state):
        _save_staleness_state(path, pruned)
    return pruned


def _capture_marker(session_name, lines=50):
    """Capture the most recent AGENT-STATE marker from a tmux session's pane.

    Captures the last `lines` lines of the first pane in the session,
    greps for <<AGENT-STATE: ...>>, and returns the **last** (most recent)
    match — `re.search` returns the first match, which would be the oldest
    when multiple markers are in the scrollback window.

    Returns the full marker text (including delimiters) or '' if none
    found or tmux fails.
    """
    try:
        r = subprocess.run(
            ['tmux', 'capture-pane', '-t', session_name, '-p', '-S', f'-{lines}'],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return ''
        # Find the LAST (most recent) marker. re.search returns the first
        # (oldest) which misses transitions within the capture window.
        matches = list(_MARKER_RE.finditer(r.stdout))
        if matches:
            return matches[-1].group(0)  # most recent full match
        return ''
    except (subprocess.TimeoutExpired, OSError):
        return ''


def _compute_status(state, title, aoe_status, marker, threshold_minutes, now):
    """Compute the effective status for a session, incorporating staleness.

    Returns {'status': str, 'state': dict|None}. `state` is the updated
    per-session entry to persist (or None if no tracking needed).

    Only 'running' sessions are checked for staleness. Other statuses
    pass through unchanged and are NOT tracked in the staleness state.
    """
    if aoe_status != 'running':
        return {'status': aoe_status, 'state': None}

    threshold_seconds = threshold_minutes * 60
    prev = state.get(title, {})
    prev_marker = prev.get('last_marker', '')
    prev_marker_at = prev.get('last_marker_at', now)  # new session → now

    if marker and marker != prev_marker:
        # Marker changed → reset the timer.
        entry = {
            'last_marker': marker,
            'last_marker_at': now,
            'last_seen_at': now,
        }
        return {'status': 'running', 'state': entry}

    if not marker and not prev_marker:
        # Never seen a marker for this session — initialize and trust aoe.
        entry = {
            'last_marker': '',
            'last_marker_at': now,
            'last_seen_at': now,
        }
        return {'status': 'running', 'state': entry}

    # Same marker (or no marker when we previously had one).
    # Check if the timer has expired.
    elapsed = now - prev_marker_at
    if elapsed >= threshold_seconds:
        stale_minutes = int(elapsed / 60)
        entry = {
            'last_marker': marker or prev_marker,
            'last_marker_at': prev_marker_at,  # unchanged
            'last_seen_at': now,
        }
        return {'status': f'stale:{stale_minutes}m', 'state': entry}

    # Within threshold — still running.
    entry = {
        'last_marker': marker or prev_marker,
        'last_marker_at': prev_marker_at,
        'last_seen_at': now,
    }
    return {'status': 'running', 'state': entry}


def log_staleness_event(title, stale_minutes, threshold_minutes=None, now=None):
    """Write a structured staleness event to stderr.

    Format: one JSON object per line. Consumed by the #53 event log
    when it lands. For now, written to stderr as a forward-compatible hook.
    """
    if threshold_minutes is None:
        threshold_minutes = STALE_THRESHOLD_MINUTES()
    if now is None:
        now = time.time()
    event = {
        'type': 'staleness',
        'session': title,
        'stale_minutes': stale_minutes,
        'threshold_minutes': threshold_minutes,
        'timestamp': now,
    }
    print(json.dumps(event, sort_keys=True), file=sys.stderr)


def build_status_map(out_path, staleness_state_path=None, threshold_minutes=None):
    """Build a title<TAB>status TSV map for all aoe sessions.

    The cache is ALWAYS written, even when no rows resolve
    (no sessions, all session-shows fail, aoe list errors / times out /
    returns invalid JSON, etc.). The previous code returned without
    writing in these cases, leaving the prior cache file stale. The
    original bash heredoc always wrote (a 0-byte file on failure);
    this matches that. The single-newline empty shape (`'\n'.join([])
    + '\n'` == `'\n'`) is also the trailing-newline terminator the
    normal path emits, so the consumer's `cut`/`grep` parsers see the
    same line shape either way. See issue #21.

    The atomic write ensures concurrent readers see a complete file,
    never a torn line.

    Staleness watchdog (issue #56): for sessions with aoe status
    'running', the pane is checked for its <<AGENT-STATE: ...>> marker.
    If the marker hasn't changed in > threshold_minutes, the status
    emitted in the TSV is 'stale:Nm' instead of 'running'.
    """
    if staleness_state_path is None:
        staleness_state_path = _STALENESS_STATE_PATH
    if threshold_minutes is None:
        threshold_minutes = STALE_THRESHOLD_MINUTES()
    now = time.time()

    # Load and prune state.
    state = _prune_staleness_state(staleness_state_path, now=now)

    sessions = []
    # aoe list --json
    try:
        r = subprocess.run(
            ['aoe', 'list', '--json'],
            capture_output=True, text=True, timeout=3,
        )
        # P0#2 fix: narrow exception types below. Bare `except: pass`
        # would also catch KeyboardInterrupt / SystemExit, breaking Ctrl+C.
        if r.returncode == 0:
            try:
                sessions = json.loads(r.stdout)
            except (json.JSONDecodeError, ValueError):
                pass  # invalid JSON — fall through, write empty
    except (subprocess.TimeoutExpired, OSError):
        pass  # aoe missing or hung — fall through, write empty

    lines = []
    state_dirty = False
    for s in sessions:
        title = s.get('title', '')
        if not title:
            continue
        prev_emitted = -1  # reset per-session
        try:
            sr = subprocess.run(
                ['aoe', 'session', 'show', title, '--json'],
                capture_output=True, text=True, timeout=3,
            )
            if sr.returncode == 0:
                d = json.loads(sr.stdout)
                aoe_status = d.get('status', 'unknown')

                # Staleness check for running sessions.
                if aoe_status == 'running':
                    ses_id = s.get('id', '')
                    # Build the full aoe tmux session name for capture-pane.
                    # aoe sanitizes titles in tmux session names: non-alnum
                    # chars (except -) become _. e.g. feat-tms#56 → feat-tms_56.
                    safe_title = ''.join(
                        c if c.isalnum() or c == '-' else '_' for c in title
                    )
                    tmux_session = f'aoe_{safe_title}_{ses_id[:8]}' if ses_id else ''
                    marker = _capture_marker(tmux_session, lines=50) if tmux_session else ''
                    result = _compute_status(
                        state, title, aoe_status, marker,
                        threshold_minutes, now,
                    )
                    if result['state'] is not None:
                        prev_entry = state.get(title, {})
                        # Carry forward last_emitted_stale_minutes so the
                        # dedup check below sees the previous value.
                        prev_emitted = prev_entry.get('last_emitted_stale_minutes', -1)
                        # Only mark dirty if state actually changed
                        if result['state'] != prev_entry:
                            state[title] = result['state']
                            state_dirty = True
                    effective_status = result['status']

                    # Log staleness events for #53 — only on transition
                    # into stale (not every refresh while stale).
                    if effective_status.startswith('stale:'):
                        stale_minutes = int(effective_status.split(':')[1].rstrip('m'))
                        if stale_minutes != prev_emitted:
                            log_staleness_event(title, stale_minutes,
                                               threshold_minutes=threshold_minutes,
                                               now=now)
                            if title in state:
                                state[title]['last_emitted_stale_minutes'] = stale_minutes
                                state_dirty = True
                else:
                    effective_status = aoe_status

                lines.append(f'{title}\t{effective_status}')
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            # P0#2: only catch the specific failure modes. KeyboardInterrupt
            # and SystemExit are NOT subclasses of these and propagate.
            pass

    # Persist state if changed.
    if state_dirty:
        _save_staleness_state(staleness_state_path, state)

    # Always write. For non-empty `lines`, this produces the normal
    # `title\tstatus\n` rows; for empty `lines` it produces a single
    # newline — matching the original heredoc's truncate-to-empty
    # behavior and replacing any stale cache. Issue #21.
    atomic_write_text(out_path, '\n'.join(lines) + '\n')


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: python3 -m tms.aoe_status <out_path>', file=sys.stderr)
        sys.exit(1)
    build_status_map(sys.argv[1])
