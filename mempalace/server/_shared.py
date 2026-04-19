"""
Shared helpers for tool functions: collection access, sanitization, cache.
"""
import os
from pathlib import Path
from ._infrastructure import invalidate_status_cache
from ..config import sanitize_name, sanitize_content


def make_collection_getter(backend, settings):
    """Return a _get_collection closure over backend/settings."""
    def _get_collection(create=False):
        try:
            return backend.get_collection(
                settings.db_path, settings.effective_collection_name, create=create
            )
        except Exception:
            return None
    return _get_collection


def no_palace():
    return {
        "error": "No palace found",
        "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
    }


__all__ = [
    "make_collection_getter",
    "no_palace",
    "invalidate_status_cache",
    "sanitize_name",
    "sanitize_content",
]
