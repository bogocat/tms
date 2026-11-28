"""Defect intake — the front door of the fleet defect register (tms#157).

The register itself is #156: ``tms_review.escaped_defects`` with one row per
``(repo, fingerprint)`` forever, a recurrence counter, regression columns and a
``defects_v`` view whose status is derived rather than written. This module is
the writer that makes it a fed register instead of four rows from July.

Why a separate function from ``review_eval.record_escaped_defect()``
---------------------------------------------------------------
``record_escaped_defect()`` is the *review-eval harness's* writer: it is
reached only through ``backjudge_confirm()``, it always has an introducing PR
(a backjudged defect came out of a diff), and it INSERTs unconditionally — a
human has to decide to file. Intake is the opposite end: a detector that
observes state must be able to call it on every sweep, cheaply, without a human,
and without ever creating a second row for a bug it has already filed. Same
table, different contract, so a different function.

The fingerprint convention
--------------------------
The **producer** supplies the fingerprint; intake never infers one from prose
(prose-inferred identity is what the #156 backfill had to apologise for).

    <detector>:<signal>:<stable-key>      e.g. pipeline-health:junction-drift:152

* ``detector`` — the producer's own id, a slug ``[a-z0-9][a-z0-9-]*``. It must
  match the ``detector`` column: a producer-supplied fingerprint that is not
  prefixed with its own detector id is **rejected**, not accepted and
  forgotten. #159 sweeps on ``detector IS NOT NULL``; a row whose identity does
  not name its detector cannot be re-checked, so accepting one now would plant
  a row that never gets swept.
* ``signal`` — the producer's signal class, stable across code changes
  (``junction-drift``, ``quote-decay``, ...).
* ``stable-key`` — the identity of the *bug*, not of a run. A book id, an item
  id, or a bounded group key. Never a run id or a timestamp: that turns one
  recurring bug into a new row per occurrence, which is exactly what the
  register exists to prevent.

Hand-filed rows use the #156 formula, ``manual:<sha256>``, and pass
``detector=None``. ``discovery_source`` defaults to ``detector`` when a detector
is given and ``manual`` when it is not.

The rule runs in both directions: a detector id **requires** a fingerprint
prefixed with it, and ``detector=None`` **requires** the ``manual:`` prefix. A
two-segment fingerprint like ``stale-run:20128`` filed without a detector is a
detector-shaped identity with no detector — the same unsweepable row as the
three-segment case, so it is rejected too.

Dedupe contract
---------------
======================  ============================================  ==============
register state          action                                        ``outcome``
======================  ============================================  ==============
no row                  INSERT, ``recurrence_count = 1``              ``inserted``
row, not verified       bump ``recurrence_count``, ``last_seen_at``   ``recurred``
row, verified           set ``regressed_at``, bump both counters      ``regressed``
row, already regressed  bump ``recurrence_count`` only                ``recurred``
======================  ============================================  ==============

Two consequences worth stating, because both are "do not overclaim":

* A row that is ``merged_at`` but not ``verified_at`` is *not* verified — the
  detector has never passed on a deployed fix, so there is nothing to regress.
  Those sightings are recurrences.
* A regression is counted once. Still-broken-after-regression is a recurrence,
  not a second ``regression_count`` bump.

The write is a single ``INSERT ... ON CONFLICT (repo, fingerprint) WHERE
fingerprint IS NOT NULL DO UPDATE ... RETURNING`` — one statement, so two
producers filing the same fingerprint concurrently cannot race into a unique
violation, and no branch needs a read-then-write window.

Why ``DefectRecord`` and not a bare id
--------------------------------------
"The same defect again" and "a new defect" are different facts, and returning
only an id collapses them: a caller that gets an id back cannot tell whether it
created the row or merely re-sighted it. ``outcome`` is the fact; the id is
just a handle.

Persistence: one psycopg2 connection per call, via
``review_eval._get_conn()`` (the tms module that owns ``tms_review`` DSN
handling). Tests monkeypatch that connection for a sqlite3 in-memory shim, so
the SQL is kept ANSI-shaped; the two postgres-only pieces (the partial-index
conflict target and ``EXCLUDED``) are exercised against real postgres by
``tests/test_defect_intake_pg.py`` (gated on ``TMS_TEST_PG_SERVICE``), which runs
the four-call contract end to end on a throwaway database.

Adopting a row this call did not create
---------------------------------------
A sighting by a detector of an existing row **already fingerprinted in that
detector's own namespace** whose ``detector`` is NULL fills the detector in
(``COALESCE``). ``discovery_source`` is **not** rewritten: it keeps the
first-sight value, because the register records how the defect was first
discovered, not who re-sighted it.

What this is **not**: a bridge from hand-filed rows to detectors. A hand-filed
row is ``manual:<sha256>``, and the prefix rule above refuses to file that
fingerprint together with a detector id — so a ``manual:<hash>`` row and a later
detector sighting of the same bug **stay two rows**, deliberately. Adoption only
reaches an anomaly row that already carries the detector's fingerprint but no
detector: rows written by an earlier revision of this module (before the prefix
rule) or by SQL outside the two writers. Neither writer can create one today
(``record_escaped_defect()`` writes no fingerprint at all), so on a clean
database this branch does not fire; it is kept as a repair path for rows that
already exist and as defence at the SQL boundary, not as a documented bridge.
Pinned by ``test_detector_adopts_a_detector_namespaced_row_without_a_detector``
and ``test_a_manual_row_and_a_detector_sighting_stay_two_rows``.

CLI: ``tms defect --repo <r> --class <c> --fingerprint <f> --description <d>``
(``main()`` lands with the CLI in #165; the verb routes to this module). The CLI
is the escape hatch that keeps the register complete when a detector does not
exist yet.
"""

