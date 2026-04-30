# PHASE40 — Startup Context Provider Truth Report

**Date:** 2026-04-30
**Mission:** Make `mempalace_startup_context` report truthful embedding/provider/storage state without relying on a non-existent or optional HTTP endpoint.

---

## 1. Audit Findings

### `wakeup_context.py` — `build_startup_context` (original)

| Issue | Detail |
|-------|--------|
| HTTP 8766 probe | Used `urllib.request.urlopen("http://127.0.0.1:8766/probe")` — embedding daemon does NOT expose HTTP, uses Unix socket |
| No stored metadata | Did not read `embedding_meta.json` from `embed_metadata.py` |
| No current probe | Could not distinguish "daemon down" vs "never set" |
| startswith claim filter | `c.get("target_id", "").startswith(resolved_project)` — `/proj-old` matched by `/proj` |
| No index stats | path_index/fts5/symbol counts not included |

### `embed_metadata.py` — Canonical Sources

| Source | What it provides |
|--------|-----------------|
| `load_meta(palace_path)` | Returns dict from `embedding_meta.json` (provider, model_id, dims) or None |
| `_probe_daemon_socket()` | Unix socket probe → `(provider, model_id, dims)` or None — no HTTP, no model load |
| `detect_current_provider()` | Tiered detection: socket → import → fallback → mock |

### `embed_daemon.py` — Protocol

- Daemon listens on Unix socket (`~/.mempalace/embed.sock`), not TCP
- Probe request: `{"probe": True, "texts": []}` → response: `{"provider": ..., "model_id": ..., "dims": 256}`
- **HTTP port 8766 does not exist** — the original code was probing a non-existent endpoint

---

## 2. Changes Made

### `mempalace/wakeup_context.py`

**Added `_path_boundary_contains(child_path, parent_path)`** — strict path-boundary helper:
```
/proj        → matches /proj, /proj/foo, /proj/a/b/c.py
/proj        → does NOT match /proj-old, /some/proj-old, /projx
/proj-old    → not matched by /proj (no prefix ambiguity)
```

**Added `_probe_embed_daemon_socket()`** — lightweight wrapper over `embed_metadata._probe_daemon_socket()`:
- Returns `(provider, model_id, dims)` or `None`
- No HTTP, no model loading, Unix socket only

**Added `_load_stored_embedding_meta(palace_path)`** — reads `embedding_meta.json` via `embed_metadata.load_meta()`.

**Added `_get_index_counts(palace_path)`** — O(1) counts:
- `path_index_count` → `PathIndex.get().count()`
- `fts5_count` → `KeywordIndex.get().count()` (LexicalIndex is actually `KeywordIndex`)
- `symbol_count` → `SymbolIndex.get().stats()["total_symbols"]`

**Replaced HTTP 8766 probe** with socket probe + stored metadata fallback.

**Replaced `startswith` claim filter** with `_path_boundary_contains`.

### New return fields

| Field | Source |
|-------|--------|
| `embedding_stored_provider` | `embedding_meta.json` → provider |
| `embedding_stored_model_id` | `embedding_meta.json` → model_id |
| `embedding_stored_dims` | `embedding_meta.json` → dims |
| `embedding_current_provider` | daemon socket probe → provider |
| `embedding_current_model_id` | daemon socket probe → model_id |
| `embedding_current_dims` | daemon socket probe → dims |
| `embedding_drift_detected` | `true` if stored≠current, `"unknown"` if only one available, `false` if identical |
| `embedding_provider` | legacy compat: current > stored > "unknown" |
| `path_index_count` | PathIndex.count() or None |
| `fts5_count` | KeywordIndex.count() or None |
| `symbol_count` | SymbolIndex.stats()["total_symbols"] or None |

### Drift detection logic

