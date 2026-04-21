"""
Symbol/index tools: find, search, callers, file_symbols, recent_changes.

Path output contract:
- source_file: canonical identity (always absolute path)
- repo_rel_path: user-friendly display (relative to project_root, when determinable)
"""
from pathlib import Path
from fastmcp import Context

from ..searcher import _compute_repo_rel_path


# ── Project root resolution (no env dependency) ───────────────────────────────

def _find_git_root(start_path: str) -> str | None:
    """Find git repo root by walking up from start_path. No env dependency."""
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


def _make_path_result(file_path: str, project_root: str) -> dict:
    """Return {source_file, repo_rel_path} dict for a file path.

    Args:
        file_path: absolute file path (source_file canonical identity)
        project_root: project root to compute repo-relative path from
    """
    return {
        "source_file": file_path,
        "repo_rel_path": _compute_repo_rel_path(file_path, project_root) if project_root else "",
    }


def register_symbol_tools(server, backend, config, settings):
    """
    Register all symbol/index @mcp.tool() as closures.
    Called by factory._register_tools().
    """
    from ..symbol_index import SymbolIndex
    from ..recent_changes import get_recent_changes, get_hot_spots

    def _get_palace_path():
        return settings.db_path

    def _get_symbol_index(palace_path: str):
        return SymbolIndex.get(palace_path)

    @server.tool(timeout=settings.timeout_read)
    def mempalace_find_symbol(ctx: Context, symbol_name: str, project_root: str | None = None, palace_path: str | None = None) -> dict:
        if not symbol_name:
            return {"error": "symbol_name is required"}
        palace_path = palace_path or _get_palace_path()
        if not project_root:
            project_root = _find_git_root(palace_path) or ""
        try:
            si = _get_symbol_index(palace_path)
            raw_results = si.find_symbol(symbol_name)
            if not raw_results:
                raw_results = si.search_symbols(symbol_name)
            results = []
            for r in raw_results:
                fp = r.get("file_path", "")
                path_info = _make_path_result(fp, project_root) if fp else {}
                results.append({**r, **path_info})
            return {"symbol_name": symbol_name, "results": results, "count": len(results)}
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_search_symbols(ctx: Context, pattern: str, project_root: str | None = None, palace_path: str | None = None) -> dict:
        if not pattern:
            return {"error": "pattern is required"}
        palace_path = palace_path or _get_palace_path()
        if not project_root:
            project_root = _find_git_root(palace_path) or ""
        try:
            si = _get_symbol_index(palace_path)
            raw_results = si.search_symbols(pattern)
            results = []
            for r in raw_results:
                fp = r.get("file_path", "")
                path_info = _make_path_result(fp, project_root) if fp else {}
                results.append({**r, **path_info})
            return {"pattern": pattern, "results": results, "count": len(results)}
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_callers(
        ctx: Context,
        symbol_name: str,
        project_root: str | None = None,
        palace_path: str | None = None,
    ) -> dict:
        if not symbol_name:
            return {"error": "symbol_name is required"}
        palace_path = palace_path or _get_palace_path()
        if not project_root:
            project_root = _find_git_root(palace_path) or ""
        try:
            si = _get_symbol_index(palace_path)
            raw_callers = si.get_callers(symbol_name, project_root)
            callers = []
            for r in raw_callers:
                fp = r.get("file_path", "")
                path_info = _make_path_result(fp, project_root) if fp else {}
                callers.append({**r, **path_info})
            return {"symbol_name": symbol_name, "callers": callers, "count": len(callers)}
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_recent_changes(ctx: Context, project_root: str | None = None, n: int = 20) -> dict:
        if not project_root:
            project_root = _find_git_root(_get_palace_path()) or ""
        if not project_root:
            return {"error": "project_root is required"}
        try:
            changes = get_recent_changes(project_root, n=n)
            hot_spots = get_hot_spots(project_root, n=5)
            # Add repo_rel_path to each entry
            for entry in changes:
                fp = entry.get("abs_path", "")
                entry["repo_rel_path"] = _compute_repo_rel_path(fp, project_root) if fp else ""
            for entry in hot_spots:
                fp = entry.get("abs_path", "")
                entry["repo_rel_path"] = _compute_repo_rel_path(fp, project_root) if fp else ""
            return {"recent_changes": changes, "hot_spots": hot_spots, "count": len(changes)}
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_file_symbols(ctx: Context, file_path: str, project_root: str | None = None, palace_path: str | None = None) -> dict:
        if not file_path:
            return {"error": "file_path is required"}
        palace_path = palace_path or _get_palace_path()
        if not project_root:
            project_root = _find_git_root(palace_path) or ""
        try:
            si = _get_symbol_index(palace_path)
            result = si.get_file_symbols(file_path)
            path_info = _make_path_result(file_path, project_root)
            return {**result, **path_info}
        except Exception as e:
            return {"error": str(e)}
