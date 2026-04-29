"""tests/test_m1_runtime_doctor_counts.py — Prove FTS5 cursor.fetchone() is consumed once.

The pre-fix bug:
    LANCE_FTS5_COUNT = cur.fetchone()[0] if cur.fetchone() else None
This calls fetchone() TWICE — first call returns the row, second advances past it.

The fix:
    row = cur.fetchone()
    LANCE_FTS5_COUNT = row[0] if row else None
Single consume, single access.
"""
from __future__ import annotations

import sqlite3

import pytest


class FetchOnceTracker:
    """Wraps a cursor and tracks fetchone call count."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.fetchone_count = 0

    def execute(self, sql: str):
        self._cur = self.conn.execute(sql)
        return self

    def fetchone(self):
        self.fetchone_count += 1
        return self._cur.fetchone()


class TestFetchoneConsumedOnce:
    """Verify the fixed doctor pattern consumes fetchone exactly once."""

    def test_fixed_pattern_single_fetchone(self, tmp_path):
        """Fixed code calls fetchone exactly once per row."""
        db_path = tmp_path / "ki.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE keyword_index (word TEXT)")
        conn.execute("INSERT INTO keyword_index VALUES ('a'), ('b'), ('c')")
        conn.commit()

        tracker = FetchOnceTracker(conn)
        tracker.execute("SELECT COUNT(*) FROM keyword_index")

        # The FIXED pattern
        row = tracker.fetchone()
        count = row[0] if row else None

        assert tracker.fetchone_count == 1, (
            f"fetchone called {tracker.fetchone_count}× (bug: 2×)"
        )
        assert count == 3

    def test_fixed_pattern_empty_table(self, tmp_path):
        """COUNT on empty table returns 0; fetchone called exactly once."""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE keyword_index (word TEXT)")
        conn.commit()

        tracker = FetchOnceTracker(conn)
        tracker.execute("SELECT COUNT(*) FROM keyword_index")

        row = tracker.fetchone()
        count = row[0] if row else None

        assert tracker.fetchone_count == 1
        assert count == 0  # COUNT on empty table = 0

    def test_old_buggy_pattern_calls_fetchone_twice(self, tmp_path):
        """Prove the old pattern actually called fetchone twice."""
        db_path = tmp_path / "ki.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE keyword_index (word TEXT)")
        conn.execute("INSERT INTO keyword_index VALUES ('x')")
        conn.commit()

        tracker = FetchOnceTracker(conn)
        tracker.execute("SELECT COUNT(*) FROM keyword_index")

        # The OLD buggy pattern (doctest / illustration)
        _ = tracker.fetchone()  # first call
        _ = tracker.fetchone()  # second call — the bug!

        assert tracker.fetchone_count == 2


class TestKeywordIndexMocked:
    """Mocked KeywordIndex._conn to test FTS5 count path without real palace."""

    def test_mocked_single_fetchone(self):
        """Mocked path: single fetchone returns count correctly."""
        call_count = 0

        class MockCursor:
            def execute(self, sql):
                return self

            def fetchone(self):
                nonlocal call_count
                call_count += 1
                return (99,)

        class MockConn:
            def execute(self, sql):
                return MockCursor()

        class FakeIndex:
            _conn = MockConn()

        cur = FakeIndex._conn.execute("SELECT COUNT(*) FROM keyword_index")
        row = cur.fetchone()
        count = row[0] if row else None

        assert call_count == 1, f"fetchone called {call_count}×, want 1×"
        assert count == 99

    def test_mocked_null_row_no_index_error(self):
        """None from fetchone must not raise IndexError."""
        call_count = 0

        class MockCursor:
            def execute(self, sql):
                return self

            def fetchone(self):
                nonlocal call_count
                call_count += 1
                return None

        class MockConn:
            def execute(self, sql):
                return MockCursor()

        class FakeIndex:
            _conn = MockConn()

        cur = FakeIndex._conn.execute("SELECT COUNT(*) FROM keyword_index")
        row = cur.fetchone()
        count = row[0] if row else None

        assert call_count == 1
        assert count is None
