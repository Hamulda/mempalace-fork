"""
MemPalace Settings — centralizovaná Pydantic konfigurace.
Všechny hodnoty lze přepsat env variables s prefixem MEMPALACE_.
Např. MEMPALACE_TRANSPORT=http, MEMPALACE_DB_PATH=/custom/path

Canonical path model:
    palace_path  — single source of truth for the palace location
    db_path      — derived from palace_path (or MEMPALACE_DB_PATH override for compat)
    collection_name — from config.json via MempalaceConfig, not hardcoded here

The MCP server and CLI tools must agree on the palace location.
Split-brain is prevented by having settings resolve palace_path from the same
MempalaceConfig chain that factory.py uses.
"""

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings
from typing import Literal, Optional
import os


_DEFAULT_PALACE_PATH = os.path.expanduser("~/.mempalace/palace")


def _resolve_palace_path() -> str:
    """Resolve palace_path from env or default — mirrors MempalaceConfig logic."""
    return os.environ.get("MEMPALACE_PALACE_PATH") or os.environ.get(
        "MEMPAL_PALACE_PATH", _DEFAULT_PALACE_PATH
    )


class MemPalaceSettings(BaseSettings):
    """
    Konfigurace MemPalace serveru.
    Všechny hodnoty lze přepsat env variables s prefixem MEMPALACE_.
    Např. MEMPALACE_TRANSPORT=http, MEMPALACE_DB_PATH=/custom/path

    palace_path is the canonical source. db_path derives from it unless
    explicitly overridden via MEMPALACE_DB_PATH (backward compat).
    """

    # Transport
    # Transport — HTTP (streamable-http) is canonical for multi-session.
    # Stdio is single-session/dev fallback, not recommended for Claude Code.
    transport: Literal["stdio", "http"] = "http"
    host: str = "127.0.0.1"
    port: int = 8765

    # ── Palace path (canonical) ─────────────────────────────────────────────
    # Resolved using the same env vars as MempalaceConfig.palace_path.
    # This ensures factory.py's config.palace_path and settings.db_path agree.
    palace_path: str = Field(default_factory=_resolve_palace_path)

    # db_path: for backward compat, MEMPALACE_DB_PATH overrides palace_path.
    # In normal operation, db_path == palace_path (both point to the same data dir).
    db_path: str = Field(default="")

    # Collection name: canonical source is MempalaceConfig (config.json).
    collection_name: str = "mempalace_drawers"

    # Storage backend — LanceDB only (ChromaDB support removed)
    db_backend: Literal["lance"] = "lance"

    @model_validator(mode="after")
    def _sync_db_path(self) -> "MemPalaceSettings":
        """Sync db_path with palace_path. MEMPALACE_DB_PATH override is respected."""
        if self.db_path:
            # Explicit override — accept it as-is (backward compat)
            pass
        else:
            self.db_path = self.palace_path
        return self

    def _resolve_collection_name(self) -> str:
        """Resolve collection_name from the same MempalaceConfig used by factory."""
        try:
            from .config import MempalaceConfig
            return MempalaceConfig().collection_name
        except Exception:
            return self.collection_name

    @property
    def effective_collection_name(self) -> str:
        """Runtime-resolved collection name from canonical config."""
        return self._resolve_collection_name()

    # Cache TTL (zachovat hodnoty z middleware.py)
    cache_ttl_status: int = 5  # sekund
    cache_ttl_metadata: int = 30  # sekund pro wings/rooms/taxonomy
    query_cache_ttl: int = 300  # sekund (env: MEMPALACE_QUERY_CACHE_TTL)

    # Circuit breaker
    cb_failure_threshold: int = 5
    cb_recovery_timeout: float = 30.0  # sekund

    # Response limiting
    max_response_size: int = 500_000  # bytes

    # Logging
    log_payloads: bool = False
    log_sessions: bool = False  # Session ID logging (pro debugging 6 paralelních sessions)

    # WAL
    wal_enabled: bool = True
    wal_dir: str = os.path.expanduser("~/.mempalace/wal")

    # Tool timeouts (sekundy)
    timeout_embed: int = 15  # embed daemon / vector search operations
    timeout_read: int = 10  # LanceDB read operations
    timeout_write: int = 20  # LanceDB write operations

    # Multi-session / shared server
    shared_server_mode: bool = False        # When True, HTTP transport is canonical
    session_registry_enabled: bool = True  # Enable session registry
    write_coordinator_enabled: bool = True  # Enable write coordinator

    # Session registry
    session_timeout_seconds: int = 300      # Mark session idle after 5min no heartbeat
    session_stale_seconds: int = 900        # Consider session stale after 15min

    # Write coordinator
    claim_timeout_seconds: int = 60        # Auto-release claim after 60s (prevents deadlocks)

    # Namespace
    namespace_default: str = "session_memory"  # Default namespace for observations

    # Reranker — disabled by default to save ~90MB RAM and ~3s startup on M1 Air 8GB.
    # CrossEncoder loads lazily on first rerank=True call. Enable only if:
    # - You run latency-sensitive workloads where the first rerank call can't wait ~3s
    # - You have confirmed memory budget headroom (~90MB above baseline)
    reranker_warmup: bool = False  # opt-in eager load of cross-encoder at server start

    model_config = {
        "env_prefix": "MEMPALACE_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = MemPalaceSettings()
