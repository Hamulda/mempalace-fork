"""
session_registry.py — Multi-Session Registry for MemPalace
==========================================================

Lightweight session registry using SQLite WAL mode (M1-friendly, no Redis/Kafka/Postgres).

Storage: SQLite WAL at {palace_path}/sessions.sqlite3
Thread-safe writes via threading.Lock() (same pattern as KnowledgeGraph).
Heartbeat-based session tracking with auto-cleanup of stale sessions.

Usage:
    from mempalace.session_registry import SessionRegistry

    registry = SessionRegistry(palace_path="/Users/john/project/.mempalace")
    registry.register_session("Claude:user@host-1234", "/Users/john/project", branch="main")
    registry.heartbeat_session("Claude:user@host-1234", revision="abc123", claimed_paths=["src/main.py"])
    active = registry.get_active_sessions(project_root="/Users/john/project")
    registry.unregister_session("Claude:user@host-1234")
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Default timeout for SQLite operations (seconds)
_SQLITE_TIMEOUT = 5.0

# Default staleness thresholds (seconds)
_IDLE_THRESHOLD = 5 * 60       # 5 minutes  -> active → idle
_STALE_THRESHOLD = 15 * 60     # 15 minutes -> considered stale
_LONG_STALE_THRESHOLD = 30 * 24 * 60 * 60  # 30 days -> auto-cleanup on startup

# SQL statements
_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        project_root TEXT NOT NULL,
        branch TEXT,
        role TEXT DEFAULT 'agent',
        status TEXT DEFAULT 'active',
        claimed_paths TEXT DEFAULT '[]',
        last_seen_revision TEXT,
        last_seen_at TEXT,
        registered_at TEXT,
        metadata TEXT DEFAULT '{}'
    );
"""

_CREATE_INDEXES = """
    CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_root);
    CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
    CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen_at);
"""

_UPSERT_SESSION = """
    INSERT INTO sessions (session_id, project_root, branch, role, status, claimed_paths,
                          last_seen_revision, last_seen_at, registered_at, metadata)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(session_id) DO UPDATE SET
        project_root = excluded.project_root,
        branch       = excluded.branch,
        role         = excluded.role,
        status       = excluded.status,
        claimed_paths = excluded.claimed_paths,
        last_seen_revision = excluded.last_seen_revision,
        last_seen_at = excluded.last_seen_at,
        metadata     = excluded.metadata;
"""

_UPDATE_HEARTBEAT = """
    UPDATE sessions SET
        last_seen_at       = ?,
        last_seen_revision = ?,
        claimed_paths      = ?,
        status             = 'active'
    WHERE session_id = ?;
"""

_UPDATE_METADATA = """
    UPDATE sessions SET metadata = ? WHERE session_id = ?;
"""

_MARK_STOPPED = """
    UPDATE sessions SET status = 'stopped' WHERE session_id = ?;
"""

_SELECT_ACTIVE = """
    SELECT session_id, project_root, branch, role, status, claimed_paths,
           last_seen_revision, last_seen_at, registered_at, metadata
    FROM sessions
    WHERE status IN ('active', 'idle')
      AND (? = '' OR project_root = ?)
    ORDER BY last_seen_at DESC;
"""

_SELECT_SESSION = """
    SELECT session_id, project_root, branch, role, status, claimed_paths,
           last_seen_revision, last_seen_at, registered_at, metadata
    FROM sessions WHERE session_id = ?;
"""

_SELECT_STALE = """
    SELECT session_id FROM sessions
    WHERE status IN ('active', 'idle')
      AND datetime(last_seen_at) < datetime(?, 'unixepoch');
"""

_DELETE_STALE = """
    DELETE FROM sessions WHERE session_id = ?;
"""


def _row_to_session(row: tuple) -> dict:
    """Convert a database row to a session dict."""
    return {
        "session_id": row[0],
        "project_root": row[1],
        "branch": row[2],
        "role": row[3],
        "status": row[4],
        "claimed_paths": json.loads(row[5]),
        "last_seen_revision": row[6],
        "last_seen_at": row[7],
        "registered_at": row[8],
        "metadata": json.loads(row[9]),
    }


