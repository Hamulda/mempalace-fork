---
description: Show MemPalace help — canonical setup, workflow path, tools, and architecture.
allowed-tools: Bash, Read
---

# MemPalace Help

## Canonical Setup (for 6 parallel Claude Code sessions)

```
1. pip install git+https://github.com/Hamulda/mempalace-fork
2. mempalace serve --host 127.0.0.1 --port 8765  (one shared server)
3. mempalace init ~/palace
4. Install Claude Code plugin: claude plugin marketplace add hamulda/mempalace-fork
```

Verify: `curl http://127.0.0.1:8765/health`

## Workflow Path (use these in this order)

### Single-File Edit

| Step | Tool | Purpose |
|------|------|---------|
| 1 | `mempalace_file_status` | Quick snapshot before editing |
| 2 | `mempalace_begin_work` | Start session, claim file, check conflicts |
| 3 | `mempalace_prepare_edit` | Get symbol context, hot-spot, auto conflict check |
| 4 | **You edit** | Model makes the edit |
| 5 | `mempalace_finish_work` | Release claim, write diary, capture decisions |

### Multi-File Edit

Use `mempalace_begin_work_batch` to claim all files atomically (all-or-nothing).
If atomic claim fails, fall back to individual `mempalace_begin_work` per file.
Use `mempalace_finish_work_batch` to release all claims + write one diary entry.

### Blocked by Claim

If `mempalace_begin_work` returns `failure_mode: claim_conflict`:
1. Check `expires_at` on the conflicting claim
2. Wait for TTL expiry, or:
3. Use `mempalace_publish_handoff` to request the holder release it
4. Or use `mempalace_takeover_work` if a handoff was published

### Handoff Work

When handing off to another session:
1. Finish your edits
2. Use `mempalace_publish_handoff` — atomic handoff + release all claims
3. Include `summary`, `next_steps`, and optionally `handoff_id` for the recipient

### Takeover Work

When accepting a handoff:
1. Use `mempalace_takeover_work` — accepts handoff + claims all paths atomically
2. Then use `mempalace_prepare_edit` per file before editing

### Mining / Reindexing

Use the CLI, not MCP tools directly:
```bash
mempalace mine ~/projects/myapp
mempalace mine ~/chats/ --mode convos
```
Then verify with `mempalace_status`.

## Tier 2 — When Tier 1 Won't Do

- `mempalace_claim_path` — refresh TTL on existing claim
- `mempalace_release_claim` — manual release
- `mempalace_conflict_check` — explicit conflict check
- `mempalace_push_handoff` / `mempalace_pull_handoffs` / `mempalace_accept_handoff`

## Tier 3 — Search & Knowledge

- `mempalace_search` — semantic search across all memories
- `mempalace_hybrid_search` — semantic + keyword + KG combined
- `mempalace_search_code` — code search with language/symbol/file-path filters. **Always pass `project_path`** to scope results and pass security checks.
- `mempalace_project_context` — set `project_path` for repo-scoped retrieval (use before search_code and find_symbol)
- `mempalace_find_symbol` — symbol lookup (function, class, variable) scoped to `project_path`
- `mempalace_file_context` — exact line ranges for a symbol. Requires `project_path` or allowed roots set.
- `mempalace_kg_query` — knowledge graph entities
- `mempalace_status` — palace overview

## M1-Specific Guidance

On MacBook Air M1 8GB:
- Use `mempalace_search_code` with `limit` parameter (e.g., `limit: 10`) — avoid unbounded result sets
- Avoid `mempalace_export_claude_md` with large room filters — use targeted `mempalace_search` instead
- Reranking (`mempalace_hybrid_search` with `rerank: true`) is expensive — use only for complex semantic queries where FTS5 keyword match alone is insufficient
- If searches feel slow, run `/mempalace:doctor` to check server health before filing issues

## Architecture

```
Claude Code (6 sessions) → MCP HTTP → mempalace serve (1 process, port 8765)
                                            ↓
                                    SessionRegistry (shared)
                                    WriteCoordinator (WAL)
                                    ClaimsManager (file locks)
                                    HandoffManager (atomic)
                                    DecisionTracker
                                            ↓
                                    LanceDB (~/.mempalace/)
```

## Organization

| Content | Wing | Room |
|---------|------|------|
| Architectural decisions | technical | decisions |
| Code and implementations | technical | code |
| Debates and conclusions | technical | discussions |
| User personal preferences | identity | preferences |

## Server Not Running?

**With hooks registered** (recommended): Server starts automatically on first session. No manual start needed.

**Without hooks registered** — start manually:
```bash
mempalace serve --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/health
```

Hooks (SessionStart/Stop/PreCompact) are registered in `~/.claude/settings.json`. See `/mempalace:init` for setup.
