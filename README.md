# tms — GitHub issue browser + tmux session manager

Primary tool for browsing and dispatching agents on GitHub issues.
Session management is secondary; use `aoe` for live session monitoring.

## Quick start

```bash
# Deploy to /usr/local/bin
sudo cp bin/tms bin/tmq /usr/local/bin/

# Issue browser (default)
tms

# Show all repos
tms --all
tms --raw --all

# Specific repo (auto-detects from CWD if not given)
tms distillery
tms --raw home-portal

# View modes
tms --view active      # issues with a live agent
tms --view yours       # assigned to you
tms --view stale       # open >14d, no activity

# Session browser (was the default in v1)
tms sessions

# Dispatch an agent on an issue
tmq distillery 245

# Other
tms ls                  # list sessions
tms stale               # find idle >7d
tms clean               # kill stale + compact scratch
tms issues --clear-cache
```

## Tools

### tms — issue browser (primary)

```
tms                              issue browser, current repo
tms --all                        all repos
tms --raw                        print rows only, no fzf
tms --view all|active|yours|stale
tms --force                      skip 5-min cache
tms --clear-cache

tms sessions                     session browser (v1 default)
tms ls                           list sessions
tms kill <name>                  kill session
tms rename <name>                rename
tms stale / clean / compact
tms --preview <row>              (internal: render preview pane)
```

#### Issue browser keybindings (fzf)

| Key | Action |
|-----|--------|
| `enter` | dispatch agent (via tmq) |
| `ctrl-o` | open issue in browser |
| `ctrl-p` | full preview in pager |
| `ctrl-e` | code review linked PR |
| `ctrl-v` | cycle view: All → Active → Yours → Stale |
| `ctrl-r` | force refresh (skip cache) |
| `ctrl-s` | jump to session browser |
| `?` | help |
| `esc` | cancel |

#### Columns

```
REPO        #    ST  STATUS     AGENT  AGE   UPD   ★  TITLE
distillery  #245 ▶   pr open    cc π   4d    4d        Film pipeline v2
```

| Column | Meaning |
|--------|---------|
| `ST` | `○` open · `●` active agent · `▶` PR open · `◐` draft · `◎` review · `✎` changes · `✓` ready/merged · `✗` blocked · `✔` reviewed · `?` question · `✕` closed |
| `STATUS` | one of: open, active, pr open, draft, review, changes, ready, merged, blocked, reviewed, question, closed |
| `AGENT` | agent icons (π=pi, cc=claude, oc=opencode) for live sessions on the issue |
| `AGE` | time since issue was created |
| `UPD` | time since last activity (or time since closed for closed issues) |
| `★` | assigned to you |

#### Sort

Open issues sort by `updatedAt` desc (most recently bumped float up). Closed issues
(merged + closed-not-merged) appear at the bottom, sorted by `closedAt` desc, with
"recently closed" (last 14d) visible by default and older ones hidden.

#### View modes

- **All** — every open issue + recently closed (14d). Default.
- **Active** — only issues with a live agent session. "What is my fleet doing right now."
- **Yours** — assigned to you (the GH user detected via `gh api user`).
- **Stale** — open >14d, no updates, no agent. Triage queue.

### tmq — issue → agent dispatcher

```
tmq <repo> <num>                     dispatch pi on issue
tmq <repo> <num> --agent cc          use Claude Code
tmq <repo> <num> --type review       code review
tmq list                             known repos
```

## Architecture

```
tms (issue-first)
├── issue browser — fzf picker, multi-repo, view modes, dispatch
└── session browser — secondary; use aoe instead

tmq (issue → agent spawn)
├── gh issue view → build prompt
├── git worktree add → isolate working tree
└── aoe add → spawn agent (appears in aoe dashboard)

aoe (Agent of Empires — monitoring)
├── session dashboard + web + mobile
└── status: Running / Waiting / Idle / Error / Stopped
```

## Dependencies

- `tmux` — session substrate
- `fzf` — interactive picker
- `gh` — GitHub CLI (issue/PR queries)
- `python3` — JSON processing
- `aoe` (optional) — session registration in Agent of Empires
