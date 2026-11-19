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
conflict target and ``EXCLUDED``) are exercised against real postgres by the
``TMS_TEST_PG_SERVICE`` integration test.

CLI: ``tms defect --repo <r> --class <c> --fingerprint <f> --description <d>``
(see ``main()``). The CLI is the escape hatch that keeps the register complete
when a detector does not exist yet.
"""

import datetime
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
    RETURNING defect_id, recurrence_count, regression_count,
              (defect_id = %s) AS inserted_now,
              (regressed_at = %s) AS regressed_now
"""


def _classify(row, new_defect_id):
    """Which of the three things happened, stated, not inferred.

    Two explicit facts about *this call* rather than arithmetic over counters
    that other calls may have moved:

    * the row's ``defect_id`` is the uuid this call generated, so this call is
      what created the row. A legacy row that happens to carry
      ``recurrence_count = 1`` (all four July rows do) cannot be mistaken for a
      fresh insert: its id is not ours.
    * ``regressed_now`` is computed in the statement as
      ``regressed_at = <this call's sighting stamp>``. The ``DO UPDATE`` writes
      exactly that stamp and never rewrites an existing ``regressed_at``, so a
      true value can only mean this call set it.

    ``recurrence_count == 1`` is deliberately NOT used: it is a fact about the
    row's history, not about what this call did.
    """
    defect_id, recurrence_count, regression_count, inserted_now, regressed_now \
        = row
    if str(defect_id) == str(new_defect_id):
        outcome = INSERTED
    elif bool(regressed_now):
        outcome = REGRESSED
    else:
        outcome = RECURRED
    return outcome, defect_id, int(recurrence_count), int(regression_count)


def record_defect(repo, defect_class, fingerprint, description, *,
                  detector=None, severity=UNGRADED, discovery_source=None,
                  introducing_pr=None, introducing_commit=None,
                  discovered_at=None):
    """File a defect sighting into the register. Idempotent per fingerprint.

    Args:
        repo: fleet short name the defect lives in (``home-portal``, ``tms``).
        defect_class: one of :data:`DEFECT_CLASSES`.
        fingerprint: producer-owned identity — see the module docstring.
            **Rejected** (``ValueError``) when ``detector`` is set and the
            fingerprint is not prefixed with it.
        description: human-readable description of the finding. First sight
            wins; a recurrence never rewrites it (the row records what was
            found, and when).
        detector: the re-runnable check that observed this defect. ``None``
            means no automatic re-check exists, which also means a later
            sighting can never be a regression.
        severity: free-form. Defaults to ``ungraded`` — a detector that does not
            grade severity must not claim one.
        discovery_source: ``detector`` (default when ``detector`` is set),
            ``manual`` (default otherwise), or one of
            :data:`DISCOVERY_SOURCES`.
        introducing_pr / introducing_commit: only when actually known. Both are
            nullable since migration 009 precisely because a detector-sourced
            defect has no introducing change; pass ``None`` rather than 0/''.
        discovered_at: ISO 8601 sighting time. Defaults to now.

    Returns:
        :class:`DefectRecord` — ``outcome`` is ``inserted``, ``recurred`` or
        ``regressed``. Never a bare id: "it took ownership" must not read as
        "recorded fresh".
    """
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError(f"repo must be a non-empty string, got {repo!r}")
    if defect_class not in DEFECT_CLASSES:
        raise ValueError(
            f"defect_class must be one of {sorted(DEFECT_CLASSES)}, "
            f"got {defect_class!r}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"description must be a non-empty string, got {description!r}")
    validate_fingerprint(fingerprint, detector)
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

    outcome, defect_id, recurrence_count, regression_count = _classify(
        row, new_defect_id)

    return DefectRecord(
        outcome=outcome,
        defect_id=defect_id,
        repo=repo,
        fingerprint=fingerprint,
        detector=detector,
        recurrence_count=recurrence_count,
        regression_count=regression_count,
    )


if __name__ == "__main__":  # pragma: no cover - CLI lands in the next commit
    sys.exit("tms defect: CLI wiring not implemented yet")
