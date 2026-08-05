#!/bin/bash
# Install the tms#137 git-trace wrapper as /usr/local/bin/git.
#
# /usr/local/bin precedes /usr/bin in every session's PATH (interactive,
# cron, tmux/aoe agents), so this fronts git host-wide without touching
# /usr/bin/git. Uninstall = rm /usr/local/bin/git.
set -euo pipefail

SRC="$(dirname "$(readlink -f "$0")")/git-trace-wrapper.sh"
DST=/usr/local/bin/git
LOG=/var/log/git-trace.log

[[ -x /usr/bin/git ]] || { echo "FATAL: /usr/bin/git missing — refusing to shadow git" >&2; exit 1; }
bash -n "$SRC"

# 0666: dispatched agents may run as non-root; logging must never fail a push.
touch "$LOG" && chmod 0666 "$LOG"

install -m 0755 "$SRC" "$DST"

cat > /etc/logrotate.d/git-trace <<'EOF'
/var/log/git-trace.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    create 0666 root root
}
EOF

# Smoke: wrapper must pass through cleanly and preserve exit codes.
"$DST" --version >/dev/null
"$DST" definitely-not-a-subcommand >/dev/null 2>&1 && { echo "FATAL: bad exit-code passthrough" >&2; exit 1; }
echo "installed: $DST -> exec /usr/bin/git, tracing to $LOG"
