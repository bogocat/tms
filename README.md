# tms — tmux session manager + agent workflow

Session management and GitHub issue dispatch for multi-agent coding workflows.

## Quick start

```bash
# Deploy to /usr/local/bin (bin + lib — the Python modules in lib/tms/
# must be installed alongside the scripts)
sudo cp -r bin lib /usr/local/

# Browse sessions
tms

# Browse open issues across repos
tms issues

# Dispatch an agent on an issue
tmq distillery 245
```

## Review Routing (tms#94)

The review poller classifies PR diffs for **specialist reviewer signals**
that determine which specialist personas join the review panel.

### Specialist signals

| Signal | Detection | Specialist persona |
|--------|-----------|-------------------|
| `security` | Diff contains auth/crypto/session/token/password/permission/policy keywords in added lines, or file paths containing `auth`, `secret`, `crypto`, `permission`, `policy` anywhere in the path (e.g. `authenticator.py`, `crypto_helper.py`, `my-secrets.yaml`) | `reviewer-security` |
| `schema` | Diff touches migration/SQL/schema/alembic files, or contains DDL keywords (`CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`, `ADD COLUMN`, `CREATE INDEX`) in added lines | `reviewer-schema` |
| `duplication` | >40% of changed lines are deletions, or duplicated added-line blocks | *(future — persona TBD)* |
| `editorial` | ALL changed files are documentation (`*.md`, `*.rst`, `*.txt`, `docs/`, `README*`, `CHANGELOG*`, `LICENSE*`) | *(fast-path — generalist still runs)* |

### Composition rule

**Specialists ADD to the default panel, never replace it.** The generalist
`reviewer` + `reviewer-m3` pass always runs. Specialist signals add additional
reviewer personas to the panel.

### TMQ_SPECIALIST env contract

When the review poller dispatches `tmq review`, it sets the `TMQ_SPECIALIST`
environment variable to a comma-separated list of signal names (e.g.
`security,schema`). The review session reads this variable to include the
corresponding specialist reviewer personas. No specialists = empty/unset
variable = generalist-only panel.

### Adding a new specialist signal

1. Add the signal name to `SPECIALIST_SIGNALS` in `lib/tms/diff_shape.py`
2. Implement detection logic in `classify_diff()`
3. Add unit tests in `tests/test_diff_shape.py`
4. Create the specialist persona skill in `pi-dotfiles/skills/reviewer-<signal>/SKILL.md`
5. Wire it in the review session prompt (reads `TMQ_SPECIALIST`)
6. Update the routing table above

## Quick start

## Tools

### tms — session manager

```
tms                  fzf picker: browse/attach/kill sessions
tms ls               list all sessions (ISSUE + AGENT + ST columns)
tms stale            find abandoned sessions (>7d idle)
tms clean            kill stale + compact scratch sessions
tms issues [repo]    browse open GitHub issues + dispatch agents
tms new              create a GitHub issue (gh issue create wrapper)
tms import           wrap a legacy c/o/p scratch session as aoe
tms events           fleet dispatch metrics (issue #53)
```

### tms events — fleet dispatch metrics + review poller

```
tms events transitions    poll aoe + tmux panes, emit state-transition events
tms events stats           compute and print aggregate metrics report
tms events stats --json    machine-readable JSON output
tms events stats --since YYYY-MM-DD   filter from date
tms events stats --by-class          per-class (repo:dispatch_type) with rounds & cost (#112)
tms events stats --by-class --json   machine-readable per-class JSON (#112)
tms events scan-reviews    list open PRs lacking reviewer verdicts (#57)
tms events scan-reviews --dispatch    spawn tmq review for never-reviewed PRs
tms events sync-outcomes   resolve dispatch outcomes from GitHub → dispatch_outcomes (#119)
tms events sync-outcomes --since 7 --dry-run    bounded window, print-only
```

