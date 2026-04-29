# PHASE27 — Private Fork README Truth Cleanup

## Actions Taken

**README.md patches:**
1. Added "Private Fork Runtime Truth" section at the top (after the logo/block) — covers Python 3.14 only, LanceDB only, ChromaDB removed, M1 8GB target, shared HTTP server at 127.0.0.1:8765/mcp, hook registration truth in `.claude-plugin/README.md`, no Docker/cloud in hot path
2. Fixed `## Requirements` section: `Python 3.9+` → `Python 3.14` (from pyproject.toml `requires-python >= 3.14`)
3. Fixed `[python-shield]` URL: `python-3.9+` → `python-3.14`

**.claude-plugin/README.md patch:**
- Prerequisites line: removed "3.9+ is the minimum" — now reads "Python 3.14 (the target and minimum version)"

**New test file:**
- `tests/test_readme_private_truth.py` — 7 checks covering Python 3.14, LanceDB only, no Chroma backend, no 3.9+ claim, no stdio recommendation for Claude Code, shared HTTP server 127.0.0.1:8765/mcp, hook registration pointed to plugin README

## Stale Claims Found and Patched

| Claim | Location | Fix |
|-------|----------|-----|
| `Python 3.9+` minimum | README.md `## Requirements` | Changed to Python 3.14 |
| `python-3.9+` shield URL | README.md link def | Changed to `python-3.14` |
| `3.9+ is the minimum` | `.claude-plugin/README.md` Prerequisites | Removed |

## Test Results

```
.venv/bin/python -m pytest tests/test_readme_private_truth.py tests/test_plugin_docs_truth.py tests/test_truth_invariants.py -q
.........................                                                [100%]
25 passed in 2.89s
```

All truth-invariant checks pass:
- `test_readme_private_truth.py`: 7/7 pass (new file)
- `test_plugin_docs_truth.py`: 5/5 pass
- `test_truth_invariants.py`: 13/13 pass

## What Was NOT Changed

- Benchmark marketing (96.6% LongMemEval, vs Published Systems table) — these are reproducible benchmark results, not private fork contradictions
- `pip install mempalace` instructions in Quick Start — the package is installable via pip from the fork
- File Reference, Project Structure, All Commands sections — no stale claims detected
- Auto-save hooks section (571-589) — mentions manual `settings.json` registration, matches plugin README