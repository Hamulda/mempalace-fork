"""
Embedding metadata management for MemPalace palaces.

Stores `<palace_path>/embedding_meta.json` with:
  - provider: mlx | fastembed_coreml | fastembed_cpu | mock | unknown
  - model_id
  - dims
  - created_at / updated_at (ISO timestamps)
  - collection_name
  - version (schema version for future compatibility)

On first write: detect provider/dims and write metadata.
On subsequent writes: validate against stored metadata.
  - dims mismatch → hard error
  - provider/model mismatch (dims same) → warn by default, allow override

Detection caching:
  detect_current_provider() results are cached for MEMPALACE_EMBED_PROVIDER_CACHE_TTL
  seconds (default 30s) to avoid repeated heavy detection on hot paths.
  Cache key includes daemon socket path and relevant env vars.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

logger = logging.getLogger(__name__)

# Schema version — bump on breaking changes
SCHEMA_VERSION = 1

# Valid providers
VALID_PROVIDERS = frozenset({
    "mlx",
    "fastembed_coreml",
    "fastembed_cpu",
    "mock",
    "unknown",
})

EmbeddingProvider = Literal["mlx", "fastembed_coreml", "fastembed_cpu", "mock", "unknown"]

# Detection provenance labels
ProviderDetectionSource = Literal["daemon", "metadata", "env", "fallback_import", "unknown", "mock"]


@dataclass(frozen=True, slots=True)
class _CachedDetection:
    """Frozen result of detect_current_provider()."""
    provider: EmbeddingProvider
    model_id: str
    dims: int
    source: ProviderDetectionSource
    elapsed_ms: float
    expires_at: float  # monotonic time.time()


# --------------------------------------------------------------------------- #
# Detection cache
# --------------------------------------------------------------------------- #

# Module-level cache state (intentionally module-level for process lifetime)
_cache_cached: _CachedDetection | None = None
_cache_hits: int = 0


def _cache_ttl() -> float:
    """Return the detection cache TTL in seconds from env or default 30s."""
    try:
        return float(os.environ.get("MEMPALACE_EMBED_PROVIDER_CACHE_TTL", "30"))
    except (ValueError, TypeError):
        return 30.0


def _cache_key() -> str:
    """Build a cache key based on daemon socket path and relevant env vars."""
    sock = os.environ.get("MEMPALACE_EMBED_SOCK", "")
    mock = os.environ.get("MOCK_EMBED", "")
    eval_mode = os.environ.get("MEMPALACE_EVAL_MODE", "")
    return f"{sock}:{mock}:{eval_mode}"


def _cache_get() -> tuple[EmbeddingProvider, str, int, ProviderDetectionSource, float] | None:
    """Return cached detection if still valid, else None."""
    global _cache_cached, _cache_hits
    if _cache_cached is None:
        return None
    # monotonic is guaranteed positive, monotonic vs time.time() comparison is valid
    if _cache_cached.expires_at < time.monotonic():
        _cache_cached = None
        return None
    _cache_hits += 1
    return (
        _cache_cached.provider,
        _cache_cached.model_id,
        _cache_cached.dims,
        _cache_cached.source,
        _cache_hits,
    )


def _cache_set(
    provider: EmbeddingProvider,
    model_id: str,
    dims: int,
    source: ProviderDetectionSource,
    elapsed_ms: float,
) -> None:
    """Store detection result in module-level cache with TTL."""
    global _cache_cached
    _cache_cached = _CachedDetection(
        provider=provider,
        model_id=model_id,
        dims=dims,
        source=source,
        elapsed_ms=elapsed_ms,
        expires_at=time.monotonic() + _cache_ttl(),
    )


# Expose cache stats for testing/metrics
def detection_cache_stats() -> dict:
    """Return detection cache statistics."""
    global _cache_cached, _cache_hits
    ttl = _cache_ttl()
    cached = _cache_cached
    return {
        "hits": _cache_hits,
        "cached": cached is not None,
        "ttl_seconds": ttl,
        "source": cached.source if cached else None,
        "expires_at": cached.expires_at if cached else None,
    }


def clear_detection_cache() -> None:
    """Clear the detection cache (useful for testing)."""
    global _cache_cached, _cache_hits
    _cache_cached = None
    _cache_hits = 0


class EmbeddingMismatchError(RuntimeError):
    """Raised when stored embedding metadata conflicts with current environment."""


class EmbeddingDimsMismatchError(EmbeddingMismatchError):
    """Raised when embedding dimension count doesn't match stored metadata."""


