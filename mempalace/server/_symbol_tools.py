"""
Symbol/index tools: find, search, callers, file_symbols, recent_changes.

Path output contract:
- source_file: canonical identity (always absolute path)
- repo_rel_path: user-friendly display (relative to project_root, when determinable)

Response contract (v1.0):
- Success: {ok: true, tool_contract_version: "1.0", tool: <name>, ...data}
- Error:   {ok: false, tool_contract_version: "1.0", tool: <name>, error: {code, message}}
"""
from pathlib import Path
from fastmcp import Context

from ..searcher import _compute_repo_rel_path

# ── Project root resolution (imported from canonical source) ───────────────────
from ._project_root import _find_git_root
from .response_contract import (
    ok_response,
    error_response,
    make_symbol_response,
    make_callers_response,
)


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
            return error_response("mempalace_find_symbol", "symbol_name is required", code="missing_argument")
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
            return make_symbol_response(symbol_name, results, project_root, tool="mempalace_find_symbol")
        except Exception as e:
            return error_response("mempalace_find_symbol", str(e), code="internal_error")

    @server.tool(timeout=settings.timeout_read)
    def mempalace_search_symbols(ctx: Context, pattern: str, project_root: str | None = None, palace_path: str | None = None) -> dict:
        if not pattern:
            return error_response("mempalace_search_symbols", "pattern is required", code="missing_argument")
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
            resp = make_symbol_response(pattern, results, project_root, tool="mempalace_search_symbols")
            resp["pattern"] = pattern
            return resp
        except Exception as e:
            return error_response("mempalace_search_symbols", str(e), code="internal_error")

    @server.tool(timeout=settings.timeout_read)
    def mempalace_callers(
        ctx: Context,
        symbol_name: str,
        project_root: str | None = None,
        palace_path: str | None = None,
    ) -> dict:
        if not symbol_name:
            return error_response("mempalace_callers", "symbol_name is required", code="missing_argument")
        palace_path = palace_path or _get_palace_path()
        if not project_root:
            project_root = _find_git_root(palace_path) or ""
        try:
            si = _get_symbol_index(palace_path)
            # Primary: AST call graph (high confidence, precise)
            raw_callers = si.get_callers_ast(symbol_name)
            # Fallback: import-based heuristic (low confidence)
            if not raw_callers:
                heuristic = si.get_callers(symbol_name, project_root)
                for r in heuristic:
                    r["confidence"] = "low"
                    r["match_type"] = "import_ref"
                    r["why"] = f"import-based heuristic: module imports suggest {symbol_name} may be called"
                    r["callee_fqn"] = symbol_name
                    raw_callers.append(r)
            callers = []
            for r in raw_callers:
                fp = r.get("source_file", "")
                path_info = _make_path_result(fp, project_root) if fp else {}
                # Attach why for all results
                if r.get("match_type") == "ast_call":
                    r["why"] = f"AST call: {r.get('caller_fqn', 'unknown')}() calls {symbol_name}() at line {r.get('line', '?')}"
                elif r.get("match_type") == "import_ref":
                    if not r.get("why"):
                        r["why"] = f"import_ref: {symbol_name} imported in {fp}"
                r["callee_fqn"] = symbol_name
                callers.append({**r, **path_info})
            return make_callers_response(symbol_name, callers)
        except Exception as e:
            return error_response("mempalace_callers", str(e), code="internal_error")

    @server.tool(timeout=settings.timeout_read)
    def mempalace_recent_changes(ctx: Context, project_root: str | None = None, n: int = 20) -> dict:
        if not project_root:
            project_root = _find_git_root(_get_palace_path()) or ""
        if not project_root:
            return error_response("mempalace_recent_changes", "project_root is required", code="missing_argument")
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
            return ok_response("mempalace_recent_changes", {
                "recent_changes": changes,
                "hot_spots": hot_spots,
                "count": len(changes),
            })
        except Exception as e:
            return error_response("mempalace_recent_changes", str(e), code="internal_error")

    @server.tool(timeout=settings.timeout_read)
    def mempalace_file_symbols(ctx: Context, file_path: str, project_root: str | None = None, palace_path: str | None = None) -> dict:
        if not file_path:
            return error_response("mempalace_file_symbols", "file_path is required", code="missing_argument")
        palace_path = palace_path or _get_palace_path()
        if not project_root:
            project_root = _find_git_root(palace_path) or ""
        try:
            si = _get_symbol_index(palace_path)
            result = si.get_file_symbols(file_path)
            path_info = _make_path_result(file_path, project_root)
            resp_data = {**result, **path_info}
            return ok_response("mempalace_file_symbols", resp_data)
        except Exception as e:
            return error_response("mempalace_file_symbols", str(e), code="internal_error")