import datetime
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass

from tms import review_eval as _review_eval

# ── vocabularies ─────────────────────────────────────────────────

#: Intake outcomes. Printed, logged and asserted on — never collapsed into a
#: boolean "did I insert?".
INSERTED = "inserted"
RECURRED = "recurred"
REGRESSED = "regressed"

OUTCOMES = frozenset({INSERTED, RECURRED, REGRESSED})

#: The defect class detector-sourced rows use. The register's original seven
#: classes (sql-syntax|logic|auth|schema|data-loss|perf|convention) are
#: review-shaped: "a defect a reviewer should have caught". A detector finding
#: is a different species — the pipeline reported OK and did nothing — so the
#: intake vocabulary adds one class instead of mislabelling findings as one of
#: the seven. ``review_eval._VALID_DEFECT_CLASSES`` is left untouched:
#: ``record_escaped_defect()`` keeps its review-shaped contract.
DETECTOR_CLASS = "silent-failure"

#: Classes intake accepts = the register's existing vocabulary + the detector
#: class. Derived from the harness's set so the two cannot drift apart.
DEFECT_CLASSES = frozenset(
    set(getattr(_review_eval, "_VALID_DEFECT_CLASSES")) | {DETECTOR_CLASS}
)

#: ``discovery_source`` vocabulary. ``detector`` is #157's addition; the
#: pre-existing values are what the live rows and the harness already use
#: (``post-merge-review`` appears on a live #156 row, which is why it is here
#: even though the issue's prose lists only ci|incident|fix-pr|manual).
DISCOVERY_SOURCES = frozenset({
    "ci", "incident", "fix-pr", "manual", "detector", "post-merge-review",
})

#: Severity a detector assigns when it does not grade severity (it usually
#: cannot). "Ungraded" is a fact; "major" would be a guess.
UNGRADED = "ungraded"

_DETECTOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_MIN_FINGERPRINT_SEGMENTS = 2
_MAX_FINGERPRINT_LEN = 400


# ── the record ───────────────────────────────────────────────────


@dataclass(frozen=True)
class DefectRecord:
    """What intake did, as a fact rather than an id.

    Frozen and tiny on purpose: callers print it, log it, or branch on
    ``outcome``. ``as_dict()`` is what the CLI's ``--json`` emits.
    """

    outcome: str
    defect_id: str
    repo: str
    fingerprint: str
    detector: object  # str | None
    recurrence_count: int
    regression_count: int

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, "
                             f"got {self.outcome!r}")
        # psycopg2 hands back a UUID, sqlite a str, postgres an int for the
        # counters — normalise at the boundary so callers never branch on type.
        object.__setattr__(self, "defect_id", str(self.defect_id))
        object.__setattr__(self, "recurrence_count",
                           int(self.recurrence_count))
        object.__setattr__(self, "regression_count",
                           int(self.regression_count))

    def as_dict(self):
        return {
            "outcome": self.outcome,
            "defect_id": self.defect_id,
            "repo": self.repo,
            "fingerprint": self.fingerprint,
            "detector": self.detector,
            "recurrence_count": self.recurrence_count,
            "regression_count": self.regression_count,
        }

    def __str__(self):
        return (
            f"{self.outcome} {self.repo}/{self.fingerprint} "
            f"defect={self.defect_id} detector={self.detector or '-'} "
            f"recurrence={self.recurrence_count} "
            f"regressions={self.regression_count}"
        )


