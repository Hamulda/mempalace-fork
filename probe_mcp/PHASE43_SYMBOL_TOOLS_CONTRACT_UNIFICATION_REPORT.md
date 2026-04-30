# PHASE43 — Symbol Tools Contract Unification Report

**Date:** 2026-04-30
**Status:** COMPLETE

## Mission

Finish MCP response contract unification for symbol tools. Code tools already used `response_contract.py`; symbol tools still returned ad-hoc dicts and raw `{"error": str(e)}` shapes.

## Changes Made

### 1. `mempalace/server/response_contract.py`

- **`make_symbol_response`**: Added `tool` keyword-only param (default `"symbol"`) so each caller can specify its actual tool name (`"mempalace_find_symbol"`, `"mempalace_search_symbols"`).

- **`make_callers_response`**: New helper for `mempalace_callers` — wraps `ok_response` with `symbol_name`, `callers`, `count`; preserves explainability fields (`why`, `match_type`, `confidence`, `caller_fqn`, `callee_fqn`, `source_file`, `repo_rel_path`) inside each caller dict.

- **Dead code removal**: Eliminated duplicate `return sf` at line 167.

- **`normalize_results` / `make_search_response`**: Restored accidentally removed functions (needed by `make_project_context_response`).

### 2. `mempalace/server/_symbol_tools.py`

All 5 tools now use `ok_response` / `error_response` / `make_symbol_response` / `make_callers_response`:

| Tool | Success | Error |
|------|---------|-------|
| `mempalace_find_symbol` | `make_symbol_response(..., tool="mempalace_find_symbol")` | `error_response(..., code="missing_argument")` / `code="internal_error"` |
| `mempalace_search_symbols` | `make_symbol_response(..., tool="mempalace_search_symbols")` + `resp["pattern"] = pattern` | `error_response(..., code="missing_argument")` / `code="internal_error"` |
| `mempalace_callers` | `make_callers_response(symbol_name, callers)` | `error_response(..., code="missing_argument")` / `code="internal_error"` |
| `mempalace_recent_changes` | `ok_response("mempalace_recent_changes", {...})` | `error_response(..., code="missing_argument")` / `code="internal_error"` |
| `mempalace_file_symbols` | `ok_response("mempalace_file_symbols", resp_data)` | `error_response(..., code="missing_argument")` / `code="internal_error"` |

**Error codes used:**
- `missing_argument` — validation failures (empty `symbol_name`, `pattern`, `file_path`, `project_root`)
- `internal_error` — exception catch-all

### 3. `tests/test_symbol_tools_response_contract.py` (new)

21 tests covering:
- `test_tool_contract_version` — `TOOL_CONTRACT_VERSION == "1.0"`
- `test_ok_response_structure` / `test_error_response_structure`
- `test_make_symbol_response_structure` / `test_make_callers_response_structure`
- `TestFindSymbolContract` — success, missing arg, internal error
- `TestSearchSymbolsContract` — success, missing arg
- `TestCallersContract` — success preserves `why`/`match_type`/`confidence`/`callee_fqn`, missing arg
- `TestFileSymbolsContract` — success includes `source_file`/`repo_rel_path`, missing arg
- `TestRecentChangesContract` — success, missing project_root
- `test_no_chromadb_in_sys_modules` — confirms no ChromaDB imported

## Contract Schema (v1.0)

### Success
```python
{
    "ok": True,
    "tool_contract_version": "1.0",
    "tool": "<tool_name>",
    ...data fields...
}
```

### Error
```python
{
    "ok": False,
    "tool_contract_version": "1.0",
    "tool": "<tool_name>",
    "error": {
        "code": "missing_argument" | "internal_error",
        "message": "<human-readable>"
    }
}
```

## Test Results

```
71 passed in 3.33s
```

### Test files verified
| File | Result |
|------|--------|
| `tests/test_symbol_tools_response_contract.py` | ✅ 21 passed |
| `tests/test_mcp_response_contract.py` | ✅ all pass |
| `tests/test_code_intel_call_graph.py` | ✅ all pass |
| `tests/test_code_intel_explainability.py` | ✅ all pass |

### ChromaDB check
```
chromadb in sys.modules: False
```

## Legacy Key Preservation

| Tool | Legacy Key | Preserved |
|------|-----------|----------|
| `mempalace_find_symbol` | `symbol_name` | ✅ via `make_symbol_response` data dict |
| `mempalace_search_symbols` | `pattern` | ✅ added after `make_symbol_response` |
| `mempalace_callers` | `symbol_name`, `callers`, `count` | ✅ via `make_callers_response` |
| `mempalace_recent_changes` | `recent_changes`, `hot_spots`, `count` | ✅ via `ok_response` data |
| `mempalace_file_symbols` | All result fields | ✅ via `{**result, **path_info}` spread |

## Explainability Preservation (`mempalace_callers`)

Each caller dict retains:
- `why` — human-readable explanation (`"AST call: X() calls login() at line 12"`)
- `match_type` — `"ast_call"` or `"import_ref"`
- `confidence` — `"high"` or `"low"`
- `caller_fqn` — `"PaymentService.process"`
- `callee_fqn` — `"login"`
- `source_file` — absolute path
- `repo_rel_path` — relative to project root
- `line` — line number

## Files Modified

| File | Change |
|------|--------|
| `mempalace/server/response_contract.py` | Added `make_callers_response`, updated `make_symbol_response` with `tool` param, restored missing functions, removed dead code |
| `mempalace/server/_symbol_tools.py` | All 5 tools use contract helpers |
| `tests/test_symbol_tools_response_contract.py` | New — 21 tests |

## No ChromaDB Import

Confirmed: `chromadb` and `chrom` are not imported by `mempalace/server/_symbol_tools.py` or any transitive dependency in the symbol tool path.
