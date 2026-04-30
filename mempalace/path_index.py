"""
path_index.py — SQLite metadata index for source_file / path queries.

Provides O(1) path lookups without scanning LanceDB metadata.
Mirrors KeywordIndex patterns (singleton, thread-safe, M1-native).

Schema:
    path_index(document_id, source_file, repo_rel_path, basename,
               language, chunk_kind, symbol_name, line_start,
               line_end, wing, room, is_latest)

Indexes:
    idx_path_source_file  ON source_file
    idx_path_repo_rel_path ON repo_rel_path
    idx_path_basename ON basename

Usage:
    idx = PathIndex(palace_path="/path/to/palace")
    idx.upsert_rows([{document_id, source_file, ...}])
    idx.delete_rows(["doc_id_1", "doc_id_2"])
    idx.search_path("src/foo.py", project_path="/proj", limit=20)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("mempalace_path_index")


class PathIndex:
    """
    Persistent SQLite index for path metadata.

    Thread-safe: uses a lock for all write operations.
    Searches are read-only and safe without locks.
    """

    _instances: dict[str, "PathIndex"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, palace_path: str):
        self.palace_path = palace_path
        self.db_path = str(
            Path(palace_path).expanduser().resolve() / "path_index.sqlite3"
        )
        self._lock = threading.RLock()
        self._init_db()

    @classmethod
    def get(cls, palace_path: str) -> "PathIndex":
        """Return the singleton PathIndex for a palace path."""
        with cls._instances_lock:
            if palace_path not in cls._instances:
                cls._instances[palace_path] = cls(palace_path)
            return cls._instances[palace_path]

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Clear all singleton instances. For test isolation only."""
        with cls._instances_lock:
            cls._instances.clear()

    def _init_db(self) -> None:
        """Create tables and indexes if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS path_index (
                    document_id TEXT PRIMARY KEY,
                    source_file TEXT NOT NULL,
                    repo_rel_path TEXT,
                    basename TEXT NOT NULL,
                    language TEXT,
                    chunk_kind TEXT,
                    symbol_name TEXT,
                    line_start INTEGER,
                    line_end INTEGER,
                    wing TEXT,
                    room TEXT,
                    is_latest INTEGER DEFAULT 1
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_path_source_file ON path_index(source_file)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_path_repo_rel_path ON path_index(repo_rel_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_path_basename ON path_index(basename)"
            )
            conn.commit()
            conn.close()

    # ── Write operations ───────────────────────────────────────────────────────

    def upsert_rows(self, rows: list[dict[str, Any]]) -> None:
        """Batch upsert path metadata rows. Failures are logged, not raised."""
        if not rows:
            return
        with self._lock:
            conn = None
            try:
                conn = sqlite3.connect(self.db_path)
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO path_index
                        (document_id, source_file, repo_rel_path, basename,
                         language, chunk_kind, symbol_name, line_start,
                         line_end, wing, room, is_latest)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["document_id"],
                            r["source_file"],
                            r.get("repo_rel_path"),
                            r.get("basename") or self._basename(r["source_file"]),
                            r.get("language"),
                            r.get("chunk_kind"),
                            r.get("symbol_name"),
                            r.get("line_start"),
                            r.get("line_end"),
                            r.get("wing"),
                            r.get("room"),
                            int(r.get("is_latest", True)),
                        )
                        for r in rows
                    ],
                )
                conn.commit()
            except Exception as e:
                logger.warning("PathIndex upsert failed for %d rows: %s", len(rows), e)
            finally:
                if conn:
                    conn.close()

    def delete_rows(self, document_ids: list[str]) -> None:
        """Batch delete rows by document_id. Failures are logged, not raised."""
        if not document_ids:
            return
        with self._lock:
            conn = None
            try:
                conn = sqlite3.connect(self.db_path)
                placeholders = ",".join("?" * len(document_ids))
                conn.execute(
                    f"DELETE FROM path_index WHERE document_id IN ({placeholders})",
                    document_ids,
                )
                conn.commit()
            except Exception as e:
                logger.warning(
                    "PathIndex delete failed for %d ids: %s", len(document_ids), e
                )
            finally:
                if conn:
                    conn.close()

    def mark_tombstoned(self, document_ids: list[str]) -> None:
        """Mark rows as is_latest=0 instead of deleting (soft delete for tombstone pattern)."""
        if not document_ids:
            return
        with self._lock:
            conn = None
            try:
                conn = sqlite3.connect(self.db_path)
                placeholders = ",".join("?" * len(document_ids))
                conn.execute(
                    f"UPDATE path_index SET is_latest=0 WHERE document_id IN ({placeholders})",
                    document_ids,
                )
                conn.commit()
            except Exception as e:
                logger.warning(
                    "PathIndex tombstone failed for %d ids: %s", len(document_ids), e
                )
            finally:
                if conn:
                    conn.close()

    def count(self) -> int:
        """Return total row count (excluding tombstoned)."""
        with self._lock:
            conn = None
            try:
                conn = sqlite3.connect(self.db_path)
                row = conn.execute(
                    "SELECT COUNT(*) FROM path_index WHERE is_latest=1"
                ).fetchone()
                return row[0] if row else 0
            finally:
                if conn:
                    conn.close()

    # ── Search operations ─────────────────────────────────────────────────────

    def search_path(
        self,
        query: str,
        project_path: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Search path index for matching document_ids.

        Matching priority:
        1. Exact source_file match
        2. Exact repo_rel_path match
        3. Suffix match (source_file.endswith(query))
        4. Basename match (basename == query)

        project_path filter is STRICT — source_file must start with project_path.

        Returns list of dicts with document_id, source_file, language, etc.
        """
        if not query:
            return []

        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row

                # Base query — always filter tombstoned
                base_where = "is_latest=1"
                params: list[Any] = []

                if project_path:
                    base_where += " AND (source_file LIKE ? OR source_file = ?)"
                    prefix = project_path.rstrip("/") + "/"
                    params.extend([f"{prefix}%", project_path])

                if language:
                    base_where += " AND language = ?"
                    params.append(language)

                rows = conn.execute(
                    f"SELECT * FROM path_index WHERE {base_where}", params
                ).fetchall()

                for row in rows:
                    doc_id = row["document_id"]
                    if doc_id in seen:
                        continue
                    source_file = row["source_file"]
                    repo_rel_path = row["repo_rel_path"] or ""
                    basename = row["basename"]

                    matched = False

                    # Priority 1: exact source_file
                    if source_file == query:
                        matched = True
                    # Priority 2: exact repo_rel_path
                    elif repo_rel_path and repo_rel_path == query:
                        matched = True
                    # Priority 3: suffix match
                    elif source_file.endswith(query) or source_file.endswith("/" + query):
                        matched = True
                    # Priority 4: basename
                    elif basename == query:
                        matched = True

                    if matched:
                        seen.add(doc_id)
                        results.append(
                            {
                                "document_id": doc_id,
                                "source_file": source_file,
                                "repo_rel_path": repo_rel_path,
                                "language": row["language"],
                                "chunk_kind": row["chunk_kind"],
                                "symbol_name": row["symbol_name"],
                                "line_start": row["line_start"],
                                "line_end": row["line_end"],
                                "wing": row["wing"],
                                "room": row["room"],
                            }
                        )
                        if len(results) >= limit:
                            break

                conn.close()
            except Exception as e:
                logger.warning("PathIndex search failed: %s", e)

        return results

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _basename(path: str) -> str:
        """Return the last path component (file name)."""
        if not path:
            return ""
        return path.rsplit("/", 1)[-1]

    @staticmethod
    def compute_repo_rel_path(source_file: str, common_prefix: str) -> str:
        """Compute repo-relative path from source_file given a common prefix."""
        if not source_file or not common_prefix:
            return source_file
        prefix = common_prefix.rstrip("/") + "/"
        if source_file.startswith(prefix):
            return source_file[len(prefix) :]
        return source_file