# ── validation ───────────────────────────────────────────────────


def validate_fingerprint(fingerprint, detector=None):
    """Enforce the fingerprint convention. Raises ``ValueError``.

    Called by every writer path, so a producer cannot file a row that #159
    will be unable to sweep.
    """
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("fingerprint must be a non-empty string, "
                         f"got {fingerprint!r}")
    if any(ch.isspace() for ch in fingerprint):
        raise ValueError(
            f"fingerprint may not contain whitespace: {fingerprint!r}")
    if len(fingerprint) > _MAX_FINGERPRINT_LEN:
        raise ValueError(
            f"fingerprint longer than {_MAX_FINGERPRINT_LEN} characters: "
            f"{fingerprint!r}")

    segments = fingerprint.split(":")
    if len(segments) < _MIN_FINGERPRINT_SEGMENTS:
        raise ValueError(
            "fingerprint needs at least 2 ':'-separated segments — "
            "'<detector>:<signal>:<stable-key>' for a detector, or "
            f"'manual:<hash>' for a hand-filed row: got {fingerprint!r}")
    if any(not segment for segment in segments):
        raise ValueError(
            f"fingerprint has an empty segment: {fingerprint!r}")

    if detector is None:
        if not fingerprint.startswith("manual:"):
            raise ValueError(
                f"fingerprint {fingerprint!r} was filed without a detector, "
                "but it is not in the hand-filed form 'manual:<sha256>'. Any "
                "other prefix is a detector-shaped identity, and a row that "
                "names a detector it does not have cannot be swept later "
                "(#159 selects on detector IS NOT NULL) — pass the detector "
                "id instead, or hand-file as 'manual:<sha256>'.")
        return

    if not _DETECTOR_RE.match(detector):
        raise ValueError(
            "detector must be a slug matching [a-z0-9][a-z0-9-]* "
            f"(no colons, no spaces): got {detector!r}")
    if not fingerprint.startswith(detector + ":"):
        raise ValueError(
            f"fingerprint {fingerprint!r} is not prefixed with its detector id "
            f"({detector + ':'!r}). A producer owns its fingerprint and it must "
            "start with the detector that produced it — a row whose identity "
            "does not name its detector cannot be swept later (#159 selects on "
            "detector IS NOT NULL).")
    if len(segments) < 3:
        raise ValueError(
            "a detector fingerprint needs a stable key after the signal, "
            f"'<detector>:<signal>:<stable-key>': got {fingerprint!r}")


def _resolve_discovery_source(detector, discovery_source):
    """Default the source from the detector, or validate an explicit one."""
    if discovery_source is None:
        return "detector" if detector else "manual"
    if discovery_source not in DISCOVERY_SOURCES:
        raise ValueError(
            f"discovery_source must be one of {sorted(DISCOVERY_SOURCES)}, "
            f"got {discovery_source!r}")
    if discovery_source == "detector" and not detector:
        raise ValueError(
            "discovery_source='detector' requires a detector id — a row that "
            "claims a detector, but names none, makes an assertion the data "
            "cannot support")
    return discovery_source


