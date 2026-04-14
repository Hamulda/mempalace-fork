"""
MemPalace storage backends.

Export BaseCollection interface and available backends.
Use get_backend() to instantiate the configured backend.

Naming convention:
  - "lance" / "lancedb" — canonical primary storage (LanceDB)
  - "chroma" / "chromadb" — legacy compatibility

Both config.py and settings.py use the same canonical names: "lance" | "chroma".
"""

from .base import BaseCollection

# Lazy import pattern: backends are imported on-demand, not at package import time.
# This prevents Chroma from being eagerly loaded when only Lance is used.
# _LANCE_AVAILABLE is set at module load; individual backends loaded in get_backend().
try:
    from .lance import LanceBackend, LanceCollection
    _LANCE_AVAILABLE = True
except ImportError:
    _LANCE_AVAILABLE = False
    LanceBackend = None  # type: ignore
    LanceCollection = None  # type: ignore

ChromaBackend = None  # type: ignore
ChromaCollection = None  # type: ignore

__all__ = [
    "BaseCollection",
    "get_backend",
    "_LANCE_AVAILABLE",
]


def get_backend(backend_type: str = "lance") -> "ChromaBackend | LanceBackend":
    """
    Factory for storage backends. Uses LAZY imports — backend module loaded on first use.

    Canonical backend names (use these consistently):
      - "lance" — LanceDB, canonical primary storage
      - "chroma" — ChromaDB, legacy compat

    Args:
        backend_type: "lance" (default, canonical) or "chroma" (legacy compat)

    Returns:
        An instance of LanceBackend or ChromaBackend.

    Raises:
        ImportError: If "lance" is requested but LanceDB is not installed.
        ValueError: If an unknown backend_type is requested.
    """
    global ChromaBackend, ChromaCollection

    if backend_type == "lance":
        if not _LANCE_AVAILABLE:
            raise ImportError(
                "LanceDB backend is not installed. "
                "Install it with: pip install 'mempalace[lance]'"
            )
        return LanceBackend()

    if backend_type == "chroma":
        # Lazy import — only loaded when chroma backend is actually requested
        # Silence Chroma telemetry before module load (chroma imports at get_backend time)
        import logging
        logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)
        from .chroma import ChromaBackend as CB, ChromaCollection as CC
        ChromaBackend = CB
        ChromaCollection = CC
        return ChromaBackend()

    raise ValueError(f"Unknown backend type: {backend_type!r}. Use 'lance' or 'chroma'.")
