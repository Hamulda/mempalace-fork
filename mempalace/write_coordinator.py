"""
WriteCoordinator — SQLite WAL-based write arbiter for MemPalace.

Phase 1 provides a single-writer / write-arbiter foundation using SQLite WAL
(M1-friendly, no Redis/Kafka). Coordinates claims, handoffs, revision metadata,
and cache invalidation signals with an on-disk intent journal for multi-session
crash recovery.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Schema constants
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA locking_mode=normal;
PRAGMA check_same_thread=False;

CREATE TABLE IF NOT EXISTS write_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    committed_at TEXT,
    FOREIGN KEY (session_id) REFERENCES write_intents(session_id)
);

CREATE INDEX IF NOT EXISTS idx_intents_session ON write_intents(session_id);
CREATE INDEX IF NOT EXISTS idx_intents_status ON write_intents(status);
CREATE INDEX IF NOT EXISTS idx_intents_target ON write_intents(target_type, target_id);

CREATE TABLE IF NOT EXISTS claims (
    session_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    revision TEXT,
    claimed_at TEXT NOT NULL,
    expires_at TEXT,
    payload TEXT DEFAULT '{}',
    PRIMARY KEY (session_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_claims_target ON claims(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_claims_expires ON claims(expires_at);
"""

_INTENT_INSERT = """
INSERT INTO write_intents (session_id, operation, target_type, target_id, payload, status, created_at)
VALUES (?, ?, ?, ?, ?, 'pending', ?)
"""

