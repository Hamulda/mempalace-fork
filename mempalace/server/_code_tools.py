from __future__ import annotations

"""
Code intelligence tools: search_code, auto_search, file_context, project_context.
"""
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import Context

# =============================================================================
# PATH MATCHING UTILITIES (module-level for testability)
# =============================================================================


def _source_file_matches(source_file: str, project_path: str) -> bool:
    """Check if source_file is inside or matches the project path.

    Matches:
    - source_file is the project_path itself (single file project)
    - source_file starts with project_path + "/" (subdirectory/file in project)
    - project_path ends with source_file basename (full path to file matches relative)
    - project_path is a path component of source_file (partial dir names)

    Case-insensitive path comparison.
    """
    if not source_file or not project_path:
        return False
    sf_lower = source_file.lower()
    pp_lower = project_path.lower()
    pp_norm = pp_lower.rstrip("/")
    sf_norm = sf_lower.rstrip("/")

    if sf_norm == pp_norm:
        return True

    if sf_norm.startswith(pp_norm + "/"):
        return True

    sf_basename = sf_norm.split("/")[-1] if "/" in sf_norm else sf_norm
    if pp_norm.endswith("/" + sf_basename) or pp_norm == sf_basename:
        return True

    parts = sf_norm.split("/")
    pp_parts = pp_norm.split("/")
    if len(pp_parts) <= len(parts):
        for i in range(len(parts) - len(pp_parts) + 1):
            if "/".join(parts[i:i + len(pp_parts)]) == pp_norm:
                return True
    return False


def _filter_by_project_path(docs: list, metas: list, project_path: str) -> list:
    """Filter documents to those within project_path, preserving order."""
    result = []
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else {}
        sf = meta.get("source_file", "")
        if sf and _source_file_matches(sf, project_path):
            result.append({
                "source_file": sf,
                "language": meta.get("language", ""),
                "line_start": meta.get("line_start", 0),
                "line_end": meta.get("line_end", 0),
                "symbol_name": meta.get("symbol_name", ""),
                "chunk_kind": meta.get("chunk_kind", ""),
                "doc": doc,
            })
    return result


# =============================================================================
# TOOL REGISTRATION
# =============================================================================


