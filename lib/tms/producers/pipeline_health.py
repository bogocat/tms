"""Producer #1 — pipeline-health (tms#157).

Home-portal's ``/admin/pipeline-health`` page computes silent-failure signal
classes for the V2 book-digest pipeline and then files nothing: someone has to be
looking at the page. This detector reads the same view that page renders
(``bogocat.v_pipeline_health_book``), plus the run ledger for the reasons the view
does not carry, and files one register row per ``(signal, book)``.

The signals, and the fact each one is allowed to claim
-------------------------------------------------------
Every signal below states something the query can prove. The distinction between
``missing-distillation`` and the three reason signals is the whole point: "no
distillation row" is a fact; **"the pipeline silently failed" is not** — the
ledger records why for every one of the books that lack a distillation today
(``insufficient-source-text`` skips and failed ``core-extract`` runs). Filing the
second claim when only the first is known is the unsupported-assertion pattern
this register exists to stop, committed by the register itself.

===========================  ===============================================  =====================
signal                       the fact the query proves                        fingerprint
===========================  ===============================================  =====================
``missing-distillation``     no distillation row **and** all 6 required       ``pipeline-health:``
                             passes recorded success — distillery#173's       ``missing-distillation:``
                             shape, the pipeline saying OK while writing      ``<item_id>``
                             no distillation
``insufficient-source-text``  no distillation, ``core-extract`` never        ``…:insufficient-``
                             succeeded, and **at least one** run was         ``source-text:<item_id>``
                             skipped with ``insufficient-source-text: no epub
                             body nor OCR sidecar found`` (not "every run":
                             4 of the 14 live rows also had real failures)
``core-extract-failed``       no distillation, ``core-extract`` never         ``…:core-extract-failed:``
                             succeeded, and at least one run recorded         ``<item_id>``
                             ``status='failed'``
``distillation-stalled``      no distillation although ``core-extract``        ``…:distillation-stalled:``
                             succeeded (fewer than 6 passes completed)        ``<item_id>``
``junction-drift``            ``is_missing_junctions``: the junction rows      ``…:junction-drift:<item_id>``
                             behind the distillation are incomplete
``quote-decay``               the most recent successful ``quote-verify`` run  ``…:quote-decay:<item_id>``
                             recorded ``near_verbatim_rate < 0.65``
``stale-run``                 passes still marked ``running`` past their       ``…:stale-run:<item_id>``
                             per-pass threshold
===========================  ===============================================  =====================

The ledger query is scoped to ``pipeline_version = 2`` because the view it is
joined against is (``v2_books`` = items with a v2 run). Without the scope, an
item reprocessed under v3 could be reported ``distillation-stalled`` because its
v3 ``core-extract`` succeeded while its v2 chain never did — the same
unsupported-claim shape, one join over.

Two things this detector deliberately does **not** file:

* a book whose pipeline has not run yet — no runs at all is not a silent failure,
  and a row that says otherwise is an assertion the detector cannot support;
* the queue-level totals (``v_pipeline_health_queue``) — they aggregate the
  per-book rows below, so filing them would double-count the same fact.

Known gap, deliberately unfiled (review of #166)
-----------------------------------------------
One ledger state matches no signal: no distillation, ``passes_succeeded < 6``,
``core-extract`` never succeeded, **no** skip with a reason, **no** ``failed``
run — only ``running``/``partial`` rows. It is not "not yet attempted" (runs
exist), and it is not filed, because the only fact the ledger proves is absence:
a ``running`` run may be in flight right now, and "no failure was recorded yet"
is not "a failure happened" (ADR-003: absence of evidence may not select a
negative claim). Filing it would be the unsupported assertion this module exists
to stop. Zero live rows match it today; if one appears, the honest fix is a
detector that can distinguish "still running past the threshold" (which the view
already reports as ``stale-run``) from "finished without a status worth naming".
Pinned by ``test_an_unexplained_missing_distillation_is_a_known_gap``.

Repo: ``distillery`` by default. The *detector* is home-portal's health page, but
the defect is in the book-digest pipeline — distillery writes
``book_distillations`` / ``book_topics`` / ``book_traditions`` / ``book_quotes``
and the run ledger. Filing it against home-portal would send the future reader to
the wrong repository. ``--repo`` overrides it.

CLI: ``python3 -m tms.producers.pipeline_health [--repo R] [--dry-run]``
(scheduled by #159; this module only defines the sweep).
"""

import sys

from tms.producers import (Finding, build_parser, fetch, file_findings,
                          rows_as_dicts)

DETECTOR = "pipeline-health"
DEFAULT_REPO = "distillery"

#: The floor home-portal's page uses (`near_verbatim_rate < 0.65` counts as
#: decay). Kept here as a named constant so a threshold change is a visible edit.
NEAR_VERBATIM_FLOOR = 0.65

#: The six passes the V2 book pipeline requires. `passes_succeeded == 6` is the
#: only state in which "no distillation" is silent — anything less means the
#: pipeline recorded a failure or a skip, i.e. it reported.
REQUIRED_PASSES = 6

#: Column order of :data:`BOOK_SQL`. The rows are turned into dicts through this
#: tuple so a future SELECT edit fails loudly (see the pinning test) instead of
#: silently shifting every field one position.
BOOK_COLUMNS = (
    "item_id", "title", "passes_succeeded", "is_missing_distillation",
    "is_missing_junctions", "topics_count", "traditions_count", "quotes_count",
    "near_verbatim_rate", "stale_running_count",
)

