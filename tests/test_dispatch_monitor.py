"""Tests for lib/tms/dispatch_monitor.py — dispatch_failed SLO monitor
and Telegram alert (issue #84).
"""

import json
import datetime as dt
from unittest.mock import patch

import pytest


# ── Rolling-hour query ────────────────────────────────────────────

def test_query_rolling_hour_filters_correctly(test_db):
    """Only dispatch_failed events from the last hour count."""
    from tms.dispatch_monitor import query_dispatch_failures

    # Insert events at known timestamps
    now = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.timezone.utc)

    def insert(timestamp_str, repo, issue, reason="aoe add failed"):
        from tms.events import append_event
        append_event({
            "event_type": "dispatch_failed",
            "timestamp": timestamp_str,
            "repo": repo,
            "issue": issue,
            "agent": "cc",
            "provider": "",
            "model": "",
            "dispatch_type": "feature",
            "reason": reason,
        })

    # Inside rolling hour
    insert("2026-07-14T11:30:00+00:00", "home-portal", 306)
    insert("2026-07-14T11:45:00+00:00", "home-portal", 306)
    insert("2026-07-14T11:50:00+00:00", "tower-fleet", 182)

    # Outside rolling hour (too old)
    insert("2026-07-14T10:59:59+00:00", "home-portal", 310)
    insert("2026-07-13T12:00:00+00:00", "tower-fleet", 157)

    # Wrong event type — must NOT count
    from tms.events import append_event
    append_event({
        "event_type": "dispatch",
        "timestamp": "2026-07-14T11:55:00+00:00",
        "repo": "tms",
        "issue": 84,
        "agent": "pi",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "dispatch_type": "feature",
        "session": "feat-tms#84",
        "aoe_id_prefix": "abc12345",
    })

    failures = query_dispatch_failures(reference_time=now)

    # Must return 3 rows (the ones inside the rolling hour)
    assert len(failures) == 3
    repos = {f["repo"] for f in failures}
    assert repos == {"home-portal", "tower-fleet"}
    issues = {f["issue"] for f in failures}
    assert issues == {306, 182}


def test_query_returns_empty_when_no_failures(test_db):
    """Empty table → empty list, no crash."""
    from tms.dispatch_monitor import query_dispatch_failures

    now = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
    failures = query_dispatch_failures(reference_time=now)
    assert failures == []


# ── Deduped alert format ──────────────────────────────────────────


class TestBuildAlertMessage:
    """Alert message must dedupe by (repo, issue) and include top reason."""

    def test_dedupes_per_issue(self):
        """hp#306 ×65 not 65 lines of the same issue."""
        from tms.dispatch_monitor import build_alert_message

        failures = [
            {"repo": "home-portal", "issue": 306, "reason": "aoe add failed",
             "event_timestamp": "2026-07-14T11:30:00+00:00"},
        ] * 65 + [
            {"repo": "tower-fleet", "issue": 182, "reason": "aoe add failed",
             "event_timestamp": "2026-07-14T11:40:00+00:00"},
        ] * 48 + [
            {"repo": "home-portal", "issue": 310, "reason": "aoe add failed",
             "event_timestamp": "2026-07-14T11:50:00+00:00"},
        ] * 17

        msg = build_alert_message(failures, threshold=10, total_count=130)

        assert "hp#306" in msg or "home-portal#306" in msg
        assert "×65" in msg
        assert "×48" in msg
        assert "×17" in msg
        # Must have 3 issue lines, not 130
        assert msg.count("×") >= 3
        assert "130" in msg  # total in heading

    def test_truncates_long_reason(self):
        """Long reasons must be truncated to 80 chars in alert."""
        from tms.dispatch_monitor import build_alert_message

        long_reason = "aoe add failed: `aoe add /root/tower-fleet -t chore-tower-fleet#200 " \
                      "--tool pi --trust-hooks --cmd-override PI_DISPATCH_AUTOAPPROVE=1 " \
                      "pi @/tmp/tmq-prompt-chore-tower-fleet#200.txt` exited 2"
        failures = [
            {"repo": "tower-fleet", "issue": 200, "reason": long_reason,
             "event_timestamp": "2026-07-14T11:55:00+00:00"},
        ]

        msg = build_alert_message(failures, threshold=10, total_count=1)
        # The truncated reason must appear and be ≤80 chars
        assert "aoe add failed" in msg
        # Must not contain the full long reason verbatim if >80 chars
        if len(long_reason) > 80:
            assert long_reason not in msg

    def test_includes_threshold_in_message(self):
        """Alert must cite the threshold that was exceeded."""
        from tms.dispatch_monitor import build_alert_message

        failures = [
            {"repo": "tms", "issue": 84, "reason": "test failure",
             "event_timestamp": "2026-07-14T12:00:00+00:00"},
        ] * 15

        msg = build_alert_message(failures, threshold=10, total_count=15)
        assert "10/hour" in msg

    def test_circuit_breaker_per_issue_signal(self):
        """When a single issue fails > threshold, include circuit-breaker mention."""
        from tms.dispatch_monitor import build_alert_message

        failures = [
            {"repo": "home-portal", "issue": 306, "reason": "aoe add failed",
             "event_timestamp": "2026-07-14T11:30:00+00:00"},
        ] * 25  # 25 consecutive for same issue, threshold is 10

        msg = build_alert_message(failures, threshold=10, total_count=25)
        assert "circuit" in msg.lower() or "consecutive" in msg.lower()


