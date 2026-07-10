# Event Log Format — Fleet Dispatch Metrics

Canonical schema for the append-only JSONL event log at
`~/.local/state/tmq/events.jsonl`. Consumed by `tms events stats`
and designed for forward-compatible extension (tms#56 staleness
watchdog, tower-fleet#193 downstream consumers).

## File location

```
~/.local/state/tmq/events.jsonl
```

The directory is created on first write. Each record is one JSON
object followed by a newline (JSONL / NDJSON). The file is append-only;
no in-place edits or deletions.

## Concurrency safety

Records are written via `open(path, 'a')` (POSIX O_APPEND). A single
`write()` call of one JSONL line is ≤ 1 KB, well under PIPE_BUF
(~4 KB). POSIX guarantees these appends are atomic across processes
without locking — concurrent tmq dispatches never produce torn lines
or silently dropped events.

The `last_status.json` cache at `/tmp/tmq-last-status-cache.json`
uses atomic replacement (tmp+`os.replace`) via `lib/tms/atomic.py`.
These are two different files with two different concurrency contracts.

## Event types

Every record carries a top-level `event_type` discriminator so consumers
can safely skip unknown types without a schema migration.

| `event_type`        | Writer        | When                                   |
|---------------------|---------------|----------------------------------------|
| `dispatch`          | `bin/tmq`     | Agent spawned successfully             |
| `dispatch_failed`   | `bin/tmq`     | Agent spawn failed (cc-root-refusal, aoe error) |
| `transition`        | `tms events transitions` | AGENT-STATE marker change detected |

Reserved for future extension: `staleness` (tms#56).

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

AGENT-STATE values: `PLAN-REVIEW`, `WORKING`, `PR-REVIEW`,
`MERGE-READY`, `BLOCKED`, `DONE`. Additional value: `terminal`
(emitted when a DONE/MERGE-READY session disappears from the aoe list).

BLOCKED states may carry a reason in the marker text
(`BLOCKED: review not converging`). The `from_status`/`to_status`
fields store only the state name; the reason is not captured in
the event schema (it's ephemeral diagnostic text, not an aggregate
dimension).

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

## Consumers

| Consumer | How |
|----------|-----|
| `tms events stats` | Reads the full log, computes aggregate metrics |
| `tms events stats --json` | Outputs JSON for piping to downstream tools |
| tms#56 (staleness watchdog) | Reads transition events, detects sessions stuck in a non-progressing state |
| tower-fleet#193 (open questions) | Q2/Q7/Q10/Q11 all need this data as baseline |

## Versioning

The event log has no explicit version field. The `event_type`
discriminator and additive-only schema (new fields are backward
compatible for JSONL consumers that ignore unknown keys) serve as
the compatibility contract. Breaking changes would require a new
event type or a new log file path.
