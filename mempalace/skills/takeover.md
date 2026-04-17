---
skill: takeover
trigger: when taking over work from another session
---

# Takeover Protocol

When another session stops and you need to take over their work, follow this protocol.

## Step 1: Pull Pending Handoffs
See what's been left for you:

```
mempalace_pull_handoffs(session_id="your-session-id")
```

Or for broadcast handoffs (no specific recipient):

```
mempalace_pull_handoffs(session_id=None)
```

## Step 2: Get Full Wakeup Context
For complete picture:

```
mempalace_wakeup_context(session_id="your-session-id", project_root="/path/to/project")
```

This returns:
- Active claims you hold
- Pending handoffs for you
- Recent decisions you made
- Any conflicts from expired claims
- Session info from registry
- Recommended tools based on context

## Step 3: Accept the Handoff
When you decide to take over work from a handoff:

```
mempalace_accept_handoff(handoff_id="handoff-uuid", session_id="your-session-id")
```

This marks the handoff as accepted and updates the status.

## Step 4: Check Claims of Other Session
If no handoff was pushed but you see claims from another session:

```
mempalace_list_claims()
```

Check `expires_at` on their claims to know when they'll expire.

## Step 5: Plan Next Steps from Blockers
Review the `blockers` and `next_steps` fields in the handoff:

```
blockers: ["Need to update API docs for new token format"]
next_steps: ["Update API docs", "Add integration tests for JWT rotation"]
```

## Claim Expiration Behavior

Claims have TTL (default 600 seconds / 10 minutes):
- If the other session's claims expire, you can claim those files
- Before editing, always run `mempalace_conflict_check`
- Claims do NOT auto-renew — they expire and become available

## Conflict Resolution

If you and another session want the same file:

1. Check `mempalace_conflict_check` to see who holds it
2. If the other session has a claim:
   - Wait for it to expire, or
   - Push a handoff to coordinate
3. If no active claim but there was recent conflict:
   - Use `mempalace_claim_path` with note to negotiate

## After Takeover

1. Claim the files you need with `mempalace_claim_path`
2. Complete the `next_steps` from the handoff
3. When done, push a handoff if there's more to do
4. Mark the original handoff as complete with `mempalace_complete_handoff`