---
skill: begin-work
trigger: when starting a new editing session on a file
---

# Begin Work — Start an Edit Session

Use `mempalace_begin_work` to start an editing session in one call.

## Single-Tool Workflow

```python
# One call does: conflict_check → claim_path → log_intent
mempalace_begin_work(
    path="/src/auth.py",
    ttl_seconds=600,        # 300=quick fix, 600=standard, 1800=large refactor
    note="fixing session expiry bug"
)
```

Returns:
```json
{
  "ok": true,
  "phase": "begin_work:done",
  "action": "begin_work",
  "path": "/src/auth.py",
  "session_id": "claude-code-123",
  "owner": "claude-code-123",
  "expires_at": "2026-04-20T19:30:00Z",
  "intent_id": "intent_0",
  "next_actions": [
    {"action": "mempalace_prepare_edit", "reason": "Get symbol context before editing", "priority": "high"}
  ],
  "failure_mode": null,
  "context_snippets": {"path": "/src/auth.py", "note": "fixing session expiry bug"}
}
```

## Next Steps

After `begin_work` succeeds:
```python
mempalace_prepare_edit(path="/src/auth.py")
```

## Conflict Handling

If `failure_mode == "claim_conflict"`:
```json
{
  "ok": false,
  "failure_mode": "claim_conflict",
  "reason": "Active claim held by 'other-session-id'",
  "hint": "Wait for TTL expiry (2026-04-20T19:30:00Z) or negotiate via mempalace_push_handoff",
  "details": {
    "owner": "other-session-id",
    "expires_at": "2026-04-20T19:30:00Z",
    "target_id": "/src/auth.py"
  }
}
```

Options:
1. **Wait** for TTL expiry (check `expires_at`)
2. **Negotiate** — `mempalace_push_handoff` to the owner requesting release
3. **Override** — `claim_mode="advisory"` on the subsequent write tool to proceed with warning

## TTL Guidance

| Edit Type | TTL |
|-----------|-----|
| Quick fix (< 5 min) | 300s |
| Standard edit | 600s (default) |
| Large refactor | 1800s |
| Multi-file change | 3600s |

## Failure Modes

| failure_mode | Root Cause | Resolution |
|-------------|-----------|------------|
| `claim_conflict` | Another session holds the claim | Wait for expiry or negotiate handoff |
| `no_coordination` | ClaimsManager unavailable | Run with `shared_server_mode=True` |
| `claim_acquire_failed` | Unexpected state | Retry or check session health |
