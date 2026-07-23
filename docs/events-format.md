# Event Log Format — Fleet Dispatch Metrics

Canonical schema for the fleet dispatch event log, stored in
`tms_review.events` (postgres) as of tms#65. Previously `~/.local/state/tmq/events.jsonl`
(JSONL) until 2026-07-12.

Designed for forward-compatible extension (tms#56 staleness watchdog,
tower-fleet#193 downstream consumers).

## Storage location

```
tms_review.events (postgres, schema: tms_review)
```

Each record is one row. The `payload` column stores the canonical
full JSON record; flat columns (`repo`, `issue`, `agent`, etc.) are
denormalized query indices.

See `schema/migrations/002-create-events-table.sql` for the full DDL.

## Concurrency safety

Postgres handles concurrent INSERTs natively via MVCC. Multiple writers
(tmq dispatch, cron-driven transition detection) can write simultaneously
without lost updates or torn records. The composite UNIQUE index on
`(event_type, aoe_id_prefix, event_timestamp)` prevents duplicates from
re-entrant backfill runs.

The `last_status.json` cache at `/tmp/tmq-last-status-cache.json`
uses atomic replacement (tmp+`os.replace`) via `lib/tms/atomic.py`.
These are two different files with two different concurrency contracts.

## Event types

Every record carries a top-level `event_type` discriminator so consumers
can safely skip unknown types without a schema migration.

| `event_type`        | Writer        | When                                   |
|---------------------|---------------|----------------------------------------|
| `dispatch`          | `bin/tmq`     | Agent spawned successfully             |
| `dispatch`          | `tms events scan-reviews --dispatch` | Poller-triggered review (source=poller, tms#57) |
| `dispatch_failed`   | `bin/tmq`     | Agent spawn failed (cc-root-refusal, aoe error) |
| `transition`        | `tms events transitions` | AGENT-STATE marker change detected |

Reserved for future extension: `staleness` (tms#56).

Dispatch events are distinguishable by the `source` field in the payload
(tms#57): `author` (default, an agent self-triggering), `poller` (the
independent review poller, `tms events scan-reviews --dispatch`), or
`manual` (operator running `tmq review` from a terminal).

## Field specifications

### `dispatch`

| Field            | Type    | Required | Description |
|------------------|---------|----------|-------------|
| `event_type`     | string  | yes      | `"dispatch"` |
| `timestamp`      | string  | yes      | ISO 8601 UTC (`2026-07-10T14:30:00.123456+00:00`) |
| `repo`           | string  | yes      | Fleet shortname (`tms`, `distillery`, `home-portal`, …) |
| `issue`          | integer | yes      | GitHub issue number |
| `agent`          | string  | yes      | Runtime shorthand: `pi`, `cc`, `oc` |
| `provider`       | string  | yes      | Provider name (`minimax`, `deepseek`, `anthropic`, …). Empty if unresolved. |
| `model`          | string  | yes      | Model ID (`MiniMax-M3`, `deepseek-v4-pro`, …). Resolved from `~/.pi/agent/settings.json` default when tmq flags are empty. |
| `dispatch_type`  | string  | yes      | `feature`, `fix`, `chore`, or `review` |
| `source`         | string  | no       | Who triggered the dispatch (tms#57): `author` (default), `poller`, or `manual`. Rides the payload column only — no flat-column schema change. |
| `worktree`       | string  | yes      | Absolute path to working tree |
| `session`        | string  | yes      | Session name (`feat-tms#53`, `fix-distillery#245-cc`, …) |
| `aoe_id_prefix`  | string  | no       | First 8 chars of the aoe session UUID (stable join key). Empty for direct-tmux fallback. |

### `dispatch_failed`

| Field            | Type    | Required | Description |
|------------------|---------|----------|-------------|
| `event_type`     | string  | yes      | `"dispatch_failed"` |
| `timestamp`      | string  | yes      | ISO 8601 UTC |
| `repo`           | string  | yes      | Fleet shortname |
| `issue`          | integer | yes      | GitHub issue number |
| `agent`          | string  | yes      | Runtime shorthand |
| `provider`       | string  | yes      | Provider name |
| `model`          | string  | yes      | Model ID |
| `dispatch_type`  | string  | yes      | `feature`, `fix`, `chore`, or `review` |
| `reason`         | string  | yes      | Human-readable failure reason |

### `transition`

| Field            | Type    | Required | Description |
|------------------|---------|----------|-------------|
| `event_type`     | string  | yes      | `"transition"` |
| `timestamp`      | string  | yes      | ISO 8601 UTC — time the transition was detected |
| `session`        | string  | yes      | Aoe session title (`feat-tms#53`) |
| `aoe_id_prefix`  | string  | yes      | First 8 chars of aoe session UUID — primary join key |
| `from_status`    | string  | yes      | Previous AGENT-STATE marker value |
| `to_status`      | string  | yes      | New AGENT-STATE marker value |
| `reason`         | string  | no       | BLOCKED marker reason text (tms#76 PR A) |
| `blocked_class`  | string  | no       | BLOCKED taxonomy class (tms#76 follow-up, migration 005) |

AGENT-STATE values: `PLAN-REVIEW`, `WORKING`, `PR-REVIEW`,
`MERGE-READY`, `BLOCKED`, `DONE`. Additional value: `terminal`
(emitted when a DONE/MERGE-READY session disappears from the aoe list).

BLOCKED states carry the marker's reason text
(`BLOCKED: review not converging`) in the `reason` field, and a
machine-sliceable taxonomy class in `blocked_class` (derived by
`classify_blocked_reason()` at transition time):

| `blocked_class` | Meaning |
|-----------------|---------|
| `mechanical`    | Tool/spawn/environment failure (e.g. aoe start failed, rate limit) |
| `ambiguous-ac`  | Issue unclear — agent stopped for human clarification |
| `capacity`      | Model couldn't handle the task |
| `scope-creep`   | Issue too large for one dispatch |
| `other`         | Anything else (including legacy events with no class) |

Query with `tms events stats --by-blocked-class`, or in SQL via the
`blocked_class` flat column (migration 005).

## Transition detection guarantees and limitations

Transition events are produced by `tms events transitions`, a one-shot
CLI command intended to be run periodically (e.g., every 60s via cron).
This is a secondary observer, NOT the source of truth for dispatch
existence — dispatch events are logged synchronously by tmq at spawn
time. The transition detector polls after the fact and may miss
sub-minute state changes.

**Accepted lossiness:** a session that flips `BLOCKED→WORKING` between
two cron polls will not produce a transition event for that flip.
However, the dispatch-loop states (PLAN-REVIEW, PR-REVIEW, BLOCKED,
MERGE-READY) typically last minutes to hours, so the 60s interval
captures >95% of transitions in practice. The dispatch event itself is
never at risk of being missed — it is written synchronously at spawn.

**Rationale (from plan review, 2026-07-10):** the cron poller is a
secondary observer. Dispatch events are logged synchronously by tmq
itself at spawn time. The poller only needs to capture state changes
that last longer than its interval, which the dispatch-loop states do.
Do not tighten the cron interval to chase sub-minute flips — that would
solve a non-problem and increase aoe API load for no statistical gain.

## Synchronous transition flush (`--session`)

When closing a session, the DONE→terminal transition must be captured
*before* the session is archived. The cron poller (1-min interval)
races the archiver — if the archiver wins, the session is gone before
the poller fires, and the terminal transition is lost.

Use `tms events transitions --session <aoe-title>` to flush a single
session's current FSM state synchronously:

```bash
# Flush DONE→terminal before archiving (close-process step 2→3)
tms events transitions --session feat-tms#98
```

This captures the pane, parses the AGENT-STATE marker, compares
against the last-status cache, and writes a transition row immediately.
Idempotent against cron overlap via the shared state cache
(last_status.json, updated atomically): if the flush writes the
new state before the cron fires, the cron sees the already-transitioned
state and emits no duplicate. The composite UNIQUE index on
`(event_type, aoe_id_prefix, event_timestamp)` provides a second
line of defense when both invocations happen to capture the same
transition at the exact same second.

**Close-process sequence:**
1. Mark the session terminal (agent prints `DONE` or `MERGE-READY`)
2. **Run `tms events transitions --session <name>`** to flush the
   DONE→terminal transition synchronously
3. Archive the session

## Example records

### Dispatch
```jsonl
{"event_type":"dispatch","timestamp":"2026-07-10T14:30:00.123456+00:00","repo":"tms","issue":53,"agent":"pi","provider":"minimax","model":"MiniMax-M3","dispatch_type":"feature","worktree":"/root/wt-tms-53","session":"feat-tms#53","aoe_id_prefix":"abc12345"}
```

### Dispatch failed
```jsonl
{"event_type":"dispatch_failed","timestamp":"2026-07-10T14:31:00.000000+00:00","repo":"tms","issue":54,"agent":"cc","provider":"","model":"","dispatch_type":"feature","reason":"cc dispatch refused under root"}
```

### Transition
```jsonl
{"event_type":"transition","timestamp":"2026-07-10T15:00:00.000000+00:00","session":"feat-tms#53","aoe_id_prefix":"abc12345","from_status":"PLAN-REVIEW","to_status":"WORKING"}
```

### Terminal
```jsonl
{"event_type":"transition","timestamp":"2026-07-10T18:00:00.000000+00:00","session":"","aoe_id_prefix":"abc12345","from_status":"MERGE-READY","to_status":"terminal"}
```

## Class taxonomy (`--by-class`, tms#112)

Per-class stats aggregate dispatch events by `repo:dispatch_type` so
sprint-planning tools (pi-dotfiles#54) can assess fleet reliability
without composing SQL. The class key is `<repo>:<dispatch_type>` —
a short repo name and the dispatch type (`feature`, `fix`, `chore`,
`review`).

### Per-class fields

| Field | Source | Notes |
|-------|--------|-------|
| `dispatches` | `tms_review.events` (event_type=dispatch) | Counted per (repo, dispatch_type) |
| `merged` | `tms_review.dispatch_outcomes` | Joined via aoe_id_prefix |
| `blocked` | `tms_review.events` (transition, to_status=BLOCKED) | Per issue within class |
| `pass_rate` | computed | merged / dispatches |
| `repo_median_rounds` | `tms_review.reviewer_runs` | Per-repo median (issue→PR mapping not directly available; rounds are at repo granularity, not per-class) |
| `blocked_class_distribution` | `tms_review.events.blocked_class` | Taxonomy breakdown per class |
| `median_cost` | `bogocat.llm_call_log` via `meta->>'encoded_cwd'` | Per merged issue; NULL when worktree cannot be resolved or no cost data exists |

### Limitations

- **Review rounds are per-repo, not per-class.** The mapping from
  `events.issue` (GitHub issue number) to `reviewer_runs.pr_number`
  (GitHub PR number) is not directly available — GitHub assigns
  distinct numbers to issues and PRs, even when a PR closes the issue.
  The `dispatch_outcomes` table carries `aoe_id_prefix` and `issue`
  but no `pr_number`. Rounds are computed from all `reviewer_runs`
  rows in the repo and applied to every class within that repo.
- **Cost is best-effort.** The worktree path (`/root/wt-<repo>-<issue>`)
  is transformed to the `encoded_cwd` pattern
  (`--root-wt-<repo>-<issue>--`) and matched against
  `bogocat.llm_call_log.meta->>'encoded_cwd'`. If the worktree path
  is non-standard or the ETL pipeline hasn't loaded the cost rows yet,
  `median_cost` is `null` — never fabricated as zero.

### Example output

```
=== Per-class breakdown (repo:dispatch_type) ===

  Class                         Disp  Merged   Pass%  RepoRnd  Blocked       Cost
  ──────────────────────────── ───── ─────── ─────── ──────── ──────── ──────────
  distillery:feature               5       4     80%      2.0        1     $3.45
  distillery:fix                   2       1     50%      2.0        1    $12.00
  tms:feature                      8       6     75%      2.0        0     $1.80
```

## Consumers

| Consumer | How |
|----------|-----|
| `tms events stats` | Queries `tms_review.events` via `_read_events_from_db()`, computes aggregate metrics |
| `tms events stats --json` | Outputs JSON for piping to downstream tools |
| `tms events stats --by-class` | Per-class breakdown with rounds and cost (tms#112) |
| tms#56 (staleness watchdog) | Queries transition events via postgres |
| tower-fleet#193 (open questions) | Q2/Q7/Q10/Q11 all need this data as baseline |
| pi-dotfiles#54 (sprint-planning) | Consumes `--by-class --json` output |
| `scripts/backfill-events.py` | One-shot migration from legacy JSONL |

## Versioning

The `event_type` discriminator and additive-only schema (new fields are
backward compatible for consumers that ignore unknown keys) serve as
the compatibility contract. Breaking changes require a new event type.

The `payload` column stores the canonical JSON record for forward
compatibility — new event types (e.g. tms#56 \`staleness\`) can add fields
without ALTER TABLE.