_CLAIM_UPSERT = """
INSERT OR REPLACE INTO claims (session_id, target_type, target_id, revision, claimed_at, expires_at, payload)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Striped Lock Implementation ────────────────────────────────────────────────

class _StripedLock:
    """
    Per-(target_type, target_id) lock for fine-grained write coordination.

    Reduces contention vs. one global lock: concurrent writes to different
    targets don't block each other. Targets on the same stripe share a lock.

    M1/8GB note: only N stripe locks exist (N << number of targets), so
    memory overhead is bounded regardless of active target count.
    """

    # Stripe count: power-of-2 so slot selection uses bitwise AND, not modulo.
    # 16 slots gives good concurrency for 6 sessions with minimal overhead.
    _NUM_STRIPES = 16

    def __init__(self) -> None:
        self._stripe = [threading.Lock() for _ in range(self._NUM_STRIPES)]

    def _slot(self, target_type: str, target_id: str) -> int:
        """Select lock slot from target namespace."""
        key = f"{target_type}:{target_id}".encode("utf-8")
        h = hashlib.sha256(key).digest()
        return int.from_bytes(h[:4], "little") & (self._NUM_STRIPES - 1)

    def lock(self, target_type: str, target_id: str) -> threading.Lock:
        """Return the lock for a given target."""
        return self._stripe[self._slot(target_type, target_id)]

    @contextmanager
    def hold(self, target_type: str, target_id: str):
        """Context manager: acquire and hold the lock for the target."""
        lock = self.lock(target_type, target_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


# ─────────────────────────────────────────────────────────────────────────────
# WriteCoordinator
# ─────────────────────────────────────────────────────────────────────────────

class WriteCoordinator:
    """SQLite WAL-based write coordinator with claim semantics and intent journaling."""

    def __init__(self, palace_path: str = None):
        if palace_path is None:
            palace_path = os.environ.get("MEMPALACE_PATH", ".mempalace")
        self._db_path = str(Path(palace_path) / "write_coordinator.sqlite3")
        self._write_lock = threading.Lock()
        self._stripe_lock = _StripedLock()
        self._conn: sqlite3.Connection | None = None
        self._local = threading.local()
        self._connect()
        self._initialize()
        atexit.register(self.close)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn

    @contextmanager
    def _conn_ctx(self):
        """Return a thread-local connection, opening one on first use in this thread."""
        try:
            conn = self._local.conn
        except AttributeError:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise

    def _initialize(self) -> None:
        """Run schema DDL and cleanup stale entries on startup."""
        with self._write_lock:
            conn = self._conn
            conn.executescript(_SCHEMA)
            conn.commit()
        self.cleanup_old_entries(older_than_days=7)
        self.recover_pending_intents(palace_path=str(Path(self._db_path).parent))

    # ------------------------------------------------------------------
    # Claims
    # ------------------------------------------------------------------

    def claim(
        self,
        target_type: str,
        target_id: str,
        session_id: str,
        revision: str = None,
        payload: dict = None,
        ttl_seconds: int = None,
    ) -> dict:
        """
        Acquire a claim on (target_type, target_id) for session_id.

        Uses INSERT OR REPLACE semantics: if a claim already exists for the
        same (target_type, target_id) this overwrites it (only one owner at a time).
        Returns {"acquired": bool, "owner": session_id or None}.
        """
        payload = payload or {}
        now = _utc_now()
        expires = None
        if ttl_seconds is not None:
            expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        with self._stripe_lock.hold(target_type, target_id):
            with self._conn_ctx() as conn:
                cursor = conn.execute(
                    "SELECT session_id FROM claims WHERE target_type=? AND target_id=?",
                    (target_type, target_id),
                )
                row = cursor.fetchone()
                if row is not None:
                    owner = row[0]
                    if owner == session_id:
                        # already owned by this session — refresh timestamp
                        conn.execute(
                            "UPDATE claims SET claimed_at=?, payload=?, expires_at=? "
                            "WHERE target_type=? AND target_id=?",
                            (now, json.dumps(payload), expires, target_type, target_id),
                        )
                        conn.commit()
                        return {"acquired": True, "owner": session_id}
                    else:
                        return {"acquired": False, "owner": owner}

                conn.execute(
                    _CLAIM_UPSERT,
                    (session_id, target_type, target_id, revision, now, expires, json.dumps(payload)),
                )
                conn.commit()
                return {"acquired": True, "owner": session_id}

    def release_claim(
        self, target_type: str, target_id: str, session_id: str
    ) -> bool:
        """Release a claim. Only the owning session can release. Returns True if released."""
        with self._stripe_lock.hold(target_type, target_id):
            with self._conn_ctx() as conn:
                cursor = conn.execute(
                    "SELECT session_id FROM claims WHERE target_type=? AND target_id=?",
                    (target_type, target_id),
                )
                row = cursor.fetchone()
                if row is None or row[0] != session_id:
                    return False
                conn.execute(
                    "DELETE FROM claims WHERE target_type=? AND target_id=?",
                    (target_type, target_id),
                )
                conn.commit()
                return True

    def get_claim(self, target_type: str, target_id: str) -> dict | None:
        """Get current claim owner info, or None if unclaimed."""
        with self._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT session_id, target_type, target_id, revision, claimed_at, payload "
                "FROM claims WHERE target_type=? AND target_id=?",
                (target_type, target_id),
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
            }

    def get_session_claims(self, session_id: str) -> list[dict]:
        """Get all claims held by a session."""
        with self._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT session_id, target_type, target_id, revision, claimed_at, payload "
                "FROM claims WHERE session_id=?",
                (session_id,),
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
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Write Intents
    # ------------------------------------------------------------------

    def log_intent(
        self,
        session_id: str,
        operation: str,
        target_type: str,
        target_id: str,
        payload: dict = None,
    ) -> int:
        """
        Log a pending write intent. Returns the new intent id.

        operation: 'claim' | 'handoff' | 'revision' | 'invalidate' | 'supersede'
        """
        payload = payload or {}
        now = _utc_now()
        with self._write_lock:
            with self._conn_ctx() as conn:
                cursor = conn.execute(
                    _INTENT_INSERT,
                    (session_id, operation, target_type, target_id, json.dumps(payload), now),
                )
                conn.commit()
                return cursor.lastrowid  # type: ignore[return-value]

    def commit_intent(self, intent_id: int, session_id: str) -> bool:
        """Mark intent as committed. Only the originating session can commit."""
        # Read target for stripe selection — no lock held during SELECT.
        with self._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT target_type, target_id FROM write_intents WHERE id=?",
                (intent_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            target_type, target_id = row[0], row[1]

        # Stripe-lock to reduce contention vs. global lock.
        with self._stripe_lock.hold(target_type, target_id):
            with self._conn_ctx() as conn:
                # Re-check ownership inside stripe lock (double-checked locking).
                cursor = conn.execute(
                    "SELECT session_id FROM write_intents WHERE id=?",
                    (intent_id,),
                )
                row = cursor.fetchone()
                if row is None or row[0] != session_id:
                    return False
                conn.execute(
                    "UPDATE write_intents SET status='committed', committed_at=? WHERE id=?",
                    (_utc_now(), intent_id),
                )
                conn.commit()
                return True

    def rollback_intent(self, intent_id: int, session_id: str) -> bool:
        """Mark intent as rolled back. Only the originating session can rollback."""
        # Read target for stripe selection — no lock held during SELECT.
        with self._conn_ctx() as conn:
            cursor = conn.execute(
                "SELECT target_type, target_id FROM write_intents WHERE id=?",
                (intent_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            target_type, target_id = row[0], row[1]

        # Stripe-lock to reduce contention vs. global lock.
        with self._stripe_lock.hold(target_type, target_id):
            with self._conn_ctx() as conn:
                # Re-check ownership inside stripe lock.
                cursor = conn.execute(
                    "SELECT session_id FROM write_intents WHERE id=?",
                    (intent_id,),
                )
                row = cursor.fetchone()
                if row is None or row[0] != session_id:
                    return False
                conn.execute(
                    "UPDATE write_intents SET status='rolled_back', committed_at=? WHERE id=?",
                    (_utc_now(), intent_id),
                )
                conn.commit()
                return True

    # ------------------------------------------------------------------
    # Write Intents
    # ------------------------------------------------------------------

    def get_pending_intents(self, session_id: str = None) -> list[dict]:
        """Get all pending intents, optionally filtered by session."""
        with self._conn_ctx() as conn:
            if session_id is not None:
                cursor = conn.execute(
                    "SELECT id, session_id, operation, target_type, target_id, payload, "
                    "status, created_at, committed_at FROM write_intents "
                    "WHERE session_id=? AND status='pending' ORDER BY created_at",
                    (session_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, session_id, operation, target_type, target_id, payload, "
                    "status, created_at, committed_at FROM write_intents "
                    "WHERE status='pending' ORDER BY created_at",
                )
            rows = cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "session_id": r[1],
                    "operation": r[2],
                    "target_type": r[3],
                    "target_id": r[4],
                    "payload": json.loads(r[5]),
                    "status": r[6],
                    "created_at": r[7],
                    "committed_at": r[8],
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover_pending_intents(self, palace_path: str = None) -> dict:
        """
        Replay pending write intents on startup.

        Pending intents from sessions that are no longer active (crashed sessions)
        are rolled back. Active sessions get to keep their pending intents for
        potential replay.

        Returns {"recovered": N, "rolled_back": N}.
        """
        pending = self.get_pending_intents()
        if not pending:
            return {"recovered": 0, "rolled_back": 0}

        # Import here to avoid circular imports
        from .session_registry import SessionRegistry
        registry = SessionRegistry(palace_path) if palace_path else None

        recovered = 0
        rolled_back = 0
        for intent in pending:
            session_id = intent["session_id"]
            intent_id = intent["id"]

            # Check if session is still active
            if registry:
                try:
                    session = registry.get_session(session_id)
                    if session is None or session.get("status") == "stopped":
                        # Session is gone — rollback the pending intent
                        self.rollback_intent(intent_id, session_id)
                        rolled_back += 1
                        continue
                except Exception:
                    pass

            # Session is still active — keep intent for potential replay
            recovered += 1

        return {"recovered": recovered, "rolled_back": rolled_back}

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def cleanup_old_entries(self, older_than_days: int = 7) -> dict:
        """
        Remove committed/rolled_back entries older than older_than_days.
        Returns {"intents_removed": N, "claims_removed": N}.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        cutoff_iso = cutoff.isoformat()
        with self._write_lock:
            with self._conn_ctx() as conn:
                cursor = conn.execute(
                    "DELETE FROM write_intents "
                    "WHERE status IN ('committed', 'rolled_back') AND created_at < ?",
                    (cutoff_iso,),
                )
                intents_removed = cursor.rowcount

                # claims table has no created_at — clean by claimed_at
                cursor2 = conn.execute(
                    "DELETE FROM claims WHERE claimed_at < ?",
                    (cutoff_iso,),
                )
                claims_removed = cursor2.rowcount
                conn.commit()
                return {"intents_removed": intents_removed, "claims_removed": claims_removed}

    def close(self) -> None:
        """Close all connections and unregister atexit."""
        try:
            atexit.unregister(self.close)
        except Exception:
            pass
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
        return (
            f"<WriteCoordinator db_path={self._db_path!r} "
            f"write_lock={self._write_lock!r} stripes={self._stripe_lock._NUM_STRIPES}>"
        )

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
