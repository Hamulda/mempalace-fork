# PHASE36: Claude Code Startup Context Pack

## Summary

Added `mempalace_startup_context` MCP tool and `build_startup_context()` function that provide a compact context pack for Claude Code session startup. Includes server health, active claims, pending handoffs, and M1 bounded defaults — so Claude knows the project state before responding.

## What Was Added

### 1. MCP Tool: `mempalace_startup_context`

**File:** `mempalace/server/_session_tools.py` (lines 621–672)

```python
@server.tool(timeout=settings.timeout_read)
def mempalace_startup_context(
    ctx: Context,
    project_path: str | None = None,
    session_id: str | None = None,
    limit: int = 8,
) -> dict
```

Auto-detects session_id via `_optional_session_id()`. Calls `build_startup_context()` with resolved params.

### 2. Function: `build_startup_context()`

**File:** `mempalace/wakeup_context.py` (lines 286–418)

Inputs:
- `session_id: str` — required
- `project_path: str | None = None` — optional project root (auto-derived from git root if None)
- `palace_path: str | None = None` — palace data directory
- `limit: int = 8` — max pending handoffs

Outputs:
| Field | Type | Description |
|-------|------|-------------|
| `server_health` | dict | HTTP /health probe result |
| `palace_path` | str | Palace data directory |
| `backend` | str | Always `'lance'` |
| `python_version` | str | e.g. `"3.14.0"` |
| `embedding_provider` | str | From embed daemon probe |
| `embedding_meta` | dict | model_id, embed_batch_size |
| `active_sessions` | int | Count from session registry |
| `current_claims` | list | Claims for project_path |
| `current_claims_count` | int | Count of current claims |
| `pending_handoffs` | list | Handoffs for this session (limited) |
| `pending_handoffs_count` | int | Count of pending handoffs |
| `recommended_first_actions` | list | Startup workflow steps |
| `project_path_reminder` | str | Resolved project path |
| `m1_defaults` | dict | Bounded defaults for M1/8GB |

### 3. Plugin Command

**File:** `.claude-plugin/commands/startup.md`

Documents:
- Call `mempalace_startup_context` at session start
- No Chroma — LanceDB only
- M1 bounded defaults
- Hooks note: do not auto-run, must call explicitly

### 4. Tests

**File:** `tests/test_startup_context.py`

16 test cases covering all output fields, no Chroma invariant, M1 defaults bounded, Python 3.14 version check.

## Verification

```
tests/test_startup_context.py        → 16 passed
tests/test_plugin_docs_truth.py       → 23 passed
tests/test_plugin_workflow_guardrails.py → 23 passed
chromadb in sys.modules             → False
```

Smoke test output:
```
Keys: ['active_sessions', 'backend', 'current_claims', 'current_claims_count',
        'embedding_meta', 'embedding_provider', 'm1_defaults', 'palace_path',
        'pending_handoffs', 'pending_handoffs_count', 'project_path_reminder',
        'python_version', 'recommended_first_actions', 'server_health']
backend: lance
python_version: 3.14.0
m1_defaults: {'max_batch': 32, 'embed_batch_default': 64,
             'memory_guard_active': True, 'query_cache_ttl': 300,
             'claim_timeout_seconds': 60, 'session_timeout_seconds': 300}
recommended: ['mempalace_status', 'mempalace_search', 'mempalace_diary_write']
```

## Hard Rules Compliance

| Rule | Status |
|------|--------|
| No Chroma | Confirmed — backend always `'lance'` |
| Python 3.14 only | `python_version` returns `3.14.x` |
| No new heavy deps | Uses stdlib (`urllib.request`, `json`) |
| No cloud/Docker | Fully local, stdlib only |
| Backup before edit | No existing files modified (new files only) |
| Compact output | Returns 14 top-level keys, no large payloads |

## Files Modified/Created

| File | Change |
|------|--------|
| `mempalace/wakeup_context.py` | Added `build_startup_context()` (133 lines) |
| `mempalace/server/_session_tools.py` | Added `mempalace_startup_context` tool (52 lines) |
| `.claude-plugin/commands/startup.md` | New plugin command doc |
| `tests/test_startup_context.py` | New test file (16 test cases) |
