"""
claims_manager.py — TTL-based path claim system with conflict detection.
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
_DEFAULT_TTL_SECONDS = 600  # 10 minutes

# Schema
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA locking_mode=normal;
PRAGMA check_same_thread=False;

CREATE TABLE IF NOT EXISTS claims (
    session_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    revision TEXT,
    claimed_at TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    expires_at TEXT NOT NULL,
    PRIMARY KEY (session_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_claims_target ON claims(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_claims_expires ON claims(expires_at);

CREATE TABLE IF NOT EXISTS claim_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_session ON claim_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_target ON claim_events(target_type, target_id);
"""

_CLAIM_UPSERT = """
INSERT OR REPLACE INTO claims (session_id, target_type, target_id, revision, claimed_at, payload, expires_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_CLAIM = """
SELECT session_id, target_type, target_id, revision, claimed_at, payload, expires_at
FROM claims WHERE target_type=? AND target_id=?
"""

_SELECT_SESSION_CLAIMS = """
SELECT session_id, target_type, target_id, revision, claimed_at, payload, expires_at
FROM claims WHERE session_id=?
"""

_SELECT_ALL_ACTIVE = """
SELECT session_id, target_type, target_id, revision, claimed_at, payload, expires_at
FROM claims WHERE expires_at > ?
"""

_SELECT_CONFLICT = """
SELECT session_id, target_type, target_id, revision, claimed_at, payload, expires_at
FROM claims WHERE target_type=? AND target_id=? AND session_id != ?
AND expires_at > ?
"""

_INSERT_EVENT = """
INSERT INTO claim_events (session_id, target_type, target_id, action, timestamp, payload)
VALUES (?, ?, ?, ?, ?, ?)
"""

_DELETE_EXPIRED = """
DELETE FROM claims WHERE expires_at <= ?
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_ts() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(ttl_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


class ClaimsManager:
    """TTL-based path claim system with conflict detection and event audit."""

    def __init__(self, palace_path: Optional[str] = None):
        if palace_path is None:
            palace_path = os.environ.get("MEMPALACE_PATH", os.path.expanduser("~/.mempalace"))
        self._db_path = str(Path(palace_path) / "claims_manager.sqlite3")
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

    def _log_event(self, session_id: str, target_type: str, target_id: str, action: str, payload: Optional[dict] = None) -> None:
        """Log a claim event to the audit trail."""
        try:
            with self._write_lock:
                self._conn_ctx.execute(
                    _INSERT_EVENT,
                    (session_id, target_type, target_id, action, _utc_now(), json.dumps(payload or {}))
                )
                self._conn_ctx.commit()
        except Exception:
            pass

    def claim(
        self,
        target_type: str,
        target_id: str,
        session_id: str,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        payload: Optional[dict] = None,
    ) -> dict:
        """Acquire or refresh a claim on (target_type, target_id).

        - If no claim exists: acquires one for session_id.
        - If session_id already holds it: refreshes TTL (renew).
        - If another session holds it: returns conflict, does NOT store a claim.

        Returns: {"acquired": bool, "owner": session_id or str, "expires_at": str}
        """
        payload = payload or {}
        now = _utc_now()
        expires = _expires_at(ttl_seconds)

        # Cleanup expired first
        self.cleanup_expired()

        with self._write_lock:
            conn = self._conn_ctx
            # Check existing unexpired claim
            cursor = conn.execute(
                "SELECT session_id, expires_at FROM claims WHERE target_type=? AND target_id=? AND expires_at > ?",
                (target_type, target_id, now),
            )
            row = cursor.fetchone()

            if row is not None:
                owner = row[0]
                if owner == session_id:
                    # Refresh TTL for self
                    conn.execute(
                        "UPDATE claims SET expires_at=?, payload=?, claimed_at=? WHERE target_type=? AND target_id=?",
                        (expires, json.dumps(payload), now, target_type, target_id),
                    )
                    conn.commit()
                    self._log_event(session_id, target_type, target_id, "renew", payload)
                    return {"acquired": True, "owner": session_id, "expires_at": expires}
                else:
                    # Another session holds it
                    self._log_event(session_id, target_type, target_id, "conflict", {"blocked_by": owner})
                    return {"acquired": False, "owner": owner, "expires_at": row[1]}

            # No active claim — acquire
            conn.execute(
                _CLAIM_UPSERT,
                (session_id, target_type, target_id, None, now, json.dumps(payload), expires),
            )
            conn.commit()
            self._log_event(session_id, target_type, target_id, "claim", payload)
            return {"acquired": True, "owner": session_id, "expires_at": expires}

    def renew_claim(
        self,
        target_type: str,
        target_id: str,
        session_id: str,
        ttl_seconds: Optional[int] = None,
    ) -> dict:
        """Extend TTL of an existing claim. Only owner can renew."""
        if ttl_seconds is None:
            ttl_seconds = _DEFAULT_TTL_SECONDS

        now = _utc_now()
        expires = _expires_at(ttl_seconds)

        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute(
                "SELECT session_id FROM claims WHERE target_type=? AND target_id=? AND expires_at > ?",
                (target_type, target_id, now),
            )
            row = cursor.fetchone()

            if row is None:
                return {"success": False, "error": "no_active_claim"}

            if row[0] != session_id:
                return {"success": False, "error": "not_owner", "owner": row[0]}

            conn.execute(
                "UPDATE claims SET expires_at=?, claimed_at=? WHERE target_type=? AND target_id=?",
                (expires, now, target_type, target_id),
            )
            conn.commit()
            self._log_event(session_id, target_type, target_id, "renew", {"ttl_seconds": ttl_seconds})
            return {"success": True, "expires_at": expires}

    def release_claim(
        self,
        target_type: str,
        target_id: str,
        session_id: str,
    ) -> dict:
        """Release a claim. Only owner can release."""
        now = _utc_now()

        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute(
                "SELECT session_id FROM claims WHERE target_type=? AND target_id=? AND expires_at > ?",
                (target_type, target_id, now),
            )
            row = cursor.fetchone()

            if row is None:
                return {"success": False, "error": "no_active_claim"}

            if row[0] != session_id:
                return {"success": False, "error": "not_owner", "owner": row[0]}

            conn.execute(
                "DELETE FROM claims WHERE target_type=? AND target_id=?",
                (target_type, target_id),
            )
            conn.commit()
            self._log_event(session_id, target_type, target_id, "release", {})
            return {"success": True}

    def get_claim(self, target_type: str, target_id: str) -> Optional[dict]:
        """Get current unexpired claim, or None."""
        self.cleanup_expired()
        now = _utc_now()

        with self._conn_ctx as conn:
            cursor = conn.execute(
                "SELECT session_id, target_type, target_id, revision, claimed_at, payload, expires_at "
                "FROM claims WHERE target_type=? AND target_id=? AND expires_at > ?",
                (target_type, target_id, now),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "session_id": row[0],
                "target_type": row[1],
                "target_id": row[2],
                "revision": row[3],
                "claimed_at": row[4],
                "payload": json.loads(row[5]),
                "expires_at": row[6],
            }

    def get_session_claims(self, session_id: str) -> list[dict]:
        """Get all unexpired claims held by a session.

        Calls cleanup_expired() first to purge stale claims from storage.
        Only returns claims where expires_at > now.
        """
        self.cleanup_expired()
        now = _utc_now()

        with self._conn_ctx as conn:
            cursor = conn.execute(
                "SELECT session_id, target_type, target_id, revision, claimed_at, payload, expires_at "
                "FROM claims WHERE session_id=? AND expires_at > ?",
                (session_id, now),
            )
            rows = cursor.fetchall()
            return [
                {
                    "session_id": r[0],
                    "target_type": r[1],
                    "target_id": r[2],
                    "revision": r[3],
                    "claimed_at": r[4],
                    "payload": json.loads(r[5]),
                    "expires_at": r[6],
                }
                for r in rows
            ]

    def get_claims_for_target(self, target_type: str, target_id: str, include_expired: bool = True) -> list[dict]:
        """Get claims for a target.

        Note: due to INSERT OR REPLACE semantics, this returns the last-known state
        per (session_id, target_type, target_id) — not a full history of all attempts.
        Conflict events are recorded in claim_events separately.

        include_expired=True: returns all stored claims (active and expired).
        include_expired=False: returns only currently unexpired claims.
        """
        with self._conn_ctx as conn:
            if include_expired:
                cursor = conn.execute(
                    "SELECT session_id, target_type, target_id, revision, claimed_at, payload, expires_at "
                    "FROM claims WHERE target_type=? AND target_id=? ORDER BY claimed_at DESC",
                    (target_type, target_id),
                )
            else:
                now = _utc_now()
                cursor = conn.execute(
                    "SELECT session_id, target_type, target_id, revision, claimed_at, payload, expires_at "
                    "FROM claims WHERE target_type=? AND target_id=? AND expires_at > ? ORDER BY claimed_at DESC",
                    (target_type, target_id, now),
                )
            rows = cursor.fetchall()
            return [
                {
                    "session_id": r[0],
                    "target_type": r[1],
                    "target_id": r[2],
                    "revision": r[3],
                    "claimed_at": r[4],
                    "payload": json.loads(r[5]),
                    "expires_at": r[6],
                }
                for r in rows
            ]

    def list_active_claims(self, project_root: Optional[str] = None) -> list[dict]:
        """List all unexpired claims across all sessions.

        Calls cleanup_expired() first. Optionally filtered by project_root prefix.
        """
        self.cleanup_expired()
        now = _utc_now()

        with self._conn_ctx as conn:
            cursor = conn.execute(_SELECT_ALL_ACTIVE, (now,))
            rows = cursor.fetchall()
            claims = [
                {
                    "session_id": r[0],
                    "target_type": r[1],
                    "target_id": r[2],
                    "revision": r[3],
                    "claimed_at": r[4],
                    "payload": json.loads(r[5]),
                    "expires_at": r[6],
                }
                for r in rows
            ]

        if project_root:
            claims = [c for c in claims if c["target_id"].startswith(project_root)]

        return claims

    def check_conflicts(self, target_type: str, target_id: str, session_id: str) -> dict:
        """Returns conflict info if another session holds a claim."""
        self.cleanup_expired()
        now = _utc_now()

        with self._conn_ctx as conn:
            cursor = conn.execute(
                "SELECT session_id, expires_at FROM claims WHERE target_type=? AND target_id=? AND expires_at > ?",
                (target_type, target_id, now),
            )
            row = cursor.fetchone()

            if row is None:
                return {"has_conflict": False}

            if row[0] == session_id:
                return {"has_conflict": False, "owner": session_id, "is_self": True}

            return {
                "has_conflict": True,
                "owner": row[0],
                "expires_at": row[1],
            }

    def claim_with_handoff(
        self,
        target_type: str,
        target_id: str,
        session_id: str,
        handoff_payload: dict,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> dict:
        """Atomic claim + handoff event. Fails if another session holds an active claim.

        Unlike claim(), this does not refresh TTL for self — it is strictly an
        acquisition attempt. Logs a 'handoff_claim' event on success.
        """
        payload = handoff_payload or {}
        now = _utc_now()
        expires = _expires_at(ttl_seconds)

        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute(
                "SELECT session_id FROM claims WHERE target_type=? AND target_id=? AND expires_at > ?",
                (target_type, target_id, now),
            )
            row = cursor.fetchone()

            if row is not None and row[0] != session_id:
                return {"acquired": False, "owner": row[0], "error": "conflict"}

            conn.execute(
                _CLAIM_UPSERT,
                (session_id, target_type, target_id, None, now, json.dumps(payload), expires),
            )
            conn.commit()
            self._log_event(session_id, target_type, target_id, "handoff_claim", payload)
            return {"acquired": True, "owner": session_id, "expires_at": expires}

    def cleanup_expired(self) -> dict:
        """Delete expired claims from storage. Returns count of deleted rows.

        An expired claim is one where expires_at <= now. This is called
        automatically before claim operations but can be invoked manually.
        """
        now = _utc_now()
        with self._write_lock:
            conn = self._conn_ctx
            cursor = conn.execute(_DELETE_EXPIRED, (now,))
            conn.commit()
            return {"removed": cursor.rowcount}

    def get_recent_events(self, session_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Get recent claim events, optionally filtered by session."""
        with self._conn_ctx as conn:
            if session_id:
                cursor = conn.execute(
                    "SELECT id, session_id, target_type, target_id, action, timestamp, payload "
                    "FROM claim_events WHERE session_id=? ORDER BY timestamp DESC LIMIT ?",
                    (session_id, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, session_id, target_type, target_id, action, timestamp, payload "
                    "FROM claim_events ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "session_id": r[1],
                    "target_type": r[2],
                    "target_id": r[3],
                    "action": r[4],
                    "timestamp": r[5],
                    "payload": json.loads(r[6]),
                }
                for r in rows
            ]

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
        return f"<ClaimsManager db_path={self._db_path!r}>"