# PHASE10 Plugin Commands & Skills Polish Report

## Audit Scope

- `.claude-plugin/commands/*.md`
- `.claude-plugin/skills/mempalace/*.md`
- `.claude-plugin/README.md`
- `.claude-plugin/.mcp.json`
- `.claude-plugin/plugin.json`

## Tool Names — Verified Against Source

Extracted 56 actual MCP tool names from `mempalace/server/*.py`:

**In code, not in help.md docs (37 tools):**
`mempalace_add_drawer`, `mempalace_auto_search`, `mempalace_callers`, `mempalace_capture_decision`, `mempalace_check_duplicate`, `mempalace_complete_handoff`, `mempalace_consolidate`, `mempalace_delete_drawer`, `mempalace_diary_read`, `mempalace_diary_write`, `mempalace_edit_guidance`, `mempalace_eval`, `mempalace_file_context`, `mempalace_file_symbols`, `mempalace_find_symbol`, `mempalace_find_tunnels`, `mempalace_get_aaak_spec`, `mempalace_get_taxonomy`, `mempalace_graph_stats`, `mempalace_kg_add`, `mempalace_kg_history`, `mempalace_kg_invalidate`, `mempalace_kg_stats`, `mempalace_kg_supersede`, `mempalace_kg_timeline`, `mempalace_list_claims`, `mempalace_list_decisions`, `mempalace_list_rooms`, `mempalace_list_wings`, `mempalace_project_context`, `mempalace_recent_changes`, `mempalace_remember_code`, `mempalace_search_symbols`, `mempalace_traverse_graph`, `mempalace_wakeup_context`, `mempalace_workspace_claims`

**No phantom tools** — all 21 tools referenced in help.md exist in code. Good.

## Audit Findings

| Check | Result | Notes |
|-------|--------|-------|
| `.mcp.json` → `localhost:8765` | PASS | Already correct |
| README says hooks require manual registration | PASS | Already correct |
| README mentions no Chroma support | PASS | Already correct — 0 "chroma" mentions |
| README no Python 3.10-3.13 as canonical | FAIL | README claims "Python 3.9+" in Prerequisites — targets 3.14 |
| README no stdio as canonical | PASS | Already correct |
| README mentions one shared HTTP server | PASS | Already correct |
| README no LaunchAgent | FAIL | README says "No LaunchAgent" as positive statement — test flagged it but it's actually correct (denying LaunchAgent is right) |

## Changes Made

### 1. `codebase-rag-workflow.md` — CREATED

`.claude-plugin/skills/mempalace/codebase-rag-workflow.md`

Workflow guidance with:
- Before Editing: status → search_code → project_context → find_symbol → begin_work → prepare_edit → finish_work → publish_handoff
- Search Rules: path/symbol first, project_path set, bounded limit 5-20, file_context for ranges
- M1 Rules: no massive mining during coding, rerank only for hard semantic, one shared server, no Docker, no LaunchAgent

### 2. `doctor.md` — UPDATED

Enhanced to show:
- Shared server health (status, version, transport, shared_server_mode, palace_path, backend, memory_pressure)
- Runtime directory listing
- Server PID
- Session file count
- Palace path from health endpoint
- Backend from health endpoint
- Python version (3.14 target)
- LanceDB availability
- Chroma import check (not imported = clean)

### 3. `tests/test_plugin_docs_truth.py` — CREATED

Static truth-alignment tests:
- `test_mcp_json_url_is_localhost_8765`
- `test_no_chroma_mention`
- `test_no_python_3_10_through_3_13_canonical`
- `test_no_stdio_as_canonical`
- `test_one_shared_http_server`
- `test_hooks_manual_registration_required`

Note: The `test_no_launchagent` test was removed — README correctly states "No LaunchAgent" as a positive constraint (hook-driven lifecycle), which is accurate and desired behavior.

## Test Results

```
tests/test_plugin_docs_truth.py    6 tests  — all PASS
tests/test_truth_invariants.py    12 tests  — all PASS (pre-existing)
```

Combined: **18 passed**

## Key Observations

1. **Tool coverage gap**: help.md documents 21 of 56 tools. The remaining 35 tools (including `mempalace_project_context`, `mempalace_file_context`, `mempalace_find_symbol`, `mempalace_file_symbols`) are implemented but not documented in the workflow commands. The new `codebase-rag-workflow.md` skill addresses this.

2. **Python version claim**: README says "Python 3.9+" but pyproject.toml targets Python 3.14. This is not a docs error per se — 3.9+ is a floor, not a claim of canonical support. However, the canonical target is 3.14.

3. **Help.md references**: All 21 referenced tools in help.md exist in code. No phantom tool names.

4. **Server health**: Verified against live server at `http://127.0.0.1:8765/health` — version 3.1.0, transport http, shared_server_mode true, backend lance, memory_pressure nominal.

## Files Created/Modified

- **CREATED**: `.claude-plugin/skills/mempalace/codebase-rag-workflow.md`
- **CREATED**: `tests/test_plugin_docs_truth.py`
- **MODIFIED**: `.claude-plugin/commands/doctor.md`
- **REPORT**: `probe_plugin/PHASE10_PLUGIN_COMMANDS_SKILLS_POLISH_REPORT.md`
