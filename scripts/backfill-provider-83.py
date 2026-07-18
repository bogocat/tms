#!/usr/bin/env python3
"""Backfill empty provider/model on dispatch events (tms#83).

The bogocat-tmq plugin (aoe) wrote dispatch events with empty provider
and model columns Jul 14-16 2026, before the tms#73 resolver was ported
(tms#83 / bogocat-tmq PR). This script repairs the rows.

Strategy:
  1. Sibling join via aoe_id_prefix: one aoe session (5be270af) has a
     Jul 12 row with the correct provider/model — copy it to the empty
     Jul 14 row.
  2. Remaining rows are honestly unrecoverable (no sibling, no model
     in payload, explicit --model flag was not used). They are reported
     but left as-is. NULLs are the truth.

Usage:
    # Dry-run: analyze and print report (read-only, no writes)
    PYTHONPATH=lib python3 scripts/backfill-provider-83.py

    # Apply the fix (writes the 1 recoverable row via service=bogocat)
    PYTHONPATH=lib python3 scripts/backfill-provider-83.py --apply

Verification query (after --apply):
    SELECT created_at::date, count(*)
    FROM tms_review.events
    WHERE event_type='dispatch' AND provider=''
    GROUP BY 1 ORDER BY 1;
    -- Expected: 0 recovered rows from Jul 14 via sibling,
    --           26 unrecoverable rows on Jul 15-16 remain.
"""

from __future__ import annotations

import argparse
import os
import sys


def _dsn(service: str) -> str:
    """Build a libpq DSN for the given pg service."""
    return f"service={service}"


def _connect(service: str):
    """Connect to postgres via the given service name."""
    import psycopg2

    return psycopg2.connect(_dsn(service))


def analyze():
    """Read-only analysis via bogocat_ro.

    Returns (recoverable_rows, unrecoverable_rows) — lists of
    (id, aoe_id_prefix, date, repo, issue) tuples.
    """
    conn = _connect("bogocat_ro")
    try:
        with conn, conn.cursor() as cur:
            # Get all empty-provider dispatch rows
            cur.execute(
                """SELECT id, aoe_id_prefix, created_at, repo, issue
                   FROM tms_review.events
                   WHERE event_type = 'dispatch' AND provider = ''
                   ORDER BY created_at"""
            )
            empty_rows = cur.fetchall()

            if not empty_rows:
                return ([], [])

            prefixes = [row[1] for row in empty_rows]

            # Find siblings with provider/model for these prefixes
            cur.execute(
                """SELECT aoe_id_prefix, provider, model
                   FROM tms_review.events
                   WHERE event_type = 'dispatch'
                     AND aoe_id_prefix = ANY (%s)
                     AND provider != ''
                   ORDER BY created_at""",
                (prefixes,),
            )
            sibling_map: dict[str, tuple[str, str]] = {}
            for prefix, provider, model in cur.fetchall():
                if prefix not in sibling_map:
                    sibling_map[prefix] = (provider, model)

            recoverable = []
            unrecoverable = []
            for row_id, prefix, ts, repo, issue in empty_rows:
                if prefix in sibling_map:
                    recoverable.append(
                        (row_id, prefix, ts, repo, issue, *sibling_map[prefix])
                    )
                else:
                    unrecoverable.append((row_id, prefix, ts, repo, issue))

            return (recoverable, unrecoverable)
    finally:
        conn.close()


def apply(recoverable_rows):
    """Write recovered rows via service=bogocat."""
    if not recoverable_rows:
        print("No recoverable rows to apply.")
        return

    conn = _connect("bogocat")
    try:
        with conn, conn.cursor() as cur:
            for row_id, prefix, ts, repo, issue, provider, model in recoverable_rows:
                cur.execute(
                    """UPDATE tms_review.events
                       SET provider = %s, model = %s
                       WHERE id = %s""",
                    (provider, model, row_id),
                )
                print(f"  Updated {row_id} ({prefix}): "
                      f"provider={provider}, model={model}")
        conn.commit()
        print(f"\nApplied {len(recoverable_rows)} row(s).")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def print_report(recoverable, unrecoverable):
    """Print the analysis report."""
    total = len(recoverable) + len(unrecoverable)
    print(f"=== Provider Backfill Report (tms#83) ===\n")
    print(f"  Total empty-provider dispatch rows: {total}")
    print(f"  Recoverable (sibling join):         {len(recoverable)}")
    print(f"  Unrecoverable (no sibling data):    {len(unrecoverable)}")
    print()

    if recoverable:
        print("  Recoverable rows:")
        for row_id, prefix, ts, repo, issue, provider, model in recoverable:
            print(f"    {row_id}  {prefix}  {ts[:10]}  "
                  f"{repo}#{issue}  →  provider={provider}  model={model}")
        print()

    if unrecoverable:
        print(f"  Unrecoverable rows ({len(unrecoverable)}):")
        by_date: dict[str, int] = {}
        for _, _, ts, _, _ in unrecoverable:
            d = ts[:10]
            by_date[d] = by_date.get(d, 0) + 1
        for d in sorted(by_date):
            print(f"    {d}: {by_date[d]} row(s)")
        print()
        print("  These rows have no sibling with provider/model data and")
        print("  no model in their payload. The bogocat-tmq plugin passed")
        print("  empty provider/model through without resolution. Marking")
        print("  as honestly unrecoverable — NULLs are the truth.")
        print()
        print("  aoe_id_prefixes:")
        for row_id, prefix, ts, repo, issue in unrecoverable:
            print(f"    {prefix}  {ts[:10]}  {repo}#{issue}")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill empty provider/model on dispatch events (tms#83)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write recovered rows via service=bogocat (default: dry-run)",
    )
    args = parser.parse_args()

    print("Analyzing (bogocat_ro)...")
    recoverable, unrecoverable = analyze()
    print_report(recoverable, unrecoverable)

    if args.apply:
        print("---")
        print("Writing via service=bogocat...")
        apply(recoverable)
        print("---")
        print("Verification query:")
        print("  SELECT created_at::date, count(*)")
        print("  FROM tms_review.events")
        print("  WHERE event_type='dispatch' AND provider=''")
        print("  GROUP BY 1 ORDER BY 1;")
    else:
        print("---")
        print("DRY-RUN complete. Re-run with --apply to write changes.")


if __name__ == "__main__":
    main()