# ── Watermark ─────────────────────────────────────────────────────


class TestWatermark:
    """Watermark prevents re-alerting the same storm on every cron tick."""

    def test_should_alert_true_when_no_watermark(self, tmp_path, monkeypatch):
        """First run with no watermark file → should alert (if threshold exceeded)."""
        from tms.dispatch_monitor import should_alert

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))

        # No watermark file → alert
        assert should_alert(total_count=15, storm_key="hp#306") is True

    def test_should_alert_false_when_recently_alerted(self, tmp_path, monkeypatch):
        """If the same storm_key was alerted < TTL ago, don't re-alert."""
        from tms.dispatch_monitor import should_alert

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))

        # Seed a watermark from 30 minutes ago
        recent = (dt.datetime.now(dt.timezone.utc) -
                  dt.timedelta(minutes=30)).isoformat()
        wm_path.write_text(json.dumps({
            "hp#306": recent,
        }))

        assert should_alert(total_count=15, storm_key="hp#306") is False

    def test_should_alert_true_after_ttl_expiry(self, tmp_path, monkeypatch):
        """After TTL expires, re-alert is allowed."""
        from tms.dispatch_monitor import should_alert

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))

        # Seed a watermark from TTL+1 hour ago
        old = (dt.datetime.now(dt.timezone.utc) -
               dt.timedelta(hours=5)).isoformat()
        wm_path.write_text(json.dumps({
            "hp#306": old,
        }))

        assert should_alert(total_count=15, storm_key="hp#306") is True

    def test_should_alert_false_below_threshold(self, tmp_path, monkeypatch):
        """Even with no watermark, below threshold → no alert."""
        from tms.dispatch_monitor import should_alert

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))

        assert should_alert(total_count=5, storm_key="any", threshold=10) is False

    def test_record_alert_writes_watermark(self, tmp_path, monkeypatch):
        """record_alert must write a watermark entry."""
        from tms.dispatch_monitor import record_alert

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))

        record_alert(storm_key="hp#306")

        data = json.loads(wm_path.read_text())
        assert "hp#306" in data
        # Timestamp must be recent (within 2 seconds)
        ts = dt.datetime.fromisoformat(data["hp#306"])
        age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
        assert age < 2

    def test_watermark_merges_existing_entries(self, tmp_path, monkeypatch):
        """record_alert must preserve existing entries for other keys."""
        from tms.dispatch_monitor import record_alert

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))

        old_ts = "2026-07-14T10:00:00+00:00"
        wm_path.write_text(json.dumps({"tf#182": old_ts}))

        record_alert(storm_key="hp#306")

        data = json.loads(wm_path.read_text())
        assert "tf#182" in data
        assert data["tf#182"] == old_ts
        assert "hp#306" in data


# ── SLO threshold from class_contracts.yaml ───────────────────────


