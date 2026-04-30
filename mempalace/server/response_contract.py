"""
MCP tool response contract — unified schema for all MemPalace tools.

Version: 1.0
Goal: Claude Code can consume results without guessing between chunks/results/doc/text.

Schema rules:
- Search/tool hits: always include id, text, source_file, language, line_start,
  line_end, symbol_name, symbol_fqn, chunk_kind, score, retrieval_path, project_path_applied.
- Legacy keys preserved: chunks (if old tool used it), doc (alias for text).
- Error shape: {ok: false, error: {code, message}, tool_contract_version: "1.0"}.
- Success shape: {ok: true, tool_contract_version: "1.0", ...tool-specific fields}.
"""

TOOL_CONTRACT_VERSION = "1.0"

# ── Success response ───────────────────────────────────────────────────────────


def ok_response(tool: str, data: dict, meta: dict | None = None) -> dict:
    """Build a success response with contract metadata."""
    resp = {
        "ok": True,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "tool": tool,
    }
    resp.update(data)
    if meta:
        resp["_meta"] = meta
    return resp


# ── Error response ─────────────────────────────────────────────────────────────


def error_response(
    tool: str,
    message: str,
    code: str = "error",
    meta: dict | None = None,
) -> dict:
    """Build an error response with structured error shape."""
    resp = {
        "ok": False,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "tool": tool,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if meta:
        resp["_meta"] = meta
    return resp


# ── No-palace response ──────────────────────────────────────────────────────────


def no_palace_response(tool: str = "unknown") -> dict:
    """Build a no-palace error response."""
    return {
        "ok": False,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "tool": tool,
        "error": {
            "code": "no_palace",
            "message": "No palace found",
            "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
        },
    }


# ── Hit normalization ──────────────────────────────────────────────────────────


def normalize_hit(hit: dict, project_path: str | None = None) -> dict:
    """
    Normalize a search hit to the canonical schema.

    Canonical fields (always present when data is available):
        id              — drawer/entity ID
        text            — content (canonical key)
        doc             — alias for text (legacy)
        source_file     — absolute path
        repo_rel_path   — relative to project root
        language        — programming language
        line_start      — start line (1-based)
        line_end        — end line (inclusive)
        symbol_name     — name of the symbol (function/class/etc)
        symbol_fqn      — fully-qualified name
        chunk_kind      — kind: function, class, method, comment, prose, etc.
        score           — retrieval score (similarity/RRF)
        retrieval_path  — how this was found (vector, fts5, kg, mixed)
        project_path_applied — project path used to filter this result

    Preserves all original keys (no data loss on partial hits).
    """
    # Resolve text: prefer non-None "text", fall back to "doc", fall back to ""
    # None text must not overwrite a present doc
    _raw_text = hit.get("text")
    text = _raw_text if _raw_text is not None else hit.get("doc") or hit.get("content", "")

    # Compute score: prefer non-None score, fall back to similarity/rrf_score
    _raw_score = hit.get("score")
    score = _raw_score if _raw_score is not None else hit.get("similarity") or hit.get("rrf_score") or 0.0

    # Project context — argument takes priority over raw hit value
    _raw_ppa = hit.get("project_path_applied")
    resolved_project_path_applied = project_path if project_path is not None else (_raw_ppa if _raw_ppa is not None else "")
    source_file = hit.get("source_file") or hit.get("file_path") or ""

    # Compute repo_rel_path if project_path provided
    repo_rel_path = ""
    if source_file and project_path:
        repo_rel_path = _compute_repo_rel(source_file, project_path)

    return {
        # Preserve original metadata for debugging (goes first so canonical fields win)
        **hit,
        # Canonical identity (overwrites any raw hit values)
        "id": hit.get("id") or hit.get("drawer_id") or "",
        # Canonical content
        "text": text,
        "doc": text,  # legacy alias
        # Source location
        "source_file": source_file,
        "repo_rel_path": repo_rel_path,
        # Language
        "language": hit.get("language") or "",
        # Line range
        "line_start": hit.get("line_start") or hit.get("lineno") or 0,
        "line_end": hit.get("line_end") or 0,
        # Symbol
        "symbol_name": hit.get("symbol_name") or "",
        "symbol_fqn": hit.get("symbol_fqn") or hit.get("fqn") or "",
        # Kind
        "chunk_kind": hit.get("chunk_kind") or hit.get("kind") or "",
        # Score
        "score": score,
        # Retrieval path
        "retrieval_path": hit.get("retrieval_path") or hit.get("source") or hit.get("_source") or "unknown",
        # Project context
        "project_path_applied": resolved_project_path_applied,
    }


def _compute_repo_rel(source_file: str, project_path: str) -> str:
    """
    Compute repo-relative path from absolute source_file given a project root.

    Returns the portion of source_file that is relative to project_path.
    If source_file doesn't start with project_path, returns source_file unchanged.
    """
    if not source_file or not project_path:
        return ""

    pp = project_path.rstrip("/")
    sf = source_file

    # Normal subpath
    if sf.startswith(pp + "/"):
        return sf[len(pp) + 1:]

    # Exact match (source_file IS the project root itself)
    if sf == pp:
        return ""

    # Basename fallback (handles single-file projects and partial overlaps)
    sf_basename = sf.split("/")[-1]
    if pp.endswith("/" + sf_basename) or pp == sf_basename:
        return sf_basename

    return sf


# ── Normalize list of hits ──────────────────────────────────────────────────────


def normalize_results(
    hits: list[dict],
    project_path: str | None = None,
    project_path_applied: bool = False,
) -> list[dict]:
    """
    Normalize a list of hits.

    Args:
        hits: list of raw hit dicts
        project_path: project root for repo_rel_path computation
        project_path_applied: if True, project_path was already applied as a
            filter server-side so project_path_applied = project_path for all hits
    """
    if not hits:
        return []

    applied_path = project_path if project_path_applied else None
    return [normalize_hit(h, applied_path) for h in hits]


# ── Search tool wrapper ─────────────────────────────────────────────────────────


def make_search_response(
    tool: str,
    hits: list[dict],
    query: str,
    project_path: str | None = None,
    project_path_applied: bool = False,
    filters: dict | None = None,
    sources: dict | None = None,
    *,
    include_chunks: bool = True,
) -> dict:
    """
    Build a normalized search tool response.

    Returns both:
    - results: normalized hits (canonical key)
    - chunks: legacy alias for results (for backward compat)

    Args:
        include_chunks: if True, also include "chunks" key (legacy compat)
    """
    results = normalize_results(hits, project_path, project_path_applied)

    resp = ok_response(tool, {
        "query": query,
        "results": results,
        "count": len(results),
        "filters": filters or {},
        "sources": sources or {},
    })

    if include_chunks:
        resp["chunks"] = results  # legacy alias

    return resp


# ── Symbol tools response helpers ──────────────────────────────────────────────


def make_symbol_response(
    symbol_name: str,
    results: list[dict],
    project_root: str | None = None,
    *,
    tool: str = "symbol",
) -> dict:
    """Build a normalized symbol tool response."""
    normalized = []
    for r in results:
        fp = r.get("file_path", "")
        repo_rel = _compute_repo_rel(fp, project_root) if fp and project_root else ""
        normalized.append({
            "symbol_name": r.get("name", symbol_name),
            "symbol_fqn": r.get("fqn", ""),
            "type": r.get("type", ""),
            "file_path": fp,
            "repo_rel_path": repo_rel,
            "line_start": r.get("line_start", 0),
            "line_end": r.get("line_end", 0),
            **r,
        })
    return ok_response(tool, {
        "results": normalized,
        "count": len(normalized),
    })


def make_callers_response(
    symbol_name: str,
    callers: list[dict],
    *,
    tool: str = "mempalace_callers",
) -> dict:
    """
    Build a normalized callers response.

    Preserves explainability fields per caller:
    why, match_type, confidence, caller_fqn, callee_fqn, source_file, repo_rel_path.
    """
    return ok_response(tool, {
        "symbol_name": symbol_name,
        "callers": callers,
        "count": len(callers),
    })


# ── File context response ───────────────────────────────────────────────────────


def make_file_context_response(
    file_path: str,
    total_lines: int,
    range_start: int,
    range_end: int,
    lines: list[dict],
    has_more_before: bool,
    has_more_after: bool,
    project_path: str | None = None,
) -> dict:
    """Build a normalized file_context response."""
    resp = ok_response("file_context", {
        "file_path": file_path,
        "total_lines": total_lines,
        "range_start": range_start,
        "range_end": range_end,
        "has_more_before": has_more_before,
        "has_more_after": has_more_after,
        "lines": lines,
        "project_path_applied": project_path or "",
    })
    # Also provide legacy shape compatibility
    return resp


def file_context_error(
    message: str,
    code: str = "file_context_error",
) -> dict:
    """Build a structured file_context error response."""
    return error_response("file_context", message, code=code)


# ── Project context response ───────────────────────────────────────────────────


def make_project_context_response(
    project_path: str,
    hits: list[dict],
    query: str | None,
    language: str | None,
    intent: str = "mixed",
    project_path_applied: bool = True,
) -> dict:
    """
    Build a normalized project_context response.

    Returns both:
    - results: normalized hits (canonical)
    - chunks: legacy alias
    """
    results = normalize_results(hits, project_path, project_path_applied)
    return ok_response("project_context", {
        "project_path": project_path,
        "query": query,
        "language": language,
        "intent": intent,
        "results": results,
        "chunks": results,  # legacy
        "count": len(results),
    })


# ── Status/list responses ──────────────────────────────────────────────────────


def make_status_response(total_drawers: int, wings: dict, rooms: dict,
                         palace_path: str, memory_guard: dict | str) -> dict:
    return ok_response("status", {
        "total_drawers": total_drawers,
        "wings": wings,
        "rooms": rooms,
        "palace_path": palace_path,
        "memory_guard": memory_guard,
    })