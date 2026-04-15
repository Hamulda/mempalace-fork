"""
LanceDB backend for MemPalace.

Multi-version concurrency control (MVCC) enables 6+ parallel Claude Code
sessions writing to the same palace without SQLite_BUSY lock conflicts.

Hybrid search: combines vector similarity + full-text search (FTS)
via LanceDB's native query engine.

Embedding: ModernBERT-embed-base 4-bit MLX (256 dims Matryoshka, ~85MB RAM).
M1-optimized: runs on CPU, no MPS/GPU conflicts across sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from ..exceptions import MemoryPressureError

# LanceDB core + dependencies
try:
    import lancedb
    import pandas  # noqa: F401
    import pyarrow as pa
except ImportError as _exc:
    raise ImportError(
        "LanceDB backend requires: pip install 'mempalace[lance]'"
    ) from _exc

from .base import BaseCollection

logger = logging.getLogger(__name__)

# ── Embedding daemon client (Unix socket) ─────────────────────────────────────

_DAEMON_LOCK = threading.Lock()
_DAEMON_STARTED = False
_SOCK_TIMEOUT = 30.0  # seconds to wait for daemon startup


def _get_socket_path() -> str:
    return os.environ.get(
        "MEMPALACE_EMBED_SOCK",
        os.path.expanduser("~/.mempalace/embed.sock"),
    )


def _daemon_is_running() -> bool:
    """Check if the embedding daemon socket is responsive."""
    sock_path = _get_socket_path()
    if not os.path.exists(sock_path):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(sock_path)
        payload = json.dumps({"texts": []}).encode("utf-8")
        s.sendall(len(payload).to_bytes(4, "big") + payload)
        raw_len = s.recv(4)
        if raw_len:
            msg_len = int.from_bytes(raw_len, "big")
            s.recv(msg_len)
        s.close()
        return True
    except Exception:
        return False


def _start_daemon_if_needed() -> bool:
    """
    Start the embedding daemon if it is not running.
    Thread-safe. Uses double-check locking.
    """
    global _DAEMON_STARTED

    if _daemon_is_running():
        return True

    with _DAEMON_LOCK:
        if _daemon_is_running():
            return True

        logger.info("Starting MemPalace embedding daemon...")

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "mempalace.embed_daemon"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

            deadline = time.monotonic() + _SOCK_TIMEOUT
            ready = False
            while time.monotonic() < deadline:
                line = proc.stdout.readline().decode("utf-8", errors="ignore").strip()
                if line == "READY":
                    ready = True
                    break
                if proc.poll() is not None:
                    err = proc.stderr.read().decode("utf-8", errors="ignore")
                    logger.error("Embedding daemon failed to start: %s", err)
                    return False

            if not ready:
                logger.warning("Embedding daemon startup timeout after %ss", _SOCK_TIMEOUT)
                return False

            if _daemon_is_running():
                _DAEMON_STARTED = True
                logger.info("Embedding daemon started (PID %d)", proc.pid)
                return True

            return False

        except Exception as e:
            logger.warning("Could not start embedding daemon: %s", e)
            return False


from ..circuit_breaker import _embed_circuit


def _embed_via_socket(texts: List[str]) -> List[List[float]]:
    """Send texts to the daemon via Unix socket, return embeddings."""
    if not _embed_circuit.should_try_socket():
        raise RuntimeError("Circuit open, using fallback")

    sock_path = _get_socket_path()
    payload = json.dumps({"texts": texts}).encode("utf-8")

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(60.0)
    s.connect(sock_path)

    try:
        s.sendall(len(payload).to_bytes(4, "big") + payload)

        raw_len = b""
        while len(raw_len) < 4:
            chunk = s.recv(4 - len(raw_len))
            if not chunk:
                raise ConnectionError("Daemon closed connection")
            raw_len += chunk
        msg_len = int.from_bytes(raw_len, "big")

        data = b""
        while len(data) < msg_len:
            chunk = s.recv(min(65536, msg_len - len(data)))
            if not chunk:
                raise ConnectionError("Daemon closed connection mid-message")
            data += chunk

        response = json.loads(data.decode("utf-8"))
        if response.get("error"):
            raise RuntimeError(f"Daemon embedding error: {response['error']}")

        embeddings = response["embeddings"]
        # Truncate to EMBEDDING_DIMS for Matryoshka compatibility
        _embed_circuit.record_success()
        return [e[:EMBEDDING_DIMS] for e in embeddings]
    except Exception:
        _embed_circuit.record_failure()
        raise
    finally:
        s.close()


# ── Fallback: in-process embedding (used when daemon is unavailable) ───────────

_fallback_model = None
_fallback_lock = threading.Lock()


def _embed_texts_fallback(texts: List[str]) -> List[List[float]]:
    """In-process fallback — loads fastembed into this process.

    Memory guardrail: refuse to load model if memory is already critical.
    On M1 8GB, fastembed model needs ~500MB RAM — loading at critical pressure
    would cause system instability.
    """
    global _fallback_model
    with _fallback_lock:
        if _fallback_model is None:
            # Memory pressure check before loading model
            try:
                from ..memory_guard import MemoryGuard, MemoryPressure
                guard = MemoryGuard.get()
                if guard.pressure == MemoryPressure.CRITICAL:
                    raise MemoryPressureError(
                        f"Cannot load in-process embedding model: "
                        f"memory pressure is {guard.used_ratio:.0%}. "
                        f"Start the embed daemon instead: mempalace embed-daemon start"
                    )
                if guard.pressure == MemoryPressure.WARN:
                    logger.warning(
                        "Memory pressure is WARN (%.0f%%). "
                        "Loading in-process embedding model — consider starting the daemon instead.",
                        guard.used_ratio * 100
                    )
            except ImportError:
                pass  # MemoryGuard not available, proceed cautiously

            logger.warning(
                "Using in-process embedding (no daemon). "
                "Run: mempalace embed-daemon start"
            )
            from fastembed import TextEmbedding

            _fallback_model = TextEmbedding(
                model_name="BAAI/bge-small-en-v1.5",
                cache_dir=os.path.expanduser("~/.cache/fastembed"),
            )

    embeddings = [emb.tolist() for emb in _fallback_model.embed(texts)]
    # Truncate to EMBEDDING_DIMS for Matryoshka compatibility
    return [e[:EMBEDDING_DIMS] for e in embeddings]


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """
    Return EMBEDDING_DIMS-dim embeddings for a list of texts.

    Prefers the Unix socket daemon. Falls back to in-process fastembed
    if the daemon is unavailable.
    """
    if not texts:
        return []

    daemon_ok = _start_daemon_if_needed()

    if daemon_ok:
        try:
            return _embed_via_socket(texts)
        except Exception as e:
            logger.warning("Socket embedding failed (%s), using fallback", e)
            global _DAEMON_STARTED
            _DAEMON_STARTED = False

    return _embed_texts_fallback(texts)


# ── Semantic Deduplicator ──────────────────────────────────────────────────────

class SemanticDeduplicator:
    """
    Detect semantically similar or conflicting memories before write.

    Three outcomes:
      unique    — no similar memory exists → write new
      duplicate — cosine > high_threshold → skip, existing is good enough
      conflict  — cosine > low_threshold + same wing/room but different content
                  → overwrite existing with newer
    """

    def __init__(
        self,
        high_threshold: float = 0.92,
        low_threshold: float = 0.82,
    ):
        self.high_threshold = float(os.environ.get(
            "MEMPALACE_DEDUP_HIGH", str(high_threshold)
        ))
        self.low_threshold = float(os.environ.get(
            "MEMPALACE_DEDUP_LOW", str(low_threshold)
        ))

    def classify(
        self,
        new_doc: str,
        new_metadata: dict,
        collection: "LanceCollection",
        n_candidates: int = 5,
    ) -> tuple[str, Optional[str]]:
        """
        Returns (action, existing_id) where action is:
          "unique"    — write normally
          "duplicate" — skip (existing is good enough)
          "conflict"  — overwrite existing
        """
        if collection.count() == 0:
            return "unique", None

        try:
            query_emb = _embed_texts([new_doc])[0]
            results = collection.query_by_vector(
                vector=query_emb,
                n_results=n_candidates,
            )
        except Exception:
            return "unique", None

        if not results["ids"] or not results["ids"][0]:
            return "unique", None

        best_dist = results["distances"][0][0]
        best_id = results["ids"][0][0]
        best_meta = (results["metadatas"][0][0] or {}) if results["metadatas"] else {}

        # LanceDB distance is Euclidean; convert to similarity
        # For normalized vectors: similarity ≈ 1 - distance
        best_similarity = max(0.0, 1.0 - best_dist)

        if best_similarity >= self.high_threshold:
            return "duplicate", best_id

        if best_similarity >= self.low_threshold:
            # Similar but not identical — check for conflict (same wing/room)
            new_wing = new_metadata.get("wing", "")
            new_room = new_metadata.get("room", "")
            old_wing = best_meta.get("wing", "")
            old_room = best_meta.get("room", "")

            if new_wing and old_wing and new_wing == old_wing and new_room == old_room:
                return "conflict", best_id

        return "unique", None

    def classify_batch(
        self,
        documents: list[str],
        metadatas: list[dict],
        collection: "LanceCollection",
        n_candidates: int = 5,
    ) -> list[tuple[str, Optional[str]]]:
        """
        Klasifikuje celý batch dokumentů najednou.

        Klíčová optimalizace: embeddingy pro dedup check jsou vypočítány
        JEDNÍM voláním _embed_texts(documents) místo N voláními po 1.
        """
        if collection.count() == 0:
            return [("unique", None)] * len(documents)

        # JEDEN batch embedding call pro všechny dokumenty
        batch_vectors = _embed_texts(documents)

        results = []
        for doc, meta, vec in zip(documents, metadatas, batch_vectors):
            # Hledej podobné vzpomínky přes přímé vector search
            similar = collection.query_by_vector(
                vector=vec,
                n_results=n_candidates,
            )

            if not similar["ids"] or not similar["ids"][0]:
                results.append(("unique", None))
                continue

            best_dist = similar["distances"][0][0]
            best_id = similar["ids"][0][0]
            best_meta = (similar["metadatas"][0][0] or {}) if similar["metadatas"] else {}

            best_similarity = max(0.0, 1.0 - best_dist)

            if best_similarity >= self.high_threshold:
                results.append(("duplicate", best_id))
            elif best_similarity >= self.low_threshold:
                if (meta.get("room") == best_meta.get("room") or
                        meta.get("wing") == best_meta.get("wing")):
                    results.append(("conflict", best_id))
                else:
                    results.append(("unique", None))
            else:
                results.append(("unique", None))

        return results


# ── LanceDB Async Optimizer ───────────────────────────────────────────────────

class LanceOptimizer:
    """
    Background compaction of LanceDB delta files.

    Triggers:
      - Every OPTIMIZE_WRITES_THRESHOLD writes
      - Every OPTIMIZE_INTERVAL_SECONDS seconds

    Runs in a background thread — does not block writes.
    Uses a lock file to prevent concurrent optimize runs.
    """

    OPTIMIZE_WRITES_THRESHOLD = int(os.environ.get("MEMPALACE_OPTIMIZE_EVERY", "200"))
    OPTIMIZE_INTERVAL_SECONDS = int(os.environ.get("MEMPALACE_OPTIMIZE_INTERVAL", "3600"))

    def __init__(self, palace_path: str, collection_name: str):
        self._palace_path = palace_path
        self._collection_name = collection_name
        self._writes_since_optimize = 0
        self._last_optimize_time = 0.0
        self._lock = threading.Lock()
        self._lock_file = Path(palace_path) / ".optimize_lock"

    def record_write(self) -> None:
        """Call after every successful write. Triggers async optimize if needed."""
        with self._lock:
            self._writes_since_optimize += 1
            now = time.monotonic()

            should_optimize = (
                self._writes_since_optimize >= self.OPTIMIZE_WRITES_THRESHOLD
                or (now - self._last_optimize_time) > self.OPTIMIZE_INTERVAL_SECONDS
            )

            if should_optimize:
                self._writes_since_optimize = 0
                self._last_optimize_time = now
                t = threading.Thread(target=self._run_optimize, daemon=True)
                t.start()

    def _run_optimize(self) -> None:
        """Compacts delta files. Runs in background thread."""
        if self._lock_file.exists():
            logger.debug("LanceDB optimize already running, skipping")
            return

        try:
            self._lock_file.touch()
            logger.info("Starting LanceDB optimize for %s", self._collection_name)

            asyncio.run(self._async_optimize())

            # Clean up .tmp* directories left by optimize
            palace = Path(self._palace_path)
            for tmp_dir in palace.rglob(".tmp*"):
                if tmp_dir.is_dir():
                    shutil.rmtree(tmp_dir, ignore_errors=True)

            # Clean up stale _indices directories — keep only 2 newest
            table_dir = palace / f"{self._collection_name}.lance" / "_indices"
            if table_dir.exists():
                index_dirs = sorted(
                    [d for d in table_dir.iterdir() if d.is_dir()],
                    key=lambda d: d.stat().st_mtime,
                    reverse=True,
                )
                for old_dir in index_dirs[2:]:
                    logger.info("Removing stale index dir: %s", old_dir.name)
                    shutil.rmtree(old_dir, ignore_errors=True)
                # Remove empty index dirs
                for index_dir in table_dir.iterdir():
                    if index_dir.is_dir() and not any(index_dir.iterdir()):
                        logger.info("Removing empty index dir: %s", index_dir.name)
                        shutil.rmtree(index_dir, ignore_errors=True)

            logger.info("LanceDB optimize complete")
        except Exception as e:
            logger.warning("LanceDB optimize failed: %s", e)
        finally:
            try:
                self._lock_file.unlink()
            except FileNotFoundError:
                pass

    async def _async_optimize(self) -> None:
        """Async compact via lancedb async connection."""
        try:
            db = await lancedb.connect_async(self._palace_path)
            table = await db.open_table(self._collection_name)
            await table.optimize(
                cleanup_older_than=timedelta(seconds=0),
                delete_unverified=True,
            )
        except Exception as e:
            # Fallback: use synchronous API if async fails
            logger.debug("Async optimize not available, skipping: %s", e)

    def run_optimize_sync(self) -> None:
        """Synchronous optimize — for CLI use."""
        if self._lock_file.exists():
            # Check if lock is stale (no PID or process dead)
            stale = True
            try:
                pid_text = self._lock_file.read_text().strip()
                if pid_text:
                    pid = int(pid_text)
                    try:
                        import signal
                        os.kill(pid, 0)  # check if process alive
                        stale = False
                    except (ProcessLookupError, ValueError, PermissionError):
                        pass
            except Exception:
                pass

            if not stale:
                raise RuntimeError("Optimize already in progress")

            # Stale lock — remove and proceed
            try:
                self._lock_file.unlink()
            except FileNotFoundError:
                pass

        self._lock_file.write_text(str(os.getpid()))
        try:
            asyncio.run(self._async_optimize())
        finally:
            try:
                self._lock_file.unlink()
            except FileNotFoundError:
                pass


# ── Retry helper ───────────────────────────────────────────────────────────────

def _write_with_retry(fn, max_retries: int = 7):
    """Exponential backoff retry for LanceDB commit conflicts (MVCC)."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e).lower()
            if any(kw in err for kw in ("commit conflict", "conflict", "retry", "transaction")) \
                    and attempt < max_retries - 1:
                wait = 0.05 * (2 ** attempt) + random.random() * 0.05
                time.sleep(wait)
                continue
            raise