class TestSloThreshold:
    """Read max_fail_per_hour from class_contracts.yaml, fall back to 10."""

    def test_default_when_no_file(self):
        """When no contracts file exists, return the built-in default."""
        from tms.dispatch_monitor import load_slo_threshold

        threshold = load_slo_threshold(
            search_paths=["/nonexistent/class_contracts.yaml"],
        )
        assert threshold == 10

    def test_reads_from_contracts_yaml(self, tmp_path):
        """When a valid contracts file exists, read the tmq-dispatch SLO."""
        import yaml
        from tms.dispatch_monitor import load_slo_threshold

        contracts = tmp_path / "class_contracts.yaml"
        contracts.write_text(yaml.dump({
            "classes": {
                "tmq-dispatch": {
                    "slo": {"max_fail_per_hour": 7, "alert": "telegram"},
                },
            },
        }))

        threshold = load_slo_threshold(search_paths=[str(contracts)])
        assert threshold == 7

    def test_missing_class_falls_back_to_default(self, tmp_path):
        """Contracts file exists but tmq-dispatch class is absent → default."""
        import yaml
        from tms.dispatch_monitor import load_slo_threshold

        contracts = tmp_path / "class_contracts.yaml"
        contracts.write_text(yaml.dump({
            "classes": {
                "debate-gen": {
                    "slo": {"max_fail_per_hour": 5, "alert": "telegram"},
                },
            },
        }))

        threshold = load_slo_threshold(search_paths=[str(contracts)])
        assert threshold == 10

    def test_missing_slo_key_falls_back_to_default(self, tmp_path):
        """Class exists but slo key is absent → default."""
        import yaml
        from tms.dispatch_monitor import load_slo_threshold

        contracts = tmp_path / "class_contracts.yaml"
        contracts.write_text(yaml.dump({
            "classes": {
                "tmq-dispatch": {
                    "description": "no slo key here",
                },
            },
        }))

        threshold = load_slo_threshold(search_paths=[str(contracts)])
        assert threshold == 10

    def test_first_found_path_wins(self, tmp_path):
        """When multiple paths exist, first readable one wins."""
        import yaml
        from tms.dispatch_monitor import load_slo_threshold

        first = tmp_path / "first.yaml"
        second = tmp_path / "second.yaml"
        first.write_text(yaml.dump({
            "classes": {
                "tmq-dispatch": {
                    "slo": {"max_fail_per_hour": 3, "alert": "telegram"},
                },
            },
        }))
        second.write_text(yaml.dump({
            "classes": {
                "tmq-dispatch": {
                    "slo": {"max_fail_per_hour": 99, "alert": "telegram"},
                },
            },
        }))

        threshold = load_slo_threshold(search_paths=[str(first), str(second)])
        assert threshold == 3

    def test_corrupt_yaml_falls_back_to_default(self, tmp_path, capsys):
        """Corrupt YAML must not crash — fall back to default and log warning."""
        from tms.dispatch_monitor import load_slo_threshold

        contracts = tmp_path / "class_contracts.yaml"
        contracts.write_text("{{{ not valid yaml")

        threshold = load_slo_threshold(search_paths=[str(contracts)])
        assert threshold == 10
        out = capsys.readouterr().out
        assert "SLO threshold source: default" in out


# ── Telegram sender ───────────────────────────────────────────────


class TestTelegramAlert:
    """Telegram alert sending via Bot API."""

    def test_send_alert_constructs_correct_message(self):
        """send_telegram_alert must POST to the correct API endpoint."""
        from tms.dispatch_monitor import send_telegram_alert

        with patch("tms.dispatch_monitor._read_telegram_token",
                   return_value="test-token"):
            with patch("tms.dispatch_monitor._get_telegram_chat_id",
                       return_value="12345"):
                with patch("tms.dispatch_monitor.requests.post") as mock_post:
                    mock_post.return_value.status_code = 200
                    mock_post.return_value.json.return_value = {"ok": True}

                    send_telegram_alert("test alert message", dry_run=False)

        assert mock_post.called
        call_args = mock_post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "api.telegram.org" in url
        assert "sendMessage" in url

    def test_dry_run_does_not_post(self):
        """Dry-run mode must log and not POST."""
        from tms.dispatch_monitor import send_telegram_alert

        with patch("tms.dispatch_monitor.requests.post") as mock_post:
            send_telegram_alert("test alert", dry_run=True)

        assert not mock_post.called

    def test_dry_run_prints_message(self, capsys):
        """Dry-run must print the message that would be sent."""
        from tms.dispatch_monitor import send_telegram_alert

        send_telegram_alert("test alert", dry_run=True)
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "test alert" in out

    def test_missing_token_does_not_crash(self):
        """If .env file is missing, log error and return without crash."""
        from tms.dispatch_monitor import send_telegram_alert

        with patch("tms.dispatch_monitor._read_telegram_token",
                   return_value=None):
            with patch("tms.dispatch_monitor.requests.post") as mock_post:
                send_telegram_alert("test alert", dry_run=False)

        assert not mock_post.called


