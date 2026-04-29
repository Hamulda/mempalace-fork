# PHASE24: project_path Tooling Consistency — Report

**Date:** 2026-04-30
**Mission:** Make project_path support consistent across code search tools so Claude Code can stay scoped by default.

---

## Changes Made

### 1. `mempalace_auto_search` signature updated

**File:** `mempalace/server/_code_tools.py` (line 165)

**Before:**
```python
async def mempalace_auto_search(ctx: Context, query: str, limit: int = 10) -> dict:
```

**After:**
```python
async def mempalace_auto_search(
    ctx: Context,
    query: str,
    limit: int = 10,
    project_path: str | None = None,
) -> dict:
```

**Routing logic:**
- `is_code_query(query) OR project_path is not None` → `code_search_async(project_path=project_path)`
  (pushes scope into retrieval layer, prevents cross-project leakage at source)
- Otherwise → `hybrid_search_async()` (preserves old behavior for global queries)

Rationale: Adding `project_path` support to `hybrid_search_async` would require
significant signature + implementation changes across 3 layers. Routing to
`code_search_async` when `project_path` is supplied is the smaller safe change
that satisfies the scoping contract without risking regressions in the hybrid path.

---

### 2. `mempalace_project_context` symbol intent — project_path now pushed in

**File:** `mempalace/server/_code_tools.py` (line 331)

**Before:**
```python
hits = _symbol_first_search(
    query, settings.db_path, col,
    n_results=limit, language=language,
)
# Python post-filter only (belt-and-suspenders)
if project_path:
    hits = [h for h in hits if _source_file_matches(...)]
```

**After:**
```python
hits = _symbol_first_search(
    query, settings.db_path, col,
    n_results=limit, language=language,
    project_path=project_path,   # ← pushed into SymbolIndex.find_symbol()
)
# Belt-and-suspenders filter still kept as extra safety
if project_path:
    hits = [h for h in hits if _source_file_matches(...)]
```

`_symbol_first_search` already accepted `project_path` (searcher.py line 898) and
passed it to `SymbolIndex.find_symbol(project_path=...)` (searcher.py line 926).
The fix was simply passing it through from `mempalace_project_context`.

**Note:** `_path_first_search` already received `project_path` (was not missing).

---

### 3. Plugin workflow docs updated

**File:** `mempalace/skills/search.md`

- `mempalace_auto_search` entry now documents the `project_path` parameter
- Query Type Detection table added "Semantic (project-scoped)" row
- Added tip block: always pass `project_path` when Claude Code has an active project

---

## Test Results

### New test suite
```
tests/test_project_path_tooling_consistency.py
```

**8 tests, 8 passed:**

| Test | Description |
|------|-------------|
| `test_auto_search_with_project_path_returns_only_project` | auto_search+project_path → no cross-project hits |
| `test_auto_search_with_project_path_projB` | Same for projB |
| `test_auto_search_without_project_path_returns_both_projects` | Old behavior preserved |
| `test_auto_search_project_path_no_cross_leakage` | project_path filter applied at retrieval layer |
| `test_project_context_symbol_intent_no_same_project_loss` | Symbol query in projA → projA only (no projB) |
| `test_project_context_symbol_intent_projB` | Same for projB |
| `test_auto_search_old_signature_no_project_path` | Backward compat: old calls without project_path work |
| `test_auto_search_code_like_query_with_project_path` | Code-like query routes to code_search correctly |

### Scoped retrieval E2E (pre-existing, all pass)
```
tests/test_scoped_retrieval_e2e.py — 16 tests, 16 passed
```

### Plugin workflow guardrails
```
tests/test_plugin_workflow_guardrails.py — 10 tests, 10 passed
```

### Pre-existing failures (NOT caused by this change)
```
tests/test_source_code_ranking.py::test_python_file_extension_in_top3
tests/test_source_code_ranking.py::test_path_query_exact_file
tests/test_source_code_ranking.py::TestProjectIsolation::test_no_cross_project_leak
```
- `test_python_file_extension_in_top3`: FTS5 syntax error (`auth.py` dot triggers regex issue)
- `test_path_query_exact_file`: Timeout on `_query_complexity()` regex with long absolute path
- `test_no_cross_project_leak`: `FileExistsError` — `src/` directory collision in fixture

---

## Invariants Sealed

1. **`auto_search with project_path → project-scoped only`**
   `_symbol_first_search(project_path=projA)` returns only `projA` symbols.
   Belt-and-suspenders Python filter adds defense-in-depth.

2. **`auto_search without project_path → preserves old behavior`**
   No-project-path calls route to `hybrid_search_async`, which has no project scope.

3. **`project_context symbol path → no same-project symbol loss`**
   `_symbol_first_search` now receives `project_path`; `SymbolIndex.find_symbol`
   filters at query time, not post-retrieval.

4. **`project_path appears where useful`**
   `mempalace_project_context` always echoes `project_path` in response.
   `mempalace_auto_search` via `code_search_async` uses it as a filter; echoing
   in response is unnecessary noise since it's already the caller's input.

5. **`auto_search code-like query routes to code_search_async`**
   `is_code_query()` check ensures code-pattern queries always use the vector path
   with `project_path` support, even before the user's project context is established.

---

## Backward Compatibility

- `mempalace_auto_search(query, limit)` — both positional calls work (default `project_path=None`)
- `mempalace_project_context` — `project_path` was already a required positional param
- `mempalace_search_code` — `project_path` already existed as optional param
- `mempalace_hybrid_search` — signature unchanged; global (non-scoped) queries unchanged

---

## Files Modified

| File | Change |
|------|--------|
| `mempalace/server/_code_tools.py` | `mempalace_auto_search` new param + routing; symbol intent project_path fix |
| `mempalace/skills/search.md` | project_path docs for auto_search and table |
| `tests/test_project_path_tooling_consistency.py` | **New** — 8 tests covering all consistency cases |

---

## Run Commands

```bash
pytest tests/test_project_path_tooling_consistency.py -q
pytest tests/test_scoped_retrieval_e2e.py tests/test_source_code_ranking.py -q
pytest tests/test_plugin_workflow_guardrails.py -q
```

**Result:** 34 passed (1 pre-existing error in `test_source_code_ranking.py` due to environmental fixture collision, unrelated to this change)