# ── time helpers (str on sqlite, datetime on postgres) ───────────


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(value):
    """Timestamps are strings on sqlite and datetimes on postgres — accept
    both on the way out and normalise to ISO 8601."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return str(value)


# ── intake ───────────────────────────────────────────────────────

_UPSERT_SQL = """
    INSERT INTO tms_review.escaped_defects (
        defect_id, repo, introducing_pr, introducing_commit, defect_class,
        severity, discovered_at, discovery_source, description,
        fingerprint, detector, last_seen_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (repo, fingerprint) WHERE fingerprint IS NOT NULL
    DO UPDATE SET
        recurrence_count = tms_review.escaped_defects.recurrence_count + 1,
        last_seen_at = EXCLUDED.last_seen_at,
        -- Defensive: a row already in the detector's namespace with no detector
        -- is adopted. Unreachable through this module's own validation (a
        -- detector fingerprint always arrives with its detector, and a
        -- manual row never does), so on a clean database this is a no-op — it
        -- only repairs rows written before the prefix rule existed, or by SQL
        -- outside the two writers.
        detector = COALESCE(tms_review.escaped_defects.detector,
                            EXCLUDED.detector),
        regressed_at = CASE
            WHEN tms_review.escaped_defects.verified_at IS NOT NULL
             AND tms_review.escaped_defects.regressed_at IS NULL
            THEN EXCLUDED.last_seen_at
            ELSE tms_review.escaped_defects.regressed_at
        END,
        regression_count = tms_review.escaped_defects.regression_count + CASE
            WHEN tms_review.escaped_defects.verified_at IS NOT NULL
             AND tms_review.escaped_defects.regressed_at IS NULL
            THEN 1 ELSE 0
        END
    RETURNING defect_id, recurrence_count, regression_count, detector,
              (defect_id = %s) AS inserted_now,
              (regressed_at = %s) AS regressed_now