# ── Full check_failure_rate pipeline ──────────────────────────────


class TestCheckFailureRate:
    """End-to-end check pipeline: query → threshold → alert decision."""

    def test_returns_result_dict(self, test_db, tmp_path, monkeypatch):
        """check_failure_rate must return a structured result."""
        from tms.dispatch_monitor import check_failure_rate
        from tms.events import append_event

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))
        monkeypatch.setattr("tms.dispatch_monitor._DEFAULT_SEARCH_PATHS",
                           ["/nonexistent/class_contracts.yaml"])

        # Insert below-threshold count (use distinct minutes to avoid
        # the unique-index dedup on same-second timestamps)
        now = dt.datetime.now(dt.timezone.utc)
        for i in range(3):
            append_event({
                "event_type": "dispatch_failed",
                "timestamp": (now - dt.timedelta(minutes=5 - i)).isoformat(),
                "repo": "tms", "issue": 84, "agent": "cc",
                "provider": "", "model": "",
                "dispatch_type": "feature", "reason": "test",
            })

        result = check_failure_rate(
            reference_time=now, dry_run=True,
        )

        assert "total_count" in result
        assert result["total_count"] == 3
        assert not result["alert_sent"]

    def test_alerts_when_above_threshold(self, test_db, tmp_path, monkeypatch):
        """When failure count > threshold, alert must be sent."""
        from tms.dispatch_monitor import check_failure_rate
        from tms.events import append_event

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))
        monkeypatch.setattr("tms.dispatch_monitor._DEFAULT_SEARCH_PATHS",
                           ["/nonexistent/class_contracts.yaml"])

        now = dt.datetime.now(dt.timezone.utc)
        for i in range(15):
            append_event({
                "event_type": "dispatch_failed",
                "timestamp": (now - dt.timedelta(minutes=30 - i)).isoformat(),
                "repo": "home-portal", "issue": 306, "agent": "cc",
                "provider": "", "model": "",
                "dispatch_type": "feature", "reason": "aoe add failed",
            })

        with patch("tms.dispatch_monitor.send_telegram_alert") as mock_send:
            result = check_failure_rate(
                reference_time=now, dry_run=False,
            )

        assert result["total_count"] == 15
        assert result["alert_sent"]
        mock_send.assert_called_once()
        # Check the message includes deduped format
        call_msg = mock_send.call_args[0][0]
        assert "home-portal" in call_msg or "hp" in call_msg

    def test_suppresses_repeat_alert_within_watermark_ttl(
            self, test_db, tmp_path, monkeypatch):
        """Same storm_key within TTL → no duplicate alert."""
        from tms.dispatch_monitor import check_failure_rate
        from tms.events import append_event

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))
        monkeypatch.setattr("tms.dispatch_monitor._DEFAULT_SEARCH_PATHS",
                           ["/nonexistent/class_contracts.yaml"])

        now = dt.datetime.now(dt.timezone.utc)

        def insert_failures():
            for i in range(15):
                append_event({
                    "event_type": "dispatch_failed",
                    "timestamp": (now - dt.timedelta(minutes=30 - i)).isoformat(),
                    "repo": "home-portal", "issue": 306, "agent": "cc",
                    "provider": "", "model": "",
                    "dispatch_type": "feature", "reason": "aoe add failed",
                })

        # First run — should alert
        insert_failures()
        with patch("tms.dispatch_monitor.send_telegram_alert") as mock_send:
            result1 = check_failure_rate(reference_time=now, dry_run=False)
        assert result1["alert_sent"]
        assert mock_send.called

        # Second run (same events still in window) — should suppress
        insert_failures()
        with patch("tms.dispatch_monitor.send_telegram_alert") as mock_send2:
            result2 = check_failure_rate(reference_time=now, dry_run=False)
        assert not result2["alert_sent"]
        assert not mock_send2.called

    def test_empty_events_no_alert(self, test_db, tmp_path, monkeypatch):
        """Zero failures → no alert."""
        from tms.dispatch_monitor import check_failure_rate

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))
        monkeypatch.setattr("tms.dispatch_monitor._DEFAULT_SEARCH_PATHS",
                           ["/nonexistent/class_contracts.yaml"])

        now = dt.datetime.now(dt.timezone.utc)
        with patch("tms.dispatch_monitor.send_telegram_alert") as mock_send:
            result = check_failure_rate(reference_time=now, dry_run=False)

        assert result["total_count"] == 0
        assert not result["alert_sent"]
        assert not mock_send.called


