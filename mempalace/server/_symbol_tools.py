"""
Symbol/index tools: find, search, callers, file_symbols, recent_changes.
"""
import os
from fastmcp import Context


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
    def mempalace_find_symbol(ctx: Context, symbol_name: str, palace_path: str | None = None) -> dict:
        if not symbol_name:
            return {"error": "symbol_name is required"}
        palace_path = palace_path or _get_palace_path()
        try:
            si = _get_symbol_index(palace_path)
            results = si.find_symbol(symbol_name)
            if not results:
                results = si.search_symbols(symbol_name)
            return {"symbol_name": symbol_name, "results": results, "count": len(results)}
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_search_symbols(ctx: Context, pattern: str, palace_path: str | None = None) -> dict:
        if not pattern:
            return {"error": "pattern is required"}
        palace_path = palace_path or _get_palace_path()
        try:
            si = _get_symbol_index(palace_path)
            results = si.search_symbols(pattern)
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
        project_root = project_root or os.environ.get("PROJECT_ROOT", "")
        try:
            si = _get_symbol_index(palace_path)
            results = si.get_callers(symbol_name, project_root)
            return {"symbol_name": symbol_name, "callers": results, "count": len(results)}
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_recent_changes(ctx: Context, project_root: str | None = None, n: int = 20) -> dict:
        if not project_root:
            project_root = os.environ.get("PROJECT_ROOT", "")
        if not project_root:
            return {"error": "project_root is required"}
        try:
            changes = get_recent_changes(project_root, n=n)
            hot_spots = get_hot_spots(project_root, n=5)
            return {"recent_changes": changes, "hot_spots": hot_spots, "count": len(changes)}
        except Exception as e:
            return {"error": str(e)}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_file_symbols(ctx: Context, file_path: str, palace_path: str | None = None) -> dict:
        if not file_path:
            return {"error": "file_path is required"}
        palace_path = palace_path or _get_palace_path()
        try:
            si = _get_symbol_index(palace_path)
            result = si.get_file_symbols(file_path)
            return {"file_path": file_path, **result}
        except Exception as e:
            return {"error": str(e)}
