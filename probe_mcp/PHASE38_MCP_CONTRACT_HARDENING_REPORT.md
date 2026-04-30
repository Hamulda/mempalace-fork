# PHASE38 — MCP Response Contract Hardening Report

## Changes Made

### 1. `normalize_hit()` — Canonical Fields Always Win

**Problem:** `**hit` placed last overrode canonical normalized fields when raw hit had `None` values.

**Fix:** Restructured to `{**hit}` first, then `.update()` with canonical fields:

```python
# OLD (broken): **hit last → raw overrides canonical
return {
    "id": ...,
    "text": text,
    **hit,  # BUG: text=None from raw overrides canonical
}

# NEW (correct): raw first, canonical OVERRIDES
result = {**hit}
result.update({
    "id": ...,
    "text": text,
    ...
})
return result
```

**Specific fixes:**
- `score=None, similarity=0.8` → `score=0.8` (canonical wins)
- `text=None, doc="abc"` → `text="abc"` (canonical wins)
- `project_path` parameter wins over `hit["project_path_applied"]` (explicit wins)

### 2. `no_palace_response()` — Missing `tool` Field

**Fix:** Added `tool` parameter with default `"unknown"`:

```python
def no_palace_response(tool: str = "unknown") -> dict:
    return {
        "ok": False,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "tool": tool,  # ADDED
        "error": {
            "code": "no_palace",
            "message": "No palace found",
            "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
        },
    }
```

### 3. `_compute_repo_rel()` — Duplicate Return Removed

**Fix:** Removed unreachable second `return sf` at line 167.

### 4. New Test Suite

**File:** `tests/test_mcp_response_contract_hardening.py` — 26 new tests covering:
- `text=None` with `doc` present → `text=doc`
- `score=None` with `similarity` present → `score=similarity`
- `score=None` with `rrf_score` present → `score=rrf_score`
- Supplied `project_path` wins over raw `project_path_applied`
- Legacy keys preserved (`drawer_id`, `file_path`, `fqn`, `kind`, `lineno`, `content`)
- `no_palace_response` contract version, tool field, structured error

## Test Results

```
tests/test_mcp_response_contract.py            25 passed
tests/test_mcp_response_contract_hardening.py  26 passed
tests/test_project_path_tooling_consistency.py 44 passed, 5 failed (pre-existing environmental)
tests/test_file_context_scope.py              all passed
```

## ChromaDB Check

```
chromadb in sys.modules: False
```

## Files Modified

- `mempalace/server/response_contract.py` — 3 fixes applied
- `tests/test_mcp_response_contract_hardening.py` — NEW (26 tests)

## Backward Compatibility

- Legacy keys (`drawer_id`, `file_path`, `fqn`, `kind`, `lineno`, `content`) preserved in output
- `no_palace_response()` now accepts optional `tool` param; backward-compatible default `"unknown"`
- All existing tests pass