# ── Embedding dimensions (configurable for Matryoshka truncation) ──────────────────

EMBEDDING_DIMS = int(os.environ.get("MEMPALACE_EMBEDDING_DIMS", "256"))


# ── Schema ─────────────────────────────────────────────────────────────────────

def _get_drawer_schema() -> pa.Schema:
    """LanceDB schema using PyArrow for drawer records."""
    return pa.schema(
        [
            pa.field("id", pa.string(), False),
            pa.field("document", pa.string(), False),
            pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIMS), False),
            pa.field("metadata_json", pa.string(), True),
            pa.field("created_at", pa.float64(), False),
        ]
    )


# ── Table creation ─────────────────────────────────────────────────────────────

def _create_lance_table(db, collection_name: str) -> "lancedb.table.Table":
    """Create a new LanceDB table with schema, vector index, and FTS index."""
    schema = _get_drawer_schema()
    table = db.create_table(collection_name, schema=schema)

    # Vector index for fast similarity search
    try:
        table.create_index("vector")
    except Exception:
        pass

    # FTS index for full-text search
    try:
        table.create_fts_index("document", replace=False)
    except Exception:
        pass

    return table


# ── Where-filter translation ──────────────────────────────────────────────────

def _sql_val(v: Any) -> str:
    if isinstance(v, str):
        escaped = v.replace("'", "''")
        return f"'{escaped}'"
    if v is None:
        return "NULL"
    return str(v)