```
stored: mlx, model_x, 256   current: coreml, model_y, 256  → drift=true
stored: mlx, model_x, 256   current: None                   → drift="unknown"
stored: None                current: mlx                    → drift="unknown"
stored: mlx, model_x        current: mlx, model_x          → drift=false
```

---

## 3. Test Coverage

**`tests/test_startup_context_provider_truth.py`** — 19 test cases:

| Test | What it verifies |
|------|-----------------|
| `test_no_http_probe_imported` | Source code contains no "127.0.0.1:8766" |
| `test_stored_meta_when_no_daemon` | No daemon → stored provider from JSON |
| `test_daemon_probe_when_running` | Daemon probe succeeds → current provider reported |
| `test_drift_detected_when_stored_differs` | Stored≠current → `drift=true` |
| `test_drift_false_when_stored_matches_current` | Stored==current → `drift=false` |
| `test_no_daemon_no_stored_unknown` | Neither available → `"unknown"` |
| `test_new_embedding_fields_present` | All 9 new fields present in result |
| `test_index_counts_are_null_or_int` | Counts are int\|None, not errors |
| `test_proj_does_not_match_proj_old` | `/proj-old` not matched by `/proj` |
| `test_proj_matches_exact_and_children` | `/proj` matches `/proj` and `/proj/foo` |
| `test_proj_with_slash_matches_children` | Trailing slash handled correctly |
| `test_nested_projects_do_not_match` | `/proj-a` not matched by `/proj` |
| `test_claims_filtered_by_boundary_in_context` | Only `/proj` claims returned for project `/proj` |
| `test_no_mlx_load_on_import` | No heavy modules loaded on import |
| `test_socket_probe_does_not_load_model` | Socket probe called exactly once, no model |

---

## 4. Test Results

```
pytest tests/test_startup_context.py tests/test_startup_context_provider_truth.py -q
............................                                      [100%]
30 passed in 2.15s

pytest tests/test_plugin_workflow_guardrails.py tests/test_plugin_docs_truth.py -q
.......................                                          [100%]
23 passed in 1.62s

python -c "import sys; import mempalace; print('chromadb' in sys.modules)"
False
```

**1 pre-existing failure** (`test_python_version_present`): expects Python 3.14.x, environment is 3.12.12 — unrelated to these changes.

---

## 5. Invariants Maintained

| Invariant | Status |
|-----------|--------|
| No ChromaDB imported | ✅ `chromadb`/`chroma` not in `sys.modules` |
| Python 3.14 only | ⚠️ Environment is 3.12 (pre-existing, unrelated) |
| No heavy model load | ✅ Socket probe is send/recv only |
| No new dependencies | ✅ Uses only existing imports |
| Startup context compact | ✅ Only 1 socket probe call |
| Path boundary strict | ✅ `/proj-old` never matches `/proj` |
| Chroma blocked | ✅ `backend="lance"` only |

---

## 6. Files Modified

| File | Change |
|------|--------|
| `mempalace/wakeup_context.py` | +`_path_boundary_contains`, +`_probe_embed_daemon_socket`, +`_load_stored_embedding_meta`, +`_get_index_counts`; replaced HTTP probe with socket+stored fallback; replaced startswith with boundary filter; added new embedding truth fields and index counts |
| `tests/test_startup_context_provider_truth.py` | New file — 19 test cases |

---

## 7. Behavioral Summary

```
build_startup_context(session_id, project_path, palace_path, limit=8)
  │
  ├─ server_health:     HTTP /health → unchanged
  │
  ├─ embedding state:
  │   ├─ stored (from embedding_meta.json)
  │   │     provider / model_id / dims
  │   ├─ current (from daemon Unix socket probe)
  │   │     provider / model_id / dims
  │   └─ drift:  true | false | "unknown"
  │
  ├─ claim filtering:  strict path boundary (not startswith)
  │
  ├─ index counts:    path_index / fts5 / symbol (None if unavailable)
  │
  └─ legacy compat:    embedding_provider = current|stored|"unknown"
```