def register_code_tools(server, backend, config, settings):
    """
    Register all code-intel @mcp.tool() as closures.
    Called by factory._register_tools().
    """
    from ..searcher import code_search_async, auto_search, is_code_query, hybrid_search_async, _compute_repo_rel_path

    def _get_collection(create=False):
        try:
            return backend.get_collection(
                settings.db_path, settings.effective_collection_name, create=create
            )
        except Exception:
            return None

    def _no_palace():
        return {"error": "No palace found", "hint": "Run: mempalace init <dir> && mempalace mine <dir>"}

    @server.tool(timeout=settings.timeout_read)
    async def mempalace_search_code(
        ctx: Context,
        query: str,
        language: str | None = None,
        symbol_name: str | None = None,
        file_path: str | None = None,
        limit: int = 10,
        project_path: str | None = None,
    ) -> dict:
        """
        Search code using vector similarity within the palace.

        Uses hybrid FTS5 + semantic search (reranked) to find code chunks
        matching the query. Returns chunks with source location, language,
        and symbol context.

        Args:
            query: Natural-language or keyword search query.
            language: Optional language filter (e.g., "python", "javascript").
            symbol_name: Optional symbol name to scope results to.
            file_path: Optional file path to restrict results to that file.
            limit: Maximum number of results to return (default 10).
            project_path: Optional project root to scope results (push-down filter).

        Returns:
            dict with query, language, chunks list, and count.
            Each chunk contains: source_file, language, line_start, line_end,
            symbol_name, chunk_kind, doc (content).
        """
        return await code_search_async(
            query=query, palace_path=settings.db_path, n_results=limit,
            language=language, symbol_name=symbol_name, file_path=file_path,
            project_path=project_path,
        )

    @server.tool(timeout=settings.timeout_read)
    async def mempalace_auto_search(ctx: Context, query: str, limit: int = 10) -> dict:
        """
        Unified search that automatically routes to the best retrieval strategy.

        Uses is_code_query heuristic to decide:
        - Code query → mempalace_search_code (vector similarity)
        - General query → hybrid_search_async (FTS5 + vector + KG)

        Args:
            query: Natural-language search query.
            limit: Maximum number of results to return (default 10).

        Returns:
            Search results dict from the appropriate search strategy.
        """
        return await code_search_async(
            query=query, palace_path=settings.db_path, n_results=limit,
        ) if is_code_query(query) else await hybrid_search_async(
            query=query, palace_path=settings.db_path, n_results=limit,
        )

    @server.tool(timeout=settings.timeout_read)
    def mempalace_file_context(
        ctx: Context,
        file_path: str,
        line_start: int | None = None,
        line_end: int | None = None,
        context_lines: int = 5,
    ) -> dict:
        """
        Read a file with surrounding context lines.

        Args:
            file_path: Absolute path to the file to read.
            line_start: Optional 1-based line number to center context around.
            line_end: Optional 1-based end line (inclusive) for the focused range.
            context_lines: Number of extra lines to include around the range
                (default 5). Applied on both sides when line_start/line_end
                are specified.

        Returns:
            dict with file_path, total_lines, range_start, range_end,
            has_more_before, has_more_after, and lines list with
            line_num and text for each line in the slice.
        """
        p = Path(file_path).expanduser().resolve()
        if not p.exists():
            return {"error": f"File not found: {file_path}"}
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"error": str(e)}
        lines = content.split("\n")
        n = len(lines)
        start = max(0, (line_start or 1) - 1 - context_lines)
        end = min(n, (line_end or n) + context_lines)
        slice_lines = lines[start:end]
        has_more_before = line_start is not None and line_start > 1 + context_lines
        has_more_after = line_end is not None and line_end < n - context_lines
        return {
            "file_path": str(p), "total_lines": n,
            "range_start": start + 1, "range_end": end,
            "has_more_before": has_more_before, "has_more_after": has_more_after,
            "lines": [{"line_num": start + i + 1, "text": line} for i, line in enumerate(slice_lines)],
        }

    @server.tool(timeout=settings.timeout_read)
    async def mempalace_project_context(
        ctx: Context,
        project_path: str,
        query: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> dict:
        """
        Retrieve project-scoped code context.

        Two distinct retrieval modes:

        WITH QUERY: Uses vector similarity search to find semantically relevant
        code chunks within the project. Best for "how does X work" style questions.

        WITHOUT QUERY: Uses deterministic metadata retrieval (col.get) ordered by
        source_file and line_start. Best for "what files exist in this project"
        or "show me the current code structure" style requests.

        Args:
            project_path: File path or directory to scope retrieval to.
                          Can be a file ("auth.py"), directory ("src/"), or
                          full path ("/Users/me/project/src").
            query: Optional semantic search query. If None, uses deterministic
                   metadata retrieval instead of vector search.
            language: Optional language filter (e.g., "Python", "JavaScript").
            limit: Maximum number of chunks to return (default 20).

        Returns:
            dict with project_path, query, language, chunks list, and count.
            Each chunk contains: source_file, language, line_start, line_end,
            symbol_name, chunk_kind, doc (content).
        """
        col = _get_collection()
        if not col:
            return _no_palace()

        try:
            if query:
                # Phase 3 retrieval planner: route by query intent, project_path as hard filter.
                # Path/symbol → specialized paths in code_search; others → code_search_async.
                from mempalace.retrieval_planner import classify_query
                from mempalace.searcher import code_search_async

                intent = classify_query(query)

                if intent == "path":
                    from mempalace.searcher import _path_first_search
                    hits = _path_first_search(
                        query, settings.db_path, col,
                        n_results=limit, language=language,
                        project_path=project_path,
                    )
                    for h in hits:
                        h["doc"] = h.pop("text", "")
                        h["repo_rel_path"] = _compute_repo_rel_path(h.get("source_file", ""), project_path)
                    return {
                        "project_path": project_path, "language": language,
                        "query": query, "chunks": hits[:limit], "count": len(hits),
                        "intent": intent,
                    }

                if intent == "symbol":
                    from mempalace.searcher import _symbol_first_search
                    hits = _symbol_first_search(
                        query, settings.db_path, col,
                        n_results=limit, language=language,
                    )
                    if project_path:
                        hits = [h for h in hits if _source_file_matches(h.get("source_file", ""), project_path)]
                    for h in hits:
                        h["doc"] = h.pop("text", "")
                        h["repo_rel_path"] = _compute_repo_rel_path(h.get("source_file", ""), project_path)
                    return {
                        "project_path": project_path, "language": language,
                        "query": query, "chunks": hits[:limit], "count": len(hits),
                        "intent": intent,
                    }

                # code_exact / code_semantic / memory / mixed: full code_search_async with planner
                result = await code_search_async(
                    query=query, palace_path=settings.db_path, n_results=limit,
                    language=language, project_path=project_path,
                )
                chunks_raw = result.get("results", [])

                # Belt-and-suspenders project filter + schema normalize
                chunks = []
                for chunk in chunks_raw:
                    sf = chunk.get("source_file", "")
                    if project_path and sf and not _source_file_matches(sf, project_path):
                        continue
                    chunks.append({
                        "source_file": sf,
                        "language": chunk.get("language", ""),
                        "line_start": chunk.get("line_start", 0),
                        "line_end": chunk.get("line_end", 0),
                        "symbol_name": chunk.get("symbol_name", ""),
                        "chunk_kind": chunk.get("chunk_kind", ""),
                        "doc": chunk.get("text", chunk.get("document", "")),
                        "repo_rel_path": _compute_repo_rel_path(sf, project_path),
                    })
                return {
                    "project_path": project_path, "language": language,
                    "query": query, "chunks": chunks[:limit], "count": len(chunks),
                    "intent": result.get("filters", {}).get("intent", "mixed"),
                }

            else:
                # Mode 2: Deterministic metadata retrieval (no vector search).
                # We fetch without DB-level filtering and apply all filters in Python:
                # wing=repo, is_latest=True, language, project_path.
                matched = []
                offset = 0
                _BATCH = 100

                while len(matched) < limit:
                    batch_result = col.get(
                        limit=_BATCH,
                        offset=offset,
                        include=["documents", "metadatas"],
                    )
                    batch_docs = batch_result.get("documents", [])
                    batch_metas = batch_result.get("metadatas", [])

                    if not batch_docs:
                        break

                    for i, doc in enumerate(batch_docs):
                        meta = batch_metas[i] if i < len(batch_metas) else {}
                        # Accept both wing="repo" (canonical) and wing="project" (legacy)
                        wing = meta.get("wing", "")
                        if wing not in ("repo", "project"):
                            continue
                        # Canonical filter: is_latest=True (excludes tombstoned chunks)
                        if meta.get("is_latest") is False:
                            continue
                        # Language filter
                        if language and meta.get("language") != language:
                            continue
                        # project_path filter
                        sf = meta.get("source_file", "")
                        if sf and _source_file_matches(sf, project_path):
                            matched.append({
                                "source_file": sf,
                                "language": meta.get("language", ""),
                                "line_start": meta.get("line_start", 0),
                                "line_end": meta.get("line_end", 0),
                                "symbol_name": meta.get("symbol_name", ""),
                                "chunk_kind": meta.get("chunk_kind", ""),
                                "doc": doc,
                            })

                    if len(batch_docs) < _BATCH:
                        break
                    offset += len(batch_docs)

                # Take limit and sort deterministically by source_file, then line_start
                matched = matched[:limit]
                matched.sort(key=lambda x: (x["source_file"], x["line_start"]))

            # Add repo_rel_path to each chunk using project_path as common prefix
            for chunk in matched:
                sf = chunk.get("source_file", "")
                if sf:
                    chunk["repo_rel_path"] = _compute_repo_rel_path(sf, project_path)

            return {
                "project_path": project_path,
                "language": language,
                "query": query,
                "chunks": matched,
                "count": len(matched),
            }
        except Exception as e:
            return {"error": str(e), "project_path": project_path}

    @server.tool(timeout=settings.timeout_read)
    def mempalace_export_claude_md(
        ctx: Context,
        wing: str | None = None,
        room: str | None = None,
        format: str = "markdown",
    ) -> dict:
        from datetime import datetime
        col = _get_collection()
        if not col:
            return _no_palace()
        try:
            where = {}
            if wing:
                where["wing"] = wing
            if room:
                where["room"] = room
            memories = []
            _BATCH = 500
            offset = 0
            while True:
                kwargs = {"include": ["documents", "metadatas"], "limit": _BATCH, "offset": offset}
                if where:
                    kwargs["where"] = where
                batch = col.get(**kwargs)
                docs = batch.get("documents", [])
                metas = batch.get("metadatas", [])
                if not docs:
                    break
                for doc, meta in zip(docs, metas):
                    memories.append({
                        "wing": meta.get("wing", "unknown"), "room": meta.get("room", "unknown"),
                        "content": doc, "source_file": meta.get("source_file", ""),
                    })
                if len(docs) < _BATCH:
                    break
                offset += len(docs)
            if not memories:
                return {"export": "", "count": 0, "message": "No memories found for the specified criteria."}
            if format == "json":
                return {"export": memories, "count": len(memories), "format": "json"}
            lines = [
                "# MemPalace Export", "",
                f"Exported at: {datetime.now().isoformat()}", f"Total memories: {len(memories)}", "",
            ]
            if wing:
                lines.append(f"## Wing: {wing}")
            if room:
                lines.append(f"### Room: {room}")
            for mem in memories:
                lines.append("")
                lines.append(f"### [{mem['wing']}] {mem['room']}")
                if mem['source_file']:
                    lines.append(f"*Source: {mem['source_file']}*")
                lines.append("")
                lines.append(mem['content'])
                lines.append("---")
            return {"export": "\n".join(lines), "count": len(memories), "format": "markdown"}
        except Exception as e:
            return {"error": str(e)}
