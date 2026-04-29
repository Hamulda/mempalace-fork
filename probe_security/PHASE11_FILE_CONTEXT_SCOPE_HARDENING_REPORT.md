# PHASE 11 — File Context Scope Hardening Report

**Date:** 2026-04-29
**Scope:** Harden `mempalace_file_context` against arbitrary file reads; preserve private/local usability

---

## Summary

Added path-scoping to `mempalace_file_context` with safe defaults. By default, the tool now rejects files outside `project_path` (if provided) or `MEMPALACE_ALLOWED_ROOTS`. `MEMPALACE_FILE_CONTEXT_ALLOW_ANY=1` restores the old permissive behavior for users who need it.

---

## Changes

### 1. `mempalace/settings.py`

Added two new fields to `MemPalaceSettings`:

```python
# ── File context security ───────────────────────────────────────────────
# Safe default: mempalace_file_context rejects files outside allowed roots.
# MEMPALACE_FILE_CONTEXT_ALLOW_ANY=1 restores the old permissive behavior.
file_context_allow_any: bool = False

# Colon-separated list of allowed root directories for file_context reads.
# Each path is resolved (symlinks expanded, trailing slashes stripped) before comparison.
# Example: /Users/me/projects:/Users/me/chats
# Not enforced when file_context_allow_any=True.
file_context_allowed_roots: str = ""
```

Both are Pydantic settings — `file_context_allow_any` maps to env `MEMPALACE_FILE_CONTEXT_ALLOW_ANY`, `file_context_allowed_roots` maps to `MEMPALACE_ALLOWED_ROOTS`.

### 2. `mempalace/server/_code_tools.py`

Added `_is_path_allowed()` utility (lines 73–102) and hardened `mempalace_file_context` (lines 156–240):

**`_is_path_allowed()`** — module-level for testability:

```
allow_any=True  → always returns True (preserves old behavior)
project_path set → uses _source_file_matches (symlinks resolved, case-normalized)
allowed_roots   → colon-separated, each checked via _source_file_matches
.. traversal    → resolved by Path().resolve() before comparison
```

**`mempalace_file_context` signature change:**

```python
# Added project_path parameter:
def mempalace_file_context(
    ctx: Context,
    file_path: str,
    line_start: int | None = None,
    line_end: int | None = None,
    context_lines: int = 5,
    project_path: str | None = None,  # NEW
) -> dict
```

Security check inserted before any file I/O:

```python
if not _is_path_allowed(
    file_path,
    project_path,
    settings.file_context_allow_any,
    settings.file_context_allowed_roots,
):
    return {"error": "file_context denied: path is outside allowed roots"}
```

### 3. `tests/test_file_context_scope.py` (new)

28 tests covering all security scenarios — no daemon, no LanceDB required:

| Test class | Coverage |
|---|---|
| `TestSourceFileMatches` | Symlink resolution, subdirectory, traversal |
| `TestIsPathAllowedAllowAny` | `allow_any=True` restores permissive behavior |
| `TestIsPathAllowedDefaultDenied` | Default deny of `/etc/hosts`, temp, home subdirs |
| `TestIsPathAllowedAllowedRoots` | Colon-separated roots, whitespace trim, empty parts |
| `TestIsPathAllowedProjectPath` | project_path as sole allowed root |
| `TestIsPathAllowedTraversal` | `../` resolved before check, denied if escapes |
| `TestIsPathAllowedNoProjectPathNoRoots` | Strict deny edge case |
| `TestMempalaceFileContextSecurity` | Full security logic via `_is_path_allowed` |

---

## Test Results

```
tests/test_file_context_scope.py    28 passed
tests/test_plugin_docs_truth.py     15 passed
tests/test_scoped_retrieval_e2e.py  43 passed (total across all three)
```

16 pre-existing warnings (datetime.utcnow deprecation in miner.py) — unrelated.

---

## Default Behavior

| Condition | Result |
|---|---|
| `MEMPALACE_FILE_CONTEXT_ALLOW_ANY=1` | Old permissive behavior — any file readable |
| `project_path` provided, file under it | Allowed |
| `MEMPALACE_ALLOWED_ROOTS=/a:/b`, file under `/a` or `/b` | Allowed |
| None of the above | `"file_context denied: path is outside allowed roots"` |

Path traversal (`/proj/src/../../../etc/hosts`) resolves to `/etc/hosts` before checking — correctly denied.

---

## Lint

Only 2 pre-existing E402 errors in `_code_tools.py` (import order, introduced before this phase). Zero new lint errors from these changes.

---

## Docs Updated

- `.claude-plugin/skills/mempalace/codebase-rag-workflow.md` — added `### mempalace_file_context Security` subsection under Search Rules, documenting the env override and safe default.

---

## ABORT Conditions — Verified

- `core code_search` / `mempalace_project_context` — **not touched**. `_source_file_matches` and `_filter_by_project_path` are used unchanged by those tools. The new `_is_path_allowed` is called only by `mempalace_file_context`.
- Existing RAG workflow — **unaffected**. `project_path` parameter is optional and defaults to `None`, which means the security check falls through to `allowed_roots` and `allow_any`. Without any env vars set, the default is to deny files not under a configured root or passed `project_path`.
