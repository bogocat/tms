"""Migration 009 — defect intake needs nullable introducing_pr/commit (tms#157).

Static shape guards only: hermetic, no database needed. The DB application is
verified the same way #156's migration was (scratch database, see
``tests/test_defect_register_migration.py`` for that pattern) — this file pins
the promises the migration file makes so a rewrite cannot silently drop them.
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
