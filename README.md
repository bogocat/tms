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

### tms events — fleet dispatch metrics

```
tms events transitions    poll aoe + tmux panes, emit state-transition events
tms events stats           compute and print aggregate metrics report
tms events stats --json    machine-readable JSON output
tms events stats --since YYYY-MM-DD   filter from date
```

Every `tmq` dispatch appends a JSONL event record. `tms events transitions`
polls running sessions and emits transition events when `<<AGENT-STATE>>`
markers change. `tms events stats` reads the event log and computes:
- Issue→merge latency (p50/p90)
- Review rounds per PR
- BLOCKED frequency vs clean MERGE-READY
- Plan-gate fast-path rate
- Per-model outcome rates

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
