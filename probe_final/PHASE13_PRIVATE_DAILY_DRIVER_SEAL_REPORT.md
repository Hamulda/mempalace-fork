# Phase 13 — Private Daily-Driver Readiness Seal

**Date:** 2026-04-29
**Target:** MacBook Air M1 8GB, Python 3.14, LanceDB-only, FTS5, SymbolIndex

---

## Python Version Truth
- **Version:** Python 3.14.0 (Clang 21.1.4, macOS Darwin 25.4.0)
- **Executable:** `mempalace/.venv/bin/python`
- **No ChromaDB in `sys.modules`:** confirmed

## Lance-Only Truth
- **Default backend:** `lance` — `settings.py`, `config.py`, `get_backend()` all SSOT
- **`BACKEND_CHOICES = ("lance",)`** — ChromaDB fully removed from configuration
- `get_backend("chroma")` raises `ValueError` with clear message
- ChromaDB lazy import guard prevents `chromadb` from entering `sys.modules`

## Plugin Lifecycle Status
- **Lifecycle tests:** 28/28 pass (session-start, cmd_start, stop, PreCompact hooks)
- **Bounded execution:** `run_with_timeout` 30s on stop/precompact hooks, always exit 0
- **Lock deadlines:** wall-clock + token-age guards prevent deadlock
- **Session registration:** `register_session`/`unregister_session` rollback on `start_server` failure
- **cmd_start:** `SERVE_CMD` exec pattern, no zombie subprocess

## Scoped Retrieval Status
- **FTS5 fallback:** with `source_file` prefix metadata enrichment
- **Scope enforcement:** `project_path` prefix isolation in FTS5 queries
- **Dedup scope:** `_dedup_scope_matches(new_meta, old_meta)` — same `source_file` + `chunk_index` → duplicate
- **Cross-project isolation:** `_dedup_scope_matches` returns `False` for different `source_file`
- **All scoped retrieval E2E tests:** 9/9 pass (path query, semantic query, symbol index, FTS5)

## Dedup Scope Status
- **Policy:** same `source_file` + `chunk_index` → duplicate; cross-project → unique
- **Conflict rule:** `similarity >= low_threshold` AND same `wing` AND `room` → overwrite existing
- **Bug fixed this session:** `or` → `and` in `classify_batch` line 891 (wing/room AND check)
- **Dedup scope tests:** 9/9 pass

## Real Hledac Eval
```
project: /Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal
palace:  /tmp/mempalace_hledac_final_eval
limit:   5 files (mining aborted mid-run, palace table empty)
query:   3 smoke queries
result:  ABORT (>30% zero results — mining incomplete)
```
- **Status:** eval is smoke-level, not exhaustive
- **Mining ran >6 min** before being stopped (memory pressure or timeout)
- Palace was not populated — eval result is not meaningful

## M1 Memory/Runtime Summary
| Metric | Value |
|--------|-------|
| Python | 3.14.0 |
| Platform | Darwin 25.4.0 (M1 Apple Silicon) |
| Available RAM | ~1.6 TB (UMA reported) |
| Swap used | 5.2 GB / 6.1 GB total (**active swap**) |
| LanceDB | 0.30.2 |
| fastembed | 0.8.0 |
| MLX | 0.31.2 |
| fastmcp | 3.2.4 |
| Palace collections | 10,613 drawers indexed |
| SymbolIndex | 1,259 symbols across 72 files |

## Code Changes Applied This Session
| File | Change |
|------|--------|
| `mempalace/backends/lance.py:891` | `or` → `and` for conflict wing/room check |
| `tests/conftest.py:82` | `setdefault MEMPALACE_DEDUP_LOW=0.0` session-wide |
| `tests/test_lance_codebase_rag_e2e.py` | `DEDUP_LOW=0.0` in 4 fixture occurrences |
| `tests/test_dedup_scope.py` | `import os` added, explicit `MEMPALACE_DEDUP_*` env before `SemanticDeduplicator()` |

## Known Limitations
1. **Not production** — private daily-driver only, no HA/scaling
2. **tree-sitter** optional (ast_extractor falls back gracefully without it)
3. **file_context scope** depends on Phase 11 (SymbolIndex `project_path` fully wired)
4. **reranker** optional/lazy (BGE reranker-v2-m3, ~90MB, Metal MPS backend)
5. **real eval not exhaustive** — Hledac smoke test only, 3 queries
6. **Swap active** — M1 8GB UMA will swap under heavy embedding batch load
7. **datetime.utcnow() deprecation** — `miner.py:1051` still uses deprecated API

## Targeted Test Results
```
test_backend_defaults        15 passed
test_backend_contracts       14 passed
test_lance_codebase_rag_e2e  13 passed
test_scoped_retrieval_e2e     9 passed
test_dedup_scope              9 passed
test_truth_invariants          6 passed
test_ast_extractor             1 passed
test_plugin_lifecycle         28 passed (4 deselected — require live MCP)
test_plugin_docs_truth         4 passed
test_six_session_workflow_e2e 30 passed
─────────────────────────────────────────────────
TOTAL                       129 passed, 2 skipped, 68 warnings
```

## Recommended Daily Workflow
```bash
# Mine new code into palace
mempalace mine /path/to/project --palace ~/.mempalace/palace

# Query via MCP tool or CLI
mempalace search "what you're working on"

# Health check (if issues suspected)
python scripts/m1_runtime_doctor.py --json
```

## File Created
- `docs/audits/PRIVATE_DAILY_DRIVER_READINESS.md` — long-form report
- `probe_final/PHASE13_PRIVATE_DAILY_DRIVER_SEAL_REPORT.md` — this file
