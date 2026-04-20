---
skill: publish-handoff
trigger: when handing off work to another session
---

# Publish Handoff — Hand Off Work to Another Session

Use `mempalace_publish_handoff` to publish a handoff and release all claims in one call.

## Single-Tool Workflow

```python
# Minimal — broadcast to any session
mempalace_publish_handoff(
    summary="Refactored auth module to use JWT instead of sessions"
)

# Directed to specific session
mempalace_publish_handoff(
    summary="Auth refactor in progress",
    touched_paths=["/src/auth.py", "/src/token.py", "/src/middleware.py"],
    blockers=["Need to update API docs for new token format"],
    next_steps=["Update API docs", "Add integration tests for JWT rotation"],
    confidence=4,
    priority="high",
    to_session_id="claude-code-456"  # specific recipient
)
```

Returns:
```json
{
  "ok": true,
  "phase": "publish_handoff:done",
  "action": "publish_handoff",
  "handoff_id": "handoff_1",
  "from_session_id": "claude-code-123",
  "to_session_id": null,
  "summary": "Refactored auth module to use JWT...",
  "touched_paths": ["/src/auth.py", "/src/token.py"],
  "released_claims": [
    {"path": "/src/auth.py", "success": true},
    {"path": "/src/token.py", "success": true}
  ],
  "release_errors": [],
  "next_actions": [
    {"action": "mempalace_diary_write", "reason": "Log the completed work to your diary", "priority": "medium"}
  ]
}
```

## Atomicity Guarantee

**Handoff creation is atomic**: if `push_handoff` fails, no claims are released.
**Claim release is per-path**: if some paths can't be released, they're reported in `release_errors`
but the handoff is still created.

## Broadcast vs Directed

- `to_session_id=None` — **broadcast** (any session can pick up)
- `to_session_id="session-id"` — **directed** (only that session can accept)

## Priority Levels

- `high` — Critical, blocking other work, needs immediate attention
- `normal` — Standard handoff (default)
- `low` — Low priority, can wait

## Failure Modes

| failure_mode | Root Cause | Resolution |
|-------------|-----------|------------|
| `handoff_push_failed` | HandoffManager error | Check HandoffManager health |
| `no_handoff_manager` | HandoffManager unavailable | Run with `shared_server_mode=True` |
| `partial_release` | Some paths not held by this session | Check `release_errors` |

## After Publishing

1. Write diary entry: `mempalace_diary_write`
2. If blockers remain, document them in the handoff payload (already captured)
3. Mark handoff complete when done: `mempalace_complete_handoff`
