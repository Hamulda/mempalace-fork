---
skill: after-edit
trigger: after completing an edit
---

# After Edit — Use finish_work or publish_handoff

For single-file changes:
```python
mempalace_finish_work(
    path="/src/auth.py",
    diary_entry="Fixed session expiry bug. Changed token refresh from 30min to 24h.",
    topic="bug-fix",
    agent_name="Claude"
)
```

For multi-file changes or when handing off to another session:
```python
mempalace_publish_handoff(
    summary="Refactored auth module to use JWT instead of sessions",
    touched_paths=["/src/auth.py", "/src/token.py", "/src/middleware.py"],
    blockers=["Need to update API docs for new token format"],
    next_steps=["Update API docs", "Add integration tests for JWT rotation"],
    priority="high"
)
```

## Why not the low-level steps?

- `finish_work` **writes the diary immediately** — no separate `mempalace_diary_write` call needed
- `publish_handoff` is **atomic** — claims are released ONLY if the handoff is created successfully
- The low-level sequence (`release_claim` then `diary_write`) leaves orphaned claims if the diary write fails
- Both workflow tools return structured `workflow_state` with `next_tool` and `handoff_pending`

## When finish_work succeeds

```json
{
  "ok": true,
  "workflow_state": {
    "current_phase": "finished",
    "next_phase": null,
    "next_tool": null,
    "conflict_status": "none",
    "handoff_pending": false
  },
  "claim_released": true,
  "diary_id": "diary_wing_claude_20260420_193000_abc123"
}
```

**Diary is written immediately** — no follow-up call needed. The `diary_id` is returned in the result.

## When publish_handoff succeeds

```json
{
  "ok": true,
  "workflow_state": {
    "current_phase": "published",
    "next_phase": null,
    "next_tool": "mempalace_diary_write",
    "handoff_pending": false
  },
  "handoff_id": "handoff_42",
  "released_claims": [...]
}
```

**Next step**: write a diary entry with `mempalace_diary_write` to log the completed work.

## Only use low-level tools when:

- You need to **release without diary or decision capture** (use `mempalace_release_claim` directly)
- You want **manual control over claim release ordering** for multi-file changes
- You need to **diagnose** which step in the workflow failed
