"""aoe status mapping — the Python side of the ST column.

Extracted from bin/tms:_aoe_status_map (PR #7 → issue #8 refactor).
The bash wrapper at bin/tms:_aoe_status_map now invokes
`python3 -m tms.aoe_status <out_path>` and reads the TSV written there.

Output format: title<TAB>status, one per line.
Status values: running | waiting | idle | error | stopped | unknown

The P0#2 fix (narrowed exceptions) lives here: a bare `except: pass`
in the original heredoc silently swallowed KeyboardInterrupt / SystemExit,
making Ctrl+C unresponsive during slow aoe calls. We catch only the
specific exception types that indicate 'aoe is broken or missing' and
let everything else propagate.

FORMAT_TABLE / format_status — the source of truth for the ST column
label and ANSI color. The bash `_format_status` and `_status_color`
case statements in bin/tms must be kept in sync; see emit_bash_format_fn
for a regeneration helper (used by the test, not yet by bin/tms).
"""

import json
import subprocess

from tms.atomic import atomic_write_text


# Status → (4-char label, ANSI color code) per bin/tms:_format_status + _status_color.
# The compact label is padded to 4 chars for column alignment.
FORMAT_TABLE = {
    'running':  ('run ',  '32;1'),   # green bold
    'waiting':  ('wait',  '33;1'),   # yellow bold (actionable)
    'idle':     ('idle',  '2'),      # dim
    'error':    ('err ',  '31;1'),   # red bold
    'stopped':  ('stop',  '31;2'),   # red dim
}


def format_status(name):
    """Return (label, color_code) for a given aoe status string.

    Unknown statuses return ('—   ', '2') (em-dash placeholder, dim).
    Used by the ST column in `tms ls` / `tms issues`.
    """
    if name in FORMAT_TABLE:
        return FORMAT_TABLE[name]
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
        # printf '...' — the label is already padded to 4 chars in the table
        lines.append(f'        {status}) printf \'{label}\'  ;;')
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
        lines.append(f'        {status}) echo \'{color}\' ;;')
    lines += [
        '        *)        echo \'2\' ;;',
        '    esac',
        '}',
    ]
    return '\n'.join(lines) + '\n'


def build_status_map(out_path):
    """Build a title<TAB>status TSV map for all aoe sessions.

    Writes nothing if `aoe` is missing, hung, or returns invalid data.
    Returns silently. The atomic write ensures concurrent readers see
    a complete file, never a torn line.
    """
    # aoe list --json
    try:
        r = subprocess.run(
            ['aoe', 'list', '--json'],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return  # aoe missing or hung
    if r.returncode != 0:
        return

    # P0#2 fix: narrow exception types. Bare `except: pass` swallowed
    # KeyboardInterrupt and SystemExit, breaking Ctrl+C.
    try:
        sessions = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return

    lines = []
    for s in sessions:
        title = s.get('title', '')
        if not title:
            continue
        try:
            sr = subprocess.run(
                ['aoe', 'session', 'show', title, '--json'],
                capture_output=True, text=True, timeout=3,
            )
            if sr.returncode == 0:
                d = json.loads(sr.stdout)
                lines.append(f'{title}\t{d.get("status", "unknown")}')
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            # P0#2: only catch the specific failure modes. KeyboardInterrupt
            # and SystemExit are NOT subclasses of these and propagate.
            pass

    if not lines:
        return

    atomic_write_text(out_path, '\n'.join(lines) + '\n')


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: python3 -m tms.aoe_status <out_path>', file=sys.stderr)
        sys.exit(1)
    build_status_map(sys.argv[1])
