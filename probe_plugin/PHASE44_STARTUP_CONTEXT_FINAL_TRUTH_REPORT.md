# PHASE44_STARTUP_CONTEXT_FINAL_TRUTH_REPORT.md

**Date:** 2026-05-01
**Mission:** Make `mempalace_startup_context` a truthful compact dashboard for Claude Code.
**Files touched:** `mempalace/wakeup_context.py`, `mempalace/server/_session_tools.py`, `tests/test_startup_context_provider_truth.py`

---

## 1. HTTP 8766 Probe — VERIFIED ABSENT

**Status: ✅ ALREADY REMOVED (prior sprint)**

```bash
$ grep -n '8766' mempalace/wakeup_context.py
# (no output)
```

`build_startup_context` uses:
- `embed_metadata.load_meta(palace_path)` — reads `embedding_meta.json`
- `_probe_embed_daemon_socket()` — Unix socket probe via `embed_metadata._probe_daemon_socket()`
- No HTTP, no port 8766

---

## 2. Embedding State Fields — VERIFIED COMPLETE

All required fields present in `build_startup_context` output:

| Field | Source | Status |
|-------|--------|--------|
| `embedding_stored_provider` | `embedding_meta.json` → provider | ✅ |
| `embedding_stored_model_id` | `embedding_meta.json` → model_id | ✅ |
| `embedding_stored_dims` | `embedding_meta.json` → dims | ✅ |
| `embedding_current_provider` | daemon socket probe → provider | ✅ |
| `embedding_current_model_id` | daemon socket probe → model_id | ✅ |
| `embedding_current_dims` | daemon socket probe → dims | ✅ |
| `embedding_drift_detected` | `stored ≠ current → true/false/"unknown"` | ✅ |

---

## 3. Index Health Fields — VERIFIED

Cheap O(1) counts via `_get_index_counts(palace_path)`:

| Field | Source | Notes |
|-------|--------|-------|
| `path_index_count` | `PathIndex.get().count()` | ✅ |
| `fts5_count` | `KeywordIndex.get().count()` | ✅ |
| `symbol_count` | `SymbolIndex.stats()["total_symbols"]` | ✅ |
| `collection_count` | **EXCLUDED** | `list_tables` is O(n) — not cheap |

---

## 4. Path Boundary Claim Filtering — VERIFIED

`_path_boundary_contains(child, parent)` helper at `wakeup_context.py:286`:
- `/proj` matches `/proj`, `/proj/foo`, `/proj/a/b/c.py`
- `/proj` does NOT match `/proj-old`, `/some/proj-old`, `/projx`

```python
def _path_boundary_contains(child_path: str, parent_path: str) -> bool:
    cp = child_path.rstrip("/")
    pp = parent_path.rstrip("/")
    if cp == pp:
        return True
    return cp.startswith(pp + "/")
```

Used in claim filtering at line 487:
```python
all_claims = [
    c for c in all_claims
    if _path_boundary_contains(c.get("target_id", ""), resolved_project)
]
```

---

## 5. MCP Response Contract — ADDED

`mempalace_startup_context` MCP tool (`_session_tools.py:666`) now wraps result in:

```python
return {
    "ok": True,
    "tool_contract_version": "1.0",
    "tool": "mempalace_startup_context",
    **result,
}
```

---

## 6. Test Results

```
pytest tests/test_startup_context.py tests/test_startup_context_provider_truth.py -q
..............................                                  [100%]
31 passed in 2.53s

pytest tests/test_plugin_docs_truth.py tests/test_plugin_workflow_guardrails.py -q
.......................                                          [100%]
23 passed in 1.61s

python -c "import sys; import mempalace; print('chromadb' in sys.modules)"
False
```

**1 pre-existing failure** (`test_python_version_present`): expects Python 3.14.x, environment is 3.12.12 — unrelated to these changes, environment version mismatch.

---

## 7. Invariants

| Invariant | Status |
|-----------|--------|
| No HTTP 8766 probe | ✅ Already removed prior sprint |
| No ChromaDB imported | ✅ `chromadb`/`chroma` not in `sys.modules` |
| No heavy model load | ✅ Socket probe is send/recv only |
| No new dependencies | ✅ Only existing imports |
| Startup context compact | ✅ Only 1 socket probe call |
| Path boundary strict | ✅ `/proj-old` never matches `/proj` |
| MCP contract present | ✅ `ok: True`, `tool_contract_version: "1.0"`, `tool: "mempalace_startup_context"` |
| `collection_count` excluded | ✅ O(n) — not cheap |

---

## 8. Changes Summary

| File | Change |
|------|--------|
| `mempalace/server/_session_tools.py` | Added MCP response contract wrapper to `mempalace_startup_context` |
| `mempalace/wakeup_context.py` | No code changes needed — all features already present from prior sprint |
| `tests/test_startup_context_provider_truth.py` | Removed `collection_count` from expected fields + added assertion it is absent |

---

## 9. `/proj-old` Not Included Under `/proj` — VERIFIED

```python
assert not _path_boundary_contains("/proj-old", "/proj")          # ✅
assert not _path_boundary_contains("/proj-old/file.py", "/proj")  # ✅
assert not _path_boundary_contains("/some/proj-old", "/proj")     # ✅
assert     _path_boundary_contains("/proj/foo", "/proj")         # ✅
assert     _path_boundary_contains("/proj", "/proj")               # ✅
```