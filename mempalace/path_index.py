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

SQLite LIKE wildcards:
    % matches any sequence of chars
    _ matches any single char
    ESCAPE clause disables these when searching for literal %, _, backslash
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("mempalace_path_index")

_ESCAPE_CHAR = "\\"


def _escape_sql_like(pattern: str) -> str:
    """Escape LIKE wildcards in a pattern so they match literally."""
    return (
        pattern
        .replace(_ESCAPE_CHAR, _ESCAPE_CHAR + _ESCAPE_CHAR)
        .replace("%", _ESCAPE_CHAR + "%")
        .replace("_", _ESCAPE_CHAR + "_")
    )


def _normalize_path_for_sql(path: str) -> str:
    """Normalize a path for SQL prefix/suffix matching.

    - Strips trailing slashes
    - Converts backslashes to forward slashes

    Note: Does NOT call realpath for project_path normalization.
    Stored source_file values are not normalized (backward compat), so
    project_path normalization must not use realpath either, or the two
    will diverge. To enable full /var ↔ /private/var symmetry, normalize
    source_file values at insert time using normalize_source_file().
    """
    if not path:
        return path
    path = path.replace("\\", "/").strip()
    return path.rstrip("/")


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
        Search path index for matching document_ids using indexed SQL stages.

        Matching priority (each stage bounded; early exit when limit reached):
        1. Exact source_file match — indexed equality, O(1)
        2. Exact repo_rel_path match — indexed equality, O(1)
        3. Suffix match — LIKE with leading wildcard, bounded scan + Python filter
        4. Basename exact match — indexed equality, bounded

        project_path filter is a strict boundary:
            source_file = project_path  OR  source_file LIKE project_path || '/%'
        Both stored source_file and project_path are normalized via realpath
        on macOS so /var and /private/var compare equal.

        SQL LIKE wildcards (percent, underscore, backslash) in query are
        escaped so they match as literals, not as wildcards.

        Returns list of dicts with document_id, source_file, language, etc.
        """
        if not query:
            return []

        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        with self._lock:
            conn = None
            try:
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row

                # Build project_path boundary (strict: exact OR prefix match)
                proj_clause, proj_params = self._project_path_filter(project_path)

                # Language filter — applied to all stages
                if language:
                    lang_clause = "language = ?"
                    lang_params: list[Any] = [language]
                else:
                    lang_clause = ""
                    lang_params = []

                def build_where(
                    extra: str = "",
                    extra_params: list[Any] | None = None,
                ) -> tuple[str, list[Any]]:
                    parts = ["is_latest=1"]
                    ps: list[Any] = []
                    if proj_clause:
                        parts.append(proj_clause)
                        ps.extend(proj_params)
                    if lang_clause:
                        parts.append(lang_clause)
                        ps.extend(lang_params)
                    if extra:
                        parts.append(extra)
                        if extra_params:
                            ps.extend(extra_params)
                    return " AND ".join(parts), ps

                def rows_to_hits(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
                    hits = []
                    for row in rows:
                        doc_id = row["document_id"]
                        if doc_id in seen:
                            continue
                        seen.add(doc_id)
                        hits.append({
                            "document_id": doc_id,
                            "source_file": row["source_file"],
                            "repo_rel_path": row["repo_rel_path"] or "",
                            "language": row["language"],
                            "chunk_kind": row["chunk_kind"],
                            "symbol_name": row["symbol_name"],
                            "line_start": row["line_start"],
                            "line_end": row["line_end"],
                            "wing": row["wing"],
                            "room": row["room"],
                        })
                        if len(hits) >= limit:
                            break
                    return hits

                def exclude_matched(extra: str, params: list[Any]) -> tuple[str, list[Any]]:
                    """Add NOT IN clause to exclude already-matched doc_ids."""
                    matched = [r["document_id"] for r in results]
                    if not matched:
                        return extra, params
                    placeholders = ",".join("?" * len(matched))
                    excl = f" AND document_id NOT IN ({placeholders})"
                    return extra + excl, params + matched

                # ── Stage 1: exact source_file (INDEXED) ─────────────────────
                where, params = build_where("source_file = ?", [query])
                rows = conn.execute(
                    f"SELECT * FROM path_index WHERE {where}", params
                ).fetchall()
                results.extend(rows_to_hits(rows))
                if len(results) >= limit:
                    return results[:limit]

                # ── Stage 2: exact repo_rel_path (INDEXED) ───────────────────
                extra, params = exclude_matched("repo_rel_path = ?", [query])
                where, params = build_where(extra, params)
                rows = conn.execute(
                    f"SELECT * FROM path_index WHERE {where}", params
                ).fetchall()
                results.extend(rows_to_hits(rows))
                if len(results) >= limit:
                    return results[:limit]

                # ── Stage 3: suffix match — bounded scan + Python filter ────
                #    LIKE with leading wildcard; limit scan to 3× limit
                escaped = _escape_sql_like(query)
                extra, params = exclude_matched(
                    f"source_file LIKE ? ESCAPE '{_ESCAPE_CHAR}'",
                    ["%" + escaped],
                )
                where, params = build_where(extra, params)
                scan_limit = limit * 3
                rows = conn.execute(
                    f"SELECT * FROM path_index WHERE {where} LIMIT {scan_limit}", params
                ).fetchall()
                # Python filter for true suffix semantics
                hits = []
                for row in rows:
                    sf = row["source_file"]
                    if sf.endswith(query) or sf.endswith("/" + query):
                        doc_id = row["document_id"]
                        if doc_id in seen:
                            continue
                        seen.add(doc_id)
                        hits.append({
                            "document_id": doc_id,
                            "source_file": sf,
                            "repo_rel_path": row["repo_rel_path"] or "",
                            "language": row["language"],
                            "chunk_kind": row["chunk_kind"],
                            "symbol_name": row["symbol_name"],
                            "line_start": row["line_start"],
                            "line_end": row["line_end"],
                            "wing": row["wing"],
                            "room": row["room"],
                        })
                        if len(hits) >= limit - len(results):
                            break
                results.extend(hits)
                if len(results) >= limit:
                    return results[:limit]

                # ── Stage 4: basename exact match (INDEXED, bounded) ───────────
                extra, params = exclude_matched("basename = ?", [query])
                where, params = build_where(extra, params)
                rows = conn.execute(
                    f"SELECT * FROM path_index WHERE {where} LIMIT {scan_limit}", params
                ).fetchall()
                results.extend(rows_to_hits(rows))

                conn.close()
            except Exception as e:
                logger.warning("PathIndex search failed: %s", e)
            finally:
                if conn:
                    conn.close()

        return results[:limit]

    def _project_path_filter(
        self, project_path: str | None
    ) -> tuple[str, list[Any]]:
        """Build strict project_path boundary SQL.

        Returns (where_clause, params) for:
            source_file = <project_path>  OR  source_file LIKE <project_path>/%

        Wildcards (%, _, backslash) in project_path itself are escaped so they
        match literally in the LIKE expression.

        Note: project_path is not normalized via realpath because stored
        source_file values are also not normalized. For full macOS
        /var ↔ /private/var symmetry, normalize source_file at insert
        time using normalize_source_file() and re-index existing data.
        """
        if not project_path:
            return "", []

        norm_pp = _normalize_path_for_sql(project_path)
        escaped = _escape_sql_like(norm_pp)
        prefix = escaped.rstrip("/") + "/"
        return (
            "(source_file = ? OR source_file LIKE ? ESCAPE ?)",
            [norm_pp, prefix + "%", _ESCAPE_CHAR],
        )

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

    @staticmethod
    def normalize_source_file(source_file: str) -> str:
        """Normalize source_file for consistent storage/lookup.

        On macOS resolves symlinks so /var -> /private/var.
        Converts backslashes to forward slashes and strips trailing slashes.
        """
        return _normalize_path_for_sql(source_file)
