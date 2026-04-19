"""
Code intelligence tools: search_code, auto_search, file_context, project_context.
"""
import os
from pathlib import Path
from fastmcp import Context


def register_code_tools(server, backend, config, settings):
    """
    Register all code-intel @mcp.tool() as closures.
    Called by factory._register_tools().
    """
    from ..searcher import code_search_async, auto_search, is_code_query, hybrid_search_async

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
    ) -> dict:
        return await code_search_async(
            query=query, palace_path=settings.db_path, n_results=limit,
            language=language, symbol_name=symbol_name, file_path=file_path,
        )

    @server.tool(timeout=settings.timeout_read)
    async def mempalace_auto_search(ctx: Context, query: str, limit: int = 10) -> dict:
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
    def mempalace_project_context(
        ctx: Context,
        project_path: str,
        query: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> dict:
        col = _get_collection()
        if not col:
            return _no_palace()
        matched = []
        try:
            n_fetch = min(limit * 4, 200)
            if query:
                where = {}
                if language:
                    where["language"] = language
                q_result = col.query(
                    query_texts=[query], n_results=n_fetch,
                    where=where if where else None,
                    include=["documents", "metadatas"],
                )
                docs = q_result.get("documents", [[]])[0] or []
                metas = q_result.get("metadatas", [[]])[0] or []
                for i, doc in enumerate(docs):
                    meta = metas[i] if i < len(metas) else {}
                    sf = meta.get("source_file", "")
                    if sf and project_path in sf:
                        matched.append({
                            "source_file": sf,
                            "language": meta.get("language", ""),
                            "line_start": meta.get("line_start", 0),
                            "line_end": meta.get("line_end", 0),
                            "symbol_name": meta.get("symbol_name", ""),
                            "chunk_kind": meta.get("chunk_kind", ""),
                            "doc": doc,
                        })
                        if len(matched) >= limit:
                            break
            else:
                where = {}
                if language:
                    where["language"] = language
                q_result = col.query(
                    query_texts=[""], n_results=n_fetch,
                    where=where if where else None,
                    include=["documents", "metadatas"],
                )
                docs = q_result.get("documents", [[]])[0] or []
                metas = q_result.get("metadatas", [[]])[0] or []
                for i, doc in enumerate(docs):
                    meta = metas[i] if i < len(metas) else {}
                    sf = meta.get("source_file", "")
                    if sf and project_path in sf:
                        matched.append({
                            "source_file": sf,
                            "language": meta.get("language", ""),
                            "line_start": meta.get("line_start", 0),
                            "line_end": meta.get("line_end", 0),
                            "symbol_name": meta.get("symbol_name", ""),
                            "chunk_kind": meta.get("chunk_kind", ""),
                            "doc": doc,
                        })
                        if len(matched) >= limit:
                            break
            return {"project_path": project_path, "language": language, "query": query,
                    "chunks": matched, "count": len(matched)}
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
