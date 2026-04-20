---
skill: takeover-work
trigger: when taking over work from another session via handoff
---

# Takeover Work — Accept a Handoff and Claim the Relevant Paths

Use `mempalace_takeover_work` to accept a handoff and claim the relevant paths in one call.

## Single-Tool Workflow

```python
# Take over a specific handoff and claim its touched paths
mempalace_takeover_work(
    handoff_id="handoff_42",
    paths_to_claim=["/src/auth.py", "/src/token.py"],
    ttl_seconds=600
)

# Take over without specifying paths (uses handoff's touched_paths)
mempalace_takeover_work(
    handoff_id="handoff_42",
    ttl_seconds=600
)
```

Returns:
```json
{
  "ok": true,
  "phase": "takeover_work:done",
  "action": "takeover_work",
  "handoff_id": "handoff_42",
  "session_id": "claude-code-123",
  "handoff_accepted": true,
  "claimed_paths": [
    {"path": "/src/auth.py", "acquired": true, "owner": "claude-code-123"},
    {"path": "/src/token.py", "acquired": true, "owner": "claude-code-123"}
  ],
  "claim_errors": [],
  "all_claims_acquired": true,
  "next_actions": [
    {"action": "mempalace_wakeup_context", "reason": "Get full context for the takeover", "priority": "high"},
    {"action": "mempalace_prepare_edit", "reason": "Get symbol context for each claimed path", "priority": "high"}
  ],
  "context_snippets": {
    "handoff_summary": "Refactored auth module to use JWT..."
  }
}
```

## Finding a Handoff to Take Over

```python
# See all pending broadcast handoffs
mempalace_pull_handoffs()

# See directed handoffs for your session
mempalace_pull_handoffs(session_id="your-session-id")
```

## Partial Takeover

If some paths are blocked by other claims:
```json
{
  "ok": true,
  "all_claims_acquired": false,
  "claimed_paths": [
    {"path": "/src/auth.py", "acquired": true, "owner": "claude-code-123"}
  ],
  "claim_errors": [
    {
      "path": "/src/token.py",
      "error": "claim_conflict",
      "owner": "claude-code-789"
    }
  ]
}
```

You can still proceed — the handoff is accepted even if some paths are blocked.
Wait for the conflicting claims to expire, then claim manually.

## Next Actions After Takeover

1. `mempalace_wakeup_context` — get full session context
2. `mempalace_prepare_edit` for each claimed path — understand the code
3. Continue the `next_steps` from the original handoff
4. When done: `mempalace_publish_handoff` with updated status

## Failure Modes

| failure_mode | Root Cause | Resolution |
|-------------|-----------|------------|
| `handoff_accept_failed` | Bad handoff_id or not the intended recipient | Verify handoff exists and you're the target |
| `no_coordination` | ClaimsManager unavailable | Run with `shared_server_mode=True` |
| `claim_conflict` | Some paths blocked by other claims | Wait for expiry, then claim manually |

## Takeover Protocol Summary

```
1. mempalace_pull_handoffs()          ← find what's available
2. mempalace_takeover_work(handoff_id, paths_to_claim)
3. mempalace_wakeup_context()         ← full picture
4. mempalace_prepare_edit(path)       ← per file
5. [edit files]
6. mempalace_publish_handoff(...)     ← if more work remains
7. mempalace_complete_handoff(handoff_id)
```
