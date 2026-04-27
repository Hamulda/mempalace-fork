"""
MemPalace storage backends.

Export BaseCollection interface and available backends.
Use get_backend() to instantiate the configured backend.

Canonical backend: "lance" (LanceDB). ChromaDB support has been removed.
"""

from typing import Literal

from .base import BaseCollection

# Canonical backend type — LanceDB only
BackendType = Literal["lance"]
BACKEND_CHOICES: tuple[str, ...] = ("lance",)

# Lazy import pattern: LanceDB is imported on-demand via get_backend().
# _LANCE_AVAILABLE is set at module load.
try:
    from .lance import LanceBackend, LanceCollection
    _LANCE_AVAILABLE = True
except ImportError:
    _LANCE_AVAILABLE = False
    LanceBackend = None  # type: ignore
    LanceCollection = None  # type: ignore

__all__ = [
    "BaseCollection",
    "get_backend",
    "_LANCE_AVAILABLE",
    "BackendType",
    "BACKEND_CHOICES",
]


def get_backend(backend_type: BackendType = "lance") -> "LanceBackend":
    """
    Factory for storage backends. Uses LAZY imports — backend module loaded on first use.

    Canonical backend: "lance" (LanceDB).

    Args:
        backend_type: "lance" (default, canonical)

    Returns:
        An instance of LanceBackend.

    Raises:
        ImportError: If LanceDB is not installed.
        ValueError: If "chroma" is requested (no longer supported).
        ValueError: If an unknown backend_type is requested.
    """
    if backend_type == "lance":
        if not _LANCE_AVAILABLE:
            raise ImportError(
                "LanceDB backend is not installed. "
                "Install it with: pip install 'mempalace[lance]'"
            )
        return LanceBackend()

    if backend_type == "chroma":
        raise ValueError(
            "ChromaDB backend has been removed. "
            "LanceDB is the only supported backend. "
            "If you have existing ChromaDB data, migrate it first with: "
            "pip install chromadb && python -m mempalace.migrate chroma-to-lance"
        )

    raise ValueError(
        f"Unknown backend type: {backend_type!r}. "
        f"Use one of: {', '.join(repr(b) for b in BACKEND_CHOICES)}."
    )
