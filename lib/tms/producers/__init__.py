"""The first two defect producers (tms#157).

A producer is a detector that files what it sees into the register on every
sweep, with a stable fingerprint, so "this is still broken" is a `recurrence`
rather than a new row.

Both producers here read **postgres state directly**, not a dashboard:

* ``pipeline-health`` reads ``bogocat.v_pipeline_health_book`` — the same view
  home-portal's ``/admin/pipeline-health`` page renders. The computation of the
  four signal classes lives in that view; the page only ``SELECT *``s it and
  counts. So tms consuming the view is a second consumer of the same detector,
  not a reimplementation of it.
* ``distillery-silent-success`` reads ``bogocat.media_digest_runs`` — the run
  ledger — and looks for the #913/#914/#915/#944 shape: a run that recorded
  ``status='success'`` while its **required content fields** are empty. The
  claim is field-level on purpose: those runs still write other keys (a 700-char
  ``high_level_summary`` beside an empty ``chapter_summaries``), so "wrote
  nothing" would be false about the very rows the detector files.

Both are deliberately *readers*: neither writes to the upstream pipeline, and
neither decides whether a finding is worth fixing. Filing is all they do.

Design note — where the rules live
----------------------------------
Each producer splits into a portable SQL fetch and a *pure Python* rule function
over the fetched rows. That is a deliberate trade: a server-side ``jsonb``
predicate would be cheaper (the distillery fetch returns ~25 MB of artifacts
instead of 3 rows) but it is unexecutable by the test suite, and an untestable
detector is exactly the kind of check that silently rots. 25 MB once a night
against a local database is the cheaper side of that trade. If the ledger grows
enough for it to matter, the upgrade path is a per-pass ``jsonb_typeof`` /
``jsonb_array_length`` predicate in the SQL fetch, with these rules pinned by
the same tests.
"""

import datetime
import json
from dataclasses import dataclass

from tms import review_eval as _review_eval
from tms.defects import DETECTOR_CLASS, UNGRADED, record_defect


@dataclass(frozen=True)
class Finding:
    """One sighting, before it has an identity.

    ``key`` is the *stable* identity of the bug (an item id, a bounded group
    key) — never a run id or a timestamp, or one recurring bug becomes a new
    row per occurrence.
    """

    signal: str
    key: str
    description: str
    severity: str = UNGRADED

    def fingerprint(self, detector):
        return f"{detector}:{self.signal}:{self.key}"


def file_findings(detector, repo, findings, dry_run=False, write=print):
    """File each finding into the register; return how many were filed.

    Every producer goes through here so the three intake outcomes
    (``inserted`` / ``recurred`` / ``regressed``) are always printed: a sweep
    that silently reports nothing must be distinguishable from a sweep where
    every finding was already known. The count covers what was filed — or, in a
    dry run, what would have been — so a summary line cannot print "0 finding(s)"
    underneath the findings it just listed.
    """
    for finding in findings:
        fingerprint = finding.fingerprint(detector)
        if dry_run:
            write(f"would file {repo}/{fingerprint} — {finding.description}")
            continue
        record = record_defect(
            repo=repo,
            defect_class=DETECTOR_CLASS,
            fingerprint=fingerprint,
            description=finding.description,
            detector=detector,
            severity=finding.severity,
        )
        write(str(record))
    return len(findings)


def fetch(sql, params=None):
    """Run one read-only query against the tms database. Returns rows."""
    with _review_eval._get_conn() as conn:
        with conn.cursor() as cur:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            return cur.fetchall()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def build_parser(prog, detector, description, default_repo):
    """Shared CLI shape for a producer: sweep, or say what a sweep would file."""
    import argparse

    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
        epilog="Detector id: " + detector + ". Findings are filed into "
               "tms_review.escaped_defects keyed by '" + detector +
               ":<signal>:<stable-key>', so a repeated sweep bumps the row's "
               "recurrence count instead of inserting. The intake contract "
               "(fingerprint convention, dedupe, outcomes) is documented in "
               "tms/defects.py.",
    )
    parser.add_argument("--repo", default=default_repo,
                        help="fleet short name to file under (default: "
                             + default_repo + ")")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be filed; write nothing")
    return parser


def rows_as_dicts(columns, rows):
    """zip fetched rows into dicts through a declared column tuple.

    Every producer's SQL fetch names its columns in a ``*_COLUMNS`` tuple and
    goes through here, so a future SELECT edit shifts no field silently: the
    pinning test compares the tuple against the SELECT list, and a positional
    unpack (which a reorder would corrupt invisibly) is never used.
    """
    return [dict(zip(columns, row)) for row in rows]


def json_payload(artifact):
    """postgres hands back a dict for jsonb, sqlite a string. Accept both."""
    if artifact is None:
        return {}
    if isinstance(artifact, (str, bytes, bytearray)):
        try:
            return json.loads(artifact)
        except (ValueError, TypeError):
            return {}
    if isinstance(artifact, dict):
        return artifact
    return {}
