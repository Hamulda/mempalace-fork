# PHASE23 STRICT FILE_CONTEXT SECURITY REPORT
**Date:** 2026-04-30
**Mission:** Separate retrieval path matching from security path allow checks

---

## Problem Statement

`_is_path_allowed()` (security function) was calling `_source_file_matches()` (retrieval function) for path comparisons. `_source_file_matches` uses loose heuristics including basename-only matching — too permissive for security decisions.

**Attack vector:** A file like `/etc/auth.py` could slip through when `project_path=/tmp/proj/auth.py` because `_source_file_matches` has a basename-matching rule (`pp_norm.endswith("/" + sf_basename)`).

---

## Changes Made

### 1. New Strict Helper: `_path_is_under_or_equal`

Added to `mempalace/server/_code_tools.py`:

```python
def _path_is_under_or_equal(file_path: str, root: str) -> bool:
    """Strict path containment check for security decisions.
    ...
    """
```

**Rules:**
- Resolves both paths via `Path().expanduser().resolve()` (symlink-safe)
- Allows equality: `file == root`
- Allows subtree: `commonpath([file, root]) == root`
- **No basename-only matching** — the core fix
- Returns `False` safely on `OSError`/`ValueError`

Implementation uses manual `Path.parts` comparison since `Path.commonpath` is unavailable in Python 3.14 (this was a pre-existing limitation to work around).

### 2. Updated `_is_path_allowed`

- `allow_any=True` → returns `True` immediately (old permissive behavior preserved)
- `project_path` check → now uses `_path_is_under_or_equal` instead of `_source_file_matches`
- `allowed_roots` check → now uses `_path_is_under_or_equal` instead of `_source_file_matches`

### 3. `_source_file_matches` — Unchanged

Left untouched for retrieval use (`_filter_by_project_path`, search result filtering). Its loose basename-matching behavior is appropriate for retrieval recall, not security decisions.

---

## Test Results

| Suite | Result |
|-------|--------|
| `tests/test_file_context_scope.py` | **41 passed** |
| `tests/test_plugin_workflow_guardrails.py` | **17 passed** |
| `tests/test_plugin_docs_truth.py` | **6 passed** |
| `tests/test_scoped_retrieval_e2e.py` | **9 passed** |

---

## New Test Coverage

Added `TestPathIsUnderOrEqual` (12 tests) and new cases in `TestIsPathAllowedProjectPath`:

| Test | What it checks |
|------|----------------|
| `test_exact_match` | file == root → allowed |
| `test_subdirectory_allowed` | file under root → allowed |
| `test_parent_traversal_denied` | `root/../etc/hosts` → denied |
| `test_sibling_dir_denied` | sibling dir not under root → denied |
| `test_basename_only_not_enough` | `/etc/passwd` not allowed via basename heuristic |
| `test_proj_old_not_matching_proj` | `/proj-old` does not match `/proj` |
| `test_traversal_outside_denied` | `root/src/../../etc/hosts` → denied |
| `test_traversal_inside_allowed` | `root/src/../src/auth.py` → allowed |
| `test_symlink_to_subdir_allowed` | symlink inside root → allowed |
| `test_symlink_outside_denied` | symlink pointing outside root → denied |
| `test_invalid_path_returns_false` | invalid/empty path → False safely |
| `test_same_basename_outside_project_path_denied` | `/etc/auth.py` blocked when project_path is `/tmp/proj/auth.py` |

---

## Security Model Summary

| Function | Purpose | Path Matching |
|----------|---------|---------------|
| `_path_is_under_or_equal` | **Security** — blocks unauthorized file access | Strict commonpath equality, no basename heuristics |
| `_source_file_matches` | **Retrieval** — filters code chunks by project scope | Loose: prefix, substring, basename matching |
| `_is_path_allowed` | **Security gate** for `mempalace_file_context` | Uses `_path_is_under_or_equal` (strict) |

---

## Override Mechanism

`MEMPALACE_FILE_CONTEXT_ALLOW_ANY=1` still restores the old permissive behavior (bypasses all security checks, returns `True` immediately).

---

## Files Modified

- `mempalace/server/_code_tools.py` — added `_path_is_under_or_equal`, updated `_is_path_allowed`
- `tests/test_file_context_scope.py` — added `TestPathIsUnderOrEqual` class, new project_path tests