---
name: mempalace_F176_phase2_metadata_contract
description: F176 Phase 2 results — canonical metadata contract, status truncation fix, BM25 cache isolation
type: project
---

# F176 Phase 2: Metadata Contract & Correctness (2026-04-14)

## Canonical Metadata Contract (defined in `backends/base.py`)

All MemPalace writers MUST produce records with these mandatory fields:
- `wing` — logical group (project, agent, etc.)
- `room` — finer category within wing
- `source_file` — provenance identifier
- `added_by` — who filed this
- `agent_id` — same as added_by (for filter compatibility)
- `timestamp` — UTC ISO8601 with `Z` suffix (e.g. `2026-04-14T10:30:00.000Z`)
- `is_latest` — `True` for current, `False` for superseded/historical
- `supersedes_id` — id of record this supersedes (empty string if new)
- `origin_type` — why created: `"observation" | "diary_entry" | "code_memory" | "convo"`
- `chunk_index` — for multi-chunk files; `0` for single-unit

### Key Policies
- **TIMESTAMP**: Use `timestamp` (UTC ISO8601 with `Z`). `filed_at` is DEPRECATED.
- **is_latest/supersedes_id SEMANTICS**: When new version replaces old: old gets `is_latest=False`, new gets `supersedes_id=old_id`
- **BACKWARD COMPAT**: Readers must tolerate missing optional fields

## What Was Inconsistent

| Writer | Problem |
|--------|---------|
| `mempalace_add_drawer` (fastmcp_server.py) | Two time fields: `filed_at` (local ISO) + `timestamp` (UTC Z) |
| `mempalace_diary_write` | Legacy fields `hall`, `type`, `agent`, `date`; duplicate time; missing `agent_id` |
| `mempalace_remember_code` | `type: "code_memory"` instead of `origin_type: "code_memory"`; had `filed_at` |
| `mine_convos` (convo_miner.py) | Missing `agent_id`, `is_latest`, `supersedes_id`, `timestamp` |
| Project miner `add_drawer` (miner.py) | Missing `is_latest`, `supersedes_id`, `agent_id`, `timestamp`; only `filed_at` |

## Fixes Applied

### Metadata Fields Fixed
- **fastmcp_server.py add_drawer**: removed `filed_at`, canonical `timestamp` UTC Z, `origin_type: "observation"`, `is_latest: True`, `supersedes_id: ""`
- **fastmcp_server.py diary_write**: removed `hall`, `type`, `agent`, `date`, `filed_at`; added `agent_id`, `timestamp` (UTC Z), `origin_type: "diary_entry"`, `is_latest: True`, `supersedes_id: ""`, `source_file: "diary://{agent}/{date}"`
- **fastmcp_server.py remember_code**: `type` → `origin_type: "code_memory"`, removed `filed_at`, canonical `timestamp`
- **convo_miner.py**: added `agent_id`, `timestamp` (UTC Z), `origin_type: "convo"`, `is_latest: True`, `supersedes_id: ""`
- **miner.py add_drawer**: added `agent_id`, `timestamp` (UTC Z), `origin_type: "observation"`, `is_latest: True`, `supersedes_id: ""`

### Status/List/Taxonomy Truncation Fixed
- All 4 functions in `fastmcp_server.py` replaced fixed `limit=10000` with iterative batch aggregation (`_BATCH = 500`, while loop with offset)
- Functions fixed: `mempalace_status`, `mempalace_list_wings`, `mempalace_list_rooms`, `mempalace_get_taxonomy`

### BM25 Cache Drift Fixed
- `searcher.py`: Added `_bm25_path_cached` to track which palace the index belongs to
- `_get_bm25(col, palace_path)` now requires palace_path parameter
- `_bm25_search` requires palace_path parameter
- `hybrid_search` passes palace_path to `_bm25_search`
- When palace_path changes, stale BM25 index is invalidated
- KG singleton already had path checking

## New Test Files Added
- `tests/test_metadata_contract.py` — 8 tests for canonical metadata fields (all pass with ChromaDB)
- `tests/test_status_aggregation.py` — 3 tests for iterative aggregation without truncation

## Known Issues / Phase 4
- Large aggregation tests (`test_status_aggregation.py`) crash PC when run with ChromaDB + temp HOME because ChromaDB downloads ~79MB ONNX model each time into temp HOME. Fix: use LanceDB backend for large tests.
- Regression test for BM25 cache isolation between palace instances not yet written
- Regression test for search correctness after write invalidation not yet written
- F184: EmbedCircuitBreakerMiddleware wiring into backends/lance.py not complete
- WriteCoalescer for diary batch writes still TODO (F176)
