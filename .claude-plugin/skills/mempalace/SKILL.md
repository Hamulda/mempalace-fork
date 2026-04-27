# MemPalace

Searchable memory palace for AI sessions. Mine projects, store decisions, recall context.

## Auto-trigger rules

**Call `mempalace_search` without being asked when:**
- User asks about past decisions: "what did we decide", "jak jsme to implementovali"
- User mentions a project or technology they may have worked on before
- User says "I don't remember", "zapomněl jsem", "kde jsme přestali"

**Call `mempalace_add_drawer` without being asked when:**
- User says "remember this", "ulož to", "poznamenej si"
- Key architectural decision just made
- Session exceeds 20 exchanges with code or design content

## Tool Tiers

### Tier 1 — Primary Workflow (use these almost always)

| Tool | Phase | Purpose |
|------|-------|---------|
| `mempalace_file_status` | orient | Quick snapshot before claiming |
| `mempalace_begin_work` | claim | Start editing session (conflict check + claim + log intent) |
| `mempalace_prepare_edit` | edit | Get symbol context + hot-spot + auto conflict check |
| `mempalace_finish_work` | finish | Release claim + diary write + decision capture (single-file) |
| `mempalace_publish_handoff` | handoff | Atomic handoff + release all claims (multi-file) |
| `mempalace_takeover_work` | takeover | Accept handoff + claim paths |

### Tier 2 — Escape Hatch (only when Tier 1 won't do)

| Tool | When to use |
|------|-------------|
| `mempalace_claim_path` | Refresh TTL on existing claim you already hold |
| `mempalace_release_claim` | Manual release without diary/decision |
| `mempalace_conflict_check` | Explicit check when workflow tools insufficient |
| `mempalace_push_handoff` | Handoff without atomic claim release |
| `mempalace_pull_handoffs` | List handoffs without accepting |
| `mempalace_accept_handoff` | Accept without auto-claiming paths |
| `mempalace_edit_guidance` | Convert any workflow_result → plain guidance |

### Tier 3 — Search & Knowledge (use as needed)

| Tool | When to use |
|------|-------------|
| `mempalace_search` | Semantic search across all memories |
| `mempalace_hybrid_search` | Semantic + keyword + KG combined |
| `mempalace_search_code` | Code-specialized search with language/symbol/file-path filters |
| `mempalace_kg_query` | Query knowledge graph entities |
| `mempalace_diary_read` | Read agent diary |
| `mempalace_status` | Palace overview: counts, wings, size |

## Workflow State

Every workflow tool result contains `workflow_state`:

```json
{
  "workflow_state": {
    "current_phase": "claim_acquired",
    "next_phase": "prepare",
    "next_tool": "mempalace_prepare_edit",
    "conflict_status": "none",
    "handoff_pending": false
  }
}
```

**`next_tool`** is the single best next action. After `prepare_edit`, it becomes `"MODEL_ACTION:edit"` — the model makes the edit without any tool call.

## Organization patterns

| Content | Wing | Room |
|---------|------|------|
| Architectural decisions | technical | decisions |
| Code and implementations | technical | code |
| Debates and conclusions | technical | discussions |
| User personal preferences | identity | preferences |
| Memory/session notes | memory | sessions |

## Server not running?

If MemPalace MCP tools return an error, ensure the shared server is running:

**Via plugin (recommended for Claude Code):** The plugin starts `mempalace serve`
automatically on first session. No manual server start needed.

**Manual server:**
```bash
mempalace serve --host 127.0.0.1 --port 8765
```

**Verify health:**
```bash
curl http://127.0.0.1:8765/health
# → {"status": "ok", "service": "mempalace"}
```
