"""Atomic file writes — the Python side of the P0#1 cache-race fix.

The bash heredoc for `_aoe_status_map` and `_tmq_registry` originally
wrote to the cache file in-place, so a concurrent reader could see a
half-written line. The bash fix was: write to `$FILE.new.$$` then
`mv -f` to the real path (atomic rename on the same filesystem).

This module provides the Python equivalent so cache writers in Python
(used by both the session_map and aoe_status modules) get the same
race-free semantics. The pattern is:
  1. Write content to a temp file in the same directory as the target
  2. os.replace(tmp, target) — atomic on POSIX (same filesystem)
"""

import json
import os
import tempfile


def atomic_write_text(path, content):
    """Write content to path atomically.

    Writes to a temp file in the same directory, then os.replace to
    the target. The target either doesn't exist, has the old content,
    or has the new content — never a partial state.
    """
    dirpath = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + '.new.', dir=dirpath
    )
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        # Clean up the tmp on any failure (including KeyboardInterrupt)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, data):
    """Write data as pretty JSON to path atomically."""
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))
