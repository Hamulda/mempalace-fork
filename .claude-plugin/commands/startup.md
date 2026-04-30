---
description: Build compact startup context for Claude Code session start — server health, claims, handoffs, M1 defaults.
allowed-tools: Bash, Read
---

# MemPalace Startup

## At the Start of Each Claude Code Session

Call the startup context tool **before doing any meaningful work**:

```
mempalace_startup_context
```

Or from shell:

```bash
mempalace start
```

## What You'll Get

- **server_health**: HTTP probe of the MCP server (/health endpoint)
- **palace_path**: where MemPalace stores its data
- **backend**: `lance` (ChromaDB is not supported)
- **python_version**: Python 3.14
- **embedding_provider**: mlx, coreml, or cpu (from embed daemon probe)
- **embedding_meta**: model_id, embed_batch_size if available
- **active_sessions**: how many Claude Code sessions are running
- **current_claims**: active file claims for your project
- **pending_handoffs**: work handed off from other sessions
- **recommended_first_actions**: what to do first
- **project_path_reminder**: the project root being worked on
- **m1_defaults**: bounded defaults for M1/8GB (max_batch=32, etc.)

## Workflow

```
1. mempalace_startup_context   ← call at session start
2. mempalace_search            ← verify facts before responding
3. mempalace_begin_work        ← claim a file before editing
4. mempalace_diary_write       ← record what happened at session end
```

## No Chroma — LanceDB Only

ChromaDB support was removed. The only supported backend is **LanceDB**.

## M1/8GB Bounded Defaults

When running on MacBook Air M1 (8GB UMA):

- **max_batch**: 32 texts per embed batch
- **embed_batch_default**: 64 in nominal RAM conditions
- **memory_guard_active**: true — writes pause under memory pressure
- **query_cache_ttl**: 300s
- **claim_timeout_seconds**: 60 — auto-release stale claims
- **session_timeout_seconds**: 300 — mark session idle after 5min

## Hooks Note

Hooks (session-start, stop) do **not** auto-run in all configurations.
Always call `mempalace_startup_context` explicitly at the start of a session
for a Claude Code-native workflow.