class SessionRegistry:
    """
    Lightweight multi-session registry using SQLite WAL mode.

    Thread-safe writes via a single _write_lock (single-writer semantics).
    Reads do NOT need locking — WAL allows concurrent reads without blocking.

    Auto-cleanup: sessions not seen for 30 days are removed on startup.
    """

    def __init__(self, palace_path: Optional[str] = None):
        """
        Connect to session DB at palace_path/sessions.sqlite3.

        Args:
            palace_path: Root path of the MemPalace store.
                        Defaults to ~/.mempalace.
        """
        if palace_path is None:
            palace_path = os.path.expanduser("~/.mempalace")
        self._db_path = os.path.join(palace_path, "sessions.sqlite3")
        self._write_lock = threading.Lock()
        self._connection = None
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        # Cleanup sessions older than 30 days on startup
        self.cleanup_stale_sessions(older_than_seconds=_LONG_STALE_THRESHOLD)

    def _conn(self) -> sqlite3.Connection:
        """Get or create the shared database connection (check_same_thread=False)."""
        if self._connection is None:
            self._connection = sqlite3.connect(
                self._db_path,
                timeout=_SQLITE_TIMEOUT,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA journal_mode=WAL;")
            self._connection.execute("PRAGMA synchronous=NORMAL;")
        return self._connection

    def _init_db(self) -> None:
        """Initialize the sessions table and indexes."""
        conn = self._conn()
        conn.executescript(_CREATE_TABLE)
        conn.executescript(_CREATE_INDEXES)
        conn.commit()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_session_id(session_id: str) -> str:
        """
        Normalize session_id to alphanumeric/dash/underscore only.

        Raises:
            ValueError: if session_id contains unsafe characters.
        """
        if not session_id:
            raise ValueError("session_id must not be empty")
        sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "", session_id)
        if not sanitized:
            raise ValueError(
                f"session_id '{session_id}' contains no safe characters after sanitization"
            )
        return sanitized

    @staticmethod
    def _now_iso() -> str:
        """Return current UTC timestamp in ISO8601 format."""
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_session(
        self,
        session_id: str,
        project_root: str,
        branch: Optional[str] = None,
        role: str = "agent",
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Register a new session (or re-register an existing one).

        Uses INSERT OR REPLACE so restarts are idempotent — calling this
        with the same session_id updates the existing record.

        Args:
            session_id: Unique identifier for the session.
                       e.g. "Claude:user@host-1234"
            project_root: Root path of the project this session is working in.
            branch: Current git branch (optional).
            role: Session role — 'agent', 'user', or 'orchestrator'.
            metadata: Free-form extra metadata (optional).

        Returns:
            The full session record as a dict.

        Raises:
            ValueError: if session_id is empty or contains no safe characters.
        """
        session_id = self._sanitize_session_id(session_id)
        now = self._now_iso()
        metadata = metadata or {}

        row = (
            session_id,
            os.path.normpath(project_root),
            branch,
            role,
            "active",
            "[]",
            None,
            now,
            now,
            json.dumps(metadata),
        )

        with self._write_lock:
            conn = self._conn()
            conn.execute(_UPSERT_SESSION, row)
            conn.commit()

        return self.get_session(session_id)

    def heartbeat_session(
        self,
        session_id: str,
        revision: Optional[str] = None,
        claimed_paths: Optional[list] = None,
    ) -> dict:
        """
        Update last_seen_at, revision, and claimed_paths. Moves idle→active.

        Args:
            session_id: The session to heartbeat.
            revision: Current git revision (optional).
            claimed_paths: List of file paths currently being edited (optional).

        Returns:
            The updated session record, or None if session was not found.

        Raises:
            ValueError: if session_id is empty or contains no safe characters.
        """
        session_id = self._sanitize_session_id(session_id)
        now = self._now_iso()
        claimed_paths = claimed_paths or []

        with self._write_lock:
            conn = self._conn()
            conn.execute(
                _UPDATE_HEARTBEAT,
                (now, revision, json.dumps(claimed_paths), session_id),
            )
            conn.commit()

        return self.get_session(session_id)

    def unregister_session(self, session_id: str) -> bool:
        """
        Mark a session as stopped.

        Args:
            session_id: The session to unregister.

        Returns:
            True if the session existed and was stopped, False otherwise.

        Raises:
            ValueError: if session_id is empty or contains no safe characters.
        """
        session_id = self._sanitize_session_id(session_id)

        with self._write_lock:
            conn = self._conn()
            conn.execute(_MARK_STOPPED, (session_id,))
            conn.commit()
            # rowcount == 0 means session did not exist
            return conn.total_changes > 0

    def get_active_sessions(self, project_root: Optional[str] = None) -> list:
        """
        Return all active or idle sessions, optionally filtered by project.

        Args:
            project_root: If provided, only return sessions for this project.

        Returns:
            List of session records (oldest first by last_seen_at).
        """
        conn = self._conn()
        project_root = project_root or ""
        cursor = conn.execute(
            _SELECT_ACTIVE,
            (project_root, project_root),
        )
        return [_row_to_session(row) for row in cursor.fetchall()]

    def get_session(self, session_id: str) -> Optional[dict]:
        """
        Get a session record by id.

        Args:
            session_id: The session to look up.

        Returns:
            Session record dict, or None if not found.

        Raises:
            ValueError: if session_id is empty or contains no safe characters.
        """
        session_id = self._sanitize_session_id(session_id)
        conn = self._conn()
        cursor = conn.execute(_SELECT_SESSION, (session_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_session(row)

    def update_session_metadata(self, session_id: str, metadata: dict) -> dict:
        """
        Merge metadata into a session's metadata JSON field.

        Existing top-level keys are preserved; keys in the incoming dict
        overwrite existing values.

        Args:
            session_id: The session to update.
            metadata: Dict to merge into the session's metadata.

        Returns:
            The updated session record.

        Raises:
            ValueError: if session_id is empty or contains no safe characters.
        """
        session_id = self._sanitize_session_id(session_id)
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Session '{session_id}' not found")

        # Merge: incoming keys win
        merged = {**session["metadata"], **metadata}

        with self._write_lock:
            conn = self._conn()
            conn.execute(_UPDATE_METADATA, (json.dumps(merged), session_id))
            conn.commit()

        return self.get_session(session_id)

    def cleanup_stale_sessions(self, older_than_seconds: int = _STALE_THRESHOLD) -> int:
        """
        Remove sessions not seen for older_than_seconds.

        Args:
            older_than_seconds: Sessions older than this threshold are removed.
                               Defaults to 15 minutes (stale threshold).
                               Use 30 days for startup cleanup.

        Returns:
            Number of sessions removed.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        cutoff_iso = cutoff.isoformat()

        with self._write_lock:
            conn = self._conn()
            cursor = conn.execute(_SELECT_STALE, (cutoff_iso,))
            stale_ids = [row[0] for row in cursor.fetchall()]
            for sid in stale_ids:
                conn.execute(_DELETE_STALE, (sid,))
            conn.commit()
            return len(stale_ids)

    # ------------------------------------------------------------------
    # Context manager (optional convenience)
    # ------------------------------------------------------------------

    def __enter__(self) -> "SessionRegistry":
        return self

    def __exit__(self, *args) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
