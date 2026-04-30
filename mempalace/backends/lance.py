"""
LanceDB backend for MemPalace.

Multi-version concurrency control (MVCC) enables 6+ parallel Claude Code
sessions writing to the same palace without SQLite_BUSY lock conflicts.

LanceDB stores documents and vector embeddings.  SQLite FTS5 via the
KeywordIndex provides canonical full-text (BM25/lexical) search.

Embedding: fastembed BAAI/bge-small-en-v1.5 (256 dims, ~33MB RAM) for the
in-process fallback, or ModernBERT-embed-base 4-bit MLX (~85MB RAM) when the
embedding daemon is running on Apple Silicon.

M1-optimized: daemon uses MLX Metal GPU; fallback uses CPU.
No MPS/GPU conflicts across sessions when using the daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

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

_EMBED_SANITIZE_REPAIRS = 0
_EMBED_SANITIZE_REPAIRS_LOCK = threading.Lock()

# ── Embedding daemon client (Unix socket) ─────────────────────────────────────

_DAEMON_LOCK = threading.Lock()
_DAEMON_STARTED = False
_SOCK_TIMEOUT = float(os.environ.get(
    "MEMPALACE_EMBED_DAEMON_STARTUP_TIMEOUT",
    os.environ.get("MEMPALACE_EMBED_SOCKET_TIMEOUT", "120"),
))


class EmbeddingDaemonError(RuntimeError):
    """Raised when the embedding daemon is unavailable and in-process fallback is disabled."""

    def __init__(self, msg: str = "", cause: Exception | None = None):
        super().__init__(msg)
        self._cause = cause

    @property
    def cause(self) -> Exception | None:
        return self._cause


class DegenerateEmbeddingError(RuntimeError):
    """Raised when a degenerate vector is detected and in-process fallback is disabled."""


def _embed_fallback_enabled() -> bool:
    """True when in-process fastembed fallback is enabled (default).

    Enabled by: "1", "true" (case-insensitive), any truthy value except "0", "", "false".
    """
    val = os.environ.get("MEMPALACE_EMBED_FALLBACK", "1")
    return val in ("1",) or val.lower() in ("true", "yes", "on")


def _mark_daemon_dead(reason: str = "") -> None:
    """Mark the daemon as dead and reset global state."""
    global _DAEMON_STARTED
    _DAEMON_STARTED = False
    try:
        from ..circuit_breaker import _embed_circuit
        _embed_circuit.record_failure()
    except Exception as e:
        logger.debug("circuit_breaker.record_failure failed (non-critical): %s", e)
    logger.warning("Embedding daemon marked dead: %s", reason)


def _get_socket_path() -> str:
    return os.environ.get(
        "MEMPALACE_EMBED_SOCK",
        os.path.expanduser("~/.mempalace/embed.sock"),
    )


def _daemon_is_running() -> bool:
    """Check if the embedding daemon socket is responsive.

    A bounded health probe — never hangs.
    - missing socket path → False
    - connect failure → False
    - malformed JSON / wrong length / timeout → False
    - response must be dict with "embeddings" key whose value is a list
    - empty request {"texts": []} must receive {"embeddings": []} back
    """
    sock_path = _get_socket_path()
    if not os.path.exists(sock_path):
        return False
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(sock_path)
        payload = json.dumps({"texts": []}).encode("utf-8")
        s.sendall(len(payload).to_bytes(4, "big") + payload)
        raw_len = s.recv(4)
        if not raw_len or len(raw_len) < 4:
            return False
        msg_len = int.from_bytes(raw_len, "big")
        if msg_len > 10_000_000:  # sanity cap
            return False
        data = b""
        while len(data) < msg_len:
            chunk = s.recv(min(65536, msg_len - len(data)))
            if not chunk:
                return False
            data += chunk
        response = json.loads(data.decode("utf-8"))
        if not isinstance(response, dict):
            return False
        if "embeddings" not in response:
            return False
        if not isinstance(response["embeddings"], list):
            return False
        # empty request must receive empty embeddings
        if response["embeddings"] != []:
            return False
        return True
    except Exception:
        return False
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def _start_daemon_if_needed() -> bool:
    """
    Start the embedding daemon if it is not running.
    Thread-safe. Uses double-check locking.

    On entry: if _DAEMON_STARTED is True but the daemon is not responding,
    we mark it dead and clean the stale socket before attempting restart.
    """
    global _DAEMON_STARTED

    # Fast path: already started and daemon is healthy
    if _DAEMON_STARTED and _daemon_is_running():
        return True

    # Stale flag: started flag is set but daemon is not responding
    if _DAEMON_STARTED and not _daemon_is_running():
        _mark_daemon_dead("socket health check failed")

    with _DAEMON_LOCK:
        # Double-check inside the lock
        if _daemon_is_running():
            _DAEMON_STARTED = True
            return True

        # Still dead — clean stale socket before starting fresh
        sock_path = _get_socket_path()
        if os.path.exists(sock_path):
            # Try to connect; if it fails, unlink the stale socket
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect(sock_path)
                s.close()
            except Exception:
                # Socket is not responsive — remove it
                try:
                    os.unlink(sock_path)
                    logger.info("Removed stale socket %s", sock_path)
                except Exception:
                    pass

        logger.info("Starting MemPalace embedding daemon...")

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "mempalace.embed_daemon"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

            deadline = time.monotonic() + _SOCK_TIMEOUT
            ready = False
            while time.monotonic() < deadline:
                # Use select with timeout — guarantees we don't block past deadline
                remaining = max(0.05, deadline - time.monotonic())
                r, _, _ = select.select([proc.stdout], [], [], remaining)
                if r:
                    line = proc.stdout.readline().decode("utf-8", errors="ignore").strip()
                    if line == "READY":
                        ready = True
                        break
                if proc.poll() is not None:
                    # Process exited before emitting READY
                    err_b = proc.stderr.read()
                    err = err_b.decode("utf-8", errors="ignore") if err_b else ""
                    logger.error("Embedding daemon exited before READY: %s", err)
                    _mark_daemon_dead("process exited before READY")
                    return False

            if not ready:
                logger.warning(
                    "Embedding daemon startup timeout after %ss", _SOCK_TIMEOUT
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                # Wait for process to actually terminate
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
                _mark_daemon_dead("startup timeout")
                return False

            # READY emitted — verify the daemon is actually responsive
            if _daemon_is_running():
                _DAEMON_STARTED = True
                logger.info("Embedding daemon started (PID %d)", proc.pid)
                return True

            # READY was emitted but socket is not responsive
            logger.error("Daemon emitted READY but socket health check failed")
            _mark_daemon_dead("READY emitted but socket not responsive")
            try:
                proc.kill()
            except Exception:
                pass
            return False

        except Exception as e:
            logger.warning("Could not start embedding daemon: %s", e)
            return False


from ..circuit_breaker import _embed_circuit


def _embed_via_socket(texts: list[str]) -> list[list[float]]:
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


def _embed_texts_fallback(texts: list[str]) -> list[list[float]]:
    """In-process fallback — loads fastembed into this process.

    Memory guardrail: refuse to load model if memory is already critical.
    On M1 8GB, fastembed model needs ~500MB RAM — loading at critical pressure
    would cause system instability.
    """
    if not _embed_fallback_enabled():
        raise RuntimeError("in-process fallback is disabled")

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


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Return EMBEDDING_DIMS-dim embeddings for a list of texts.

    Prefers the Unix socket daemon. Falls back to in-process fastembed
    if the daemon is unavailable.

    Caching: each text is checked against EmbeddingCache individually.
    Cache hits avoid socket/fallback round-trips.
    """
    if not texts:
        return []

    from ..query_cache import get_embedding_cache
    cache = get_embedding_cache()

    # Split texts into cached and uncached
    uncached_texts: list[str] = []
    emb_map: dict[str, list[float]] = {}  # text → embedding

    for text in texts:
        cached_emb = cache.get(text)
        if cached_emb is not None:
            emb_map[text] = cached_emb
        else:
            uncached_texts.append(text)

    # All cached — return immediately in original order
    if not uncached_texts:
        return [emb_map[text] for text in texts]

    # Compute embeddings for uncached texts
    daemon_ok = _start_daemon_if_needed()

    if daemon_ok:
        try:
            computed = _embed_via_socket(uncached_texts)
            # Sanitize daemon output — central guarantee before cache and write paths
            computed = _sanitize_embedding_batch(computed, context="_embed_via_socket")
        except Exception as e:
            global _DAEMON_STARTED
            _DAEMON_STARTED = False
            if not _embed_fallback_enabled():
                logger.warning("Socket embedding failed (%s); in-process fallback disabled", e)
                raise EmbeddingDaemonError(
                    f"Socket embedding failed and in-process fallback is disabled: {e}",
                    cause=e,
                ) from e
            logger.warning("Socket embedding failed (%s), using fallback", e)
            computed = _embed_texts_fallback(uncached_texts)
            computed = _sanitize_embedding_batch(computed, context="_embed_texts_fallback")
    else:
        if not _embed_fallback_enabled():
            logger.warning("Embedding daemon unavailable; in-process fallback disabled")
            raise EmbeddingDaemonError(
                "Embedding daemon unavailable and in-process fallback is disabled",
            )
        computed = _embed_texts_fallback(uncached_texts)
        computed = _sanitize_embedding_batch(computed, context="_embed_texts_fallback")

    # Store computed in cache and merge into result dict
    for text, emb in zip(uncached_texts, computed):
        cache.set(text, emb)
        emb_map[text] = emb

    # Return in original order
    return [emb_map[text] for text in texts]


