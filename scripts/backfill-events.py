#!/usr/bin/env python3
"""Backfill historical events from JSONL into tms_review.events (tms#65).

Reads ~/.local/state/tmq/events.jsonl and INSERTs each record into
the postgres events table. Idempotent — the composite UNIQUE index on
(event_type, aoe_id_prefix, event_timestamp) ensures re-runs are safe.

Usage:
    PYTHONPATH=lib python3 scripts/backfill-events.py [path-to-events.jsonl]
"""

import json
import os
import sys

# Ensure lib/ is on the path so we can import tms.events
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from tms.events import append_event

DEFAULT_PATH = os.path.expanduser("~/.local/state/tmq/events.jsonl")


def backfill(jsonl_path):
    """Read JSONL file and INSERT all records into postgres."""
    if not os.path.exists(jsonl_path):
        print(f"Error: file not found: {jsonl_path}", file=sys.stderr)
        sys.exit(1)

    with open(jsonl_path) as f:
        lines = [l.strip() for l in f if l.strip()]

    inserted = 0
    skipped = 0

    for i, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
            append_event(record)
            inserted += 1
        except json.JSONDecodeError as e:
            print(f"Warning: line {i} not valid JSON: {e}", file=sys.stderr)
            skipped += 1
        except Exception as e:
            # Duplicate key violation (ON CONFLICT DO NOTHING) or other DB error
            err_msg = str(e).lower()
            if "unique constraint" in err_msg or "duplicate key" in err_msg:
                skipped += 1
            else:
                print(f"Error at line {i}: {e}", file=sys.stderr)
                skipped += 1

    print(f"Backfill complete: {inserted} inserted, {skipped} skipped "
          f"(of {len(lines)} total records)")

    if skipped > 0:
        print(f"Note: skipped records are likely duplicates from a prior backfill run.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    backfill(path)
