#!/bin/bash
# git-trace wrapper — tms#137 AC-1 ("pin the planter with tracing, not inference").
#
# Installed as /usr/local/bin/git (PATH-front of /usr/bin/git for every
# interactive, cron, and dispatched-agent session), it appends one line per
# ref-mutating git invocation to /var/log/git-trace.log and then execs the
# real git. One traced clobber episode names the process that planted it.
#
# Invariants:
#   - The wrapped command's behavior is untouched: same stdin/stdout/stderr,
#     same exit code (exec), no output of its own.
#   - Logging is strictly best-effort — any failure (unwritable log, missing
#     /proc entries) is swallowed and git still runs.
#   - git dispatches its own subcommands via its exec-path, not $PATH, so the
#     wrapper never recurses.
#
# Install/rotation: scripts/install-git-trace.sh

REAL_GIT=/usr/bin/git
TRACE_LOG=/var/log/git-trace.log

# Find the subcommand: skip global options, including the ones that consume
# a separate value argument.
sub=""
argv=("$@")
i=0
while (( i < ${#argv[@]} )); do
    a=${argv[i]}
    case "$a" in
        -C|-c|--git-dir|--work-tree|--namespace|--super-prefix|--exec-path|--config-env|--attr-source)
            (( i += 2 )) ;;
        -*)
            (( i += 1 )) ;;
        *)
            sub=$a
            break ;;
    esac
done

# The clobber signature (core.bare flip, fixture init commits, ref theft,
# junk pushed to PR branches) is producible only through these subcommands.
case "$sub" in
    init|config|commit|checkout|switch|branch|update-ref|symbolic-ref|reset|push|worktree|clone)
        {
            # Ancestor chain: pid(comm) up the tree — enough to attribute a
            # call through bash -c wrappers to the agent process that ran it.
            chain="" p=$$ depth=0
            while [[ -n "$p" && "$p" != "0" && "$p" != "1" && $depth -lt 6 ]]; do
                comm=$(cat "/proc/$p/comm" 2>/dev/null) || comm="?"
                chain+="${chain:+<-}$p($comm)"
                stat=$(cat "/proc/$p/stat" 2>/dev/null) || break
                # stat = "pid (comm) state ppid ..."; comm may contain spaces,
                # so strip through the last ')' before field-splitting.
                read -r _state p _ <<< "${stat##*) }"
                (( depth++ ))
            done
            pcmd=$(tr '\0' ' ' < "/proc/$PPID/cmdline" 2>/dev/null)
            printf '%s\tcwd=%s\tchain=%s\ttmux=%s/%s\tparent=%.300s\targv=git' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PWD" "$chain" \
                "${TMUX:-}" "${TMUX_PANE:-}" "${pcmd:-?}" >> "$TRACE_LOG"
            printf ' %q' "$@" >> "$TRACE_LOG"
            printf '\n' >> "$TRACE_LOG"
        } 2>/dev/null || true
        ;;
esac

exec "$REAL_GIT" "$@"
