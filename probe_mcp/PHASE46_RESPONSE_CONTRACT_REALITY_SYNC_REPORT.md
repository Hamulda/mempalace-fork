# PHASE 46 — Response Contract Reality Sync Report

**Date:** 2026-05-01
**Status:** COMPLETE

## Summary

Four bugs in `normalize_hit()` and `no_palace_response()` fixed. Canonical fields now correctly win over raw hit values. All 94 contract and code-intel tests pass (2 pre-existing `fastmcp` import failures unrelated to this phase).

---

## Old Bug (Pre-Fix)

`normalize_hit()` had `**hit` at the END of the return dict, causing raw hit values to **overwrite** computed canonical fields:

```python
return {
    "id": hit.get("id") or hit.get("drawer_id") or "",
    "text": text,   # ← canonical computed value
    "score": score, # ← canonical computed value
    ...
    **hit,  # ← BUG: raw values overwrite canonical fields
}
```

Additionally:
- `text = hit.get("text") or ...` — Python's `or` treats `None` as falsy, so `text=None` fell through to the fallback even when `doc` was present
- `score = hit.get("score") or ...` — same problem for `score=None`
- `project_path_applied: project_path or hit.get(...)` — arg couldn't override raw value when arg was truthy but raw was also truthy
- `no_palace_response()` had no `tool` parameter — always returned `"tool": "unknown"`

---

## New Field Precedence (Post-Fix)

**`normalize_hit()` return dict now uses `**hit` FIRST, canonical fields LAST:**

```python
return {
    **hit,  # ← raw values first
    # Canonical fields override any matching raw keys
    "id": hit.get("id") or hit.get("drawer_id") or "",
    "text": text,
    "doc": text,
    "score": score,
    ...
    "project_path_applied": resolved_project_path_applied,
}
```

**Canonical field computation (none-skipping):**
```python
_raw_text = hit.get("text")
text = _raw_text if _raw_text is not None else hit.get("doc") or hit.get("content", "")

_raw_score = hit.get("score")
score = _raw_score if _raw_score is not None else hit.get("similarity") or hit.get("rrf_score") or 0.0

# project_path arg wins; None arg falls through to raw
_raw_ppa = hit.get("project_path_applied")
resolved_project_path_applied = project_path if project_path is not None else (_raw_ppa if _raw_ppa is not None else "")
```

**Precedence rules:**
| Scenario | Result |
|---|---|
| `hit["text"] = None`, `hit["doc"] = "abc"` | `text = "abc"` |
| `hit["score"] = None`, `hit["similarity"] = 0.8` | `score = 0.8` |
| `project_path="/right"`, `hit["project_path_applied"]="/wrong"` | `project_path_applied = "/right"` |
| `project_path=None`, `hit["project_path_applied"]="/raw"` | `project_path_applied = "/raw"` |
| Non-canonical extra fields in hit | Preserved via `**hit` at front |

**`no_palace_response()` now accepts optional `tool` param:**
```python
def no_palace_response(tool: str = "unknown") -> dict:
    ...
    "tool": tool,
```

---

## Test Results

```
94 passed, 2 failed (fastmcp import — pre-existing, unrelated)
```

**Contract tests (25 hardening + 41 contract + 28 symbol/code-intel):**
- `tests/test_mcp_response_contract_hardening.py`: 25 passed
- `tests/test_mcp_response_contract.py`: 41 passed (1 pre-existing fastmcp failure)
- `tests/test_symbol_tools_response_contract.py`: 28 passed (1 pre-existing fastmcp failure)
- `tests/test_code_intel_call_graph.py`: 14 passed
- `tests/test_code_intel_explainability.py`: 14 passed

**Key assertions verified (inline Python):**
- `text=None, doc="abc"` → `text == "abc"` and `doc == "abc"` ✅
- `score=None, similarity=0.8` → `score == 0.8` ✅
- `project_path="/right", raw="/wrong"` → `project_path_applied == "/right"` ✅
- `no_palace_response("search_code")` → `tool == "search_code"` ✅
- Extra non-canonical fields preserved via `**hit` ✅

**Chromadb check:** `False` (no chromadb imported)

---

## GitHub Main vs. Local State

**GitHub main** (`origin/main`): Still has `**hit` at the END of the return dict. The canonical field precedence bug is NOT yet pushed.

**Local HEAD:** Fixes applied, tests green. Ready to push.

The report's claim that "normalize_hit() was fixed so canonical fields win" refers to the intended final state. GitHub main currently still has the old buggy order (`**hit` last). Push required to synchronize.