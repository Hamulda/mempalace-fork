"""
MemPalace storage backends.

Export BaseCollection interface and available backends.
Use get_backend() to instantiate the configured backend.
"""

from .base import BaseCollection

# ChromaDB is always available (default dependency)
from .chroma import ChromaBackend, ChromaCollection

# LanceDB is optional — may fail import if not installed
try:
    from .lance import LanceBackend, LanceCollection
    _LANCE_AVAILABLE = True
except ImportError:
    _LANCE_AVAILABLE = False
    LanceBackend = None  # type: ignore
    LanceCollection = None  # type: ignore

__all__ = [
    "BaseCollection",
    "ChromaBackend",
    "ChromaCollection",
    "LanceBackend",
    "LanceCollection",
    "_LANCE_AVAILABLE",
]


def get_backend(backend_type: str = "chroma") -> "ChromaBackend | LanceBackend":
    """
    Factory for storage backends.

    Args:
        backend_type: "chroma" (default) or "lance"

    Returns:
        An instance of ChromaBackend or LanceBackend.

    Raises:
        ImportError: If "lance" is requested but LanceDB is not installed.
        ValueError: If an unknown backend_type is requested.
    """
    if backend_type == "lance":
        if not _LANCE_AVAILABLE:
            raise ImportError(
                "LanceDB backend is not installed. "
                "Install it with: pip install 'mempalace[lance]'"
            )
        return LanceBackend()

    if backend_type in ("chroma", "chromadb"):
        return ChromaBackend()

    raise ValueError(f"Unknown backend type: {backend_type!r}. Use 'chroma' or 'lance'.")
