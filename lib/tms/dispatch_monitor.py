"""Dispatch-failure rate monitor and Telegram alert (issue #84).

Query dispatch_failed events per rolling hour from tms_review.events,
compare against SLO threshold from class_contracts.yaml (or default),
and send a deduped Telegram alert when exceeded.

Mirrors review_poll.py shape: Python module with public API + CLI
entry point, designed to run from cron.

Public API:
  - query_dispatch_failures(reference_time=None) -> list[dict]
  - build_alert_message(failures, threshold, total_count) -> str
  - should_alert(total_count, storm_key) -> bool
  - record_alert(storm_key) -> None
  - load_slo_threshold(search_paths=None) -> int
  - send_telegram_alert(message, dry_run=False) -> None
  - check_failure_rate(reference_time=None, dry_run=False) -> dict

Watermark: /tmp/tmq-dispatch-monitor-watermark.json (ephemeral per-boot).
"""

import datetime
import json
import os
import sys
from collections import Counter

import psycopg2
import requests
import yaml

from tms.events import _read_db_config


# ── Paths ─────────────────────────────────────────────────────────

WATERMARK_PATH = "/tmp/tmq-dispatch-monitor-watermark.json"

# Watermark TTL: how long before a previously-alerted storm can be
# re-alerted. 4 hours is longer than the 1-hour rolling window, so a
# sustained storm will re-fire every 4h (not every cron tick), but a
# genuinely resolved-then-re-emerged storm still gets caught same-day.
_WATERMARK_TTL_HOURS = 4

# Built-in fallback SLO threshold for dispatch failures per hour.
# Overridden by class_contracts.yaml if available.
_DEFAULT_MAX_FAIL_PER_HOUR = 10

# class_contracts.yaml lookup: production deploy path → dev checkout
# → built-in default. Only the first readable file wins.
_DEFAULT_SEARCH_PATHS = [
    "/root/deploy/distillery/scripts/class_contracts.yaml",
    "/root/projects/distillery/scripts/class_contracts.yaml",
]

# Telegram bot env file (same token as the claude-telegram channel).
_TELEGRAM_ENV_PATH = os.path.expanduser("~/.claude/channels/telegram/.env")

# Max reason length in alert message before truncation.
_MAX_REASON_LEN = 80

# ── Database connection (shared with events.py pattern) ───────────

def _get_conn():
    """Return a psycopg2 connection. Monkeypatched in tests."""
    cfg = _read_db_config()
    return psycopg2.connect(
        host=cfg["host"],
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
    )


# ── Rolling-hour query ────────────────────────────────────────────

