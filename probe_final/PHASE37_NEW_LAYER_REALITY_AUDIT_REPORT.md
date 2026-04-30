# PHASE37_NEW_LAYER_REALITY_AUDIT_REPORT
**Date:** 2026-04-30
**Audit scope:** Phases 32–36 new layers: PathIndex, MCP response contract, embedding provider metadata lock, AST call graph, startup context pack
**Test status:** 99/99 pass (5 test files)
**Verification:** All findings backed by grep/line citations — no assumptions

---

## VERIFICATION SUMMARY

| Check | Status | Evidence |
|-------|--------|---------|
| PathIndex used by _path_metadata/_path_first_search | ✅ PASS | searcher.py:1069,1091–1099 |
| PathIndex sync on Lance upsert/add/delete | ✅ PASS | lance.py:2074–2094 (_sync_path_index_upsert), 2105–2109 (_sync_path_index_delete) |
| MCP tools return tool_contract_version | ✅ PASS | All 4 contracts include version in ok + error responses |
| normalize_hit canonical fields overwritable | ❌ **BUG** | response_contract.py:135 — `**hit` AFTER canonical fields |
| startup_context uses real probe path | ❌ **BUG** | wakeup_context.py:349 — HTTP to port 8766 (not Unix socket) |
| call graph: ast_call/import_ref/text_ref | ❌ **PARTIAL** | text_ref never appears anywhere in symbol_index.py |
| embedding provider lock not loading fallback on every search | ✅ PASS | validate_write called only at write-time (lance.py:1802) |
| No Chroma in sys.modules | ✅ PASS | python -c "import sys; import mempalace; print('chromadb' in sys.modules)" → False |
| embed_daemon /probe endpoint (Unix socket, not HTTP) | ⚠️ INCONISTENT | embed_daemon.py:289-299 — socket probe exists at `request.get("probe")` |

---

## BUGS FOUND

### BUG-1: `normalize_hit` spread operator overwrites canonical fields
**Severity:** HIGH
**File:** `mempalace/server/response_contract.py:135`
**Category:** Contract violation — canonical field corruption

**Description:**
`normalize_hit` builds canonical fields FIRST (lines 109–133), then spreads `**hit` AFTER (line 135). Python dict spread replaces existing keys, so if `hit` contains any canonical field name, it overwrites the computed canonical value.

**Concrete failure scenario:**
```python
# Malicious or buggy hit with "text" key:
hit = {"id": "drawer_123", "text": "fake payload from vector db"}
normalize_hit(hit)
# Line 113: "text": text  → text = "fake payload from vector db"
# Line 135: **hit        → {"id": ..., "text": "fake payload from vector db", ...}
#           BUT "text" key in **hit OVERWRITES the canonical text set above
# Result: "text" in return dict is "fake payload" not the real document content
```

**Affected canonical fields at risk:** id, text, doc, source_file, repo_rel_path, language, line_start, line_end, symbol_name, symbol_fqn, chunk_kind, score, retrieval_path, project_path_applied

**Fix (minimal):** Move `**hit` BEFORE canonical field assignments — it should come first so canonical values take precedence:

```python
return {
    **hit,  # spread first — preserves debug info without overwriting canonicals
    # Canonical identity (override hit values with correct ones)
    "id": hit.get("id") or hit.get("drawer_id") or "",
    "text": hit.get("text") or hit.get("doc") or hit.get("content", ""),
    "doc": hit.get("text") or hit.get("doc") or hit.get("content", ""),
    # ... rest of canonical fields ...
}
```

**Test gap:** Current tests pass because test fixtures don't include raw hit keys that conflict with canonical field names. A regression test should use a hit dict with `text`, `score`, or `source_file` keys to verify canonical values are preserved.

---

### BUG-2: Dead code — duplicate `return sf` in `_compute_repo_rel`
**Severity:** LOW (dead code, no runtime impact)
**File:** `mempalace/server/response_contract.py:167`
**Category:** Code quality / unreachable code

**Description:**
`_compute_repo_rel` has two consecutive `return sf` statements at lines 165 and 167. The second is unreachable. Likely from a copy-paste error during refactoring.

**Evidence:**
```
165:     return sf
166:
167:     return sf    # ← unreachable
```

**Fix:** Remove line 167. No functional impact.

---

### BUG-3: `text_ref` match_type never appears in call graph
**Severity:** MEDIUM
**File:** `mempalace/symbol_index.py`
**Category:** Incomplete contract — call graph result types

**Description:**
The contract specifies three distinct match types for call graph results:
- `ast_call` — from AST-based call graph (get_callers_ast, line 874)
- `import_ref` — from import-based heuristic (get_callers, line 611)
- `text_ref` — from text/reference search (NEVER implemented)

`get_callers_ast` returns `match_type: "ast_call"` (line 901). `get_callers` returns `import_type: "direct"/"module"` but does NOT set `match_type` at all (lines 664–702). `text_ref` does not appear anywhere in the symbol_index or _symbol_tools codebase.

**Impact:** MCP tool `mempalace_callers` can only ever return `ast_call` or `import_ref` (with `confidence: "low"` set manually in _symbol_tools.py:106). There is no `text_ref` path.

**Note:** The `_symbol_tools.py:100–107` correctly applies the two-tier approach (AST first, then heuristic fallback). The gap is that:
1. `get_callers` results don't include `match_type` field
2. `text_ref` is not implemented

