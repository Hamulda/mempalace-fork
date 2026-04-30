# PHASE33: MCP Response Contract — Unified Schema

**Date:** 2026-04-30
**Status:** ✅ Complete — 26 tests pass

## Summary

Unified MCP response contracts across all MemPalace server tools. Claude Code can now consume results without guessing between `chunks`, `results`, `doc`, `text`, or different error shapes.

---

## What Was Done

### 1. New Module: `mempalace/server/response_contract.py`

Shared response helpers for all MCP tools:

| Function | Purpose |
|---|---|
| `ok_response(tool, data, meta)` | Success response with `tool_contract_version: "1.0"` |
| `error_response(tool, message, code, meta)` | Structured error with `ok: false` |
| `no_palace_response()` | Canonical no-palace error (`code: "no_palace"`) |
| `normalize_hit(hit, project_path)` | Normalize single hit to canonical schema |
| `normalize_results(hits, project_path, project_path_applied)` | Normalize list of hits |
| `make_search_response(tool, hits, query, ...)` | Build search response (includes both `results` and legacy `chunks`) |
| `make_file_context_response(...)` | Normalized file context response |
| `make_project_context_response(...)` | Normalized project context (both `results` + `chunks`) |
| `file_context_error(message, code)` | Structured file_context denial |
| `_compute_repo_rel(source_file, project_path)` | Compute repo-relative path |

**Canonical hit schema** (all fields present when data available):
```
id, text, doc (legacy alias), source_file, repo_rel_path,
language, line_start, line_end, symbol_name, symbol_fqn,
chunk_kind, score, retrieval_path, project_path_applied
```

**Error shape** (consistent across all tools):
```json
{
  "ok": false,
  "tool_contract_version": "1.0",
  "tool": "...",
  "error": {"code": "...", "message": "..."}
}
```

### 2. Updated `_code_tools.py`

**`mempalace_search_code`:** Wraps `code_search_async` results through `make_search_response`. Returns both `results` (canonical) and `chunks` (legacy). Includes `project_path_applied`.

**`mempalace_auto_search`:** Wraps both code and hybrid paths through `make_search_response`. Sets `project_path_applied=True` when project_path provided.

**`mempalace_file_context`:** Uses `file_context_error()` for structured denial (`ok: false`, `code: "access_denied"`, `code: "not_found"`, `code: "read_error"`). Success uses `make_file_context_response()`.

**`mempalace_project_context`:** All three branches (path intent, symbol intent, code_exact/semantic/mixed, deterministic) now use `make_project_context_response()`, returning both `results` and `chunks`.

**`_no_palace()`:** Now returns `no_palace_response()`.

### 3. Tests: `tests/test_mcp_response_contract.py`

26 tests covering:
- `TOOL_CONTRACT_VERSION == "1.0"`
- `ok_response` structure verification
- `error_response` with code + message
- `no_palace_response` shape
- `normalize_hit` with all fields, legacy fallbacks, original key preservation
- `normalize_results` empty + multiple
- `make_search_response` includes `chunks` (legacy) + filters/sources
- `make_search_response` `include_chunks=False` works
- `make_project_context_response` shape
- `make_file_context_response` shape
- `file_context_error` with code and default
- `_compute_repo_rel` for subpath, root file, exact dir match, file-to-file
- Import verification of response_contract in _code_tools
- Integration tests for search_code, project_context, file_context error, auto_search

---

## Results

```bash
$ uv run pytest tests/test_mcp_response_contract.py -q
..........................                                               [100%]
26 passed in 2.23s

$ uv run pytest tests/test_project_path_tooling_consistency.py tests/test_file_context_scope.py tests/test_plugin_workflow_guardrails.py -q
..................................................................       [100%]
66 passed in 3.88s

$ python -c "import sys; import mempalace; print('chromadb' in sys.modules)"
False
```

---

## Files Changed

| File | Change |
|---|---|
| `mempalace/server/response_contract.py` | **New** — shared response helpers |
| `mempalace/server/_code_tools.py` | Updated 5 tools to use contract helpers |
| `tests/test_mcp_response_contract.py` | **New** — 26 contract tests |

---

## Backward Compatibility

- `chunks` key preserved (alias for `results`) in all search/project_context tools
- `doc` key preserved as alias for `text` in all normalized hits
- No breaking changes to existing tool call signatures
- All pre-existing tests pass (66 tests in dependent suites)