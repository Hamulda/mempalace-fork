---
skill: finish-work
trigger: after completing an edit session
---

# Finish Work — Wrap Up an Edit Session

Use `mempalace_finish_work` to release the claim, optionally log to diary, and capture architectural decisions.

## Single-Tool Workflow

```python
# Minimal — just release the claim
mempalace_finish_work(path="/src/auth.py")

# With diary logging
mempalace_finish_work(
    path="/src/auth.py",
    diary_entry="Fixed session expiry bug. Changed token refresh from 30min to 24h.",
    topic="bug-fix",
    agent_name="Claude"
)

# With architectural decision
mempalace_finish_work(
    path="/src/auth.py",
    capture_decision="Use JWT instead of sessions for auth",
    rationale="Stateless tokens reduce auth server load and improve scalability",
    decision_category="security",
    decision_confidence=4
)
```

Returns:
```json
{
  "ok": true,
  "phase": "finish_work:done",
  "action": "finish_work",
  "path": "/src/auth.py",
  "session_id": "claude-code-123",
  "claim_released": true,
  "diary_id": "diary_wing_claude_20260420_193000_abc123",
  "decision_id": "decision_1",
  "operations": [
    "claim_released:True",
    "diary_ready:diary_wing_claude_20260420_193000_abc123"
  ],
  "errors": [],
  "next_actions": [
    {"action": "mempalace_diary_write", "reason": "Write the diary entry prepared above", "priority": "high"}
  ],
  "context_snippets": {
    "diary_entry": "Fixed session expiry bug...",
    "decision": "Use JWT instead of sessions"
  }
}
```

## Diary vs Decision Capture

**Diary**: Log what was done (bug fixes, feature implementations, incremental changes)
- `diary_entry`: what happened
- `topic`: bug-fix, feature, refactor, general

**Decision capture**: Persist architectural rationale (why you chose one approach over another)
- `capture_decision`: the decision text
- `rationale`: why this decision was made
- `decision_category`: security, architecture, performance, api, other
- `decision_confidence`: 1-5

## Next Actions After finish_work

- **Multi-file changes**: `mempalace_publish_handoff` with all touched paths
- **Architectural change**: `mempalace_capture_decision` (if not done in finish_work)
- **Context for next session**: `mempalace_diary_write`

## Failure Modes

| failure_mode | Root Cause | Resolution |
|-------------|-----------|------------|
| `no_coordination` | ClaimsManager unavailable | Run with `shared_server_mode=True` |
| `no_decision_tracker` | DecisionTracker unavailable | Skip decision capture |
| `release_failed` | Claim not held by this session | Verify session still holds the claim |