def _is_degenerate_vector(vec) -> tuple[bool, str]:
    """
    Check if a vector is degenerate (all-zero, near-zero, all-NaN, or all-Inf).

    Returns (is_degenerate, reason).  reason is "" if valid, otherwise describes
    the failure mode.
    """
    try:
        float_vec = [float(v) for v in vec]
    except (TypeError, ValueError):
        return True, "not convertible to float"
    if len(float_vec) != EMBEDDING_DIMS:
        return True, f"dimension {len(float_vec)} != {EMBEDDING_DIMS}"
    has_nan = any(math.isnan(v) for v in float_vec)
    has_inf = any(math.isinf(v) for v in float_vec)
    if has_nan or has_inf:
        return True, f"contains NaN/Inf (nan={has_nan}, inf={has_inf})"
    norm = math.sqrt(sum(v * v for v in float_vec))
    if norm < 1e-9:
        return True, f"zero-norm ({norm:.2e})"
    return False, ""


def _quarantine_record(
    source_file: str,
    chunk_index: int,
    reason: str,
    preview: str,
    model: str,
    wing: str,
) -> None:
    """Append a quarantine record to the shared JSONL log."""
    import datetime

    record = {
        "source_file": source_file,
        "chunk_index": chunk_index,
        "reason": reason,
        "preview": preview[:200],
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "model": model,
        "wing": wing,
    }
    qpath = Path(os.path.expanduser("~/.mempalace/palace/mining_quarantine.jsonl"))
    qpath.parent.mkdir(parents=True, exist_ok=True)
    with qpath.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


_EMBED_TEXT_RESILIENT_LOCK = threading.Lock()
_EMBED_QUARANTINE_COUNT = 0

# ---------------------------------------------------------------------------
# Per-vector error classification
# ---------------------------------------------------------------------------


def _is_per_vector_embedding_error(exc: Exception) -> bool:
    """Return True if exc is a per-vector (degenerate) error, not a systemic one.

    Per-vector errors: a specific chunk produced a bad embedding.
    Systemic errors: daemon unavailable, socket broken, memory pressure, etc.
    """
    msg = str(exc).lower()
    per_vector_tokens = (
        "degenerate",
        "zero-dimensional",
        "all zero",
        "nan",
        "inf",
        "dimension",
        "_embed_via_socket[",
    )
    if not any(token in msg for token in per_vector_tokens):
        return False
    # Exclude systemic error tokens
    systemic_tokens = (
        "daemon unavailable",
        "connection",
        "timeout",
        "fallback is disabled",
        "memory pressure",
        "malformed",
        "json",
    )
    return not any(token in msg for token in systemic_tokens)


