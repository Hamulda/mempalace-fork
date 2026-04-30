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
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

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

    Tries in order:
      1. Daemon socket — live probe with provider model response
      2. _detect_embed_provider from embed_daemon (importable function)
      3. Fallback: in-process detection via _embed_texts_fallback probe
      4. mock (only when MOCK_EMBED or eval mode is active)

    Returns (provider, model_id, dims).
    """
    # Check mock/eval mode first — set by eval harness or lexical mode
    if os.environ.get("MOCK_EMBED") or os.environ.get("MEMPALACE_EVAL_MODE"):
        return ("mock", "eval-mock", 256)

    # Try to probe the daemon socket directly
    provider_info = _probe_daemon_socket()
    if provider_info is not None:
        return provider_info

    # Try importing _detect_embed_provider from embed_daemon
    try:
        from mempalace.embed_daemon import _detect_embed_provider
        provider, model_id = _detect_embed_provider()
        dims = 256  # canonical dims for all MemPalace models
        return (provider, model_id, dims)
    except Exception:
        pass

    # Fallback: try in-process fastembed detection
    try:
        import fastembed  # noqa: F401
        provider: EmbeddingProvider = "fastembed_cpu"  # type: ignore[assignment]
        model_id = "BAAI/bge-small-en-v1.5"
        dims = 256
        return (provider, model_id, dims)
    except Exception:
        pass

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