"""


def _classify(row, new_defect_id):
    """Which of the three things happened, stated, not inferred.

    Two facts about *this call* rather than arithmetic over counters that other
    calls may have moved:

    * the row's ``defect_id`` is the uuid this call generated, so this call is
      what created the row. This arm is exact: a legacy row that happens to
      carry ``recurrence_count = 1`` (all four July rows do) cannot be mistaken
      for a fresh insert, because its id is not ours.
    * ``regressed_now`` is computed in the statement as
      ``regressed_at = <this call's sighting stamp>``. It is a fact about the
      *stamp*, not a proof of authorship: the ``DO UPDATE`` writes exactly that
      stamp and never rewrites an existing ``regressed_at``, so a true value
      means the row holds the stamp this call wrote — but two sightings in the
      same microsecond would be indistinguishable by it. That window is
      accepted rather than papered over; the alternative (a per-call marker
      column) is a schema change for a case the register does not care about.

    ``recurrence_count`` is deliberately NOT used: it is a fact about the row's
    history, not about what this call did.
    """
    (defect_id, recurrence_count, regression_count, detector, inserted_now,
     regressed_now) = row
    if str(defect_id) == str(new_defect_id):
        outcome = INSERTED
    elif bool(regressed_now):
        outcome = REGRESSED
    else:
        outcome = RECURRED
    return (outcome, defect_id, int(recurrence_count), int(regression_count),
            detector)


def _validate_intake(repo, defect_class, fingerprint, description, detector,
                     discovery_source, severity=UNGRADED, discovered_at=None):
    """Everything intake refuses to write must be refused here first.

    Shared by :func:`record_defect` and the CLI's ``--dry-run``, so a dry run is
    a real validation and not a cheaper approximation of one. The note that
    matters for a dry run is ``discovered_at``: it is the one argument the
    database, not this module, would otherwise be the first to judge
    (``TIMESTAMPTZ`` input syntax), which turned a dry-run pass into a
    write-time traceback rather than the documented exit 2.
    """
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError(f"repo must be a non-empty string, got {repo!r}")
    if repo != repo.strip():
        raise ValueError(f"repo may not have surrounding whitespace, "
                         f"got {repo!r}")
    if defect_class not in DEFECT_CLASSES:
        raise ValueError(
            f"defect_class must be one of {sorted(DEFECT_CLASSES)}, "
            f"got {defect_class!r}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"description must be a non-empty string, got {description!r}")
    if not isinstance(severity, str) or not severity.strip():
        # severity is NOT NULL in postgres, so a None/'' reaching the insert
        # surfaces as a NotNullViolation from the driver — the wrong layer, and
        # after the row's uuid has been minted. Reject it here with the argument
        # named, exactly like description.
        raise ValueError(
            "severity must be a non-empty string (use 'ungraded' when a "
            f"detector cannot grade severity), got {severity!r}")
    validate_fingerprint(fingerprint, detector)
    _resolve_discovery_source(detector, discovery_source)
    _validate_discovered_at(discovered_at)


def _validate_discovered_at(value):
    """ISO 8601 or nothing. Postgres is the wrong layer to find this out.

    Accepts what ``psycopg2`` accepts for a ``TIMESTAMPTZ`` in practice: an ISO
    8601 string (``2026-11-19T00:00:00+00:00``, with a space separator, a
    ``Z`` suffix, or a naive local-ish value). A ``datetime`` is passed through
    untouched; it is already the right type.
    """
    if value is None or isinstance(value, datetime.datetime):
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"discovered_at must be an ISO 8601 timestamp, got {value!r}")
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"discovered_at must be an ISO 8601 timestamp "
            f"(e.g. 2026-11-19T00:00:00+00:00), got {value!r}") from None


def record_defect(repo, defect_class, fingerprint, description, *,
                  detector=None, severity=UNGRADED, discovery_source=None,
                  introducing_pr=None, introducing_commit=None,
                  discovered_at=None):
    """File a defect sighting into the register. Idempotent per fingerprint.

    Args:
        repo: fleet short name the defect lives in (``home-portal``, ``tms``).
            Surrounding whitespace is rejected, not trimmed: dedupe is per
            ``(repo, fingerprint)``, so a padded repo would fork the register.
        defect_class: one of :data:`DEFECT_CLASSES`.
        fingerprint: producer-owned identity — see the module docstring.
            **Rejected** (``ValueError``) when ``detector`` is set and the
            fingerprint is not prefixed with it.
        description: human-readable description of the finding. First sight
            wins; a recurrence never rewrites it (the row records what was
            found, and when).
        detector: the re-runnable check that observed this defect. ``None``
            means no automatic re-check exists, which also means a later
            sighting can never be a regression. When ``None``, the fingerprint
            must be ``manual:<sha256>``.
        severity: free-form. Defaults to ``ungraded`` — a detector that does not
            grade severity must not claim one.
        discovery_source: ``detector`` (default when ``detector`` is set),
            ``manual`` (default otherwise), or one of
            :data:`DISCOVERY_SOURCES`.
        introducing_pr / introducing_commit: only when actually known. Both are
            nullable since migration 009 precisely because a detector-sourced
            defect has no introducing change; pass ``None`` rather than 0/''.
        discovered_at: ISO 8601 sighting time. Defaults to now. The caller's
            value becomes the row's ``discovered_at``; ``last_seen_at`` and a
            ``regressed_at`` this call sets are stamped with intake time (wall
            clock), so a replayed historical sighting records "observed now"
            for those two columns.

    Returns:
        :class:`DefectRecord` — ``outcome`` is ``inserted``, ``recurred`` or
        ``regressed``. Never a bare id: "it took ownership" must not read as
        "recorded fresh". Its ``detector`` is the **row's** detector as read
        back, not the argument, so a record cannot understate ownership.
    """
    _validate_intake(repo, defect_class, fingerprint, description, detector,
                     discovery_source, severity, discovered_at)
    source = _resolve_discovery_source(detector, discovery_source)

    sighting = _iso(discovered_at or _now())
    last_seen = _iso(_now())
    new_defect_id = str(uuid.uuid4())

    with _review_eval._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, (
                new_defect_id, repo, introducing_pr, introducing_commit,
                defect_class, severity, sighting, source, description,
                fingerprint, detector, last_seen,
                # RETURNING predicates: this call's own uuid, this call's
                # sighting stamp. Positional, so they follow the VALUES.
                new_defect_id, last_seen,
            ))
            row = cur.fetchone()
        conn.commit()

    if row is None:
        raise RuntimeError(
            "defect intake returned no row — the upsert neither inserted nor "
            f"updated ({repo}/{fingerprint})")

    outcome, defect_id, recurrence_count, regression_count, row_detector = \
        _classify(row, new_defect_id)

    return DefectRecord(
        outcome=outcome,
        defect_id=defect_id,
        repo=repo,
        fingerprint=fingerprint,
        detector=row_detector,
        recurrence_count=recurrence_count,
        regression_count=regression_count,
    )


# ── CLI ──────────────────────────────────────────────────────────


def _build_parser():
    """``tms defect`` — file one defect with full intake semantics.

    The escape hatch that keeps the register complete when a detector does not
    exist yet: same validation, same dedupe, same outcome report as a producer
    calling :func:`record_defect` directly.

    argparse rather than the hand-rolled parsers used elsewhere in ``lib/tms``:
    eleven flags with two required-word options and a boolean, where a typo'd
    flag must fail loudly instead of being silently ignored.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="tms defect",
        description="File a defect into tms_review.escaped_defects. "
                    "Idempotent per (repo, fingerprint): a second sighting "
                    "bumps the row's recurrence count, it never inserts.",
        epilog="Fingerprint convention: <detector>:<signal>:<stable-key>, e.g. "
               "pipeline-health:junction-drift:152. Hand-filed rows use "
               "manual:<sha256> and omit --detector. Exit status is 0 for all "
               "three outcomes (inserted/recurred/regressed) — 'recurred' is "
               "a successful intake, not a failure.",
    )
    parser.add_argument("--repo", required=True,
                        help="fleet short name (home-portal, distillery, tms, ...)")
    parser.add_argument("--class", dest="defect_class", required=True,
                        help="one of: " + ", ".join(sorted(DEFECT_CLASSES)))
    parser.add_argument("--fingerprint", required=True,
                        help="stable identity of the bug '<detector>:<signal>:<key>'")
    parser.add_argument("--description", required=True,
                        help="what was observed (first sight is kept forever)")
    parser.add_argument("--detector", default=None,
                        help="id of the re-runnable check that saw this; "
                             "omit when no automatic re-check exists")
    parser.add_argument("--severity", default=UNGRADED,
                        help=f"free-form severity (default: {UNGRADED})")
    parser.add_argument("--source", dest="discovery_source", default=None,
                        help="discovery_source (default: detector if "
                             "--detector given, else manual); one of: "
                             + ", ".join(sorted(DISCOVERY_SOURCES)))
    parser.add_argument("--introducing-pr", type=int, default=None,
                        help="PR that introduced the defect, when known")
    parser.add_argument("--introducing-commit", default=None,
                        help="commit that introduced the defect, when known")
    parser.add_argument("--discovered-at", default=None,
                        help="ISO 8601 sighting time (default: now)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and print what would be filed; write nothing")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    return parser


