---
skill: workflow-guide
trigger: when starting or continuing a Claude Code editing session
---

# Workflow-First Guide for MemPalace MCP

## The One True Path

Claude Code should almost always follow this sequence:

### Editing a File

```
1. mempalace_file_status(path="...")       → orientation
2. mempalace_begin_work(path="...", note="...")  → claim acquired
3. mempalace_prepare_edit(path="...")      → context ready
4. [model makes the edit]                  → no tool call needed
5. mempalace_finish_work(path="...", diary_entry="...")  → done
```

### Multi-File / Handoff

```
1. mempalace_publish_handoff(summary="...", touched_paths=[...])  → atomic publish + release
2. mempalace_diary_write(...)              → log the work
```

### Takeover from Another Session

```
1. mempalace_pull_handoffs()               → find pending handoffs
2. mempalace_takeover_work(handoff_id="...")  → accept + claim paths
3. mempalace_wakeup_context()              → get full context
4. mempalace_prepare_edit(path="...")       → per file
5. [model continues the work]
6. mempalace_publish_handoff(...)          → if more work remains
```

## Why not low-level tools?

The workflow tools (Tier 1) are **compound** — they compress multiple low-level operations into one call AND return structured guidance (`workflow_state`, `next_actions`, `context_snippets`). Low-level tools give you raw materials; workflow tools give you the complete picture.

**Low-level tools are escape-hatches** for when:
- You need fine-grained control over claim/release ordering
- A workflow tool returns an error and you need to diagnose which step failed
- You need to refresh an existing claim TTL only (no conflict check needed)

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

### Tier 2 — Expert / Escape-Hatch (only when Tier 1 won't do)

| Tool | When to use |
|------|-------------|
| `mempalace_claim_path` | Refresh TTL on existing claim you already hold |
| `mempalace_release_claim` | Manual release without diary/decision |
| `mempalace_conflict_check` | Explicit check when workflow tools insufficient |
| `mempalace_push_handoff` | Handoff without atomic claim release |
| `mempalace_pull_handoffs` | List handoffs without accepting |
| `mempalace_accept_handoff` | Accept without auto-claiming paths |
| `mempalace_complete_handoff` | Mark handoff done without publish flow |
| `mempalace_edit_guidance` | Convert any workflow_result → plain guidance |

## The `workflow_state` Field

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

**`current_phase` values:**
- `orienting` — `file_status` result, orienting before claiming
- `claim_acquired` — `begin_work` succeeded, claim is held → call `prepare_edit` next
- `context_ready` — `prepare_edit` succeeded → call `MODEL_ACTION:edit` (make the edit, no tool needed)
- `blocked` — conflict or error, model cannot proceed
- `finished` — `finish_work` succeeded
- `published` — `publish_handoff` succeeded
- `takeover` — `takeover_work` succeeded

**`next_tool`** is the single best next action. After `prepare_edit` succeeds with `current_phase: "context_ready"`, the model should make the edit directly — `next_tool: "MODEL_ACTION:edit"` signals no more tool calls are needed before editing.

**`conflict_status` values:**
- `none` — no conflicts
- `self_claim` — model already holds this claim (refresh)
- `other_claim` — another session holds the claim
- `hotspot` — file changed frequently recently
- `unknown` — coordination unavailable
