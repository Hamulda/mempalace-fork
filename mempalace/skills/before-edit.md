---
skill: before-edit
trigger: before claiming a file to edit
---

# Before Edit — Use begin_work

For Claude Code, the single best path is:

```python
mempalace_begin_work(path="/src/auth.py", note="fixing auth bug")
```

Then immediately:
```python
mempalace_prepare_edit(path="/src/auth.py")
```

## Why not the low-level steps?

- `begin_work` combines `conflict_check` + `claim_path` + `log_intent` in one call
- The low-level sequence (`claim_path` then `conflict_check`) risks race conditions
- Workflow tools return structured `workflow_state` with `next_tool` — the model always knows what to do next
- `prepare_edit` auto-checks conflicts before you edit a hot file

## When begin_work succeeds

The result contains:
```json
{
  "ok": true,
  "workflow_state": {
    "current_phase": "claim_acquired",
    "next_phase": "prepare",
    "next_tool": "mempalace_prepare_edit",
    "conflict_status": "none",
    "handoff_pending": false
  },
  "next_actions": [
    {"action": "mempalace_prepare_edit", "priority": "high", "skill": "prepare-edit"}
  ]
}
```

**Next step**: call `mempalace_prepare_edit` to get symbol context and hot-spot info.

## When begin_work fails with claim_conflict

```json
{
  "ok": false,
  "workflow_state": {
    "current_phase": "blocked",
    "next_phase": "negotiate",
    "next_tool": "mempalace_push_handoff",
    "conflict_status": "other_claim"
  }
}
```

**Next step**: use `mempalace_push_handoff` to negotiate with the owner, or wait for the TTL to expire.

## Only use low-level tools when:

- You need to **refresh a claim TTL** on a file you already hold (`mempalace_claim_path` with your existing session_id)
- `begin_work` returned an error and you need to **diagnose** which step failed
- You need fine-grained control over the order of claim/release operations

## TTL Guidance

| Edit Type | TTL |
|-----------|-----|
| Quick fix (< 5 min) | 300s |
| Standard edit | 600s (default) |
| Large refactor | 1800s |
| Multi-file change | 3600s |
