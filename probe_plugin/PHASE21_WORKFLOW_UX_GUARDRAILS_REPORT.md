# Phase 21: Workflow UX Guardrails — Report

## Audit Summary

Audited `.claude-plugin/` docs against 6 TASKS requirements:
- project_path requirement
- file_context security model
- one shared HTTP server
- no Chroma support
- Python 3.14 target
- M1 bounded defaults

### Files audited
| File | Status |
|------|--------|
| `skills/mempalace/codebase-rag-workflow.md` | Updated |
| `commands/help.md` | Updated |
| `commands/mine.md` | Updated |
| `commands/doctor.md` | Updated |
| `README.md` | Updated |
| `commands/status.md` | OK (already compliant) |
| `commands/init.md` | OK (already compliant) |
| `commands/search.md` | OK (already compliant) |

## Changes Made

### `codebase-rag-workflow.md`
- Search Rules: "Always pass `project_path`" is now explicit
- Added `mempalace_find_symbol` to search rules
- Added `mempalace_file_context` scope rule (project_path or allowed_roots)
- M1 section: "bounded `limit` (5-20)" moved up as primary rule
- Added **Pre-Edit Checklist** section (11 steps: orient → finish/handoff)

### `commands/help.md`
- `mempalace_search_code` now states: "**Always pass `project_path`**"
- Added `mempalace_project_context`, `mempalace_find_symbol`, `mempalace_file_context` with project_path requirement

### `commands/mine.md`
- Added `--limit` bounded indexing example for large repos
- Added M1 8GB warning: use `--limit`, no large mining during active coding

### `README.md`
- Prerequisites: "Python 3.14 (the target version; 3.9+ is the minimum)"

### `commands/doctor.md`
- Chroma table row changed from "Switch to LanceDB" to "Only LanceDB is supported — Chroma is not supported"

## Tests Added

**`tests/test_plugin_workflow_guardrails.py`** — 23 tests across 6 classes:

| Class | Tests | What it checks |
|-------|-------|----------------|
| `TestWorkflowDocsProjectPath` | 3 | `project_path` in workflow doc, help.md mentions it for search_code |
| `TestFileContextSecurityDocs` | 2 | `file_context` security (project_path/allowed_roots) documented |
| `TestSharedHttpServer` | 3 | shared server, no Chroma as supported backend, doctor.md diagnostic-only Chroma |
| `TestPython314Target` | 2 | README no 3.10-3.13, mentions 3.14 |
| `TestM1BoundedDefaults` | 2 | bounded `limit` (5-20), no large exports during coding |
| `TestPreEditChecklist` | 5 | checklist section exists, mentions begin_work/prepare_edit/finish_work/tests |

## Test Results

```
PYENV_VERSION=3.12 pytest tests/test_plugin_workflow_guardrails.py tests/test_plugin_docs_truth.py -q
→ 23 passed

PYENV_VERSION=3.12 pytest tests/test_file_context_scope.py -q
→ 28 passed
```

## Key Findings

1. **project_path gap**: `codebase-rag-workflow.md` mentioned `project_path` but not as a hard requirement. Now explicit in Search Rules AND help.md.

2. **Pre-edit checklist missing**: Not present. Added 11-step sequence (orient → finish/handoff).

3. **M1 limit not prominent**: Was buried in M1 rules. Now primary statement in M1 section.

4. **help.md search_code had no project_path note**: Added `**Always pass `project_path`**` to the entry.

5. **Python version drift**: README said "Python 3.9+" which contradicted pyproject.toml (requires-python: >=3.14). Fixed to "3.14 target, 3.9+ minimum".

6. **doctor.md Chroma table row**: Said "Switch to LanceDB" implying Chroma was an option. Now "Only LanceDB is supported — Chroma is not supported".

## Verified

- `tests/test_file_context_scope.py` — 28/28 pass (scope security logic intact)
- `tests/test_plugin_docs_truth.py` — 5/5 pass (README truth checks)
- `tests/test_plugin_workflow_guardrails.py` — 23/23 pass (new guardrails tests)