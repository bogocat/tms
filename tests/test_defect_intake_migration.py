"""Migration 009 — defect intake needs nullable introducing_pr/commit (tms#157).

**These are documentation guards, not behaviour guards.** Every test here greps
``009-defect-intake-nullable.sql`` for a phrase; a grep matches a commented-out
``ALTER COLUMN … DROP NOT NULL`` as happily as a live one, so a reviewer who
comments the DDL out will see this file stay green (verified — that experiment
was run against this suite). What they do pin is that the file keeps claiming
what it claims: the application path, the two columns, one transaction, and the
scope limit.

The behaviour lives in ``tests/test_defect_intake_migration_db.py``, which
applies 009 to a throwaway database and asserts ``attnotnull = 'f'``, a
detector-row insert, re-run safety and the surviving 008 CHECKs. One of its
tests fails if either ALTER is disabled.
"""

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "schema" / "migrations"
MIGRATION_009 = MIGRATIONS / "009-defect-intake-nullable.sql"


def test_migration_file_exists():
    assert MIGRATION_009.is_file(), "missing schema/migrations/009-*.sql"


def test_header_documents_application_path():
    text = MIGRATION_009.read_text()
    assert "psql service=pg-superuser -f" in text


def test_drops_not_null_on_the_introducing_columns():
    text = MIGRATION_009.read_text()
    for column in ("introducing_pr", "introducing_commit"):
        assert re.search(
            r"ALTER COLUMN\s+" + column + r"\s+DROP NOT NULL",
            text,
        ), f"{column} is never made nullable"


def test_is_wrapped_in_one_transaction():
    text = MIGRATION_009.read_text()
    assert text.count("BEGIN;") == 1
    assert text.count("COMMIT;") == 1
    assert text.index("BEGIN;") < text.index("COMMIT;")


def test_does_not_weaken_any_other_constraint():
    """009 is a narrow schema fix. It must not touch the 008 status-arm
    CHECKs, the unique index, or the defect columns themselves."""
    text = MIGRATION_009.read_text()
    for forbidden in (
        "DROP CONSTRAINT",
        "DROP INDEX",
        "DROP COLUMN",
        "DROP TABLE",
        "ADD COLUMN",
    ):
        assert forbidden not in text, (
            f"009 must not contain {forbidden!r} — scope is the two NOT NULLs"
        )


def test_documents_why_null_is_the_honest_value():
    text = MIGRATION_009.read_text()
    # The reasoning is the thing a future reader needs; pin that it is there.
    assert "no introducing pr" in text.lower()
    assert "required by" in text.lower()


def test_readme_documents_applying_009():
    """#156 set the precedent: the documented apply path is asserted, not
    assumed. An operator who follows the README to 008 and stops leaves the
    register unable to accept a detector-filed row."""
    readme = (MIGRATIONS.parent.parent / "README.md").read_text()
    assert "009-defect-intake-nullable.sql" in readme
    assert "introducing_pr" in readme, (
        "the README does not explain why the introducing columns are nullable"
    )
    apply_block = readme.split("# Apply (documented path)", 1)[-1]
    assert apply_block.index("008-defect-fingerprint.sql") < \
        apply_block.index("009-defect-intake-nullable.sql"), (
        "009 is documented before 008"
    )
