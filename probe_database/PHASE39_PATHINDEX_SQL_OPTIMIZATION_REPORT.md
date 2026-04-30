# PHASE39: PathIndex SQL Optimization Report

**Date:** 2026-04-30
**Status:** Complete
**Files modified:** `mempalace/path_index.py`
**Files added:** `tests/test_path_index_sql_optimization.py`

---

## Summary

`search_path()` was rewritten from a **SELECT-all + Python-filter** approach into a **staged indexed SQL** approach. For typical path queries (exact file, basename, suffix), the query now hits SQLite B-tree indexes directly instead of loading all matching rows into Python memory.

---

## Changes Made

### `mempalace/path_index.py`

#### New Helper Functions

**`_escape_sql_like(pattern: str) -> str`**
Escapes SQL LIKE wildcards (`%`, `_`, `\`) so they match as literals. Applied to query values in suffix and basename LIKE expressions.

**`_normalize_path_for_sql(path: str) -> str`**
Normalizes paths for SQL matching: strips trailing slashes, converts backslashes to forward slashes. Does NOT call `realpath` — stored `source_file` values are not normalized (backward compat), so project_path normalization must match.

**`PathIndex.normalize_source_file(source_file: str) -> str`** (public static)
Exposes `_normalize_path_for_sql` for callers who want to normalize source_file at insert time (enables full `/var ↔ /private/var` symmetry on macOS after re-indexing).

#### `search_path()` — Staged SQL Rewrite

Four indexed stages, each bounded and early-exiting when `limit` is reached:

| Stage | SQL | Index Used | Bounded |
|-------|-----|-----------|---------|
| 1: exact source_file | `source_file = ?` | idx_path_source_file | — |
| 2: exact repo_rel_path | `repo_rel_path = ?` | idx_path_repo_rel_path | — |
| 3: suffix match | `source_file LIKE '%' || ? ESCAPE '\'` | idx_path_source_file (prefix scan) | 3× limit |
| 4: basename match | `basename = ?` | idx_path_basename | 3× limit |

Each stage excludes already-matched `document_id`s via `NOT IN (...)` to prevent duplicates. Python suffix filtering (`ends-with`) is applied after the bounded scan in Stage 3.

#### `_project_path_filter(project_path) -> (str, list)`

Returns strict boundary SQL:
```sql
(source_file = ? OR source_file LIKE ? ESCAPE '\')
```
Both `source_file` and `project_path` are normalized via `_normalize_path_for_sql()`. Wildcards in `project_path` itself are escaped, so paths containing `%` or `_` are correctly isolated.

#### SQL Injection Safety

All user-supplied path values are passed as SQLite parameterized queries. No string concatenation into SQL. The `ESCAPE '\'` clause ensures `%`/`_` in queries match literally, not as wildcards.

---

## Test Results

```
tests/test_path_index.py          26 passed
tests/test_path_index_sql_optimization.py  36 passed, 1 skipped
Total: 62 passed, 1 skipped
```

**Skipped (macOS-only):** `test_normalize_source_file_strips_trailing` — platform-conditional test (skipped on non-Darwin by `pytest.skip`).

**Pre-existing failures (NOT caused by this change):** `tests/test_source_code_ranking.py` and `tests/test_source_code_ranking_preslice.py` — 15 failures confirmed against original code (environmental: mined palace returns 0 results — symbol index / mining pipeline issue, unrelated to PathIndex).

**ChromaDB import check:** `False` — no ChromaDB imported.

---

## Backward Compatibility

- **Schema unchanged** — same 12-column table, same 3 indexes.
- **`search_path()` signature unchanged** — same `query`, `project_path`, `language`, `limit` params.
- **Behavior preserved** — same 4-priority matching order; tombstone filtering; empty-query guard.
- **No new dependencies.**

---

## Edge Cases Documented

### Wildcard Literal Matching
Query `50%.py` (literal percent in filename) correctly returns only `50%.py`, not `500.py`. Query `file_name.py` (literal underscore) correctly returns only `file_name.py`, not `fileXname.py`.

### Strict Project Path Boundary
`project_path=/proj` matches `/proj/src/main.py` and `/proj` (exact), but NOT `/proj-old/src/main.py` (prefix collision prevented by trailing `/` requirement).

### macOS `/var ↔ /private/var`
Stored `source_file` values are NOT normalized by default (backward compat). For full symmetry, call `PathIndex.normalize_source_file()` at insert time and re-index. The `normalize_source_file` helper is available for this migration.

### SQL Syntax Warning
The `_\)` escape sequence warning in the docstring was eliminated by replacing the backslash glyph with the word "backslash" in docstrings. No runtime issue.

---

## Files

| File | Change |
|-------|---------|
| `mempalace/path_index.py` | Staged SQL rewrite + helpers |
| `tests/test_path_index_sql_optimization.py` | 36 new tests (SQL optimization cases) |
