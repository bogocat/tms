"""Shared test fixtures for tms tests.

Provides a test_db fixture that monkeypatches _get_conn() to return a
sqlite3 in-memory connection — same pattern as test_review_eval.py.
All tests within a module share the same in-memory database.
"""

import sqlite3

import pytest


# -- events table schema (sqlite3-compatible) --

_CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    repo            TEXT,
    issue           INTEGER,
    agent           TEXT,
    provider        TEXT,
    model           TEXT,
    dispatch_type   TEXT,
    worktree        TEXT,
    session         TEXT,
    aoe_id_prefix   TEXT,
    reason          TEXT,
    from_status     TEXT,
    to_status       TEXT,
    issue_labels    TEXT,
    point_estimate  TEXT,
    area            TEXT,
    blocked_class   TEXT,
    payload         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_unique
    ON events (event_type, aoe_id_prefix, event_timestamp);
"""


@pytest.fixture
def test_db(monkeypatch):
    """Replace _get_conn() with sqlite3 in-memory, with events table.

    All connections within a single test share the same in-memory
    database, so data written by append_event() etc. is visible
    to the test's query.

    Usage:
        def test_something(test_db):
            from tms.events import append_event
            append_event({"event_type": "dispatch", ...})
            conn = test_db()
            rows = conn.cursor().execute("SELECT * FROM events").fetchall()
    """
    from tms import events as events_mod
    _orig_events = events_mod._get_conn

    try:
        from tms import dispatch_monitor as dm_mod
        _orig_dm = dm_mod._get_conn
    except ImportError:
        dm_mod = None
        _orig_dm = None

    _conn = sqlite3.connect(":memory:")
    for stmt in _CREATE_EVENTS_TABLE.split(";"):
        stmt = stmt.strip()
        if stmt:
            _conn.execute(stmt)
    _conn.commit()

    class _SharedCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=None):
            import re
            sql = sql.replace("tms_review.", "")
            # psycopg2 pyformat (%(name)s) → sqlite3 named (:name)
            sql = re.sub(r'%\(([^)]+)\)s', r':\1', sql)
            # psycopg2 format (%s) → sqlite3 positional (?)
            sql = sql.replace("%s", "?")
            if params is not None:
                return self._cur.execute(sql, params)
            return self._cur.execute(sql)

        def fetchall(self):
            return self._cur.fetchall()

        def fetchone(self):
            return self._cur.fetchone()

        @property
        def description(self):
            return self._cur.description

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class _SharedConnection:
        def cursor(self):
            return _SharedCursor(_conn.cursor())

        def commit(self):
            _conn.commit()

        def close(self):
            pass  # shared — don't close

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def _make_conn():
        return _SharedConnection()

    monkeypatch.setattr(events_mod, "_get_conn", _make_conn)
    if dm_mod is not None:
        monkeypatch.setattr(dm_mod, "_get_conn", _make_conn)
    yield _make_conn
    monkeypatch.setattr(events_mod, "_get_conn", _orig_events)
    if dm_mod is not None:
        monkeypatch.setattr(dm_mod, "_get_conn", _orig_dm)
    _conn.close()
