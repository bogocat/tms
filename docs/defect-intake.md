# Defect register — intake contract

`tms_review.escaped_defects` is the fleet's defect register: **one row per
`(repo, fingerprint)`, forever**. A bug that comes back is the same row with a
higher `recurrence_count`, never a second row, so "has this class of defect
recurred?" is a query instead of a memory.

- Schema and derived status: `schema/migrations/008-defect-fingerprint.sql`
  (`tms_review.defects_v`, status ∈ `new | fix-proposed | fixed | verified | regressed`).
- Nullable introducing columns for detector-sourced rows:
  `schema/migrations/009-defect-intake-nullable.sql`.
- Which writer increments `regression_count`, and when:
  `schema/migrations/010-defect-regression-count-semantics.sql` (intake, at
  detection — the re-fix writer must not touch it).
- Intake (this document): `lib/tms/defects.py`.
- Verification against a deployed fix (#158) and the nightly detector sweep
  (#159) are **not** part of intake.

## `record_defect()`

```python
from tms.defects import record_defect

record = record_defect(
    repo="home-portal",
    defect_class="silent-failure",
    fingerprint="pipeline-health:junction-drift:152",
    description="Oathbringer has no book_topics/book_traditions rows",
    detector="pipeline-health",
)
print(record.outcome, record.recurrence_count)
```

| register state for `(repo, fingerprint)` | action | `outcome` |
|---|---|---|
| no row | INSERT, `recurrence_count = 1` | `inserted` |
| row, not verified | bump `recurrence_count` + `last_seen_at` | `recurred` |
| row, verified | set `regressed_at`, bump `recurrence_count` + `regression_count` | `regressed` |
| row, already regressed | bump `recurrence_count` only | `recurred` |
| row with `merged_at` but no `verified_at` | bump `recurrence_count` (`fixed` is not `verified`) | `recurred` |

`record_defect()` returns a `DefectRecord` — `outcome`, `defect_id`, `repo`,
`fingerprint`, `detector`, `recurrence_count`, `regression_count` — **never a bare
id**. "The same defect again" and "a new defect" are different facts, and an id
alone cannot tell them apart.

The write is one atomic statement
(`INSERT … ON CONFLICT (repo, fingerprint) WHERE fingerprint IS NOT NULL DO UPDATE … RETURNING`),
so two producers filing the same fingerprint cannot race a read-then-insert into
a unique violation. The outcome is read off that statement, not inferred from
counters: the insert arm compares the row's `defect_id` with the uuid **this
call** generated.

First sight wins for `description` and `severity`; a recurrence never rewrites
what the row says it found.

## The fingerprint convention

```
<detector>:<signal>:<stable-key>
```

| segment | who chooses it | rule |
|---|---|---|
| `detector` | the producer | slug `[a-z0-9][a-z0-9-]*`, no colons, no spaces. Must equal the `detector` argument. |
| `signal` | the producer | the producer's own signal class, stable across code changes (`junction-drift`, `quote-decay`, `empty-mapping`, ...). |
| `stable-key` | the producer | the identity of the **bug**, not of a run: a book id, an item id, or a bounded group key (a pass/pipeline name). Never a run id or a timestamp — that turns one recurring bug into a new row per occurrence. |

Hand-filed rows use the #156 formula `manual:<sha256(repo || defect_class || description)>`
and pass `detector=None`.

Intake **rejects** a producer-supplied fingerprint that is not prefixed with its
own detector id (`ValueError`, or exit 2 from the CLI). #159 sweeps on
`detector IS NOT NULL`; a row whose identity does not name its detector can never
be re-checked, so accepting one would plant a row that is never swept.

The full rejection list — everything below is exit 2 / `ValueError`, and the CLI's
`--dry-run` applies exactly the same checks as a write, so nothing here can pass a
dry run and fail later:

| rejected | why |
|---|---|
| a fingerprint that does not start with the given `detector` | its identity does not name the check that can re-sweep it |
| a fingerprint with a detector prefix and **no** `--detector` | same row, unsweepable; the fix is to pass `--detector` or hand-file as `manual:<hash>` |
| any non-`manual:` fingerprint with no `--detector` (including two-segment `<signal>:<key>`) | detector-shaped identity without a detector |
| fewer than two `:`-separated segments, or an empty segment | no signal/key to speak of |
| a fingerprint containing whitespace, or longer than 400 characters | not a stable key |
| a `--detector` that is not a slug `[a-z0-9][a-z0-9-]*` | convention; also makes the prefix rule decidable |
| a `--repo` with surrounding whitespace | dedupe is per `(repo, fingerprint)`; a padded repo would fork the register |
| an unknown `--class`, an empty `--description`/`--severity` | the column vocabulary and NOT NULL constraints, named early |
| `discovery_source='detector'` with no detector | a row that claims a detector it does not name |
| a `--discovered-at` that is not ISO 8601 | found in the CLI, not by `TIMESTAMPTZ` after the row's uuid was minted |

## Vocabulary

- `defect_class` — the register's review-shaped classes
  (`sql-syntax | logic | auth | schema | data-loss | perf | convention`) plus
  **`silent-failure`** for detector findings: the pipeline reported OK and did
  nothing. `review_eval._VALID_DEFECT_CLASSES` (the harness's own contract) is
  deliberately unchanged.
- `discovery_source` — `ci | incident | fix-pr | manual | detector | post-merge-review`.
  Defaults to `detector` when a detector is given, `manual` when it is not.
- `severity` — free-form, default **`ungraded`**. A detector that cannot
  distinguish major from minor must not assert one.
- `detector` — nullable on purpose. No re-runnable check means no automatic
  regression detection, so `regressed_at` may never be set (schema `CHECK`).

## CLI

```bash
tms defect --repo home-portal --class silent-failure \
           --fingerprint pipeline-health:junction-drift:152 \
           --description "Oathbringer has no junction rows" \
           --detector pipeline-health
# inserted home-portal/pipeline-health:junction-drift:152 defect=… recurrence=1 regressions=0

tms defect … --json        # machine-readable DefectRecord
tms defect … --dry-run     # validate + print the plan, write nothing
```

Exit status `0` means the sighting was recorded (any of the three outcomes);
`2` means intake rejected it and nothing was written — including a driver or
connection failure, which is reported as `tms defect: <message>` rather than a
traceback (set `TMS_DEBUG=1` to keep the traceback). Exit status does **not**
distinguish "new" from "recurred" — a caller that needs to react reads `outcome`
from `--json`. Both are successful intakes; only one is news.

`--dry-run` prints the same fields a write would use — `repo`, `defect_class`,
`fingerprint`, `description`, `detector`, `discovery_source`, `severity`,
`introducing_pr`, `introducing_commit`, `discovered_at` — and with `--json` the
payload carries `"outcome": "would-file"` instead of one of the three recorded
outcomes. Nothing is written.

Hand-filing without a detector. The hash covers all three inputs in 008's order
(`repo || defect_class || description`) — `printf '%s'` concatenates its
arguments with no separator, so this reproduces the backfill formula exactly:

```bash
repo=tms; class=convention; desc='what I saw'
fp="manual:$(printf '%s' "$repo" "$class" "$desc" | sha256sum | cut -d' ' -f1)"
tms defect --repo "$repo" --class "$class" --fingerprint "$fp" --description "$desc"
```

A hand-filed row and a later detector sighting of the same bug are **two rows**:
the row's identity must name the check that can re-sweep it, and a `manual:`
hash does not. That is a deliberate cost, not an oversight — see the adoption note
in `lib/tms/defects.py`.

## Adding a producer

1. Choose a detector id (slug) and a signal vocabulary; write the fingerprints
   as `<detector>:<signal>:<stable-key>`.
2. Query your own signal, extract the stable key per finding, call
   `record_defect(...)` once per finding with `detector=<id>`.
3. Print each returned `DefectRecord` — the outcome is the operator-facing fact
   ("it was already known" must be visible).
4. Ship a `--dry-run` that lists what would be filed, and run it before the first
   real sweep.
5. **File only what the detector can falsify.** A register row is a claim; an
   empty table that has not run yet is not a defect.

Producers wired by #157 (both read the same postgres state their dashboards read):

| detector id | source of truth | signals |
|---|---|---|
| `pipeline-health` | `bogocat.v_pipeline_health_book` + `bogocat.media_digest_runs` | `missing-distillation`, `insufficient-source-text`, `core-extract-failed`, `distillation-stalled`, `junction-drift`, `quote-decay`, `stale-run` |
| `distillery-silent-success` | `bogocat.media_digest_runs` | `empty-mapping`, `empty-extraction`, `empty-review` |

## Reading the register

```bash
psql service=bogocat_ro \
  -c "SELECT status, count(*) FROM tms_review.defects_v GROUP BY 1" \
  -c "SELECT repo, fingerprint, recurrence_count, regression_count, status
        FROM tms_review.defects_v ORDER BY last_seen_at DESC LIMIT 20"
```

There is no UI for the register yet, and no notification on new defects — the
ledger comes first (see #157's out-of-scope list).