def query_dispatch_failures(reference_time=None):
    """Return dispatch_failed events from the last rolling hour.

    Args:
        reference_time: datetime (UTC) to use as "now." Defaults to
            actual wall clock. Callers with frozen-time override this.

    Returns:
        List of dicts: [{repo, issue, reason, event_timestamp}, ...]
        ordered by event_timestamp ascending.
    """
    if reference_time is None:
        reference_time = datetime.datetime.now(datetime.timezone.utc)

    cutoff = reference_time - datetime.timedelta(hours=1)
    cutoff_str = cutoff.isoformat()

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT repo, issue, reason, event_timestamp
                   FROM tms_review.events
                   WHERE event_type = 'dispatch_failed'
                     AND event_timestamp >= %s
                   ORDER BY event_timestamp""",
                (cutoff_str,),
            )
            rows = cur.fetchall()

    return [
        {
            "repo": row[0],
            "issue": row[1],
            "reason": row[2],
            "event_timestamp": row[3],
        }
        for row in rows
    ]


# ── Alert message builder ──────────────────────────────────────


def build_alert_message(failures, threshold, total_count):
    """Build a deduped Telegram alert message from failure rows.

    Groups by (repo, issue) so "hp#306 ×65" not 65 lines.
    Includes top reason per group, truncated. Adds per-issue
    circuit-breaker signal when a single issue exceeds threshold.
    """
    # Group by (repo, issue) with counts
    groups = Counter()
    first_reason = {}  # (repo, issue) → first reason seen
    for f in failures:
        key = (f["repo"], f["issue"])
        groups[key] += 1
        if key not in first_reason:
            first_reason[key] = f["reason"]

    # Build issue lines, sorted by count descending
    lines = [
        f"\U0001f534 <b>dispatch_failed SLO breach</b> — "
        f"{total_count} failures in the last hour "
        f"(threshold: {threshold}/hour)"
    ]
    lines.append("")

    for (repo, issue), count in groups.most_common():
        short = _short_repo(repo)
        reason = first_reason.get((repo, issue), "")
        if len(reason) > _MAX_REASON_LEN:
            reason = reason[:_MAX_REASON_LEN] + "…"
        label = f"{short}#{issue}"
        lines.append(f"  <b>{label}</b> ×{count}")
        lines.append(f"    {reason}")

        # Circuit-breaker signal: if this issue alone exceeds the
        # fleet-wide threshold, flag it for per-issue intervention.
        if count > threshold:
            lines.append(
                f"    \u26a0\ufe0f <i>per-issue threshold exceeded "
                f"({count} > {threshold}) — consider circuit-breaker</i>"
            )

    return "\n".join(lines)


def _short_repo(repo):
    """Abbreviate known repos: home-portal → hp, tower-fleet → tf."""
    short_map = {
        "home-portal": "hp",
        "tower-fleet": "tf",
        "tms": "tms",
        "distillery": "distillery",
        "palimpsest": "pd",
    }
    return short_map.get(repo, repo)


# ── Watermark ─────────────────────────────────────────────────────


def _read_watermark():
    """Read the watermark file. Returns dict, empty on missing/corrupt."""
    try:
        with open(WATERMARK_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_watermark(data):
    """Atomic write of the watermark file."""
    tmp = WATERMARK_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, WATERMARK_PATH)


def should_alert(total_count, storm_key, threshold=None):
    """Check whether an alert should fire for a given storm_key.

    Returns True if:
      - total_count exceeds threshold (when provided), AND
      - storm_key has no recent watermark entry within TTL.

    If threshold is None or not provided, the watermark check is the
    sole gate (caller already decided the threshold).
    """
    if threshold is not None and total_count <= threshold:
        return False

    watermark = _read_watermark()
    last_alert = watermark.get(storm_key)
    if last_alert is None:
        return True

    try:
        last_ts = datetime.datetime.fromisoformat(last_alert)
    except (ValueError, TypeError):
        return True  # corrupt timestamp → re-alert

    age = datetime.datetime.now(datetime.timezone.utc) - last_ts
    return age > datetime.timedelta(hours=_WATERMARK_TTL_HOURS)


def record_alert(storm_key):
    """Record that we alerted for storm_key at the current time."""
    watermark = _read_watermark()
    watermark[storm_key] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat()
    _write_watermark(watermark)


# ── SLO threshold from class_contracts.yaml ───────────────────────


def load_slo_threshold(search_paths=None):
    """Read max_fail_per_hour for tmq-dispatch from class_contracts.yaml.

    Search order (first readable file wins):
      1. search_paths[0] if provided, else _DEFAULT_SEARCH_PATHS[0]
      2. search_paths[1], ...
      3. Built-in default (_DEFAULT_MAX_FAIL_PER_HOUR = 10)

    Logs which source was used to stdout.
    """
    paths = search_paths if search_paths is not None else _DEFAULT_SEARCH_PATHS

    for path in paths:
        try:
            with open(path) as f:
                contracts = yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError, OSError):
            continue

        if not isinstance(contracts, dict):
            continue

        classes = contracts.get("classes", {})
        tmq = classes.get("tmq-dispatch", {})
        slo = tmq.get("slo", {})
        threshold = slo.get("max_fail_per_hour")
        if threshold is not None and isinstance(threshold, (int, float)):
            print(f"SLO threshold source: {path} → {threshold}/hour")
            return int(threshold)

    print(f"SLO threshold source: default → {_DEFAULT_MAX_FAIL_PER_HOUR}/hour")
    return _DEFAULT_MAX_FAIL_PER_HOUR


# ── Telegram sender ───────────────────────────────────────────────


def _read_telegram_token():
    """Read the bot token from the claude-telegram .env file.

    Returns the token string, or None if the file is missing/unreadable.
    """
    try:
        with open(_TELEGRAM_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1]
    except (FileNotFoundError, OSError):
        pass
    return None


def _get_telegram_chat_id():
    """Return the chat ID to send alerts to.

    Reads the access.json allowlist for the configured operator.
    Uses the first allowed sender ID as the chat target (Telegram
    Bot API: for a direct message, chat_id == user_id).

    Fragility note: if an additional user is ever added to the
    allowlist before the operator's ID, alerts would misroute.
    The access.json is single-operator by design (claude-telegram
    channel setup) — this is not a multi-user bot.

    Returns None if not resolvable.
    """
    # The access.json file lists allowed senders by ID.
    # The bot was configured with sender ID 7658477301.
    # Telegram Bot API sends messages to a chat_id; for a direct
    # message to the user, the chat_id equals the user ID.
    access_path = os.path.expanduser(
        "~/.claude/channels/telegram/access.json"
    )
    try:
        with open(access_path) as f:
            access = json.load(f)
        # access.json format: {"allow": ["7658477301"], ...}
        allowed = access.get("allow", [])
        if allowed:
            return allowed[0]  # first allowed sender = the operator
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def send_telegram_alert(message, dry_run=False):
    """Send a Telegram alert to the fleet channel.

    In dry-run mode, prints the message to stdout instead of sending.
    """
    if dry_run:
        print(f"[dry-run] Telegram alert would be sent:")
        print(message)
        return

    token = _read_telegram_token()
    if not token:
        print("ERROR: Telegram bot token not found — alert not sent",
              file=sys.stderr)
        return

    chat_id = _get_telegram_chat_id()
    if not chat_id:
        print("ERROR: Telegram chat_id not resolved — alert not sent",
              file=sys.stderr)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if resp.status_code != 200 or not resp.json().get("ok"):
            print(
                f"ERROR: Telegram API returned "
                f"{resp.status_code}: {resp.text}",
                file=sys.stderr,
            )
    except requests.RequestException as e:
        print(f"ERROR: Telegram send failed: {e}", file=sys.stderr)


# ── Full check pipeline ───────────────────────────────────────────


def check_failure_rate(reference_time=None, dry_run=False):
    """Run the full monitor pipeline: query → threshold → alert decision.

    Args:
        reference_time: datetime (UTC) for "now," defaults to wall clock.
        dry_run: if True, print alerts instead of sending to Telegram.

    Returns:
        dict with keys: total_count, threshold, alert_sent, storm_key
    """
    failures = query_dispatch_failures(reference_time=reference_time)
    total_count = len(failures)
    threshold = load_slo_threshold()

    result = {
        "total_count": total_count,
        "threshold": threshold,
        "alert_sent": False,
        "storm_key": "",
    }

    if total_count <= threshold:
        return result

    # Build a storm key from the set of affected issues (sorted for
    # determinism). Changes in the issue mix produce a different key
    # and thus re-trigger the alert.
    issue_keys = sorted(set(
        f"{_short_repo(f['repo'])}#{f['issue']}" for f in failures
    ))
    storm_key = ",".join(issue_keys)
    result["storm_key"] = storm_key

    if not should_alert(total_count, storm_key, threshold=threshold):
        return result

    message = build_alert_message(failures, threshold, total_count)
    send_telegram_alert(message, dry_run=dry_run)
    record_alert(storm_key)
    result["alert_sent"] = True

    return result


# ── CLI entry point ───────────────────────────────────────────────

def main():
    """Entry point for ``python3 -m tms.dispatch_monitor <subcommand>``.

    Subcommands:
      check [--dry-run]
          Query failures, compare against SLO, alert if needed.
    """
    if len(sys.argv) < 2:
        print("usage: python3 -m tms.dispatch_monitor <check> [...]",
              file=sys.stderr)
        sys.exit(1)

    subcmd = sys.argv[1]

    if subcmd == "check":
        dry_run = "--dry-run" in sys.argv
        result = check_failure_rate(dry_run=dry_run)
        if not result["alert_sent"]:
            print(
                f"dispatch_failed events in last hour: "
                f"{result['total_count']} "
                f"(threshold: {result['threshold']}/hour) — no alert"
            )
    else:
        print(f"unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