# ── Replay: Jul 14 2026 storm ─────────────────────────────────────


class TestJul14Replay:
    """Replay the Jul 14 dispatch_failed storm to verify the monitor
    WOULD have fired within the first hour."""

    def test_replay_jul14_storm_fires_alert(self, test_db, tmp_path, monkeypatch):
        """Insert the known Jul 14 pattern and verify alert fires."""
        from tms.dispatch_monitor import query_dispatch_failures, \
            build_alert_message, load_slo_threshold
        from tms.events import append_event

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))

        # Replay the Jul 14 storm: 195 "aoe add failed" events across
        # hp#306 (65), tf#182 (48), tf#157 (48), hp#310 (17), plus 1
        # tf#200 with long reason — all within ~5 min bursts over 5 hours.
        #
        # The issue says 198 events in one day. 195 were at ~5-min
        # retries which means ~12/hour per issue. At that rate, we
        # exceed 10/hr in the first hour.
        events = []
        # First hour: 11:00-12:00 UTC — 12 events each for hp#306, tf#182, tf#157
        base = dt.datetime(2026, 7, 14, 11, 0, 0, tzinfo=dt.timezone.utc)
        for issue in [306, 182, 157]:
            for minute in range(0, 55, 5):  # every 5 minutes
                ts = (base + dt.timedelta(minutes=minute)).isoformat()
                repo = "home-portal" if issue == 306 else "tower-fleet"
                append_event({
                    "event_type": "dispatch_failed",
                    "timestamp": ts,
                    "repo": repo, "issue": issue, "agent": "cc",
                    "provider": "", "model": "",
                    "dispatch_type": "feature", "reason": "aoe add failed",
                })

        # Query at 12:00 — should see ~36 events (12×3 issues)
        reference = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
        failures = query_dispatch_failures(reference_time=reference)

        assert len(failures) >= 30, (
            f"Expected >= 30 failures in first hour, got {len(failures)}"
        )

        # With threshold 10, this must trigger
        msg = build_alert_message(failures, threshold=10,
                                  total_count=len(failures))
        assert "×" in msg  # deduped
        assert str(len(failures)) in msg

    def test_replay_jul14_deduped_format(self, test_db, tmp_path, monkeypatch):
        """Verify the Jul 14 alert message reads as a deduped rollup."""
        from tms.dispatch_monitor import query_dispatch_failures, \
            build_alert_message
        from tms.events import append_event

        wm_path = tmp_path / "watermark.json"
        monkeypatch.setattr("tms.dispatch_monitor.WATERMARK_PATH", str(wm_path))

        # Concentrate all 178 events within a single rolling hour.
        # Each issue gets its events at second-level intervals to
        # avoid unique-index dedup while staying in the window.
        base = dt.datetime(2026, 7, 14, 11, 0, 0, tzinfo=dt.timezone.utc)
        offset = 0
        for issue, repo, count in [
            (306, "home-portal", 65),
            (182, "tower-fleet", 48),
            (157, "tower-fleet", 48),
            (310, "home-portal", 17),
        ]:
            for i in range(count):
                ts = (base + dt.timedelta(seconds=offset)).isoformat()
                offset += 1
                append_event({
                    "event_type": "dispatch_failed",
                    "timestamp": ts,
                    "repo": repo, "issue": issue, "agent": "cc",
                    "provider": "", "model": "",
                    "dispatch_type": "feature", "reason": "aoe add failed",
                })

        reference = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
        failures = query_dispatch_failures(reference_time=reference)

        msg = build_alert_message(failures, threshold=10,
                                  total_count=len(failures))

        # The alert must group by issue, not be 178 lines
        lines = msg.split("\n")
        # Should have: heading, 4 issue lines, maybe footer
        assert len(lines) < 15
        assert "hp#306" in msg or "home-portal#306" in msg
        assert "×65" in msg
        assert "×48" in msg
        assert "×17" in msg