BOOK_SQL = """
    SELECT item_id, title, passes_succeeded, is_missing_distillation,
           is_missing_junctions, topics_count, traditions_count, quotes_count,
           near_verbatim_rate, stale_running_count
      FROM bogocat.v_pipeline_health_book
     ORDER BY item_id
"""

#: Why a distillation is missing lives in the ledger, not the view. `MAX(CASE …)`
#: keeps this portable (the unit tests run it on sqlite).
LEDGER_COLUMNS = (
    "item_id", "skipped_no_source", "core_extract_failed",
    "core_extract_succeeded",
)

LEDGER_SQL = """
    SELECT item_id,
           MAX(CASE WHEN status = 'skipped'
                     AND error_detail LIKE 'insufficient-source-text%'
                    THEN 1 ELSE 0 END) AS skipped_no_source,
           MAX(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS core_extract_failed,
           MAX(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS core_extract_succeeded
      FROM bogocat.media_digest_runs
     WHERE pass_id = 'core-extract'
       AND pipeline_version = 2
     GROUP BY item_id
"""


def find_findings(book_rows, ledger_rows, near_verbatim_floor=NEAR_VERBATIM_FLOOR):
    """Pure rules: view dicts + ledger dicts -> findings. No side effects."""
    ledger = {row["item_id"]: row for row in ledger_rows}
    findings = []

    for book in book_rows:
        item_id = book["item_id"]
        key = str(item_id)
        title = book.get("title")
        label = f"book {item_id}" + (f" ({title})" if title else "")

        passes = int(book.get("passes_succeeded") or 0)
        missing_distillation = bool(book.get("is_missing_distillation"))
        run = ledger.get(item_id)

        if missing_distillation:
            findings.extend(_missing_distillation_findings(
                item_id, label, passes, run))

        if bool(book.get("is_missing_junctions")):
            findings.append(Finding(
                "junction-drift", key,
                f"{label} has a distillation but its junction rows are "
                f"incomplete (topics={book.get('topics_count')}, "
                f"traditions={book.get('traditions_count')}, "
                f"quotes={book.get('quotes_count')}). The view proves the "
                "junctions are incomplete; it does not say which step dropped "
                "them (the canonical_map projection, #171, owns that).",
            ))

        near_verbatim_rate = book.get("near_verbatim_rate")
        if near_verbatim_rate is not None and \
                float(near_verbatim_rate) < near_verbatim_floor:
            findings.append(Finding(
                "quote-decay", key,
                f"{label}: the most recent successful quote-verify run recorded "
                f"near_verbatim_rate={float(near_verbatim_rate):.2f} < "
                f"{near_verbatim_floor} — the quotes no longer resemble the "
                "source",
            ))

        stale_running = int(book.get("stale_running_count") or 0)
        if stale_running > 0:
            findings.append(Finding(
                "stale-run", key,
                f"{label} has {stale_running} run(s) still marked running past "
                "their per-pass threshold",
            ))

    return findings


def _missing_distillation_findings(item_id, label, passes, run):
    """Split "no distillation" into the facts that explain it — never guess."""
    key = str(item_id)
    findings = []

    if passes >= REQUIRED_PASSES:
        findings.append(Finding(
            "missing-distillation", key,
            f"{label} has no book_distillations row although all "
            f"{REQUIRED_PASSES} required passes recorded success — the pipeline "
            "reported OK and produced no distillation (distillery#173)",
        ))
        return findings

    if run is None:
        # No core-extract runs at all: not yet attempted. Not a silent failure,
        # and the register will not claim one.
        return findings

    never_succeeded = not bool(run.get("core_extract_succeeded"))
    if never_succeeded and bool(run.get("skipped_no_source")):
        findings.append(Finding(
            "insufficient-source-text", key,
            f"{label} has no book_distillations row and {passes} of "
            f"{REQUIRED_PASSES} passes succeeded: no core-extract run ever "
            "succeeded, and at least one was skipped with "
            "'insufficient-source-text: no epub body nor OCR sidecar found', "
            "so no distillation could be produced",
        ))
    if never_succeeded and bool(run.get("core_extract_failed")):
        findings.append(Finding(
            "core-extract-failed", key,
            f"{label} has no book_distillations row and {passes} of "
            f"{REQUIRED_PASSES} passes succeeded: core-extract never succeeded "
            "for it and at least one run recorded status='failed'",
        ))
    if not never_succeeded:
        findings.append(Finding(
            "distillation-stalled", key,
            f"{label} has no book_distillations row although core-extract "
            f"succeeded: {passes} of {REQUIRED_PASSES} passes completed and the "
            "downstream chain never produced a distillation",
        ))
    return findings


def collect(near_verbatim_floor=NEAR_VERBATIM_FLOOR):
    books = rows_as_dicts(BOOK_COLUMNS, fetch(BOOK_SQL))
    ledger = rows_as_dicts(LEDGER_COLUMNS, fetch(LEDGER_SQL))
    return find_findings(books, ledger, near_verbatim_floor=near_verbatim_floor)


def sweep(repo=DEFAULT_REPO, dry_run=False, write=print,
          near_verbatim_floor=NEAR_VERBATIM_FLOOR):
    """Read the pipeline health view + ledger and file every finding."""
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
