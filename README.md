# tms — tmux session manager + agent workflow

Session management and GitHub issue dispatch for multi-agent coding workflows.

## Quick start

```bash
# Deploy to /usr/local/bin
sudo cp bin/tms bin/tmq /usr/local/bin/

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
```

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

## Dependencies

- `tmux` — session substrate
- `fzf` — interactive picker
- `gh` — GitHub CLI (issue/PR queries + creation)
- `python3` — JSON processing
- `aoe` (optional) — session registration + status in Agent of Empires