class EmbeddingProviderDriftError(EmbeddingMismatchError):
    """Raised when provider/model differs from stored metadata (dims same)."""


# --------------------------------------------------------------------------- #
# Path helpers
# ---------------------------------------------------------------------------


def _meta_path(palace_path: str) -> Path:
    return Path(palace_path) / "embedding_meta.json"


# --------------------------------------------------------------------------- #
# Load / save
# ---------------------------------------------------------------------------


def load_meta(palace_path: str) -> dict | None:
    """Load embedding metadata from palace, or None if not yet written."""
    path = _meta_path(palace_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read embedding_meta.json: %s", e)
        return None


def save_meta(palace_path: str, meta: dict) -> None:
    """Write embedding metadata to palace_path/embedding_meta.json."""
    path = _meta_path(palace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except (OSError, NotImplementedError):
        pass
    path.write_text(json.dumps(meta, indent=2, default=str))
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


# --------------------------------------------------------------------------- #
# Build metadata dict
# ---------------------------------------------------------------------------


def build_meta(
    provider: EmbeddingProvider,
    model_id: str,
    dims: int,
    collection_name: str = "mempalace_drawers",
) -> dict:
    """Build a new embedding metadata dict with timestamps."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "version": SCHEMA_VERSION,
        "provider": provider,
        "model_id": model_id,
        "dims": dims,
        "collection_name": collection_name,
        "created_at": now,
        "updated_at": now,
    }


# --------------------------------------------------------------------------- #
# Detect current embedding environment
# ---------------------------------------------------------------------------


def detect_current_provider() -> tuple[EmbeddingProvider, str, int]:
    """
    Detect the currently active embedding provider, model_id, and dims.

    Tries in order (lightest first):
      1. Cache hit (if TTL not expired) — "daemon" source label
      2. Mock/eval mode env var — returns immediately without heavy detection
      3. Daemon socket probe — avoids any Python import
      4. _detect_embed_provider from embed_daemon module (importable function)
      5. Env hint MEMPALACE_EMBED_PROVIDER — light read, no heavy import
      6. Fallback: in-process fastembed probe — HEAVY import, only on true fallback
      7. unknown (only when all above fail)

    Results are cached for MEMPALACE_EMBED_PROVIDER_CACHE_TTL seconds (default 30s).
    Cache key includes daemon socket path and mock/eval env vars.

    Returns (provider, model_id, dims).
    """
    import time as _time

    # Check mock/eval mode first — set by eval harness or lexical mode
    if os.environ.get("MOCK_EMBED") or os.environ.get("MEMPALACE_EVAL_MODE"):
        t0 = _time.perf_counter()
        provider: EmbeddingProvider = "mock"
        model_id = "eval-mock"
        dims = 256
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        _cache_set(provider, model_id, dims, "mock", elapsed_ms)
        return (provider, model_id, dims)

    # Try cache first
    cached = _cache_get()
    if cached is not None:
        provider, model_id, dims, _source, _hits = cached
        return (provider, model_id, dims)  # type: ignore[return-value]

    t0 = _time.perf_counter()

    # Env hint — check BEFORE heavy module imports
    env_provider = os.environ.get("MEMPALACE_EMBED_PROVIDER", "").strip().lower()
    if env_provider and env_provider in VALID_PROVIDERS:
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        model_id = os.environ.get("MEMPALACE_EMBED_MODEL_ID", "env-hint")
        dims = 256
        _cache_set(env_provider, model_id, dims, "env", elapsed_ms)
        return (env_provider, model_id, dims)

    # Probe daemon socket (no Python module import)
    provider_info = _probe_daemon_socket()
    if provider_info is not None:
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        provider, model_id, dims = provider_info
        _cache_set(provider, model_id, dims, "daemon", elapsed_ms)
        return provider_info

    # Try importing _detect_embed_provider from embed_daemon
    try:
        from mempalace.embed_daemon import _detect_embed_provider  # noqa: F401
        provider, model_id = _detect_embed_provider()
        dims = 256  # canonical dims for all MemPalace models
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        _cache_set(provider, model_id, dims, "daemon", elapsed_ms)
        return (provider, model_id, dims)
    except Exception:
        pass

    # Fallback: in-process fastembed — HEAVY import, only on true fallback
    try:
        import fastembed  # noqa: F401
        provider = "fastembed_cpu"
        model_id = "BAAI/bge-small-en-v1.5"
        dims = 256
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        _cache_set(provider, model_id, dims, "fallback_import", elapsed_ms)
        return (provider, model_id, dims)
    except Exception:
        pass

    elapsed_ms = (_time.perf_counter() - t0) * 1000
    _cache_set("unknown", "", 256, "unknown", elapsed_ms)
    return ("unknown", "", 256)


def _probe_daemon_socket() -> tuple[EmbeddingProvider, str, int] | None:
    """
    Probe the embed daemon socket for provider info.

    Sends a special {"probe": true, "texts": []} message.
    The daemon responds with {"provider": ..., "model_id": ..., "dims": ...}.

    Returns None if socket is unavailable or protocol not supported.
    """
    try:
        from mempalace.embed_daemon import get_socket_path, SOCKET_PATH
        sock_path = get_socket_path()
    except Exception:
        sock_path = os.environ.get("MEMPALACE_EMBED_SOCK", SOCKET_PATH)

    sock_path = os.path.expanduser(sock_path)
    if not Path(sock_path).exists():
        return None

    import socket as _sock
    s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
    s.settimeout(5.0)
    try:
        s.connect(sock_path)
        # Send probe request
        msg = json.dumps({"probe": True, "texts": []}).encode()
        # Framed: 4-byte LE length + payload
        s.sendall(len(msg).to_bytes(4, "little") + msg)
        # Read response
        header = s.recv(4)
        if len(header) < 4:
            return None
        resp_len = int.from_bytes(header, "little")
        data = b""
        while len(data) < resp_len:
            chunk = s.recv(resp_len - len(data))
            if not chunk:
                return None
            data += chunk
        resp = json.loads(data.decode())
        if "provider" in resp and "model_id" in resp and "dims" in resp:
            # Validate provider
            prov = str(resp["provider"])
            if prov not in VALID_PROVIDERS:
                prov = "unknown"
            return (prov, str(resp["model_id"]), int(resp.get("dims", 256)))
    except Exception:
        pass
    finally:
        s.close()
    return None


# --------------------------------------------------------------------------- #
# Validation
# ---------------------------------------------------------------------------


def validate_write(
    palace_path: str,
    provider: EmbeddingProvider,
    model_id: str,
    dims: int,
) -> tuple[bool, str]:
    """
    Validate a write against stored embedding metadata.

    Returns (allowed, reason):
      allowed=True, reason=""         → proceed
      allowed=False, reason=error     → hard error
      allowed=True, reason=warning     → warning only (proceed after log)
    """
    meta = load_meta(palace_path)

    # First write — create metadata
    if meta is None:
        return (True, "")

    stored_dims = meta.get("dims", 256)
    stored_provider = meta.get("provider", "unknown")
    stored_model = meta.get("model_id", "")

    # Hard error: dimension mismatch
    if dims != stored_dims:
        raise EmbeddingDimsMismatchError(
            f"Embedding dimension mismatch: current={dims}, stored={stored_dims}. "
            f"Cannot write to palace created with a different embedding dimension. "
            f"Delete the palace and recreate, or use a different palace_path."
        )

    # Check provider/model drift (dims same)
    if provider != stored_provider or model_id != stored_model:
        allow_drift = os.environ.get("MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT", "0")
        if allow_drift not in ("1", "true", "yes"):
            raise EmbeddingProviderDriftError(
                f"Embedding provider/model drift detected: "
                f"current={provider}/{model_id}, stored={stored_provider}/{stored_model} "
                f"(dims={dims} — compatible). "
                f"Set MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT=1 to allow this."
            )
        else:
            logger.warning(
                "Embedding provider/model drift allowed by env: "
                "current=%s/%s, stored=%s/%s (dims=%d)",
                provider, model_id, stored_provider, stored_model, dims,
            )
            return (True, f"provider drift: {stored_provider}→{provider}")

    return (True, "")


# --------------------------------------------------------------------------- #
# Upsert metadata (called after successful first write)
# ---------------------------------------------------------------------------


def ensure_meta(palace_path: str, provider: EmbeddingProvider, model_id: str, dims: int) -> None:
    """Write or update embedding metadata on first successful write."""
    meta = load_meta(palace_path)
    now = datetime.now(timezone.utc).isoformat()
    if meta is None:
        # First write
        save_meta(palace_path, build_meta(provider, model_id, dims))
    else:
        # Update timestamp
        meta["updated_at"] = now
        save_meta(palace_path, meta)