def main(argv=None):
    """Entry point for ``tms defect``. Returns a process exit status.

    0 = the sighting was recorded (any of inserted/recurred/regressed).
    2 = intake rejected it (bad fingerprint, unknown class/source, ...); nothing
        was written.

    A caller that needs to *react* to a new defect should read ``outcome`` from
    ``--json`` — exit status deliberately does not conflate "recorded" with
    "this bug is new", because both are success and only one is news.

    Anything that escapes as an exception (driver down, connection refused) is
    reported as ``tms defect: <message>`` with status 2 rather than a traceback
    and status 1: a script that parses stderr should not have to tell a
    ``ValueError`` from a ``psycopg2.OperationalError``. Set ``TMS_DEBUG=1`` to
    keep the traceback.
    """
    args = _build_parser().parse_args(argv)

    try:
        if args.dry_run:
            _validate_intake(
                args.repo, args.defect_class, args.fingerprint,
                args.description, args.detector, args.discovery_source,
                args.severity, args.discovered_at)
            source = _resolve_discovery_source(args.detector,
                                               args.discovery_source)
            plan = {
                "outcome": "would-file",
                "repo": args.repo,
                "defect_class": args.defect_class,
                "fingerprint": args.fingerprint,
                "description": args.description,
                "detector": args.detector,
                "discovery_source": source,
                "severity": args.severity,
                "introducing_pr": args.introducing_pr,
                "introducing_commit": args.introducing_commit,
                "discovered_at": args.discovered_at,
            }
            print(json.dumps(plan) if args.json else
                  f"would file {args.repo}/{args.fingerprint} "
                  f"class={args.defect_class} detector={args.detector or '-'} "
                  f"source={source} severity={args.severity} "
                  f"discovered_at={args.discovered_at or 'now'} "
                  f"description={args.description!r} "
                  "(dry run — nothing written)")
            return 0

        record = record_defect(
            repo=args.repo,
            defect_class=args.defect_class,
            fingerprint=args.fingerprint,
            description=args.description,
            detector=args.detector,
            severity=args.severity,
            discovery_source=args.discovery_source,
            introducing_pr=args.introducing_pr,
            introducing_commit=args.introducing_commit,
            discovered_at=args.discovered_at,
        )
    except Exception as exc:  # noqa: BLE001 - the CLI's edge, by design
        if os.environ.get("TMS_DEBUG"):
            raise
        print(f"tms defect: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(record.as_dict()) if args.json else str(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
