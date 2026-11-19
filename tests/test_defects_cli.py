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