def _embed_texts_resilient(
    texts: list[str],
    *,
    context: str = "",
) -> tuple[list[str], list[list[float]], list[dict], list[int]]:
    """
    Embed texts with per-chunk degenerate vector recovery.

    Returns (valid_texts, valid_embeddings, failures, valid_orig_indices).
    - valid_texts / valid_embeddings: only chunks whose vectors passed validation
    - failures: list of per-chunk failure records (index, reason, preview, context)
    - valid_orig_indices: original positions in `texts` that produced valid embeddings

    One bad chunk does not abort the batch.  All failures are quarantined.
    """
    global _EMBED_QUARANTINE_COUNT

    if not texts:
        return [], [], [], []

    fallback_disabled = os.environ.get("MEMPALACE_EMBED_FALLBACK", "1") != "1"

    # First attempt: batch embed everything
    try:
        vectors = _embed_texts(texts)
    except Exception as initial_error:
        if fallback_disabled and _is_per_vector_embedding_error(initial_error):
            # Per-vector degenerate error from daemon with fallback disabled: quarantine affected chunks.
            import re

            chunk_idx_match = re.search(r'\[(\d+)\]', str(initial_error))
            failed_chunk_idx = int(chunk_idx_match.group(1)) if chunk_idx_match else None
            failures = []
            for i, text in enumerate(texts):
                if failed_chunk_idx is None or i == failed_chunk_idx:
                    failures.append({
                        "index": i,
                        "reason": f"daemon_degenerate:{initial_error}",
                        "preview": text[:200],
                        "context": context,
                    })
                    _quarantine_record(
                        source_file=context,
                        chunk_index=i,
                        reason=f"daemon_degenerate:{initial_error}",
                        preview=text,
                        model="mlx",
                        wing="",
                    )
            return [], [], failures, []
        # Systemic error, MemoryPressureError, daemon dead, or fallback disabled but error
        # is not per-vector → raise and stop mining.
        raise

    valid_texts, valid_vectors, failures, valid_orig_indices = [], [], [], []

    for i, (text, vec) in enumerate(zip(texts, vectors)):
        is_deg, reason = _is_degenerate_vector(vec)
        if is_deg:
            failures.append({
                "index": i,
                "reason": reason,
                "preview": text[:200],
                "context": context,
            })
            _quarantine_record(
                source_file=context,
                chunk_index=i,
                reason=f"degenerate_embedding:{reason}",
                preview=text,
                model="mlx" if _DAEMON_STARTED else "fastembed",
                wing="",
            )
            with _EMBED_TEXT_RESILIENT_LOCK:
                _EMBED_QUARANTINE_COUNT += 1
        else:
            valid_texts.append(text)
            valid_vectors.append(vec)
            valid_orig_indices.append(i)

    return valid_texts, valid_vectors, failures, valid_orig_indices


def _sanitize_embedding_vector(
    vec,
    *,
    expected_dim: int | None = None,
    context: str = "",
) -> list[float]:
    """
    Ensure a vector is finite and correctly-dimensioned before LanceDB write.

    Policy:
      - Wrong dimension → RuntimeError
      - Sparse NaN/Inf (some values valid) → replace with 0.0, renormalize
      - All invalid or zero norm after repair → RuntimeError
      - Return value is always a plain list[float] with exactly expected_dim elements

    Never returns NaN or Inf.
    """
    if expected_dim is None:
        expected_dim = EMBEDDING_DIMS

    if vec is None:
        raise RuntimeError(
            f"Embedding is None during mining{': ' + context if context else ''}. "
            "The MLX daemon may have crashed on this input."
        )

    try:
        float_vec = [float(v) for v in vec]
    except (TypeError, ValueError) as e:
        raise RuntimeError(
            f"Embedding values are not convertible to float{': ' + context if context else ''}: {e}"
        )

    if len(float_vec) != expected_dim:
        raise RuntimeError(
            f"Embedding dimension {len(float_vec)} != expected {expected_dim}"
            f"{': ' + context if context else ''}"
        )

    # Detect any NaN/Inf
    has_bad = any(not math.isfinite(v) for v in float_vec)

    if has_bad:
        # Replace NaN/Inf with 0.0
        cleaned = [0.0 if not math.isfinite(v) else v for v in float_vec]

        # Renormalize
        norm = math.sqrt(sum(v * v for v in cleaned))
        if norm > 1e-9:
            factor = 1.0 / norm
            cleaned = [v * factor for v in cleaned]
            logger.warning(
                "Embedding contained NaN/Inf and was repaired by zero-fill+renormalize"
                f"{': ' + context if context else ''}"
            )
            with _EMBED_SANITIZE_REPAIRS_LOCK:
                global _EMBED_SANITIZE_REPAIRS
                _EMBED_SANITIZE_REPAIRS += 1
        else:
            # All zero or degenerate — cannot repair
            raise RuntimeError(
                f"Embedding is degenerate (all zero or invalid){': ' + context if context else ''}. "
                "The MLX model produced a broken embedding for this input."
            )

        return cleaned

    # All finite — check for zero vector (degenerate but not NaN/Inf)
    norm = math.sqrt(sum(v * v for v in float_vec))
    if norm < 1e-9:
        raise RuntimeError(
            f"Embedding is zero-dimensional{': ' + context if context else ''}. "
            "The MLX model produced a degenerate (all-zero) embedding for this input."
        )

    return float_vec


def _sanitize_embedding_batch(
    vectors,
    *,
    expected_dim: int | None = None,
    context: str = "",
) -> list[list[float]]:
    """Sanitize a batch of vectors. Applies _sanitize_embedding_vector to each."""
    if expected_dim is None:
        expected_dim = EMBEDDING_DIMS
    return [
        _sanitize_embedding_vector(v, expected_dim=expected_dim, context=f"{context}[{i}]")
        for i, v in enumerate(vectors)
    ]


