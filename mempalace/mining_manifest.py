"""
mining_manifest.py — Skip-unchanged manifest for mining.

Stores per-(wing, project_path, source_file) fingerprints in a local SQLite DB
so repeated mine runs can skip files before the expensive chunk/embed/upsert work.

Schema:
    file_state(
        wing, project_path, source_file,
        size_bytes, mtime_ns, quick_hash,
        chunk_count, status, updated_at,
        PRIMARY KEY(wing, project_path, source_file)
    )

Fingerprint: size_bytes + mtime_ns + quick_hash (first+last 4KB for >8KB files).
Fail-open: any manifest error logs a warning and returns "unchanged=False".

Collision tradeoff: quick_hash is a head+tail sample — content inserted in the
middle region (byte 4096 onward) is detected via the tail offset changing, but
theoretical false skips are possible when size+mtime+hash accidentally match.
The size+mtime guard mitigates this significantly; probability is negligible
for practical mining workloads.
"""

import hashlib
import os
import sqlite3
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DB_NAME = "mining_manifest.sqlite3"
_CHUNK_SIZE = 4096


def _quick_hash(filepath: Path, size_bytes: int) -> Optional[str]:
    """Hash first+last 4KB for files >8KB, full hash for smaller files.

    Returns None on any error — caller must NOT skip when fingerprint is unknown.

    Collision tradeoff: head+tail sampling can miss content mutations in the
    untouched middle region (e.g., a 10MB file with 4KB of new content inserted
    at byte 4096 produces a different tail offset, so the collision is detected).
    However, two different small files can share a quick_hash — the size+mtime
    guard reduces this risk substantially. The probability of a false skip
    (identical size+mtime+hash but different content) is negligible for
    practical mining workloads.
    """
    try:
        if size_bytes <= _CHUNK_SIZE * 2:
            # Small file — hash everything
            return hashlib.sha256(filepath.read_bytes()).hexdigest()[:32]
        # Large file — hash head + tail
        with open(filepath, "rb") as f:
            head = f.read(_CHUNK_SIZE)
            f.seek(max(0, size_bytes - _CHUNK_SIZE))
            tail = f.read(_CHUNK_SIZE)
        return hashlib.sha256(head + tail).hexdigest()[:32]
    except Exception as e:
        logger.warning("[mining_manifest] quick_hash failed for %s: %s", filepath, e)
        return None


class MiningManifest:
    """SQLite WAL manifest tracking file fingerprints and processed state."""

    def __init__(self, palace_path: str):
        db_path = os.path.join(palace_path, _DB_NAME)
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._conn = sqlite3.connect(db_path, timeout=10.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_table()
        except Exception as e:
            logger.warning("[mining_manifest] failed to open %s: %s — fail-open active", db_path, e)
            self._conn = None

    def _ensure_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS file_state (
                wing            TEXT NOT NULL,
                project_path    TEXT NOT NULL,
                source_file     TEXT NOT NULL,
                size_bytes      INTEGER NOT NULL,
                mtime_ns        INTEGER NOT NULL,
                quick_hash      TEXT,
                chunk_count     INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'success',
                updated_at      TEXT NOT NULL,
                PRIMARY KEY(wing, project_path, source_file)
            )
        """)

    def is_unchanged(
        self,
        wing: str,
        project_path: str,
        source_file: str,
        size_bytes: int,
        mtime_ns: int,
        quick_hash: str,
    ) -> bool:
        """Return True if manifest has a matching success entry for this fingerprint."""
        if self._conn is None:
            return False
        try:
            cursor = self._conn.execute(
                """
                SELECT size_bytes, mtime_ns, quick_hash, chunk_count, status
                FROM file_state
                WHERE wing=? AND project_path=? AND source_file=?
                """,
                (wing, project_path, source_file),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            manifest_size, manifest_mtime, manifest_hash, chunk_count, status = row
            return (
                status == "success"
                and manifest_size == size_bytes
                and manifest_mtime == mtime_ns
                and manifest_hash == quick_hash
            )
        except Exception as e:
            logger.warning("[mining_manifest] is_unchanged lookup failed: %s — fail-open", e)
            return False

    def update_success(
        self,
        wing: str,
        project_path: str,
        source_file: str,
        size_bytes: int,
        mtime_ns: int,
        quick_hash: Optional[str],
        chunk_count: int,
    ) -> None:
        """Record or update a successful processing entry."""
        if self._conn is None:
            return
        try:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._conn.execute(
                """
                INSERT INTO file_state
                    (wing, project_path, source_file, size_bytes, mtime_ns, quick_hash, chunk_count, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'success', ?)
                ON CONFLICT(wing, project_path, source_file) DO UPDATE SET
                    size_bytes=excluded.size_bytes,
                    mtime_ns=excluded.mtime_ns,
                    quick_hash=excluded.quick_hash,
                    chunk_count=excluded.chunk_count,
                    status='success',
                    updated_at=excluded.updated_at
                """,
                (wing, project_path, source_file, size_bytes, mtime_ns, quick_hash, chunk_count, now),
            )
            self._conn.commit()
        except Exception as e:
            logger.warning("[mining_manifest] update_success failed: %s — ignoring", e)

    def update_error(
        self,
        wing: str,
        project_path: str,
        source_file: str,
    ) -> None:
        """Record an error for this file (do NOT mark as unchanged-success)."""
        if self._conn is None:
            return
        try:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._conn.execute(
                """
                INSERT INTO file_state
                    (wing, project_path, source_file, size_bytes, mtime_ns, quick_hash, chunk_count, status, updated_at)
                VALUES (?, ?, ?, 0, 0, NULL, 0, 'error', ?)
                ON CONFLICT(wing, project_path, source_file) DO UPDATE SET
                    status='error',
                    updated_at=excluded.updated_at
                """,
                (wing, project_path, source_file, now),
            )
            self._conn.commit()
        except Exception as e:
            logger.warning("[mining_manifest] update_error failed: %s — ignoring", e)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
