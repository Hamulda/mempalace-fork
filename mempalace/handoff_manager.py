"""
handoff_manager.py — Cross-session handoff protocol with TTL and status tracking.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# TTL defaults
_DEFAULT_TTL_SECONDS = 86400 * 3  # 3 days

# Status values
_STATUS_PENDING = "pending"
_STATUS_ACCEPTED = "accepted"
_STATUS_COMPLETED = "completed"
_STATUS_EXPIRED = "expired"
_STATUS_CANCELLED = "cancelled"

# Schema
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA locking_mode=normal;
PRAGMA check_same_thread=False;

CREATE TABLE IF NOT EXISTS handoffs (
    id TEXT PRIMARY KEY,
    from_session_id TEXT NOT NULL,
    to_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT DEFAULT 'normal',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    completed_at TEXT,
    payload TEXT DEFAULT '{}',
    touched_paths TEXT DEFAULT '[]',
    blockers TEXT DEFAULT '[]',
    next_steps TEXT DEFAULT '[]',
    confidence INTEGER,
    summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_handoffs_from ON handoffs(from_session_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_to ON handoffs(to_session_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(status);
CREATE INDEX IF NOT EXISTS idx_handoffs_expires ON handoffs(expires_at);
"""

_UPSERT_HANDOFF = """
INSERT OR REPLACE INTO handoffs (id, from_session_id, to_session_id, status, priority, created_at, expires_at, payload, touched_paths, blockers, next_steps, confidence, summary)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_BY_ID = """
SELECT id, from_session_id, to_session_id, status, priority, created_at, expires_at, accepted_at, completed_at, payload, touched_paths, blockers, next_steps, confidence, summary
FROM handoffs WHERE id=?
"""

_SELECT_BY_SESSION = """
SELECT id, from_session_id, to_session_id, status, priority, created_at, expires_at, accepted_at, completed_at, payload, touched_paths, blockers, next_steps, confidence, summary
FROM handoffs WHERE from_session_id=? OR to_session_id=?
ORDER BY created_at DESC
"""

_SELECT_BY_STATUS = """
SELECT id, from_session_id, to_session_id, status, priority, created_at, expires_at, accepted_at, completed_at, payload, touched_paths, blockers, next_steps, confidence, summary
FROM handoffs WHERE status=?
ORDER BY created_at DESC LIMIT ?
"""

_SELECT_PENDING = """
SELECT id, from_session_id, to_session_id, status, priority, created_at, expires_at, accepted_at, completed_at, payload, touched_paths, blockers, next_steps, confidence, summary
FROM handoffs WHERE status IN ('pending', 'accepted') AND expires_at > ?
ORDER BY created_at DESC
"""

_SELECT_EXPIRED = """
SELECT id FROM handoffs WHERE expires_at <= ? AND status NOT IN ('completed', 'cancelled')
"""

_UPDATE_STATUS = """
UPDATE handoffs SET status=?, completed_at=? WHERE id=?
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_ts() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(ttl_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


def _row_to_handoff(row: tuple) -> dict:
    return {
        "id": row[0],
        "from_session_id": row[1],
        "to_session_id": row[2],
        "status": row[3],
        "priority": row[4],
        "created_at": row[5],
        "expires_at": row[6],
        "accepted_at": row[7],
        "completed_at": row[8],
        "payload": json.loads(row[9]) if row[9] else {},
        "touched_paths": json.loads(row[10]) if row[10] else [],
        "blockers": json.loads(row[11]) if row[11] else [],
        "next_steps": json.loads(row[12]) if row[12] else [],
        "confidence": row[13],
        "summary": row[14],
    }


class HandoffManager:
    """Cross-session handoff protocol with TTL and status tracking."""

    def __init__(self, palace_path: Optional[str] = None):
        if palace_path is None:
            palace_path = os.environ.get("MEMPALACE_PATH", os.path.expanduser("~/.mempalace"))
        self._db_path = str(Path(palace_path) / "handoff_manager.sqlite3")
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
        self.cleanup_expired()

    def push_handoff(
        self,
        from_session_id: str,
        summary: str,
        touched_paths: list[str],
        blockers: list[str],
        next_steps: list[str],
        confidence: int,
        priority: str = "normal",
        to_session_id: Optional[str] = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> dict:
        """Create a new handoff.

        to_session_id=None creates a broadcast handoff — any session can accept it.
        to_session_id="X" creates a directed handoff — only session X can accept it.
        """
        handoff_id = str(uuid.uuid4())
        now = _utc_now()
        expires = _expires_at(ttl_seconds)

        with self._write_lock:
            conn = self._conn_ctx
            conn.execute(
                _UPSERT_HANDOFF,
                (
                    handoff_id,
                    from_session_id,
                    to_session_id,
                    _STATUS_PENDING,
                    priority,
                    now,
                    expires,
                    "{}",
                    json.dumps(touched_paths),
                    json.dumps(blockers),
                    json.dumps(next_steps),
                    confidence,
                    summary,
                ),
            )
            conn.commit()

        return {
            "success": True,
            "handoff_id": handoff_id,
            "status": _STATUS_PENDING,
            "expires_at": expires,
        }

    def pull_handoffs(
        self,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """List handoffs filtered by session and/or status.

        - session_id=X: returns handoffs where from_session_id=X OR to_session_id=X
          (both sent and received directed handoffs for X).
        - session_id=None + status=X: returns handoffs with that status.
        - session_id=None (no status): returns broadcast handoffs
          (to_session_id IS NULL), filtered to pending status.
        """
        self.cleanup_expired()

        with self._conn_ctx as conn:
            if session_id is not None:
                cursor = conn.execute(_SELECT_BY_SESSION, (session_id, session_id))
                rows = cursor.fetchall()
            elif status:
                cursor = conn.execute(_SELECT_BY_STATUS, (status, limit))
                rows = cursor.fetchall()
            else:
                # Broadcast: to_session_id is NULL
                cursor = conn.execute(
                    "SELECT id, from_session_id, to_session_id, status, priority, created_at, expires_at, accepted_at, completed_at, payload, touched_paths, blockers, next_steps, confidence, summary "
                    "FROM handoffs WHERE to_session_id IS NULL AND status=? ORDER BY created_at DESC LIMIT ?",
                    (_STATUS_PENDING, limit),
                )
                rows = cursor.fetchall()

            return [_row_to_handoff(r) for r in rows]

    def accept_handoff(self, handoff_id: str, session_id: str) -> dict:
        """Mark handoff as accepted by session."""
        self.cleanup_expired()
        now = _utc_now()

        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute("SELECT status, to_session_id FROM handoffs WHERE id=?", (handoff_id,))
            row = cursor.fetchone()

            if row is None:
                return {"success": False, "error": "handoff_not_found"}

            current_status = row[0]
            expected_to = row[1]

            if current_status not in (_STATUS_PENDING,):
                return {"success": False, "error": f"cannot_accept_status_{current_status}"}

            # Broadcast handoffs (to_session_id=None) can be accepted by any session
            # Directed handoffs can only be accepted by the target session
            if expected_to is not None and expected_to != session_id:
                return {"success": False, "error": "not_target_session"}

            conn.execute(
                "UPDATE handoffs SET status=?, accepted_at=?, to_session_id=? WHERE id=?",
                (_STATUS_ACCEPTED, now, session_id, handoff_id),
            )
            conn.commit()

        return {"success": True, "status": _STATUS_ACCEPTED, "accepted_at": now}

    def complete_handoff(self, handoff_id: str, session_id: str) -> dict:
        """Mark handoff as completed. Either from_session_id or to_session_id can complete."""
        self.cleanup_expired()
        now = _utc_now()

        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute("SELECT status, from_session_id, to_session_id FROM handoffs WHERE id=?", (handoff_id,))
            row = cursor.fetchone()

            if row is None:
                return {"success": False, "error": "handoff_not_found"}

            current_status = row[0]
            from_sid = row[1]
            to_sid = row[2]

            if current_status not in (_STATUS_PENDING, _STATUS_ACCEPTED):
                return {"success": False, "error": f"cannot_complete_status_{current_status}"}

            # Only from or to session can complete
            if session_id != from_sid and session_id != to_sid:
                return {"success": False, "error": "not_participant"}

            conn.execute(
                "UPDATE handoffs SET status=?, completed_at=? WHERE id=?",
                (_STATUS_COMPLETED, now, handoff_id),
            )
            conn.commit()

        return {"success": True, "status": _STATUS_COMPLETED, "completed_at": now}

    def cancel_handoff(self, handoff_id: str, session_id: str) -> dict:
        """Cancel a handoff. Only the owner (from_session_id) can cancel."""
        self.cleanup_expired()
        now = _utc_now()

        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute("SELECT status, from_session_id FROM handoffs WHERE id=?", (handoff_id,))
            row = cursor.fetchone()

            if row is None:
                return {"success": False, "error": "handoff_not_found"}

            if row[0] not in (_STATUS_PENDING, _STATUS_ACCEPTED):
                return {"success": False, "error": f"cannot_cancel_status_{row[0]}"}

            if row[1] != session_id:
                return {"success": False, "error": "not_owner"}

            conn.execute(
                "UPDATE handoffs SET status=?, completed_at=? WHERE id=?",
                (_STATUS_CANCELLED, now, handoff_id),
            )
            conn.commit()

        return {"success": True, "status": _STATUS_CANCELLED}

    def get_handoff(self, handoff_id: str) -> Optional[dict]:
        """Get single handoff with full payload."""
        self.cleanup_expired()
        with self._conn_ctx as conn:
            cursor = conn.execute(_SELECT_BY_ID, (handoff_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return _row_to_handoff(row)

    def get_handoffs_for_session(self, session_id: str) -> list[dict]:
        """All handoffs involving session (sent or received)."""
        self.cleanup_expired()
        with self._conn_ctx as conn:
            cursor = conn.execute(_SELECT_BY_SESSION, (session_id, session_id))
            rows = cursor.fetchall()
            return [_row_to_handoff(r) for r in rows]

    def list_pending_handoffs(self, project_root: Optional[str] = None) -> list[dict]:
        """For takeover planning — list all pending/accepted unexpired handoffs."""
        self.cleanup_expired()
        now = _utc_now()

        with self._conn_ctx as conn:
            cursor = conn.execute(_SELECT_PENDING, (now,))
            rows = cursor.fetchall()
            handoffs = [_row_to_handoff(r) for r in rows]

        if project_root:
            handoffs = [
                h for h in handoffs
                if any(p.startswith(project_root) for p in h.get("touched_paths", []))
            ]

        return handoffs

    def cleanup_expired(self) -> dict:
        """Mark expired handoffs. Returns count of marked."""
        now = _utc_now()
        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute(
                "UPDATE handoffs SET status=? WHERE expires_at <= ? AND status NOT IN (?, ?, ?)",
                (_STATUS_EXPIRED, now, _STATUS_COMPLETED, _STATUS_CANCELLED, _STATUS_EXPIRED),
            )
            conn.commit()
            return {"expired": cursor.rowcount}

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
        return f"<HandoffManager db_path={self._db_path!r}>"