Every `tmq` dispatch appends an event record to `tms_review.events`
(postgres). `tms events transitions`
polls running sessions and emits transition events when `<<AGENT-STATE>>`
markers change. `tms events stats` reads the event log and computes:
- Issue→merge latency (p50/p90)
- Review rounds per PR
- BLOCKED frequency vs clean MERGE-READY
- Plan-gate fast-path rate
- Per-model outcome rates
- Per-class breakdown (`--by-class`): dispatches, pass-rate, median rounds,
  blocked-class distribution, median cost per merged PR (tms#112)

See [docs/events-format.md](docs/events-format.md) for the event log schema
and consumer guide.

### tmq — issue → agent dispatcher

```
tmq <repo> <num>                     dispatch pi on issue
tmq <repo> <num> --agent cc          use Claude Code
tmq <repo> <num> --type review       code review
tmq list                             known repos
```

## Architecture

```
tms (browse + manage)
├── session view — attach, kill, rename, clean stale
│   └── column ST: aoe Running/Waiting/Idle for aoe sessions
│       (or derived from pane cmd for raw tmux sessions)
├── issues view — browse GitHub issues, pipeline status, dispatch
├── new       — create GitHub issues (wraps gh issue create)
└── import    — wrap a legacy c/o/p scratch session as an aoe session

tmq (issue → agent spawn)
├── gh issue view → build prompt
├── git worktree add → isolate working tree
└── aoe add → spawn agent (appears in aoe dashboard)

aoe (Agent of Empires — monitoring)
├── session dashboard + web + mobile
└── status: Running / Waiting / Idle / Error / Stopped
```

Three tools, three layers. aoe manages sessions; tmq dispatches work; tms connects them.
The session→issue mapping in tms reads the worktree branch directly (via
`git -C <path> branch --show-current`) and falls back to parsing the aoe
title only when the session is on a main checkout.

## Event log (dispatch metrics)

Fleet-wide dispatch metrics are stored in postgres (migrated from JSONL, tms#65):

```
tms_review.events                 dispatch/transition/failed events (postgres)
/tmp/tmq-last-status-cache.json    transition detector state (ephemeral)
~/.local/state/tmq/events.jsonl.bak.YYYY-MM-DD  legacy JSONL (migrated 2026-07-12)
```

Event types: `dispatch` (tmq spawn), `dispatch_failed` (spawn failure),
`transition` (AGENT-STATE marker change). Full schema and consumer guide
in [docs/events-format.md](docs/events-format.md).

## Defect register

`tms_review.escaped_defects` is the fleet's defect register (revived in
tms#156): **one row per `(repo, fingerprint)` forever**, enforced by a partial
unique index for rows that carry a fingerprint. A bug that comes back bumps
`recurrence_count` — it never inserts a second row. Hand-filed pre-#156 rows
carry a `manual:` fingerprint derived from
`repo || defect_class || description`.

Intake has landed (#157): `record_defect()` in `tms/defects.py` is the upsert
path that enforces the one-row invariant for every row a **producer** or the
`tms defect` CLI files. `record_escaped_defect()` (the review-eval harness) is
unchanged and still writes NULL fingerprints, and those rows remain deliberately
unconstrained — do not assume the one-row invariant covers NULL-fingerprint rows.

`status` is **derived**, never written. It lives in `tms_review.defects_v`:

| status | the fact the row proves |
|---|---|
| `regressed` | `regressed_at` — a detector saw the bug come back after a merged fix |
| `verified` | `verified_at` — a detector passed against the deployed fix |
| `fixed` | `merged_at` — a fix is merged |
| `fix-proposed` | `fix_pr` names a fix PR; the merge is unproven |
| `new` | discovery only |

`regressed` is a *current* state, not a history flag: the writer that verifies a
re-fix clears `regressed_at`, and `regression_count` keeps the history. The
counter is incremented by **intake, at detection** — the sighting of a verified
row that sets `regressed_at` — so one regression is counted once. The re-fix
writer must not touch it (migration 010 replaced the column comment that said the
reverse; #158/#159 own the clearing side). `detector`, `merged_at` and
`regressed_at` are cross-checked by constraints — a row cannot report a
regression it has no detector for, nor a regression/verification of a fix it
never merged.

`detector` is nullable on purpose: a defect with no re-runnable check may not
claim automatic regression detection, and the schema enforces the
consequence (`regressed_at` requires `detector`). The naming convention is live
with intake (#157): a producer supplies `<detector>:<signal>:<stable-key>` and
intake rejects a detector-sourced fingerprint that is not prefixed with its own
detector id. Verification is #158/#159.

`introducing_pr` and `introducing_commit` are nullable too (migration 009), for
the same reason: a defect filed by a detector has **no introducing PR** to name,
and storing `0` or `''` to satisfy a NOT NULL would be exactly the fabricated
fact the `fix_pr`-is-not-`fixed` rule exists to prevent.

```bash
# Apply (documented path) — in order; an operator who stops at 008 leaves the
# register unable to accept a detector-filed row, and one who stops at 009
# leaves the regression counter documented as the re-fix writer's business.
psql service=pg-superuser -f schema/migrations/008-defect-fingerprint.sql
psql service=pg-superuser -f schema/migrations/009-defect-intake-nullable.sql
psql service=pg-superuser -f schema/migrations/010-defect-regression-count-semantics.sql

# What the register currently says
psql service=bogocat_ro \
  -c 'SELECT status, count(*) FROM tms_review.defects_v GROUP BY 1'
```

## Dependencies

- `tmux` — session substrate
- `fzf` — interactive picker
- `gh` — GitHub CLI (issue/PR queries + creation)
- `python3` — JSON processing
- `pytest` (dev) — `pip install pytest`
- `aoe` (optional) — session registration + status in Agent of Empires

## Development

```bash
# Run the test suite (fast — 137 tests, ~0.5s)
pytest tests/ -v

# The pre-push hook runs the same tests before allowing a push.
# To install (one-time):
git config core.hooksPath .githooks
# Bypass with `git push --no-verify` if needed.

# Test layout
#   lib/tms/                  Python modules extracted from bin/tms
#   tests/                    pytest test suite
#   .githooks/pre-push        version-controlled pre-push hook
```

The test suite guards the five P0/P1 regressions from PR #7's
multi-model review (cache-write races, bare `except`, `os.path.isdir`
vs worktrees, `re.match` vs `re.search`, branch-first false-positives).
See `tests/test_session_matcher.py` and `tests/test_cache_atomic_writes.py`
for the named regression tests.