**Minimal fix for correctness:** `get_callers` should add `match_type: "import_ref"` to its results:

```python
# symbol_index.py get_callers, after lines 664 and 697:
callers.append({
    ...
    "match_type": "import_ref",  # ← missing
})
```

The `text_ref` type is a separate feature — can be deferred to PHASE38 if text-based caller search is needed.

---

### BUG-4: `build_startup_context` probes wrong address for embed daemon
**Severity:** MEDIUM
**File:** `mempalace/wakeup_context.py:348–362`
**Category:** Silent failure — wrong probe target

**Description:**
`build_startup_context` probes `http://127.0.0.1:8766/probe` for embedding provider info. This is an HTTP URL. However, the embed daemon (`embed_daemon.py`) only provides a Unix domain socket probe (`.sock` at `~/.mempalace/embed.sock`) — NOT an HTTP server on port 8766.

The probe at lines 348–362 will always fail (connection refused), meaning `embedding_provider` always returns `"unknown"` in startup context. The failure is silently suppressed (bare `except Exception: pass`).

**Why tests pass:** `test_startup_context.py` mocks the HTTP probe responses, so it doesn't catch this mismatch.

**Root cause:** The `/probe` path was likely copy-pasted from the MCP server HTTP health probe (which correctly hits `http://127.0.0.1:8765/health`).

**Contrast with `embed_metadata.py`:** `embed_metadata._probe_daemon_socket()` (line 168) correctly uses Unix socket:
```python
s.connect(sock_path)  # Unix domain socket, not HTTP
msg = json.dumps({"probe": True, "texts": []}).encode()
```

**Fix (minimal):** `build_startup_context` should use the Unix socket probe instead of HTTP:
```python
# Option A: import from embed_metadata (consistent with lance.py usage)
from ..embed_metadata import _probe_daemon_socket
provider_info = _probe_daemon_socket()

# Option B: direct socket probe inline (matches embed_metadata._probe_daemon_socket)
```

---

## CONFIRMED WORKING (No Action Needed)

### PathIndex wiring ✅
- `searcher.py:_path_metadata_search` (L1069, 1091): imports and calls `PathIndex.get(palace_path)`
- `lance.py:_sync_path_index_upsert` (L2074): called after upsert/add — wires `PathIndex.upsert_rows`
- `lance.py:_sync_path_index_delete` (L2105): called after delete — wires `PathIndex.delete_rows`
- No PathIndex sync for tombstone (mark_tombstoned) — but this is by design, tombstone is Lance-layer only

### MCP response contract ✅
- `TOOL_CONTRACT_VERSION = "1.0"` defined at line 15
- `ok_response` (L20): includes version in every success response
- `error_response` (L36): includes version in every error response
- `no_palace_response` (L60): includes version
- All tool response builders (`make_search_response`, `make_symbol_response`, `make_file_context_response`, `make_project_context_response`, `make_status_response`) use `ok_response` → all include version

### Embedding provider lock ✅
- `embed_metadata.validate_write` (lance.py:1802–1805): called at write-time, not on every search
- `embed_metadata.ensure_meta` (lance.py:1906–1910): called after first successful write — idempotent
- No lock on search hot path — confirmed clean

### `get_callers_ast` correct ✅
- Returns `match_type: "ast_call"` at symbol_index.py:901
- `mempalace_callers` in `_symbol_tools.py:100–107` correctly tries AST first, falls back to heuristic

### Start/embed daemon probe correctly set ✅
- `embed_daemon.py:60–61`: `_provider_for_probe`, `_model_id_for_probe` globals set at daemon startup
- `embed_daemon.py:289–299`: socket probe handler returns provider/model/dims on `{"probe": true}` request
- `embed_daemon.py:604–605`: probe vars initialized via `_detect_embed_provider()`

---

## MINIMAL FOLLOW-UP PHASES

| Phase | Priority | Action | Files |
|-------|----------|--------|-------|
| PHASE37-BUGFIX-1 | **HIGH** | Fix `normalize_hit` `**hit` order — canonical fields must win | response_contract.py:109–136 |
| PHASE37-BUGFIX-2 | **LOW** | Remove dead `return sf` at line 167 | response_contract.py:167 |
| PHASE37-BUGFIX-3 | **MED** | Add `match_type: "import_ref"` to `get_callers` results + add `text_ref` placeholder or document as unimplemented | symbol_index.py:664,697; _symbol_tools.py:100–107 |
| PHASE37-BUGFIX-4 | **MED** | Fix `build_startup_context` to probe Unix socket instead of HTTP port 8766 | wakeup_context.py:344–363 |
| PHASE37-TEST-1 | **HIGH** | Add regression test for normalize_hit with conflicting hit keys | tests/test_mcp_response_contract.py |

---

## TEST RESULTS

```
uv run --with pytest tests/test_path_index.py tests/test_mcp_response_contract.py \
  tests/test_embedding_provider_lock.py tests/test_code_intel_call_graph.py \
  tests/test_startup_context.py -q

99 passed in 3.76s
```

Note: All tests pass because fixtures don't exercise the BUG-1 scenario (hit dict with conflicting canonical field keys).

---

*Audit complete. No modifications made — findings only.*
