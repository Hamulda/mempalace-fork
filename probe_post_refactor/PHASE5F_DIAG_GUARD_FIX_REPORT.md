# PHASE 5F — DIAG GUARD FIX REPORT

## Issue
In `tests/test_scoped_retrieval_e2e.py`, `MEMPALACE_TEST_DIAG=1` printed `chunks` before it was defined.

## Fix
Reordered in two tests:

| Test | Before | After |
|------|--------|-------|
| `test_search_code_scoped_to_projA_only` | `print chunks` → `assert data` → `chunks =` | `assert data` → `chunks =` → `print chunks` |
| `test_semantic_query_scoped_to_projA_not_projB` | `print chunks` → `assert data` → `chunks =` | `assert data` → `chunks =` → `print chunks` |

Pattern: assert `data is not None` first, then assign `chunks`, then diagnostics print.

## Verification

```bash
$ rtk uv run pytest tests/test_scoped_retrieval_e2e.py -q
.........                                                                [100%]
9 passed, 16 warnings in 3.86s

$ rtk uv run pytest tests/test_dedup_scope.py tests/test_truth_invariants.py -q
.....................                                                    [100%]
21 passed in 2.35s

$ rtk uv run python -c "import sys; import mempalace; print('chromadb' in sys.modules)"
False
```

All green. No runtime changes.
