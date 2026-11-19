"""Producer #1 — pipeline-health (tms#157).

Home-portal's ``/admin/pipeline-health`` page computes four silent-failure signal
classes for the V2 book-digest pipeline and then files nothing: someone has to be
looking at the page. This detector reads the same view that page renders
(``bogocat.v_pipeline_health_book``) and files one register row per
``(signal class, book)`` on first sight.

The four classes, and what each one actually proves:

| signal | the view's fact | what it means |
|---|---|---|
| ``missing-distillation`` | ``is_missing_distillation`` | a V2 book has no ``book_distillations`` row — the pipeline never finished, silently |
| ``junction-drift`` | ``is_missing_junctions`` | a distillation exists but a junction table (topics/traditions/quotes) is empty — a pass reported success and wrote nothing |
| ``quote-decay`` | ``near_verbatim_rate < 0.65`` | the quote-verify pass passed, but the quotes no longer resemble the source |
| ``stale-run`` | ``stale_running_count > 0`` | passes still marked ``running`` past their per-pass threshold — the batch believes they are in flight |

Fingerprints: ``pipeline-health:<signal>:<item_id>``. The book id is the stable
key — the same book's junction drift recurring must land on the same row.

Two things this detector deliberately does **not** file:

* the queue-level totals (``v_pipeline_health_queue``) — they are aggregates over
  the per-book rows below, so filing them would double-count the same fact;
* anything about a freshly started pipeline. "Not yet run" is not a silent
  failure, and a row that says otherwise is an assertion the detector cannot
  support.

Repo: ``distillery`` by default. The *detector* is home-portal's health page, but
the defect is in the book-digest pipeline — distillery writes
``book_distillations`` / ``book_topics`` / ``book_traditions`` / ``book_quotes``
and the run ledger. Filing it against home-portal would send the future reader
to the wrong repository. ``--repo`` overrides it.

CLI: ``python3 -m tms.producers.pipeline_health [--repo R] [--dry-run]``
(scheduled by #159; this module only defines the sweep).
"""

import sys

from tms.producers import (Finding, build_parser, fetch, file_findings)

DETECTOR = "pipeline-health"
DEFAULT_REPO = "distillery"

#: The floor home-portal's page uses (`near_verbatim_rate < 0.65` counts as
#: decay, `quotes_count` aside). Kept here as a named constant and mirrored in
#: the docstring so a threshold change is a visible edit, not a magic number.
NEAR_VERBATIM_FLOOR = 0.65

SQL = """
    SELECT item_id, title, is_missing_distillation, is_missing_junctions,
           topics_count, traditions_count, quotes_count,
           near_verbatim_rate, stale_running_count
      FROM bogocat.v_pipeline_health_book
     ORDER BY item_id
"""


def find_findings(rows, near_verbatim_floor=NEAR_VERBATIM_FLOOR):
    """Pure rules: view rows -> findings. No database, no side effects."""
    findings = []
    for (item_id, title, missing_distillation, missing_junctions,
         topics, traditions, quotes, near_verbatim_rate,
         stale_running) in rows:
        key = str(item_id)
        label = f"book {item_id}" + (f" ({title})" if title else "")

        if bool(missing_distillation):
            findings.append(Finding(
                "missing-distillation", key,
                f"{label} has no book_distillations row: the V2 digest pipeline "
                "never produced a distillation for it and reported no failure",
            ))
        if bool(missing_junctions):
            findings.append(Finding(
                "junction-drift", key,
                f"{label} has a distillation but its junctions are incomplete "
                f"(topics={topics}, traditions={traditions}, quotes={quotes}) — "
                "a pass recorded success without writing them",
            ))
        if near_verbatim_rate is not None and \
                float(near_verbatim_rate) < near_verbatim_floor:
            findings.append(Finding(
                "quote-decay", key,
                f"{label} near_verbatim_rate={float(near_verbatim_rate):.2f} < "
                f"{near_verbatim_floor}: quote-verify passed but the quotes no "
                "longer resemble the source",
            ))
        if int(stale_running or 0) > 0:
            findings.append(Finding(
                "stale-run", key,
                f"{label} has {int(stale_running)} run(s) still marked running "
                "past their per-pass threshold",
            ))
    return findings


def collect(near_verbatim_floor=NEAR_VERBATIM_FLOOR):
    return find_findings(fetch(SQL), near_verbatim_floor=near_verbatim_floor)


def sweep(repo=DEFAULT_REPO, dry_run=False, write=print,
          near_verbatim_floor=NEAR_VERBATIM_FLOOR):
    """Read the pipeline health view and file every finding."""
    findings = collect(near_verbatim_floor=near_verbatim_floor)
    return file_findings(DETECTOR, repo, findings, dry_run=dry_run, write=write)


def main(argv=None):
    args = build_parser(
        "python3 -m tms.producers.pipeline_health",
        DETECTOR,
        "File the book-digest pipeline's silent failures into the defect "
        "register.",
        DEFAULT_REPO,
    ).parse_args(argv)
    filed = sweep(repo=args.repo, dry_run=args.dry_run)
    mode = "would file" if args.dry_run else "filed"
    print(f"{DETECTOR}: {mode} {filed} finding(s)"
          + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
