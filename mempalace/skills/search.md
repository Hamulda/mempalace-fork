# MemPalace Search — Symbol-First Priority

When the user wants to search their MemPalace memories, follow these steps:

## Priority Order

### 1. Symbol-First Lookup (for code queries)

If the query looks like a symbol/function/class name, start with symbol tools:

```
mempalace_find_symbol(symbol_name="process_file")
```
→ Returns file path, line range, signature immediately.

For partial matches:
```
mempalace_search_symbols(pattern="process*")
```

For "who calls this" queries:
```
mempalace_callers(symbol_name="my_function", project_root="/path/to/project")
```

For "what's in this file":
```
mempalace_file_symbols(file_path="/src/myfile.py")
```

### 2. Semantic Search (Preferred for general queries)

If MCP tools are available, use them in this priority order:

- mempalace_auto_search(query, n_results, project_path) -- Auto-detects code vs prose, routes automatically.
  Use this as the DEFAULT entry point for unknown query types.
  **Pass `project_path` when Claude Code has an active project context** — this
  pushes the scope into the retrieval layer so cross-project results are excluded
  at the source. All code-search tools (`mempalace_search_code`,
  `mempalace_project_context`) also support `project_path`.
- mempalace_search(query, wing, room, rerank, is_latest, agent_id) -- Fast semantic search.
  Use for keyword/topic search. rerank=True for better precision on complex queries.
- mempalace_hybrid_search(query, wing, room, use_kg, rerank) -- Hybrid search combining
  semantic (LanceDB) + knowledge graph entity matches. Use when context is rich or query
  involves entities. use_kg=True (include KG triples), rerank=True (cross-encoder reranking).
- mempalace_code_search(query, language, symbol_name, file_path) -- Code-specialized search
  with language/symbol/path filters. Best for "find me Python code about X".

### 3. Discover Structure

- mempalace_list_wings -- Discover all available wings
- mempalace_list_rooms(wing) -- List rooms within a specific wing
- mempalace_get_taxonomy -- Full wing/room/drawer tree
- mempalace_traverse(room) -- Walk the knowledge graph from a room
- mempalace_find_tunnels(wing1, wing2) -- Find cross-wing connections

### 4. CLI Fallback

If MCP tools are not available, fall back to the CLI:

    mempalace search "query" [--wing X] [--room Y]

## Query Type Detection

| Query Type | Example | Start With |
|-----------|---------|------------|
| Symbol lookup | "where is process_file defined" | mempalace_find_symbol |
| Caller search | "who calls validate_token" | mempalace_callers |
| Code search | "find Python code about auth" | mempalace_code_search |
| Semantic (project-scoped) | "how does auth work" | mempalace_auto_search + project_path |
| Semantic (global) | "sessions don't expire correctly" | mempalace_auto_search |
| Entity/fact | "what did we decide about JWT" | mempalace_hybrid_search |
| File symbols | "what's in auth.py" | mempalace_file_symbols |

**Tip:** When Claude Code has an active project, always pass `project_path` to
`mempalace_auto_search` or `mempalace_search_code` — the scope is applied at
the retrieval layer, preventing cross-project leakage before results are ranked.

## Recent Changes Awareness

Before doing deep searches, consider checking recent changes:

```
mempalace_recent_changes(project_root="/path", n=10)
```

This shows files changed in recent commits — helps prioritize results from active files.

## Present Results

When presenting search results:
- Always include source attribution: wing, room, and drawer for each result
- Show relevance or similarity scores if available
- For symbol results, show file path and line range
- Group results by wing/room when returning multiple hits

## After Results

Offer next steps:
- Drill deeper -- search within a specific room or narrow the query
- Explore symbols -- use symbol tools to find callers/definitions
- Traverse -- explore the knowledge graph from a related room
- Check tunnels -- look for cross-wing connections if the topic spans domains