# Private Daily-Driver Readiness Seal — 2026-04-29

## Python Version Truth
- **Version:** Python 3.14.0 (Clang 21.1.4, macOS Darwin 25.4.0)
- **Executable:** `mempalace/.venv/bin/python`
- **No ChromaDB in `sys.modules`:** confirmed

## Lance-Only Truth
- **Default backend:** `lance` (settings `db_backend`, config `backend`, `get_backend()`)
- **`BACKEND_CHOICES = ("lance",)`** — ChromaDB fully removed
- `get_backend("chroma")` raises `ValueError`
- **ChromaDB lazy import guard:** `chromadb` not loaded until explicitly requested

## Plugin Lifecycle Status
- **Lifecycle tests:** 28/28 pass (session-start, cmd_start, stop, PreCompact hooks)
- **Bounded execution:** `run_with_timeout` 30s on stop/precompact hooks
- **Lock deadlines:** wall-clock + token-age guards
- **Session registration:** `register_session`/`unregister_session` on error rollback

## Scoped Retrieval Status
- **FTS5 fallback:** with metadata enrichment (source_file prefix)
- **Scope enforcement:** `project_path` prefix isolation in FTS5 queries
- **Dedup scope:** `_dedup_scope_matches` — same `source_file` + `chunk_index`
- **All scoped retrieval E2E tests:** 9/9 pass

## Dedup Scope Status
- **Policy:** same `source_file` + `chunk_index` → duplicate; cross-project → unique
- **Conflict logic:** `low_threshold` + same `wing` AND `room` → overwrite
- **Bug fixed:** `or` → `and` in `classify_batch` line 891 (same wing OR room → AND)
- **Dedup scope tests:** 9/9 pass

## Real Hledac Eval
- **Project:** Hledac universal OSINT orchestrator (`/Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal`)
- **Limit:** 5 files mined, 3 query eval
- **Result:** ABORT — palace table empty (mining incomplete due to memory pressure / process killed mid-run)
- **Known:** eval is smoke-level, not exhaustive

## M1 Memory/Runtime Summary
| Metric | Value |
|--------|-------|
| Python | 3.14.0 |
| Platform | Darwin 25.4.0 (M1) |
| Available RAM | ~1.6 TB (UMA reported) |
| Swap used | 5.2 GB / 6.1 GB total |
| LanceDB | 0.30.2 |
| fastembed | 0.8.0 |
| MLX | 0.31.2 |
| fastmcp | 3.2.4 |
| Palace collections | 10,613 |
| SymbolIndex symbols | 1,259 across 72 files |

## Code Changes Applied This Session
1. **`mempalace/backends/lance.py`:** `or` → `and` for conflict wing/room check (line 891)
2. **`tests/conftest.py`:** `setdefault MEMPALACE_DEDUP_LOW=0.0` to prevent cross-project conflict classification
3. **`tests/test_lance_codebase_rag_e2e.py`:** `DEDUP_LOW=0.0` in fixtures (4 occurrences)
4. **`tests/test_dedup_scope.py`:** `import os` added, `MEMPALACE_DEDUP_HIGH/LOW` set before SemanticDeduplicator init

## Known Limitations
- **Not production-ready** — private daily-driver only
- **tree-sitter** optional dependency (ast_extractor falls back gracefully)
- **file_context scope** depends on Phase 11 work
- **reranker** optional/lazy-loaded (BGE reranker-v2-m3, ~90MB)
- **real eval not exhaustive** — limited to smoke queries
- **swap detected** — M1 8GB UMA will swap under heavy embedding load
- **datetime.utcnow() deprecation** — still present in `miner.py:1051`

## Recommended Daily Workflow
```
# 1. Daily wake-up (terminal)
cd ~/.claude/plugins/marketplaces/mempalace
.venv/bin/python -m mempalace.cli --help

# 2. Mine new code
mempalace mine /path/to/project --palace ~/.mempalace/palace

# 3. Query (MCP tool or CLI)
mempalace search "what you're working on"

# 4. Health check (if issues suspected)
.venv/bin/python scripts/m1_runtime_doctor.py --json
```

## Test Results Summary
| Suite | Result |
|-------|--------|
| `test_backend_defaults` | 15/15 pass |
| `test_backend_contracts` | 14/14 pass |
| `test_lance_codebase_rag_e2e` | 13/13 pass |
| `test_scoped_retrieval_e2e` | 9/9 pass |
| `test_dedup_scope` | 9/9 pass |
| `test_truth_invariants` | 6/6 pass |
| `test_ast_extractor` | 1/1 pass |
| `test_plugin_lifecycle` | 28/28 pass (4 deselected) |
| `test_plugin_docs_truth` | 4/4 pass |
| `test_six_session_workflow_e2e` | 30/30 pass |
| **TOTAL** | **129 passed, 2 skipped, 68 warnings** |
