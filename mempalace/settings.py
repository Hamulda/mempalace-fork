"""
MemPalace Settings — centralizovaná Pydantic konfigurace.
Všechny hodnoty lze přepsat env variables s prefixem MEMPALACE_.
Např. MEMPALACE_TRANSPORT=http, MEMPALACE_DB_PATH=/custom/path
"""

from pydantic_settings import BaseSettings
from typing import Literal
import os


class MemPalaceSettings(BaseSettings):
    """
    Konfigurace MemPalace serveru.
    Všechny hodnoty lze přepsat env variables s prefixem MEMPALACE_.
    Např. MEMPALACE_TRANSPORT=http, MEMPALACE_DB_PATH=/custom/path
    """

    # Transport
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765

    # Database (zachovat stávající env vars z config.py)
    db_path: str = os.path.expanduser("~/.mempalace/palace")
    db_backend: Literal["lance", "chroma"] = "lance"  # canonical: lance is primary, chroma is legacy compat

    # Collection name: canonical source is MempalaceConfig (config.json).
    # This field defaults to the same value so settings.py works standalone
    # (e.g. in tests that don't load config.json) while matching production behavior.
    collection_name: str = "mempalace_drawers"

    def _resolve_collection_name(self) -> str:
        """Resolve collection_name from canonical config source at runtime.

        Uses MempalaceConfig (config.json) as the single source of truth.
        This ensures the MCP server respects user configuration from config.json
        rather than having a hardcoded separate default.
        """
        try:
            from .config import MempalaceConfig
            return MempalaceConfig().collection_name
        except Exception:
            return self.collection_name  # fallback to hardcoded default

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
    timeout_read: int = 10  # ChromaDB/LanceDB read operations
    timeout_write: int = 20  # ChromaDB/LanceDB write operations (writes are slower)

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

    class Config:
        env_prefix = "MEMPALACE_"
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = MemPalaceSettings()