# ── Dedup Scope Helper ─────────────────────────────────────────────────────────


def _dedup_scope_matches(new_meta: dict, old_meta: dict) -> bool:
    """
    Returns True when two memories are in the same dedup scope.

    For code/repo chunks, dedup is scoped to the same source_file (optionally
    same chunk_index). Cross-project or cross-file chunks are NEVER considered
    duplicates — they must all be stored.

    For non-code memories (no source_file), falls back to True (allow dedup).
    """
    new_sf = new_meta.get("source_file")
    old_sf = old_meta.get("source_file")

    if new_sf or old_sf:
        # If either has a source_file but not both, don't dedup across boundary
        if not new_sf or not old_sf:
            return False
        # Different source files → different scope
        if str(new_sf) != str(old_sf):
            return False
        # Same source_file: optionally check chunk_index
        new_chunk = new_meta.get("chunk_index")
        old_chunk = old_meta.get("chunk_index")
        if new_chunk is not None and old_chunk is not None:
            return new_chunk == old_chunk
        return True

    # Neither has source_file → legacy dedup allowed
    return True


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
    ) -> tuple[str, str | None]:
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
            if _dedup_scope_matches(new_metadata, best_meta):
                return "duplicate", best_id
            return "unique", None

        if best_similarity >= self.low_threshold:
            if not _dedup_scope_matches(new_metadata, best_meta):
                return "unique", None
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
        quarantine_ctx: str = "",
    ) -> tuple[list[tuple[str, str | None]], list[list[float] | None], list[dict]]:
        """
        Klasifikuje celý batch dokumentů najednou.
        Vrací (classifications, embeddings, failures) — embeddingy jsou vypočítány
        JEDNÍM voláním _embed_texts a caller je může reuseovat.

        failures: list of per-chunk degenerate embedding records that were quarantined.
                  Caller can log these for visibility.

        Caller si musí sám vyfiltrovat embeddingy podle toho,
        které dokumenty mají akci "unique"/"conflict" (ne "duplicate").
        """
        if collection.count() == 0:
            valid_texts, valid_embs, failures, valid_orig_indices = _embed_texts_resilient(
                documents, context=quarantine_ctx or "classify_batch(empty_collection)"
            )
            # Align results with original document indices so upsert zip is correct
            results: list[tuple[str, str | None]] = [("unique", None)] * len(documents)
            for qi in {f["index"] for f in failures}:
                results[qi] = ("quarantined", None)
            all_embs_aligned: list[list[float] | None] = [None] * len(documents)
            for vi, orig_i in enumerate(valid_orig_indices):
                all_embs_aligned[orig_i] = valid_embs[vi]
            return results, all_embs_aligned, failures

        # JEDEN batch embedding call pro všechny dokumenty (resilient: skips bad chunks)
        valid_texts, batch_vectors, failures, valid_orig_indices = _embed_texts_resilient(
            documents, context=quarantine_ctx or "classify_batch"
        )

        # Build mapping: original_index → (doc, meta, vec)
        orig_vec_map: dict[int, tuple[str, dict, list[float]]] = {}
        for vi, orig_i in enumerate(valid_orig_indices):
            orig_vec_map[orig_i] = (documents[orig_i], metadatas[orig_i], batch_vectors[vi])

        from concurrent.futures import ThreadPoolExecutor, as_completed

        n_workers = max(1, min(len(valid_orig_indices), 4))

        results: list[tuple[str, str | None]] = [("unique", None)] * len(documents)
        # Mark quarantined indices as "quarantined" so upsert skips them
        failed_orig_indices = {f["index"] for f in failures}
        for qi in failed_orig_indices:
            results[qi] = ("quarantined", None)

        if not orig_vec_map:
            all_embeddings_aligned: list[list[float] | None] = [None] * len(documents)
            return results, all_embeddings_aligned, failures

        def _classify_one(args: tuple[int, str, dict, list[float]]) -> tuple[int, tuple[str, str | None]]:
            i, doc, meta, vec = args
            similar = collection.query_by_vector(vector=vec, n_results=n_candidates)
            if not similar["ids"] or not similar["ids"][0]:
                return i, ("unique", None)
            best_dist = similar["distances"][0][0]
            best_id = similar["ids"][0][0]
            best_meta = (similar["metadatas"][0][0] or {}) if similar["metadatas"] else {}
            best_similarity = max(0.0, 1.0 - best_dist)
            if best_similarity >= self.high_threshold:
                if _dedup_scope_matches(meta, best_meta):
                    return i, ("duplicate", best_id)
                return i, ("unique", None)
            if best_similarity >= self.low_threshold:
                if not _dedup_scope_matches(meta, best_meta):
                    return i, ("unique", None)
                if (meta.get("room") == best_meta.get("room") and
                        meta.get("wing") == best_meta.get("wing")):
                    return i, ("conflict", best_id)
                return i, ("unique", None)
            return i, ("unique", None)

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {
                executor.submit(_classify_one, (orig_i, doc, meta, vec)): orig_i
                for orig_i, (doc, meta, vec) in orig_vec_map.items()
            }
            for future in as_completed(futures):
                orig_i, classification = future.result()
                results[orig_i] = classification

        # Build aligned embeddings list: None at quarantined indices so upsert zip stays aligned
        all_embeddings_aligned: list[list[float] | None] = [None] * len(documents)
        for vi, orig_i in enumerate(valid_orig_indices):
            all_embeddings_aligned[orig_i] = batch_vectors[vi]

        return results, all_embeddings_aligned, failures


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
        self._last_optimize_time = time.monotonic()
        self._lock = threading.Lock()
        self._lock_file = Path(palace_path) / ".optimize_lock"
        self._optimize_loop: asyncio.AbstractEventLoop | None = None
        self._optimize_thread: threading.Thread | None = None
        self._optimize_lock = threading.Lock()  # guard _optimize_loop access

    def _get_optimize_loop(self) -> asyncio.AbstractEventLoop:
        """Lazily start a persistent daemon thread with one event loop.

        Unlike asyncio.run() which creates a new loop per call, this keeps one
        loop alive across all optimize calls — no nested loop creation, no
        orphaned tasks when the loop closes.
        """
        with self._optimize_lock:
            if self._optimize_loop is None or not self._optimize_loop.is_running():
                self._stop_optimize_loop()
                ev = asyncio.new_event_loop()
                self._optimize_loop = ev

                def _run_loop():
                    asyncio.set_event_loop(ev)
                    ev.run_forever()

                t = threading.Thread(target=_run_loop, daemon=True, name="mp_optimize")
                t.start()
                # yield to the new thread — ev.run_forever() starts instantly, no spin needed
                time.sleep(0)
            return self._optimize_loop

    def _stop_optimize_loop(self) -> None:
        """Stop and close the optimize event loop."""
        loop = self._optimize_loop
        if loop is None:
            return
        self._optimize_loop = None
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        # Don't join — daemon thread exits when process exits

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
                loop = self._get_optimize_loop()
                loop.call_soon_threadsafe(self._schedule_optimize)

    def _schedule_optimize(self) -> None:
        """Schedule one optimize run on the persistent loop."""
        if self._lock_file.exists():
            logger.debug("LanceDB optimize already running, skipping")
            return
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_optimize())

        def _on_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                pass  # success already logged inside _run_optimize
            else:
                logger.error("LanceDB optimize task failed: %s", exc)

        task.add_done_callback(_on_done)

    async def _run_optimize(self) -> None:
        """Compacts delta files. Runs on the persistent optimize event loop."""
        if self._lock_file.exists():
            logger.debug("LanceDB optimize already running, skipping")
            return

        try:
            self._lock_file.touch()
            logger.info("Starting LanceDB optimize for %s", self._collection_name)

            await self._async_optimize()

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
            # Sync fallback — use blocking LanceDB API directly
            try:
                import lancedb
                db = lancedb.connect(self._palace_path)
                table = db.open_table(self._collection_name).to_lance()
                table.optimize()
            except Exception as e2:
                logger.debug("Both async and sync optimize unavailable: %s", e2)

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
    """Retry with logarithmic backoff for LanceDB commit conflicts (MVCC).
    Non-blocking: max wait at peak is ~0.16s (vs ~6.4s with exponential),
    keeping the thread active and responsive to memory pressure checks.
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e).lower()
            if any(kw in err for kw in ("commit conflict", "conflict", "retry", "transaction")) \
                    and attempt < max_retries - 1:
                # Logarithmic backoff: wait = 0.08 * ln(2^attempt) + jitter
                # attempt=0 → ~0.06s, attempt=3 → ~0.11s, attempt=6 → ~0.16s
                import math as _math
                wait = 0.08 * _math.log(2 ** attempt + 1) + random.random() * 0.01
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
    """Create a new LanceDB table with schema and vector index."""
    schema = _get_drawer_schema()
    table = db.create_table(collection_name, schema=schema)

    # Vector index for fast similarity search
    try:
        table.create_index("vector")
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


def _json_in(k: str, v: list[Any]) -> str:
    vals = ", ".join(_sql_val(x) for x in v)
    return f"json_extract({_json_col('metadata_json')}, '$.{k}') IN ({vals})"


def _json_cmp(k: str, op_: str, v: Any) -> str:
    return f"json_extract({_json_col('metadata_json')}, '$.{k}') {op_} {_sql_val(v)}"


def _where_to_sql(where: dict[str, Any] | None) -> str | None:
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


def _parse_metadata(df: "pandas.DataFrame") -> "pandas.DataFrame":
    """Parse metadata_json column once and cache parsed dicts as a column.

    Mutates df in-place by adding a _meta parsed column.
    Safe to call on any DataFrame — drops _meta if already present.
    """
    if "_meta" in df.columns:
        return df
    def _parse_one(val):
        try:
            return json.loads(val) if val else {}
        except Exception:
            return {}
    df["_meta"] = df["metadata_json"].apply(_parse_one)
    return df


def _apply_where_filter(df: "pandas.DataFrame", where: dict[str, Any] | None) -> "pandas.DataFrame":
    """Apply a ChromaDB-style where filter to a pandas DataFrame.

    Handles:
      - Scalar metadata equality: {"wing": "x"} → metadata.wing == 'x'
      - Explicit operators: {"wing": {"$eq": "x"}} → same
      - $and / $or logical combinators
      - id filter: {"id": "..."} or {"id": {"$eq": "..."}}

    Parse-once optimization: metadata_json is parsed exactly once per page
    (into the `_meta` column) rather than once per filter condition per row.
    """
    if not where:
        return df

    # Parse once per page — all conditions share the same parsed metadata column
    df = _parse_metadata(df)

    if "$and" in where:
        for sub in where["$and"]:
            df = _apply_where_filter(df, sub)
        return df

    if "$or" in where:
        # Collect matching ids from each sub-filter, then filter once
        id_sets: list[set] = []
        for sub in where["$or"]:
            sub_df = _apply_where_filter(df, sub)
            id_sets.append(set(sub_df["id"].tolist()))
        combined_ids = set()
        for s in id_sets:
            combined_ids.update(s)
        return df[df["id"].isin(combined_ids)]

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
            # Use pre-parsed _meta column — O(1) dict.get instead of json.loads per call
            if op == "$eq":
                df = df[df["_meta"].apply(lambda m: m.get(key) == val)]
            elif op == "$ne":
                df = df[df["_meta"].apply(lambda m: m.get(key) != val)]
            elif op == "$in":
                df = df[df["_meta"].apply(lambda m: m.get(key) in val)]
            elif op == "$nin":
                df = df[df["_meta"].apply(lambda m: m.get(key) not in val)]
            elif op == "$gt":
                df = df[df["_meta"].apply(lambda m: m.get(key) is not None and m.get(key) > val)]
            elif op == "$gte":
                df = df[df["_meta"].apply(lambda m: m.get(key) is not None and m.get(key) >= val)]
            elif op == "$lt":
                df = df[df["_meta"].apply(lambda m: m.get(key) is not None and m.get(key) < val)]
            elif op == "$lte":
                df = df[df["_meta"].apply(lambda m: m.get(key) is not None and m.get(key) <= val)]
            elif op == "$starts_with":
                df = df[df["_meta"].apply(lambda m: isinstance(m.get(key), str) and m.get(key).startswith(val))]
        else:
            # Scalar metadata equality: {"wing": "x"} → metadata.wing == 'x'
            df = df[df["_meta"].apply(lambda m: m.get(key) == cond)]

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
    - Canonical lexical search: SQLite FTS5 (KeywordIndex), incremental sync
    - M1 CPU: no GPU memory conflicts
    """

    BATCH_SIZE = 500  # Records per add/upsert batch
    _DELETE_MAX_SCAN = 50000  # hard cap on delete(where=...) scan
    _GET_MAX_SCAN = 100_000  # hard cap on get(where=...) raw-row scan

    def __init__(
        self,
        table: "lancedb.table.Table",
        palace_path: str = None,
        collection_name: str = "mempalace_drawers",
    ):
        self._table = table
        self._palace_path = palace_path or ""
        self._collection_name = collection_name
        self._optimizer: LanceOptimizer | None = None
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
        """Retry with logarithmic backoff for LanceDB commit conflicts."""
        for attempt in range(max_retries):
            try:
                return fn()
            except Exception as e:
                err = str(e).lower()
                if any(kw in err for kw in ("commit conflict", "conflict", "retry", "transaction")) \
                        and attempt < max_retries - 1:
                    import math as _math
                    wait = 0.08 * _math.log(2 ** attempt + 1) + random.random() * 0.01
                    time.sleep(wait)
                    continue
                raise

    def run_optimize(self) -> None:
        """Run synchronous LanceDB optimize. For CLI use."""
        if self._optimizer:
            self._optimizer.run_optimize_sync()

    def query_by_vector(
        self,
        vector: list[float],
        n_results: int = 5,
        where: dict | None = None,
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

        # Use _parse_metadata — parses all metadata in one vectorized pass (O(n) not O(n*json))
        df = _parse_metadata(results)
        return {
            "ids": [[str(r) for r in results["id"]]],
            "documents": [[str(r) for r in results["document"]]],
            "metadatas": [df["_meta"].tolist()],
            "distances": [distances],
        }

    # ── Write operations ──────────────────────────────────────────────────

    def add(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: list[dict[str, Any]] | None = None,
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
        documents: list[str],
        ids: list[str],
        metadatas: list[dict[str, Any]] | None = None,
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
                    f"Cannot write to palace at {self._palace_path}: system memory at "
                    f"{guard.used_ratio:.0%}. Close some apps and retry."
                )

        # BATCH Semantic deduplication – jeden embedding call pro celý batch
        deduplicator = SemanticDeduplicator()
        classifications, all_embeddings, failures = deduplicator.classify_batch(
            documents=documents,
            metadatas=metadatas,
            collection=self,
            quarantine_ctx="_do_add",
        )

        final_docs, final_ids, final_metas, final_embs = [], [], [], []
        skipped, conflicts, quarantined = 0, 0, 0

        for doc, doc_id, meta, (action, existing_id), emb in zip(
            documents, ids, metadatas, classifications, all_embeddings
        ):
            if action == "quarantined":
                quarantined += 1
                logger.debug("Skipping quarantined chunk: %s", doc_id)
                continue
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
            final_embs.append(emb)

        if quarantined > 0:
            logger.info("_do_add quarantined %d degenerate embedding(s)", quarantined)
        if skipped > 0 or conflicts > 0:
            logger.info(
                "Semantic dedup: skipped %d duplicates, resolved %d conflicts",
                skipped, conflicts,
            )

        if not final_docs:
            return

        # Sanitize embeddings — repair sparse NaN/Inf, reject degenerate vectors
        final_embs = _sanitize_embedding_batch(final_embs, context="_do_add")

        # Embeddingy jsou už spočítané v classify_batch – reuse
        now = time.time()

        records = [
            {
                "id": did,
                "document": doc,
                "vector": emb,
                "metadata_json": json.dumps(meta, default=str),
                "created_at": now,
            }
            for did, doc, emb, meta in zip(final_ids, final_docs, final_embs, final_metas)
        ]

        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i : i + self.BATCH_SIZE]
            self._write_with_retry(lambda b=batch: self._table.add(b))

        if self._optimizer:
            self._optimizer.record_write()

        # Invalidate query cache na této collection
        from ..query_cache import get_query_cache
        get_query_cache().invalidate_collection(self._palace_path, self._collection_name)

        # ── FTS5 incremental sync (add) ───────────────────────────────────
        self._sync_fts5upsert(final_ids, final_docs, final_metas)

    def upsert(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> str | None:
        if not documents:
            return

        metadatas = metadatas or [{}] * len(documents)

        # Memory pressure check — canonical write path must respect MemoryGuard
        from ..memory_guard import MemoryGuard
        guard = MemoryGuard.get()
        if guard.should_pause_writes():
            logger.warning(
                "Memory pressure CRITICAL (%.0f%% RAM used). "
                "Pausing upsert for up to 30s...",
                guard.used_ratio * 100
            )
            if not guard.wait_for_nominal(timeout=30.0):
                raise MemoryPressureError(
                    f"Cannot upsert to palace: system memory at "
                    f"{guard.used_ratio:.0%}. Close some apps and retry."
                )

        # Semantic deduplication – používá classify_batch pro embedding reuse
        deduplicator = SemanticDeduplicator()
        classifications, all_embeddings, failures = deduplicator.classify_batch(
            documents=documents,
            metadatas=metadatas,
            collection=self,
            quarantine_ctx="upsert",
        )

        quarantined = 0
        skipped = 0
        final_docs, final_ids, final_metas, final_embs = [], [], [], []

        for doc, doc_id, meta, (action, existing_id), emb in zip(
            documents, ids, metadatas, classifications, all_embeddings
        ):
            if action == "quarantined":
                quarantined += 1
                logger.debug("Upsert skipping quarantined chunk: %s", doc_id)
                continue
            if action == "duplicate":
                skipped += 1
                logger.debug("Upsert skipping duplicate: %s", doc_id)
                continue
            final_docs.append(doc)
            final_ids.append(doc_id)
            final_metas.append(meta)
            final_embs.append(emb)

        if quarantined > 0:
            logger.info("Upsert quarantined %d degenerate embedding(s)", quarantined)
        if skipped > 0:
            logger.info("Upsert semantic dedup: skipped %d duplicates", skipped)

        if not final_docs:
            return

        # Sanitize embeddings — repair sparse NaN/Inf, reject degenerate vectors
        final_embs = _sanitize_embedding_batch(final_embs, context="upsert")

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

        # Embeddingy jsou už spočítané v classify_batch – reuse
        now = time.time()

        records = [
            {
                "id": did,
                "document": doc,
                "vector": emb,
                "metadata_json": json.dumps(meta, default=str),
                "created_at": now,
            }
            for did, doc, emb, meta in zip(final_ids, final_docs, final_embs, final_metas)
        ]

        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i : i + self.BATCH_SIZE]
            self._write_with_retry(lambda b=batch: self._table.add(b))

        if self._optimizer:
            self._optimizer.record_write()

        # Invalidate query cache
        from ..query_cache import get_query_cache
        get_query_cache().invalidate_collection(self._palace_path, self._collection_name)

        # ── FTS5 incremental sync ─────────────────────────────────────────
        return self._sync_fts5upsert(final_ids, final_docs, final_metas)

    def _sync_fts5upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
    ) -> str | None:
        """Synchronize written records into the FTS5 keyword index.

        Called after every successful upsert/add. Uses batch upsert for
        efficiency — one sqlite connection, one transaction.
        Returns None on success, or a warning string on failure.
        Failures are silently suppressed (lexical index staleness is
        recoverable via rebuild_keyword_index).
        """
        if not ids:
            return None
        try:
            from ..lexical_index import KeywordIndex
            idx = KeywordIndex.get(self._palace_path)
            entries = [
                {
                    "document_id": doc_id,
                    "content": doc,
                    "wing": meta.get("wing", ""),
                    "room": meta.get("room", ""),
                    "language": meta.get("language"),
                }
                for doc_id, doc, meta in zip(ids, documents, metadatas)
            ]
            idx.upsert_drawer_batch(entries)
            return None
        except Exception as e:
            logger.warning("FTS5 upsert failed for %d entries: %s — index may be stale", len(ids), e)
            return "FTS5 index sync failed — keyword search may be stale; run rebuild_keyword_index()"

    # ── Read operations ───────────────────────────────────────────────────

    def query(
        self,
        query_texts: list[str] | None = None,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
        include: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, list[list[Any]]]:
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
        # Use _parse_metadata — parses all metadata in one vectorized pass
        results = _parse_metadata(results)
        metas_out = results["_meta"].tolist()

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
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, list[Any]]:
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
                #
                # IMPORTANT: offset must apply to the FILTERED result set, not the
                # raw table. LanceDB does not support server-side offset with pandas
                # post-filtering.
                #
                # Memory-efficient scan: we use a fixed large scan batch regardless
                # of the user's limit, and only store rows that fall within the
                # requested [offset, offset+limit] window. We track the running
                # filtered count to know when to start/stop collecting.
                effective_limit = min(limit or 5000, 5000)
                target_offset = offset or 0

                # Fixed scan batch — independent of limit. Large enough to avoid
                # N-round-trips for sparse filters; small enough to keep one page
                # in RAM safely.
                SCAN_BATCH = 2000
                MAX_SCAN = self._GET_MAX_SCAN  # class-level configurable cap

                seen_filtered = 0  # total filtered rows encountered so far
                total_scanned = 0  # total raw rows scanned
                window: list[dict] = []  # only rows in [offset, offset+limit]

                while total_scanned < MAX_SCAN:
                    # Never request more rows than the remaining scan budget — this
                    # ensures we don't skip over the ceiling in one large page.
                    # Also cap at the user's effective_limit to avoid wasted I/O.
                    batch_limit = min(SCAN_BATCH, MAX_SCAN - total_scanned, effective_limit + target_offset - total_scanned)
                    page = (
                        self._table.search()
                        .limit(batch_limit)
                        .offset(total_scanned)
                        .to_pandas()
                    )
                    if page.empty:
                        break
                    # Cap page to remaining scan budget before advancing total_scanned.
                    # If LanceDB returns more rows than we requested via limit(), we must
                    # not count those extra rows against the scan budget.
                    remaining = MAX_SCAN - total_scanned
                    if len(page) > remaining:
                        page = page.iloc[:remaining]
                    total_scanned += len(page)
                    filtered = _apply_where_filter(page, where)
                    if not filtered.empty:
                        for _, row in filtered.iterrows():
                            if seen_filtered >= target_offset:
                                if len(window) < effective_limit:
                                    window.append({
                                        "id": row["id"],
                                        "document": row["document"],
                                        "metadata_json": row["metadata_json"],
                                    })
                            seen_filtered += 1
                            if seen_filtered >= target_offset + effective_limit:
                                break
                    if seen_filtered >= target_offset + effective_limit:
                        break

                if total_scanned >= MAX_SCAN and seen_filtered < target_offset + effective_limit:
                    # Safety ceiling hit — we cannot determine if more filtered rows
                    # exist beyond the cap. Raise instead of returning a misleading
                    # partial result.
                    raise RuntimeError(
                        f"get(where=...) scanned {total_scanned} raw rows but only "
                        f"found {seen_filtered} filtered rows ({target_offset} offset + "
                        f"{effective_limit} limit requested) before hitting "
                        f"MAX_SCAN={MAX_SCAN}. The filter may be too broad or the table "
                        f"is too large. Use a more selective filter."
                    )

                results = pandas.DataFrame(window) if window else pandas.DataFrame()
            else:
                # No filter — enforce hard limit to prevent full-table RAM spike.
                # 5000 is a safe default that fits comfortably in 8GB RAM.
                effective_limit = min(limit or 5000, 5000)
                if limit is None and effective_limit == 5000:
                    logger.warning(
                        "get(where=None) with no limit hit 5000-row hard cap. "
                        "Results are truncated. Use a filter or explicit limit."
                    )
                search = self._table.search()
                if offset is not None and offset > 0:
                    search = search.offset(offset)
                results = search.limit(effective_limit).to_pandas()
        except RuntimeError:
            raise
        except Exception:
            return {"ids": [], "documents": [], "metadatas": []}

        if results.empty:
            return {"ids": [], "documents": [], "metadatas": []}

        if limit:
            results = results.head(limit)

        results = _parse_metadata(results)
        return {
            "ids": results["id"].tolist(),
            "documents": results["document"].tolist(),
            "metadatas": results["_meta"].tolist(),
        }

    def delete(
        self,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str | None:
        if ids:
            if len(ids) == 1:
                where_clause = f"id = '{ids[0]}'"
            else:
                where_clause = "id IN (" + ", ".join(repr(i) for i in ids) + ")"

            def do_delete():
                self._table.delete(where_clause)

            self._write_with_retry(do_delete)
        elif where:
            # IMPORTANT: LanceDB's json_extract SQL cannot filter UTF-8 metadata_json
            # strings because _json_col() emits CAST(...AS VARBINARY) which current
            # LanceDB/DataFusion rejects with "Unsupported SQL type VARBINARY".
            # We must NEVER pass a metadata filter through LanceDB SQL prefilter.
            # Strategy: collect ALL matching ids via bounded Python-filtered scan,
            # THEN delete by id. LanceDB's ANN scan narrows the search region even
            # without a SQL predicate, and _apply_where_filter() handles all metadata
            # conditions correctly.
            try:
                batch_size = 500
                MAX_SCAN = self._DELETE_MAX_SCAN
                matching_ids: list[str] = []
                total_scanned = 0
                consecutive_empty_pages = 0
                EMPTY_PAGES_BEFORE_BREAK = 2

                page_start = 0
                while total_scanned < MAX_SCAN:
                    batch_limit = min(batch_size, MAX_SCAN - total_scanned)
                    # Use search without SQL predicate — LanceDB ANN index narrows
                    # the scan; all metadata filtering happens in Python via
                    # _apply_where_filter which parses metadata_json once per page.
                    page = (
                        self._table.search()
                        .limit(batch_limit)
                        .offset(page_start)
                        .to_pandas()
                    )
                    if page.empty:
                        consecutive_empty_pages += 1
                        total_scanned += batch_limit
                        if consecutive_empty_pages >= EMPTY_PAGES_BEFORE_BREAK:
                            break
                        page_start += batch_size
                        continue
                    consecutive_empty_pages = 0
                    # Cap page to remaining scan budget before advancing total_scanned.
                    remaining = MAX_SCAN - total_scanned
                    if len(page) > remaining:
                        page = page.iloc[:remaining]
                    total_scanned += len(page)
                    filtered = _apply_where_filter(page, where)
                    if not filtered.empty:
                        matching_ids.extend(filtered["id"].tolist())
                    if len(matching_ids) >= MAX_SCAN:
                        break
                    page_start += batch_size

                if total_scanned >= MAX_SCAN and len(matching_ids) < MAX_SCAN:
                    raise RuntimeError(
                        f"delete(where=...) scanned {total_scanned} raw rows "
                        f"({len(matching_ids)} matches) before hitting "
                        f"MAX_SCAN={MAX_SCAN}. Either a sparse table, a "
                        f"LanceDB compaction in progress, or the filter is not selective "
                        f"enough. Delete by id instead, or use a more selective filter."
                    )

                if not matching_ids:
                    return self._sync_fts5delete([])

                # Batch delete using WHERE id IN (...) instead of per-ID deletes
                for i in range(0, len(matching_ids), batch_size):
                    batch = matching_ids[i:i + batch_size]
                    if len(batch) == 1:
                        where_clause = f"id = '{batch[0]}'"
                    else:
                        where_clause = "id IN (" + ", ".join(repr(j) for j in batch) + ")"
                    self._write_with_retry(lambda wc=where_clause: self._table.delete(wc))
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning("delete(where=...) skipped due to scan error: %s", e)
                return

        # ── FTS5 incremental sync (delete) ────────────────────────────────
        # deleted_ids: explicit ids list when provided, otherwise the matching_ids
        # collected from the where-filter scan (empty list when where filtered to zero).
        deleted_ids = ids if ids else (matching_ids if where else [])
        return self._sync_fts5delete(deleted_ids)

    def _sync_fts5delete(self, ids: list[str]) -> str | None:
        """Remove deleted document IDs from the FTS5 keyword index.

        Called after every successful delete. Returns None on success,
        or a warning string on failure. Failures are silently suppressed
        — stale FTS5 entries are safe to recover via rebuild.
        """
        if not ids:
            return None
        try:
            from ..lexical_index import KeywordIndex
            idx = KeywordIndex.get(self._palace_path)
            idx.delete_drawer_batch(ids)
            return None
        except Exception as e:
            logger.warning("FTS5 delete failed for %d entries: %s — index may be stale", len(ids), e)
            return "FTS5 index sync failed — keyword search may be stale; run rebuild_keyword_index()"

    def count(self) -> int:
        try:
            return self._table.count_rows()
        except Exception:
            return 0

    def get_by_id(self, record_id: str) -> dict[str, Any] | None:
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
            tables_response = db.list_tables()
            table_names = (
                tables_response.tables
                if hasattr(tables_response, "tables")
                else list(tables_response)
            )
        except Exception:
            table_names = []

        # create=True means "create if not exists" — not "always create".
        # If the table already exists, open it regardless of the create flag.
        if collection_name not in table_names:
            table = _create_lance_table(db, collection_name)
            return LanceCollection(table, palace_path=palace_path, collection_name=collection_name)

        else:
            if not os.path.isdir(palace_path):
                raise FileNotFoundError(f"Palace not found: {palace_path}")
            table = db.open_table(collection_name)
            return LanceCollection(table, palace_path=palace_path, collection_name=collection_name)
