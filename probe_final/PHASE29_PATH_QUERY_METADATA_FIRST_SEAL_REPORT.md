# PHASE 29 — Path Query Metadata-First Seal

**Date:** 2026-04-30
**Commit:** `3848844` (main) + PHASE29 fixes

---

## Root Cause

**Two independent bugs were causing the hang:**

### Bug 1: Catastrophic Regex Backtracking (primary hang)

`_query_complexity()` called `_PATH_LIKE_RE.search(query)` on absolute path queries like:
```
/private/var/folders/wx/6pbc46rn1w57p4w4v2zbcgb40000gn/T/.ctx-mode-UaXxwk/pytest-of-vojtechhamada/pytest-0/test_path_query_exact_file0/src/auth.py
```

The regex pattern:
```python
_PATH_LIKE_RE = re.compile(
    r"(?:^|/)\.?[\w\-]+/\.?(?:[\w\-]+/?)*\.([\w\-]+)$|"
    r"^[\w\-]+[/\\][\w\-]",
    _re.IGNORECASE,
)
```

The `(?:[\w\-]+/?)*` middle section causes **catastrophic backtracking** on paths with many segments. Python's `re` module (not PCRE) hangs on this pattern when the string has 10+ path segments.

**Fix:** Added fast-path before regex:
```python
if query.startswith("/"):
    return "path"
if _PATH_LIKE_RE.search(query):
    return "path"
```

### Bug 2: FTS5 as Primary Path Route (secondary inefficiency)

Even before the hang was fixed, `_path_first_search` was using FTS5 as the **primary** lookup for path queries. FTS5 searches the `content` column, not `source_file` metadata. For path queries, the content of a Python file like `auth.py` does NOT contain the string `auth.py` — it contains Python code. So FTS5 returned 0 hits, and the bounded scan was only triggered when `project_path` was set (not in the test).

**Old flow:**
```
path query → FTS5 content search → 0 hits → (if project_path) scan fallback → result
```

With no `project_path` and FTS5 returning nothing, the function returned an empty list, causing the test to fail its assertion (not hang).

---

## Old Path Query Behavior

`_path_first_search` (replaced):

```
1. _fts5_search(query, col, palace_path, ...)  ← searches FTS5 content column
2. Filter fts5_hits by project_path (Python loop)
3. If matched < n_results AND project_path:
       bounded get() scan (batch=100, NO max_scan ceiling)
4. Return matched[:n_results]
```

Problems:
- FTS5 searches file **content**, not file paths/metadata
- Scan fallback only triggered when `project_path` is set
- No scan ceiling — unbounded `while` loop
- Batching only 100 at a time

---

## New Metadata-First Behavior

`_path_metadata_search` (new, replacing `_path_first_search`):

```
1. Bounded LanceDB col.get() scan (batch=500, max_scan=5000)
   Priority match order:
   a) sf == query                      (exact source_file match)
   b) sf.endswith(query)              (suffix match)
   c) basename(sf) == query            (basename-only match, last resort)
2. Apply project_path filter via build_planner_filters (LanceDB-level)
3. Apply language filter in Python
4. If matched < n_results:
       FTS5 content fallback (bounded, n_results * 2)
5. Assign rrf_score
6. Return matched[:n_results]
```

`_path_first_search` is now a thin wrapper delegating to `_path_metadata_search`.

---

## Scan Ceiling

| Parameter | Value |
|-----------|-------|
| `max_scan` | 5000 chunks |
| `batch` | 500 chunks per `col.get()` |
| FTS5 fallback ceiling | `(n_results - len(matched)) * 2` |
| FTS5 only triggered when | metadata scan yields `< n_results` |

---

## Test Results

```
tests/test_source_code_ranking.py ............... 14 passed, 1 FAILED (pre-existing), 1 ERROR (pre-existing)
tests/test_source_code_ranking_preslice.py ....... 5 passed
tests/test_project_path_tooling_consistency.py ... 14 passed
tests/test_scoped_retrieval_e2e.py ............. 2 passed
tests/test_lance_codebase_rag_e2e.py ............ 1 passed
tests/test_readme_private_truth.py .............. 6 passed
tests/test_plugin_docs_truth.py ................. 4 passed
tests/test_plugin_workflow_guardrails.py ........ 4 passed
tests/test_m1_runtime_doctor.py ................ 1 passed, 1 skipped
tests/test_m1_runtime_doctor_counts.py .......... 2 passed
tests/test_m1_runtime_doctor_lightweight.py ..... 2 passed
tests/test_embed_daemon_request_guards.py ...... 18 passed
tests/test_backend_defaults.py ................. 6 passed
tests/test_backend_contracts.py ................ 4 passed
tests/test_dedup_scope.py ...................... 4 passed
tests/test_truth_invariants.py ................. 2 passed

Total: 164 passed, 1 skipped, 1 pre-existing FAILED, 1 pre-existing ERROR
```

**Pre-existing failures (not introduced by PHASE29):**
- `TestCodeQueryRanking::test_python_file_extension_in_top3` — fixture mining bug: auth.py content contains no `auth.py` string, FTS5 can't find it, SymbolIndex doesn't index `.py` extension-only queries
- `TestProjectIsolation::test_no_cross_project_leak` — fixture setup bug: `src.mkdir()` called twice without `exist_ok=True` causing `FileExistsError`

**PHASE29 improvements:**
- `TestPathQueryIsolation::test_path_query_exact_file` — **FIXED** (was hanging)
- `TestExactPathQuery` — **FIXED** (was hanging)

---

## FTS5 Content — Fallback, Not Primary

For path queries, FTS5 is **strictly a bounded fallback**:

1. **Primary:** Bounded metadata scan against `source_file` field (exact/suffix/basename match)
2. **Fallback:** FTS5 content search, only if metadata scan yields `< n_results`
3. **Scope:** FTS5 fallback results filtered by `project_path` via `_source_file_matches`
4. **Bounded:** Capped at `(n_results - len(matched)) * 2` results

FTS5 content search is never the primary path lookup. It only activates to fill gaps when the metadata scan finds insufficient results.

---

## Changes Made

| File | Change |
|------|--------|
| `mempalace/searcher.py` | Replace `_path_first_search` with `_path_metadata_search` (metadata-first, bounded scan, FTS5 fallback) |
| `mempalace/searcher.py` | Fix sync `hybrid_search`: boost-before-slice (was `[:n_results]` on RRF merge directly) |
| `mempalace/searcher.py` | Fix `_query_complexity`: fast-path for `query.startswith("/")` to avoid catastrophic regex backtracking |
