"""``tms defect`` — the CLI escape hatch for the defect register (tms#157).

The CLI exists so a human, or a script whose detector does not exist yet, can
file a defect with the same dedupe semantics as a producer: same validation, same
outcome report, never a silent second row.

Database isolation: the shared ``test_db`` fixture (conftest.py) points
``review_eval._get_conn`` at a sqlite3 in-memory register, which intake uses.
"""

import json
from pathlib import Path

import pytest

from tms.defects import main

FP = "pipeline-health:junction-drift:152"
BIN_TMS = Path(__file__).resolve().parent.parent / "bin" / "tms"


def _argv(**overrides):
    argv = [
        "--repo", "home-portal",
        "--class", "silent-failure",
        "--fingerprint", FP,
        "--description", "Oathbringer has no book_topics/book_traditions rows",
        "--detector", "pipeline-health",
    ]
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if value is None:
            continue
        if isinstance(value, bool):
            if value:
                argv.append(flag)
            continue
        argv += [flag, str(value)]
    return argv


def _rows(test_db, sql, params=None):
    cur = test_db().cursor()
    return cur.execute(sql).fetchall() if params is None \
        else cur.execute(sql, params).fetchall()


def test_cli_files_a_defect_and_reports_the_outcome(test_db, capsys):
    rc = main(_argv())

    assert rc == 0
    out = capsys.readouterr().out
    assert "inserted" in out
    assert FP in out
    assert _rows(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 1


def test_cli_second_call_reports_a_recurrence_not_an_insert(test_db, capsys):
    assert main(_argv()) == 0
    capsys.readouterr()

    assert main(_argv()) == 0

    out = capsys.readouterr().out
    assert "recurred" in out
    assert "inserted" not in out
    assert _rows(
        test_db, "SELECT count(*), max(recurrence_count) FROM escaped_defects"
    )[0] == (1, 2)


def test_cli_json_output_is_machine_readable(test_db, capsys):
    assert main(_argv(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "inserted"
    assert payload["repo"] == "home-portal"
    assert payload["fingerprint"] == FP
    assert payload["detector"] == "pipeline-health"
    assert payload["recurrence_count"] == 1


def test_cli_dry_run_validates_but_writes_nothing(test_db, capsys):
    assert main(_argv(dry_run=True)) == 0

    out = capsys.readouterr().out
    assert "would file" in out
    assert _rows(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 0


def test_cli_dry_run_still_rejects_a_bad_fingerprint(test_db, capsys):
    rc = main(_argv(dry_run=True, fingerprint="junction-drift:152"))

    assert rc == 2
    assert "prefixed" in capsys.readouterr().err
    assert _rows(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 0


def test_cli_rejects_an_unprefixed_fingerprint(test_db, capsys):
    rc = main(_argv(fingerprint="junction-drift:152"))

    assert rc == 2
    assert "prefixed" in capsys.readouterr().err
    assert _rows(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 0


def test_cli_rejects_an_unknown_class(test_db, capsys):
    rc = main(_argv(**{"class": "not-a-class"}))

    assert rc == 2
    assert "defect_class" in capsys.readouterr().err


def test_cli_requires_the_documented_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--repo", "home-portal"])

    assert exc.value.code == 2
    assert "--class" in capsys.readouterr().err


def test_cli_defaults_to_a_manual_row(test_db, capsys):
    """A human filing without --detector gets discovery_source='manual'."""
    rc = main([
        "--repo", "tms",
        "--class", "convention",
        "--fingerprint", "manual:" + "c" * 64,
        "--description", "hand-filed finding",
        "--json",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "inserted"
    assert payload["detector"] is None
    assert _rows(
        test_db, "SELECT discovery_source FROM escaped_defects"
    ) == [("manual",)]


def test_cli_passes_introducing_pr_through(test_db, capsys):
    assert main(_argv(introducing_pr=102,
                      introducing_commit="deadbeef", json=True)) == 0
    capsys.readouterr()

    assert _rows(
        test_db,
        "SELECT introducing_pr, introducing_commit FROM escaped_defects",
    ) == [(102, "deadbeef")]


# ── the shell verb ───────────────────────────────────────────────


def test_bin_tms_routes_the_defect_verb():
    """`tms defect` must reach the intake module with the repo's lib on the
    path — the same PYTHONPATH shape every other python-backed verb uses."""
    text = BIN_TMS.read_text()
    assert "python3 -m tms.defects" in text
    assert "PYTHONPATH" in text.split("python3 -m tms.defects")[0].rsplit(
        ";;", 1)[-1]
    assert "tms defect --repo" in text, "the verb is not in the usage text"


def test_bin_tms_usage_line_lists_defect():
    text = BIN_TMS.read_text()
    usage_lines = [
        line for line in text.splitlines()
        if 'echo "Usage: tms [' in line
    ]
    assert usage_lines, "the top-level usage line is gone"
    assert "defect" in usage_lines[-1], (
        "`tms defect` is missing from the top-level usage line"
    )


# ── the one-way-to-get-it-wrong cases ────────────────────────────


def test_cli_rejects_a_detector_shaped_fingerprint_without_a_detector(
        test_db, capsys):
    """The P1 from review: `--fingerprint pipeline-health:152` with no
    `--detector` used to file a row that names a detector it does not have —
    `detector = NULL`, invisible to #159's sweep, permanently `new` because
    `regressed_at` requires a detector. Dry run and write must both refuse."""
    for extra in (["--dry-run"], []):
        rc = main(["--repo", "home-portal", "--class", "silent-failure",
                   "--fingerprint", "pipeline-health:152",
                   "--description", "a row nobody can re-check"] + extra)

        assert rc == 2, extra
        assert "manual:" in capsys.readouterr().err

    assert _rows(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 0


def test_cli_rejects_the_detector_source_without_a_detector(test_db, capsys):
    rc = main(_argv(detector=None, source="detector",
                    fingerprint="manual:" + "e" * 64))

    assert rc == 2
    assert "detector" in capsys.readouterr().err
    assert _rows(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 0


def test_cli_passes_severity_through(test_db, capsys):
    assert main(_argv(severity="major")) == 0
    capsys.readouterr()

    assert _rows(test_db, "SELECT severity FROM escaped_defects") == [
        ("major",)
    ]


def test_cli_rejects_an_unparseable_discovered_at(test_db, capsys):
    """`--discovered-at` used to pass a dry run and die inside postgres on a
    real write — a traceback and exit 1, neither documented status."""
    for extra in (["--dry-run"], []):
        rc = main(_argv(discovered_at="not-a-date") + extra)

        assert rc == 2, extra
        assert "discovered_at" in capsys.readouterr().err

    assert _rows(test_db, "SELECT count(*) FROM escaped_defects")[0][0] == 0


def test_cli_reports_a_regression(test_db, capsys):
    """The third outcome, through the CLI: a sighting of a verified row."""
    test_db().cursor().execute(
        """INSERT INTO escaped_defects (
               defect_id, repo, introducing_pr, introducing_commit,
               defect_class, severity, discovered_at, discovery_source,
               description, fingerprint, detector, last_seen_at,
               merged_at, verified_at)
           VALUES ('11111111-1111-1111-1111-111111111111', 'home-portal',
                   NULL, NULL, 'silent-failure', 'ungraded',
                   '2026-11-01T00:00:00+00:00', 'detector', 'first sight', ?,
                   'pipeline-health', '2026-11-01T00:00:00+00:00',
                   '2026-11-10T00:00:00+00:00',
                   '2026-11-11T00:00:00+00:00')""",
        (FP,),
    )
    test_db().commit()

    assert main(_argv(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "regressed"
    assert payload["regression_count"] == 1
    assert _rows(
        test_db,
        "SELECT count(*), regressed_at IS NOT NULL FROM escaped_defects",
    ) == [(1, 1)]


def test_cli_dry_run_plan_lists_every_field(test_db, capsys):
    """A dry run is the producer checklist's "lists what would be filed" — so
    it carries the fields, not just the identity."""
    assert main(_argv(dry_run=True, json=True, severity="major",
                      introducing_pr=102, introducing_commit="deadbeef",
                      discovered_at="2026-11-19T00:00:00+00:00")) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["outcome"] == "would-file"
    assert plan["description"] == (
        "Oathbringer has no book_topics/book_traditions rows")
    assert plan["severity"] == "major"
    assert plan["introducing_pr"] == 102
    assert plan["introducing_commit"] == "deadbeef"
    assert plan["discovered_at"] == "2026-11-19T00:00:00+00:00"


def test_cli_reports_a_driver_failure_as_exit_2(test_db, capsys, monkeypatch):
    """A connection error is the same operator-facing fact as a bad argument:
    "nothing was written, here is why" — not a traceback and exit 1."""
    def _boom():
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr("tms.defects._review_eval._get_conn", _boom)

    assert main(_argv()) == 2
    assert "could not connect" in capsys.readouterr().err


def test_bin_tms_executes_the_defect_verb(tmp_path):
    """Run the shell verb, not a slice of its text: a `;;` or PYTHONPATH edit
    that the string assertions above would happily pass breaks here."""
    import os
    import subprocess

    result = subprocess.run(
        [str(BIN_TMS), "defect",
         "--repo", "tms", "--class", "convention",
         "--fingerprint", "pipeline-health:152",
         "--description", "routing smoke test",
         "--dry-run"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    # exit 2 is the intake rejection the fingerprint rule produces; the point is
    # that the shell arm reached the module and its exit status propagated.
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "manual:" in result.stderr
