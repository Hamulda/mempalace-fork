"""
Shared project root resolution utilities.

Consolidates _find_git_root and _resolve_project_root from:
  - _session_tools
  - _workflow_tools
  - _symbol_tools
  - wakeup_context

All implementations were byte-for-byte identical. This module is the
canonical source of truth — the original definitions in those files are
now redirects via import.
"""
from pathlib import Path


def _find_git_root(start_path: str) -> str | None:
    """
    Find the git repository root by walking up from start_path.

    Returns the containing git repo root, or None if no .git directory found.
    Deterministic — no env variables required.
    """
    try:
        current = Path(start_path).expanduser().resolve()
        if current.is_file():
            current = current.parent
        for parent in [current] + list(current.parents):
            if (parent / ".git").is_dir():
                return str(parent)
    except Exception:
        pass
    return None


def _resolve_project_root(explicit: str | None, palace_path: str, file_path: str | None = None) -> str | None:
    """
    Resolve project_root with explicit priority and deterministic fallback.

    Resolution order:
    1. explicit parameter (caller-provided)
    2. git root from file_path (when editing a known file)
    3. git root from palace_path (palace lives inside a project)
    4. None (caller handles gracefully)

    No env dependency.
    """
    if explicit:
        return explicit
    if file_path:
        git_root = _find_git_root(file_path)
        if git_root:
            return git_root
    git_root = _find_git_root(palace_path)
    if git_root:
        return git_root
    return None