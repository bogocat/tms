"""Producer #2 — distillery silent success (tms#157).

The #913/#914/#915/#944 class: a run that reports **success** while writing
nothing. That class is invisible to an error rate — ``status='success'`` *is* the
signal the dashboard draws — so it needs a detector that reads the payload,
not the status.

This detector reads ``bogocat.media_digest_runs`` and, for the passes that must
write content, flags ``status='success'`` runs whose written payload is empty:

| pass | the payload that must exist | signal |
|---|---|---|
| ``canonical-map`` | ``canonical_topics`` and ``canonical_traditions`` | ``empty-mapping`` |
| ``core-extract`` | ``chapter_summaries`` and ``best_quotes`` | ``empty-extraction`` |
| ``review-synthesis`` | ``review_html`` | ``empty-review`` |

A pass is judged empty only when **every** field it must write is empty: a
canonical-map run that mapped topics but no traditions did write something, and
calling that "wrote nothing" would be an over-claim.

Fingerprints: ``distillery-silent-success:<signal>:<item_id>``. Runs of the same
pass for the same item are grouped into one finding (with the run count in the
description), so a book whose mapping silently failed twice is one row with
``recurrence_count``, not two rows — the item is the stable identity, the run is
not.

Why the window defaults to the whole ledger: an old silent success is still a
silent success, and a bounded window would hide exactly the class of defect this
detector exists to find (the May 2026 empty mappings are the first thing it
finds). ``since_days`` exists for callers that want a cheap nightly sweep over
recent runs only.

CLI:
``python3 -m tms.producers.distillery_silent_success [--repo R] [--since-days N] [--dry-run]``
"""

import datetime
import sys
from dataclasses import dataclass

from tms.producers import (Finding, _json_payload, build_parser, fetch,
                           file_findings, utc_now)

DETECTOR = "distillery-silent-success"
DEFAULT_REPO = "distillery"


@dataclass(frozen=True)
class EmptyRule:
    """What a pass must write, and what to call it when it wrote none of it."""

    signal: str
    fields: tuple
    explanation: str


RULES = {
    "canonical-map": EmptyRule(
        "empty-mapping",
        ("canonical_topics", "canonical_traditions"),
        "canonical_topics and canonical_traditions are both empty — the pass "
        "mapped nothing",
    ),
    "core-extract": EmptyRule(
        "empty-extraction",
        ("chapter_summaries", "best_quotes"),
        "chapter_summaries and best_quotes are both empty — the pass extracted "
        "nothing",
    ),
    "review-synthesis": EmptyRule(
        "empty-review",
        ("review_html",),
        "review_html is blank — the pass produced no review",
    ),
}

SQL = """
    SELECT item_id, pass_id, started_at, ended_at, artifact
      FROM bogocat.media_digest_runs
     WHERE status = 'success'
       AND pass_id IN ({placeholders})
       {window}
     ORDER BY item_id, pass_id, ended_at
"""


def _is_empty(value):
    """Empty means "nothing was written", not "falsy".

    A payload field that is ``0`` or ``false`` is content; only missing, blank or
    zero-length containers count as empty.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def find_findings(rows):
    """Pure rules: run-ledger rows -> findings, grouped per (item, pass)."""
    grouped = {}
    for item_id, pass_id, _started_at, ended_at, artifact in rows:
        rule = RULES.get(pass_id)
        if rule is None:
            continue
        payload = _json_payload(artifact)
        # The fields must be PRESENT and empty. A pass id can be reused by a
        # different pipeline version whose payload has other keys entirely
        # (`core-extract` v3 writes scene_summaries/best_lines, not
        # chapter_summaries) — an absent key is not an empty one, and reading it
        # as empty files "wrote nothing" against runs that wrote plenty.
        if not all(field in payload for field in rule.fields):
            continue
        if not all(_is_empty(payload[field]) for field in rule.fields):
            continue
        entry = grouped.setdefault((item_id, pass_id),
                                   {"runs": 0, "last_ended": None})
        entry["runs"] += 1
        stamp = str(ended_at) if ended_at is not None else None
        if stamp and (entry["last_ended"] is None
                      or stamp > str(entry["last_ended"])):
            entry["last_ended"] = stamp

    findings = []
    for (item_id, pass_id), entry in sorted(grouped.items()):
        rule = RULES[pass_id]
        runs = entry["runs"]
        run_phrase = "1 run" if runs == 1 else f"{runs} runs"
        findings.append(Finding(
            rule.signal, str(item_id),
            f"{pass_id} recorded status='success' in {run_phrase} for item "
            f"{item_id} (last ended {entry['last_ended']}) and {rule.explanation}",
        ))
    return findings


def collect(since_days=0, now=None):
    passes = sorted(RULES)
    placeholders = ", ".join(["%s"] * len(passes))
    params = list(passes)
    window = ""
    if since_days and int(since_days) > 0:
        reference = now or utc_now()
        if isinstance(reference, str):
            reference = datetime.datetime.fromisoformat(reference)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=datetime.timezone.utc)
        cutoff = reference - datetime.timedelta(days=int(since_days))
        # The predicate goes in the WHERE clause. Appending it to the end of the
        # statement would land it after ORDER BY, where sqlite parses it as part
        # of the sort expression (silently unfiltered) and postgres rejects it.
        window = "AND started_at >= %s"
        params.append(cutoff.isoformat())
    return find_findings(fetch(SQL.format(placeholders=placeholders,
                                          window=window), tuple(params)))


def sweep(repo=DEFAULT_REPO, dry_run=False, since_days=0, now=None, write=print):
    """Read the run ledger and file every silent success it finds."""
    findings = collect(since_days=since_days, now=now)
    return file_findings(DETECTOR, repo, findings, dry_run=dry_run, write=write)


def main(argv=None):
    parser = build_parser(
        "python3 -m tms.producers.distillery_silent_success",
        DETECTOR,
        "File distillery runs that reported success while writing nothing into "
        "the defect register.",
        DEFAULT_REPO,
    )
    parser.add_argument("--since-days", type=int, default=0,
                        help="only consider runs started in the last N days "
                             "(default: the whole ledger)")
    args = parser.parse_args(argv)
    filed = sweep(repo=args.repo, dry_run=args.dry_run,
                  since_days=args.since_days)
    mode = "would file" if args.dry_run else "filed"
    print(f"{DETECTOR}: {mode} {filed} finding(s)"
          + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