def _json_col(col: str) -> str:
    """Cast metadata_json to VARBINARY for LanceDB 4.x json_extract compatibility."""
    return f"CAST({col} AS VARBINARY)"


def _json_eq(k: str, v: Any) -> str:
    return f"json_extract({_json_col('metadata_json')}, '$.{k}') = {_sql_val(v)}"


def _json_in(k: str, v: List[Any]) -> str:
    vals = ", ".join(_sql_val(x) for x in v)
    return f"json_extract({_json_col('metadata_json')}, '$.{k}') IN ({vals})"


def _json_cmp(k: str, op_: str, v: Any) -> str:
    return f"json_extract({_json_col('metadata_json')}, '$.{k}') {op_} {_sql_val(v)}"


def _where_to_sql(where: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Convert ChromaDB-style where dict to SQL WHERE clause.

    ChromaDB where syntax:
        {"key": {"$eq": "val"}}                   → key = 'val'
        {"key": {"$ne": "val"}}                   → key != 'val'
        {"key": {"$in": ["a","b"]}}               → key IN ('a','b')
        {"key": {"$nin": ["a","b"]}}              → key NOT IN ('a','b')
        {"key": {"$gt": 5}}                       → key > 5
        {"key": {"$gte": 5}}                      → key >= 5
        {"key": {"$lt": 5}}                       → key < 5
        {"key": {"$lte": 5}}                      → key <= 5
        {"$and": [{"key1": {"$eq": "a"}}, ...]}   → key1 = 'a' AND ...
        {"$or": [{"key1": {"$eq": "a"}}, ...]}    → key1 = 'a' OR ...

    Scalar syntax (metadata equality, NOT id filter):
        {"wing": "x"}                             → metadata_json.wing = 'x'
        {"room": "y"}                            → metadata_json.room = 'y'
        {"wing": "x", "room": "y"}               → wing='x' AND room='y'
    """
    if not where:
        return None

    if "$and" in where:
        parts = [_where_to_sql(sub) for sub in where["$and"]]
        parts = [p for p in parts if p]
        return "(" + " AND ".join(parts) + ")" if parts else None

    if "$or" in where:
        parts = [_where_to_sql(sub) for sub in where["$or"]]
        parts = [p for p in parts if p]
        return "(" + " OR ".join(parts) + ")" if parts else None

    parts = []
    for key, cond in where.items():
        # Scalar value — metadata equality, NOT id filter
        if not isinstance(cond, dict):
            parts.append(_json_eq(key, cond))
            continue

        op = list(cond.keys())[0]
        val = cond[op]

        if op == "$eq":
            parts.append(_json_eq(key, val))
        elif op == "$ne":
            parts.append(f"NOT {_json_eq(key, val)}")
        elif op == "$in":
            parts.append(_json_in(key, val))
        elif op == "$nin":
            parts.append(f"NOT {_json_in(key, val)}")
        elif op == "$gt":
            parts.append(_json_cmp(key, ">", val))
        elif op == "$gte":
            parts.append(_json_cmp(key, ">=", val))
        elif op == "$lt":
            parts.append(_json_cmp(key, "<", val))
        elif op == "$lte":
            parts.append(_json_cmp(key, "<=", val))

    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "(" + " AND ".join(parts) + ")"


def _meta_val(row: "pandas.Series", key: str) -> Any:
    """Extract metadata field value from a DataFrame row."""
    try:
        m = json.loads(row.get("metadata_json") or "{}")
        return m.get(key)
    except (json.JSONDecodeError, TypeError):
        return None


def _apply_where_filter(df: "pandas.DataFrame", where: Optional[Dict[str, Any]]) -> "pandas.DataFrame":
    """Apply a ChromaDB-style where filter to a pandas DataFrame.

    Handles:
      - Scalar metadata equality: {"wing": "x"} → metadata.wing == 'x'
      - Explicit operators: {"wing": {"$eq": "x"}} → same
      - $and / $or logical combinators
      - id filter: {"id": "..."} or {"id": {"$eq": "..."}}
    """
    if not where:
        return df

    if "$and" in where:
        for sub in where["$and"]:
            df = _apply_where_filter(df, sub)
        return df

    if "$or" in where:
        mask = pandas.Series([False] * len(df), index=df.index)
        for sub in where["$or"]:
            mask = mask | _apply_where_filter(df, sub)["id"].isin(df["id"])
        return df[mask]

    for key, cond in where.items():
        if key == "id":
            if isinstance(cond, dict):
                op = list(cond.keys())[0]
                val = cond[op]
                if op == "$eq":
                    df = df[df["id"] == val]
                elif op == "$in":
                    df = df[df["id"].isin(val)]
            else:
                df = df[df["id"] == cond]
        elif isinstance(cond, dict):
            op = list(cond.keys())[0]
            val = cond[op]
            if op == "$eq":
                df = df[df.apply(lambda r: _meta_val(r, key) == val, axis=1)]
            elif op == "$ne":
                df = df[df.apply(lambda r: _meta_val(r, key) != val, axis=1)]
            elif op == "$in":
                df = df[df.apply(lambda r: _meta_val(r, key) in val, axis=1)]
            elif op == "$nin":
                df = df[df.apply(lambda r: _meta_val(r, key) not in val, axis=1)]
            elif op == "$gt":
                df = df[df.apply(lambda r: _meta_val(r, key) is not None and _meta_val(r, key) > val, axis=1)]
            elif op == "$gte":
                df = df[df.apply(lambda r: _meta_val(r, key) is not None and _meta_val(r, key) >= val, axis=1)]
            elif op == "$lt":
                df = df[df.apply(lambda r: _meta_val(r, key) is not None and _meta_val(r, key) < val, axis=1)]
            elif op == "$lte":
                df = df[df.apply(lambda r: _meta_val(r, key) is not None and _meta_val(r, key) <= val, axis=1)]
        else:
            # Scalar metadata equality: {"wing": "x"} → metadata.wing == 'x'
            df = df[df.apply(lambda r: _meta_val(r, key) == cond, axis=1)]

    return df


def _apply_time_decay(
    results_df: "pandas.DataFrame",
    decay_lambda: float = 0.005,
) -> "pandas.DataFrame":
    """
    Re-rank results by combining semantic relevance with recency.

    Final score = relevance * recency_weight
    where recency_weight = exp(-lambda * days_old)

    decay_lambda=0.005 means:
      - 0 days old:  weight = 1.00
      - 30 days old: weight = 0.86
      - 90 days old: weight = 0.64
      - 180 days old: weight = 0.41
    """
    if decay_lambda <= 0.0 or results_df.empty:
        return results_df

    if "created_at" not in results_df.columns:
        return results_df

    now = time.time()

    # Determine relevance column
    if "_relevance_score" in results_df.columns:
        scores = np.clip(results_df["_relevance_score"].values, 0, 1)
    elif "_distance" in results_df.columns:
        max_dist = results_df["_distance"].max()
        scores = 1.0 - (results_df["_distance"].values / (max_dist + 1e-9))
    else:
        return results_df

    # Compute recency weights
    days_old = np.clip((now - results_df["created_at"].values) / 86400.0, 0, None)
    recency_weights = np.exp(-decay_lambda * days_old)

    # Combined score
    combined = scores * recency_weights

    results_df = results_df.copy()
    results_df["_combined_score"] = combined
    results_df = results_df.sort_values("_combined_score", ascending=False)

    return results_df


# ── LanceCollection ──────────────────────────────────────────────────────────

class LanceCollection(BaseCollection):
    """
    LanceDB-backed collection implementing BaseCollection.

    Features:
    - MVCC: safe concurrent writes from multiple processes (with retry logic)
    - Hybrid search: vector similarity + FTS on document text
    - Semantic deduplication: prevents duplicate/conflicting writes
    - LanceDB compaction: background optimize every 200 writes / 1 hour
    - FTS throttling: rebuild index every 50 writes, not every write
    - M1 CPU: no GPU memory conflicts
    """

    BATCH_SIZE = 500  # Records per add/upsert batch

    def __init__(
        self,
        table: "lancedb.table.Table",
        palace_path: str = None,
        collection_name: str = "mempalace_drawers",
    ):
        self._table = table
        self._writes_since_reindex: int = 0
        self._reindex_threshold: int = 50
        self._palace_path = palace_path or ""
        self._collection_name = collection_name
        self._optimizer: Optional[LanceOptimizer] = None
        if palace_path:
            self._optimizer = LanceOptimizer(palace_path, collection_name)

        # Write coalescer (500ms window, deaktivovat přes MEMPALACE_COALESCE_MS=0)
        coalesce_ms = int(os.environ.get("MEMPALACE_COALESCE_MS", "500"))
        if coalesce_ms > 0:
            from ..write_coalescer import WriteCoalescer
            self._coalescer = WriteCoalescer(self, window_ms=coalesce_ms)
        else:
            self._coalescer = None

        # Query cache (inicializuje se lazily při prvním query)
        self._query_cache = None

    def _write_with_retry(self, fn, max_retries: int = 7):
        """Exponential backoff retry for LanceDB commit conflicts."""
        for attempt in range(max_retries):
            try:
                return fn()
            except Exception as e:
                err = str(e).lower()
                if any(kw in err for kw in ("commit conflict", "conflict", "retry", "transaction")) \
                        and attempt < max_retries - 1:
                    wait = 0.05 * (2 ** attempt) + random.random() * 0.05
                    time.sleep(wait)
                    continue
                raise

    def _maybe_rebuild_fts(self) -> None:
        """Throttled FTS index rebuild — runs every _reindex_threshold writes."""
        self._writes_since_reindex += 1
        if self._writes_since_reindex >= self._reindex_threshold:
            try:
                self._table.create_fts_index("document", replace=True)
            except Exception:
                pass
            self._writes_since_reindex = 0

    def rebuild_fts_index(self) -> None:
        """Force immediate FTS index rebuild. Call after bulk migration."""
        self._table.create_fts_index("document", replace=True)
        self._writes_since_reindex = 0

    def run_optimize(self) -> None:
        """Run synchronous LanceDB optimize. For CLI use."""
        if self._optimizer:
            self._optimizer.run_optimize_sync()

    def query_by_vector(
        self,
        vector: list[float],
        n_results: int = 5,
        where: Optional[dict] = None,
    ) -> dict:
        """
        Vector search s předpočítaným vektorem (bez re-embedding).
        Používá se z SemanticDeduplicator pro batch dedup.
        """
        search = (
            self._table
            .search(vector, query_type="vector", vector_column_name="vector")
            .limit(n_results)
        )
        if where:
            sql = _where_to_sql(where)
            if sql:
                search = search.where(sql, prefilter=True)

        results = search.to_pandas()

        if results.empty:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        if "_distance" in results.columns:
            distances = [float(d) for d in results["_distance"]]
        else:
            distances = [0.0] * len(results)

        metas_out = []
        for meta_json in results["metadata_json"]:
            try:
                metas_out.append(json.loads(meta_json) if meta_json else {})
            except json.JSONDecodeError:
                metas_out.append({})

        return {
            "ids": [[str(r) for r in results["id"]]],
            "documents": [[str(r) for r in results["document"]]],
            "metadatas": [metas_out],
            "distances": [distances],
        }

    # ── Write operations ──────────────────────────────────────────────────

    def add(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not documents:
            return

        if self._coalescer is not None:
            self._coalescer.enqueue(documents, ids, metadatas or [{} for _ in documents])
        else:
            self._do_add(documents=documents, ids=ids, metadatas=metadatas)

    def _do_add(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Internal add implementation. Bypasses coalescer. Used by WriteCoalescer."""
        if not documents:
            return

        metadatas = metadatas or [{}] * len(documents)

        # Memory pressure check
        from ..memory_guard import MemoryGuard
        guard = MemoryGuard.get()
        if guard.should_pause_writes():
            logger.warning(
                "Memory pressure CRITICAL (%.0f%% RAM used). "
                "Pausing write for up to 30s...",
                guard.used_ratio * 100
            )
            if not guard.wait_for_nominal(timeout=30.0):
                raise MemoryPressureError(
                    f"Cannot write to palace: system memory at "
                    f"{guard.used_ratio:.0%}. Close some apps and retry."
                )

        # Check for duplicate ids (with retry)
        for doc_id in ids:
            def check_dup():
                dup = self._table.search().where(f"id = '{doc_id}'").to_pandas()
                return dup

            result = self._write_with_retry(check_dup)
            if not result.empty:
                raise ValueError(
                    f"Record with id '{doc_id}' already exists. Use upsert() to update."
                )

        # BATCH Semantic deduplication – jeden embedding call pro celý batch
        deduplicator = SemanticDeduplicator()
        classifications = deduplicator.classify_batch(
            documents=documents,
            metadatas=metadatas,
            collection=self,
        )

        final_docs, final_ids, final_metas = [], [], []
        skipped, conflicts = 0, 0

        for doc, doc_id, meta, (action, existing_id) in zip(
            documents, ids, metadatas, classifications
        ):
            if action == "duplicate":
                skipped += 1
                logger.debug("Skipping duplicate memory: %s (similar to %s)", doc_id, existing_id)
                continue

            if action == "conflict":
                self._write_with_retry(
                    lambda eid=existing_id: self._table.delete(f"id = '{eid}'")
                )
                conflicts += 1
                logger.debug("Resolved conflict: replaced %s with %s", existing_id, doc_id)

            final_docs.append(doc)
            final_ids.append(doc_id)
            final_metas.append(meta)

        if skipped > 0 or conflicts > 0:
            logger.info(
                "Semantic dedup: skipped %d duplicates, resolved %d conflicts",
                skipped, conflicts,
            )

        if not final_docs:
            return

        # Compute embeddings
        embeddings = _embed_texts(final_docs)
        now = time.time()

        records = [
            {
                "id": did,
                "document": doc,
                "vector": emb,
                "metadata_json": json.dumps(meta, default=str),
                "created_at": now,
            }
            for did, doc, emb, meta in zip(final_ids, final_docs, embeddings, final_metas)
        ]

        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i : i + self.BATCH_SIZE]
            self._write_with_retry(lambda b=batch: self._table.add(b))

        self._maybe_rebuild_fts()
        if self._optimizer:
            self._optimizer.record_write()

        # Invalidate query cache na této collection
        from ..query_cache import get_query_cache
        get_query_cache().invalidate_collection(self._palace_path, self._collection_name)

    def upsert(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not documents:
            return

        metadatas = metadatas or [{}] * len(documents)

        # Semantic deduplication for upsert too
        deduplicator = SemanticDeduplicator()
        final_docs, final_ids, final_metas = [], [], []
        skipped = 0

        for doc, doc_id, meta in zip(documents, ids, metadatas):
            action, existing_id = deduplicator.classify(doc, meta, self)
            if action == "duplicate":
                skipped += 1
                logger.debug("Upsert skipping duplicate: %s", doc_id)
                continue
            final_docs.append(doc)
            final_ids.append(doc_id)
            final_metas.append(meta)

        if skipped > 0:
            logger.info("Upsert semantic dedup: skipped %d duplicates", skipped)

        if not final_docs:
            return

        # Delete existing records (with retry)
        id_set = set(final_ids)
        if id_set:
            id_list = list(id_set)
            if len(id_list) == 1:
                where_clause = f"id = '{id_list[0]}'"
            else:
                where_clause = "id IN (" + ", ".join(repr(i) for i in id_list) + ")"

            def do_delete():
                self._table.delete(where_clause)

            self._write_with_retry(do_delete)

        # Compute embeddings
        embeddings = _embed_texts(final_docs)
        now = time.time()

        records = [
            {
                "id": did,
                "document": doc,
                "vector": emb,
                "metadata_json": json.dumps(meta, default=str),
                "created_at": now,
            }
            for did, doc, emb, meta in zip(final_ids, final_docs, embeddings, final_metas)
        ]

        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i : i + self.BATCH_SIZE]
            self._write_with_retry(lambda b=batch: self._table.add(b))

        self._maybe_rebuild_fts()
        if self._optimizer:
            self._optimizer.record_write()

        # Invalidate query cache
        from ..query_cache import get_query_cache
        get_query_cache().invalidate_collection(self._palace_path, self._collection_name)

    # ── Read operations ───────────────────────────────────────────────────

    def query(
        self,
        query_texts: Optional[List[str]] = None,
        n_results: int = 10,
        where: Optional[Dict[str, Any]] = None,
        include: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, List[List[Any]]]:
        query_text = (query_texts or [""])[0]

        if not query_text:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        # Cache lookup (pouze pro requesty bez where filtru – cacheable)
        from ..query_cache import get_query_cache
        cache = get_query_cache()
        if where is None:
            cached = cache.get(self._palace_path, self._collection_name, [query_text], n_results)
            if cached is not None:
                return cached

        # Compute query embedding
        query_emb = _embed_texts([query_text])[0]

        try:
            from lancedb.rerankers import RRFReranker

            search = (
                self._table
                .search(query_emb, vector_column_name="vector")
                .query_type("vector")
                .rerank(RRFReranker())
                .limit(n_results)
            )
            if where:
                sql = _where_to_sql(where)
                if sql:
                    search = search.where(sql, prefilter=True)

            results = search.to_pandas()

        except Exception:
            # Fallback: pure vector search without reranker
            search = (
                self._table
                .search(query_emb, vector_column_name="vector")
                .limit(n_results)
            )
            if where:
                sql = _where_to_sql(where)
                if sql:
                    search = search.where(sql, prefilter=True)
            results = search.to_pandas()

        if results.empty:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        # Apply time-decay re-ranking
        decay_lambda = float(os.environ.get("MEMPALACE_DECAY_LAMBDA", "0.005"))
        if decay_lambda > 0:
            results = _apply_time_decay(results, decay_lambda)

        ids_out = [str(r) for r in results["id"]]
        docs_out = [str(r) for r in results["document"]]
        metas_out = []
        for meta_json in results["metadata_json"]:
            try:
                metas_out.append(json.loads(meta_json) if meta_json else {})
            except json.JSONDecodeError:
                metas_out.append({})

        # LanceDB returns _distance (lower = better); ChromaDB convention matches
        # If time-decay was applied, use _combined_score
        if "_combined_score" in results.columns:
            distances = [1.0 - float(s) for s in results["_combined_score"]]
        elif "_distance" in results.columns:
            distances = [float(d) for d in results["_distance"]]
        elif "_relevance_score" in results.columns:
            distances = [1.0 - float(s) for s in results["_relevance_score"]]
        else:
            distances = [0.0] * len(ids_out)

        result = {
            "ids": [ids_out],
            "documents": [docs_out],
            "metadatas": [metas_out],
            "distances": [distances],
        }

        # Cache set (pouze pro requesty bez where filtru)
        if where is None:
            cache.set(self._palace_path, self._collection_name, [query_text], n_results, result)

        return result

    def get(
        self,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, List[Any]]:
        try:
            if ids:
                if len(ids) == 1:
                    results = (
                        self._table.search()
                        .where(f"id = '{ids[0]}'")
                        .to_pandas()
                    )
                else:
                    id_list = ", ".join(repr(i) for i in ids)
                    results = (
                        self._table.search()
                        .where(f"id IN ({id_list})")
                        .to_pandas()
                    )
            elif where:
                # LanceDB's json_extract SQL function cannot operate on the UTF-8
                # metadata_json string column (requires LargeBinary). Fall back to
                # pandas filtering for metadata conditions.
                effective_limit = min(limit or 5000, 5000)
                search = self._table.search()
                if offset is not None and offset > 0:
                    search = search.offset(offset)
                results = search.limit(effective_limit).to_pandas()
                # Apply metadata filter via pandas (safe — works on JSON strings)
                if not results.empty:
                    results = _apply_where_filter(results, where)
            else:
                # No filter — enforce hard limit to prevent full-table RAM spike.
                # 5000 is a safe default that fits comfortably in 8GB RAM.
                effective_limit = min(limit or 5000, 5000)
                search = self._table.search()
                if offset is not None and offset > 0:
                    search = search.offset(offset)
                results = search.limit(effective_limit).to_pandas()
        except Exception:
            return {"ids": [], "documents": [], "metadatas": []}

        if results.empty:
            return {"ids": [], "documents": [], "metadatas": []}

        if limit:
            results = results.head(limit)

        return {
            "ids": results["id"].tolist(),
            "documents": results["document"].tolist(),
            "metadatas": [
                json.loads(m) if m else {}
                for m in results["metadata_json"]
            ],
        }

    def delete(
        self,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        if ids:
            if len(ids) == 1:
                where_clause = f"id = '{ids[0]}'"
            else:
                where_clause = "id IN (" + ", ".join(repr(i) for i in ids) + ")"

            def do_delete():
                self._table.delete(where_clause)

            self._write_with_retry(do_delete)
        elif where:
            # LanceDB's json_extract SQL cannot filter UTF-8 metadata_json strings.
            # Use pandas to find matching ids, then delete them one by one.
            try:
                batch_size = 500
                offset = 0
                while True:
                    # Pull a batch — LanceDB's limit works fine for pagination
                    all_batch = (
                        self._table.search()
                        .limit(batch_size)
                        .offset(offset)
                        .to_pandas()
                    )
                    if all_batch.empty:
                        break
                    # Filter to matching rows via pandas (handles JSON metadata)
                    match_batch = _apply_where_filter(all_batch, where)
                    if match_batch.empty:
                        if len(all_batch) < batch_size:
                            break
                        offset += batch_size
                        continue
                    matching_ids = match_batch["id"].tolist()
                    for mid in matching_ids:
                        self._write_with_retry(
                            lambda i=mid: self._table.delete(f"id = '{i}'")
                        )
                    if len(all_batch) < batch_size:
                        break
                    offset += batch_size
            except Exception:
                pass  # If we can't check, skip delete rather than risk RAM spike

    def count(self) -> int:
        try:
            return self._table.count_rows()
        except Exception:
            return 0

    def get_by_id(self, record_id: str) -> Optional[Dict[str, Any]]:
        result = self.get(ids=[record_id])
        if not result.get("ids"):
            return None
        return {
            "id": result["ids"][0],
            "document": result["documents"][0],
            "metadata": result["metadatas"][0] if result.get("metadatas") else {},
        }


# ── LanceBackend ─────────────────────────────────────────────────────────────

class LanceBackend:
    """
    LanceDB backend factory for MemPalace.

    Usage:
        backend = LanceBackend()
        collection = backend.get_collection("~/.mempalace/palace", "mempalace_drawers", create=True)
        collection.add(documents=["hello world"], ids=["1"], metadatas=[{"wing": "test"}])
    """

    def get_collection(
        self,
        palace_path: str,
        collection_name: str = "mempalace_drawers",
        create: bool = False,
    ):
        """
        Get or create a LanceDB table.

        Each call creates a fresh connection — never cache the db object
        across processes/threads to ensure proper MVCC isolation.

        Args:
            palace_path: Path to the palace data directory.
            collection_name: Name of the table.
            create: If True, create the table with schema and indexes if it doesn't exist.

        Returns:
            LanceCollection instance.
        """
        import lancedb

        os.makedirs(palace_path, exist_ok=True)
        try:
            os.chmod(palace_path, 0o700)
        except (OSError, NotImplementedError):
            pass

        # Fresh connection per call — MVCC requires this
        db = lancedb.connect(palace_path)

        try:
            table_names = db.list_table_names()
        except Exception:
            table_names = []

        if collection_name not in table_names:
            create = True

        if create:
            table = _create_lance_table(db, collection_name)
            return LanceCollection(table, palace_path=palace_path, collection_name=collection_name)

        else:
            if not os.path.isdir(palace_path):
                raise FileNotFoundError(f"Palace not found: {palace_path}")
            table = db.open_table(collection_name)
            return LanceCollection(table, palace_path=palace_path, collection_name=collection_name)
