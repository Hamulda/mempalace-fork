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
    collection_name: str = "mempalace_drawers"

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

    class Config:
        env_prefix = "MEMPALACE_"
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = MemPalaceSettings()
