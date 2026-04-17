"""
decision_tracker.py — Structured decision capture with confidence tracking.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# TTL defaults
_DEFAULT_TTL_SECONDS = 86400 * 90  # 90 days

# Schema
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA locking_mode=normal;
PRAGMA check_same_thread=False;

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    category TEXT NOT NULL,
    decision_text TEXT NOT NULL,
    rationale TEXT NOT NULL,
    alternatives TEXT DEFAULT '[]',
    confidence INTEGER,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    superseded_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_session ON decisions(session_id);
CREATE INDEX IF NOT EXISTS idx_decisions_category ON decisions(category);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_decisions_expires ON decisions(expires_at);
"""

_UPSERT_DECISION = """
INSERT OR REPLACE INTO decisions (id, session_id, category, decision_text, rationale, alternatives, confidence, status, created_at, expires_at, superseded_by)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_BY_ID = """
SELECT id, session_id, category, decision_text, rationale, alternatives, confidence, status, created_at, expires_at, superseded_by
FROM decisions WHERE id=?
"""

_SELECT_BY_SESSION = """
SELECT id, session_id, category, decision_text, rationale, alternatives, confidence, status, created_at, expires_at, superseded_by
FROM decisions WHERE session_id=? ORDER BY created_at DESC
"""

_SELECT_BY_CATEGORY = """
SELECT id, session_id, category, decision_text, rationale, alternatives, confidence, status, created_at, expires_at, superseded_by
FROM decisions WHERE category=? ORDER BY created_at DESC
"""

_SELECT_BY_STATUS = """
SELECT id, session_id, category, decision_text, rationale, alternatives, confidence, status, created_at, expires_at, superseded_by
FROM decisions WHERE status=? ORDER BY created_at DESC
"""

_SELECT_RECENT = """
SELECT id, session_id, category, decision_text, rationale, alternatives, confidence, status, created_at, expires_at, superseded_by
FROM decisions ORDER BY created_at DESC LIMIT ?
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_ts() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(ttl_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


def _row_to_decision(row: tuple) -> dict:
    return {
        "id": row[0],
        "session_id": row[1],
        "category": row[2],
        "decision_text": row[3],
        "rationale": row[4],
        "alternatives": json.loads(row[5]) if row[5] else [],
        "confidence": row[6],
        "status": row[7],
        "created_at": row[8],
        "expires_at": row[9],
        "superseded_by": row[10],
    }


class DecisionTracker:
    """Structured decision capture with confidence tracking."""

    def __init__(self, palace_path: Optional[str] = None):
        if palace_path is None:
            palace_path = os.environ.get("MEMPALACE_PATH", os.path.expanduser("~/.mempalace"))
        self._db_path = str(Path(palace_path) / "decision_tracker.sqlite3")
        self._write_lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._local = threading.local()
        self._connect()
        self._initialize()

    def _connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn

    @property
    def _conn_ctx(self):
        """Return a thread-local connection."""
        try:
            return self._local.conn
        except AttributeError:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            return conn

    def _initialize(self) -> None:
        with self._write_lock:
            self._conn_ctx.executescript(_SCHEMA)
            self._conn_ctx.commit()

    def capture_decision(
        self,
        session_id: str,
        decision_text: str,
        rationale: str,
        alternatives: list[str],
        category: str,
        confidence: int,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> dict:
        """Store a decision."""
        decision_id = str(uuid.uuid4())
        now = _utc_now()
        expires = _expires_at(ttl_seconds) if ttl_seconds else None

        with self._write_lock:
            conn = self._conn_ctx
            conn.execute(
                _UPSERT_DECISION,
                (
                    decision_id,
                    session_id,
                    category,
                    decision_text,
                    rationale,
                    json.dumps(alternatives),
                    confidence,
                    "active",
                    now,
                    expires,
                    None,
                ),
            )
            conn.commit()

        return {
            "success": True,
            "decision_id": decision_id,
            "category": category,
            "confidence": confidence,
            "created_at": now,
            "expires_at": expires,
        }

    def list_decisions(
        self,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query decisions by session/category/status."""
        with self._conn_ctx as conn:
            if session_id:
                cursor = conn.execute(_SELECT_BY_SESSION, (session_id,))
            elif category:
                cursor = conn.execute(_SELECT_BY_CATEGORY, (category,))
            elif status:
                cursor = conn.execute(_SELECT_BY_STATUS, (status,))
            else:
                cursor = conn.execute(_SELECT_RECENT, (limit,))

            rows = cursor.fetchall()
            decisions = [_row_to_decision(r) for r in rows]

        # Apply limit if fetching without filter (recent)
        if session_id is None and category is None and status is None:
            decisions = decisions[:limit]

        # Filter by status if both session_id and status given
        if session_id and status:
            decisions = [d for d in decisions if d["status"] == status]

        return decisions

    def supersede_decision(
        self,
        decision_id: str,
        superseding_decision_id: str,
        session_id: str,
    ) -> dict:
        """Mark a decision as superseded by another decision."""
        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute("SELECT session_id FROM decisions WHERE id=?", (decision_id,))
            row = cursor.fetchone()

            if row is None:
                return {"success": False, "error": "decision_not_found"}

            # Only the session that created the decision can supersede it
            if row[0] != session_id:
                return {"success": False, "error": "not_owner"}

            cursor2 = conn.execute("SELECT id FROM decisions WHERE id=?", (superseding_decision_id,))
            if cursor2.fetchone() is None:
                return {"success": False, "error": "superseding_decision_not_found"}

            conn.execute(
                "UPDATE decisions SET status='superseded', superseded_by=? WHERE id=?",
                (superseding_decision_id, decision_id),
            )
            conn.commit()

        return {"success": True, "superseded_by": superseding_decision_id}

    def get_decision(self, decision_id: str) -> Optional[dict]:
        """Get single decision by ID."""
        with self._conn_ctx as conn:
            cursor = conn.execute(_SELECT_BY_ID, (decision_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return _row_to_decision(row)

    def close(self) -> None:
        with self._write_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            try:
                conn = self._local.__dict__.get("conn")
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def __repr__(self) -> str:
        return f"<DecisionTracker db_path={self._db_path!r}>"