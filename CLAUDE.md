# tms conventions

> **Dispatched agents:** see [AGENTS.md](AGENTS.md) for the in-session loop
> (state-marker contract + plan-gate / TDD-first / AC-verify discipline).

## Project structure
- `bin/tms` — session manager (deploy to /usr/local/bin)
- `bin/tmq` — issue dispatcher (deploy to /usr/local/bin)
- `lib/tms/` — Python modules extracted from bin/tms (issue #8)
- `tests/` — pytest test suite (run `pytest tests/`)
- `.githooks/pre-push` — runs `pytest tests/` before push
- Deploy: `sudo cp -r bin lib /usr/local/` (lib must travel with bin)

## Conventions
- Bash, `set -euo pipefail`
- fzf for interactive UI
- python3 for JSON processing (gh CLI output)
- Session naming: `feat-<repo>#<num>`, `fix-<repo>#<num>`, `review-<repo>#<num>`
- Scratch sessions: `c<n>` (claude), `o<n>` (opencode), `p<n>` (pi)
- Worktrees at `/root/wt-<repo>-<num>` (managed by tmq)
- `TMS_TMQ_LAUNCH=1` opts in to auto-attach: direct `tmq` calls switch your
  tmux client into the spawned session. Default (unset) preserves the tms
  fzf dispatch-then-return flow. Only the literal `1` opts in.
- Issue registry mirrors tmq's REPO_GH: `distillery`, `home-portal`, `tower-fleet`, `scripts`, `palimpsest`, `rms`

## Git
- Branch: `feat/issue-<num>-<slug>` for features
- Commit: `type: description` (feat, fix, docs, refactor, chore)
- No emojis in commits
