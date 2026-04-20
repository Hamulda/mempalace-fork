"""
lexical_index.py — SQLite FTS5 persistent keyword index for MemPalace.

Provides a lightweight lexical search layer that persists across restarts,
handles unlimited corpus size, and is M1-native (no external service).

FTS5 schema:
    drawers_fts(document_id, content, wing, room, language)
    tokenize='porter unicode61'  # Porter stemmer + Unicode normalization

Usage:
    idx = KeywordIndex(palace_path="/path/to/palace")
    idx.upsert_drawer("drawer_abc123", "def foo(): pass", "repo", "src_main_py", "Python")
    results = idx.search("def foo", n_results=10, wing="repo")

This module is used by searcher.py instead of the in-memory rank_bm25 layer.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mempalace_lexical")

# Default FTS5 tokenize parameter
_DEFAULT_TOKENIZE = "porter unicode61"


class KeywordIndex:
    """
    Persistent FTS5 keyword index for drawer content.

    Thread-safe: uses a lock for all write operations.
    Does NOT hold locks during search — searches are read-only and safe.
    """

    _instances: dict[str, "KeywordIndex"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, palace_path: str):
        self.palace_path = palace_path
        self.db_path = str(Path(palace_path).expanduser().resolve() / "keyword_index.sqlite3")
        self._lock = threading.RLock()
        self._init_db()

    @classmethod
    def get(cls, palace_path: str) -> "KeywordIndex":
        """Return the singleton KeywordIndex for a palace path."""
        with cls._instances_lock:
            if palace_path not in cls._instances:
                cls._instances[palace_path] = cls(palace_path)
            return cls._instances[palace_path]

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Clear all singleton instances. For test isolation only."""
        with cls._instances_lock:
            for inst in cls._instances.values():
                try:
                    if inst._lock:
                        inst._lock.acquire()
                except Exception:
                    pass
            cls._instances.clear()

    def _init_db(self) -> None:
        """Initialize the FTS5 virtual table if it doesn't exist."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS drawers_fts USING fts5(
                    document_id,
                    content,
                    wing,
                    room,
                    language,
                    tokenize='porter unicode61'
                )
            """)
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning("FTS5 init failed: %s — lexical search unavailable", e)

    def upsert_drawer(
        self,
        document_id: str,
        content: str,
        wing: str,
        room: str,
        language: Optional[str] = None,
    ) -> None:
        """Upsert a drawer into the FTS5 index. Replaces existing entry by document_id."""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                # Delete existing entry
                conn.execute("DELETE FROM drawers_fts WHERE document_id = ?", (document_id,))
                # Insert new entry
                conn.execute(
                    "INSERT INTO drawers_fts(document_id, content, wing, room, language) VALUES (?, ?, ?, ?, ?)",
                    (document_id, content, wing, room, language or ""),
                )
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                logger.warning("FTS5 upsert failed for %s: %s", document_id, e)

    def delete_drawer(self, document_id: str) -> None:
        """Remove a drawer from the FTS5 index."""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute("DELETE FROM drawers_fts WHERE document_id = ?", (document_id,))
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                logger.warning("FTS5 delete failed for %s: %s", document_id, e)

    def search(
        self,
        query: str,
        n_results: int = 10,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        language: Optional[str] = None,
    ) -> list[dict]:
        """
        Search the FTS5 index for matching document IDs.

        Uses BM25 ranking (FTS5's built-in bm25() scoring).

        Returns list of dicts with keys: document_id, score, wing, room, language
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row

            # Build WHERE clause
            conditions = []
            params = []
            if wing:
                conditions.append("wing = ?")
                params.append(wing)
            if room:
                conditions.append("room = ?")
                params.append(room)
            if language:
                conditions.append("language = ?")
                params.append(language)

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            sql = f"""
                SELECT document_id, bm25(drawers_fts) as score, wing, room, language
                FROM drawers_fts
                WHERE drawers_fts MATCH ?
                AND {where_clause}
                ORDER BY score
                LIMIT ?
            """
            params = [query] + params + [n_results]

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            conn.close()

            return [
                {"document_id": r["document_id"], "score": r["score"], "wing": r["wing"], "room": r["room"], "language": r["language"]}
                for r in rows
            ]
        except sqlite3.Error as e:
            logger.warning("FTS5 search failed for query '%s': %s", query, e)
            return []

    def search_by_prefix(
        self,
        prefix: str,
        n_results: int = 10,
        wing: Optional[str] = None,
        room: Optional[str] = None,
    ) -> list[dict]:
        """Search for documents matching a prefix (for identifier lookup)."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row

            conditions = []
            params = []
            if wing:
                conditions.append("wing = ?")
                params.append(wing)
            if room:
                conditions.append("room = ?")
                params.append(room)

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            sql = f"""
                SELECT document_id, bm25(drawers_fts) as score, wing, room, language
                FROM drawers_fts
                WHERE drawers_fts MATCH ?
                AND {where_clause}
                ORDER BY score
                LIMIT ?
            """
            # Add * for prefix matching
            params = [prefix + "*"] + params + [n_results]

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            conn.close()

            return [
                {"document_id": r["document_id"], "score": r["score"], "wing": r["wing"], "room": r["room"], "language": r["language"]}
                for r in rows
            ]
        except sqlite3.Error as e:
            logger.warning("FTS5 prefix search failed: %s", e)
            return []

    def bulk_insert_batch(self, entries: list[dict]) -> None:
        """
        Bulk-insert a batch of entries in one transaction.

        Does NOT clear the table — safe for streaming rebuild where the caller
        clears once before the first batch.

        entries: list of dicts with keys: document_id, content, wing, room, language
        """
        if not entries:
            return
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                for entry in entries:
                    conn.execute(
                        "INSERT INTO drawers_fts(document_id, content, wing, room, language) VALUES (?, ?, ?, ?, ?)",
                        (
                            entry["document_id"],
                            entry["content"],
                            entry.get("wing", ""),
                            entry.get("room", ""),
                            entry.get("language", ""),
                        ),
                    )
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                logger.warning("FTS5 bulk insert failed: %s", e)

    def sample_ids(self, n: int = 10) -> list[str]:
        """
        Return a sample of up to n document IDs from the index.

        Public API — diagnostics should use this instead of opening
        a raw sqlite3 connection on db_path.
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            cur = conn.execute(f"SELECT document_id FROM drawers_fts LIMIT {n}")
            ids = [row[0] for row in cur.fetchall()]
            conn.close()
            return ids
        except sqlite3.Error:
            return []

    def count(self) -> int:
        """Return the total number of indexed documents."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            cursor = conn.execute("SELECT COUNT(*) FROM drawers_fts")
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except sqlite3.Error:
            return 0

    def clear(self) -> None:
        """Clear all entries from the index. Used for rebuilds."""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute("DELETE FROM drawers_fts")
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                logger.warning("FTS5 clear failed: %s", e)