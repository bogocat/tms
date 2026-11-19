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

Fingerprints: ``distillery-silent-success:<signal>:<item_id>``. Only the latest
successful run per ``(item, pass)`` is judged, so a superseded empty run stops
being a current finding — the item is the stable identity, the run is not, and a
re-run that wrote content is the pipeline's own statement that the earlier run
is no longer the state of the world.

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
    """Pure rules: run-ledger rows -> findings, one per (item, pass).

    Only the **latest successful run** per ``(item_id, pass_id)`` is judged. An
    empty payload from a run that a later run superseded is history, not a
    current defect: without this, every sweep re-files findings for items that
    have since been re-run successfully and ``recurrence_count`` climbs for a
    fixed defect. (There is no "resolved" write path yet — #158/#159 — so the
    detector has to be honest about what is still true.)
    """
    latest = {}
    for row in rows:
        item_id, pass_id, started_at, ended_at, artifact = row
        rule = RULES.get(pass_id)
        if rule is None:
            continue
        stamp = str(ended_at or started_at or "")
        key = (item_id, pass_id)
        if key not in latest or stamp >= latest[key]["stamp"]:
            latest[key] = {"stamp": stamp, "ended_at": ended_at,
                           "artifact": artifact, "rule": rule}

    findings = []
    for (item_id, pass_id), entry in sorted(latest.items()):
        rule = entry["rule"]
        payload = _json_payload(entry["artifact"])
        # The fields must be PRESENT and empty. A pass id can be reused by a
        # different pipeline version whose payload has other keys entirely
        # (`core-extract` v3 writes scene_summaries/best_lines, not
        # chapter_summaries) — an absent key is not an empty one, and reading it
        # as empty files "wrote nothing" against runs that wrote plenty.
        if not all(field in payload for field in rule.fields):
            continue
        if not all(_is_empty(payload[field]) for field in rule.fields):
            continue
        findings.append(Finding(
            rule.signal, str(item_id),
            f"the most recent successful {pass_id} run for item {item_id} "
            f"(ended {entry['ended_at']}) wrote nothing and {rule.explanation}",
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
