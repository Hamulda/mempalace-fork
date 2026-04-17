---
skill: conflict-check
trigger: when detecting and resolving session collisions
---

# Conflict Check — Detect and Resolve Session Collisions

When multiple sessions might edit the same file, use this protocol.

## Types of Conflicts

### Path-Level Conflict
Two sessions claim the same file path.

```
mempalace_conflict_check(path="/src/auth.py", session_id="your-session-id")
```

Response when conflict:
```json
{
  "has_conflict": true,
  "owner": "other-session-id",
  "expires_at": "2026-04-17T10:00:00Z"
}
```

### Wing-Level Conflict
Two sessions working in the same wing (e.g., both in `wing_code`) without coordination.

### Session Registry Conflict
Session A's claimed_paths overlaps with Session B's claimed_paths.

## Resolution Strategies

### Strategy 1: Wait for Expiry
If the conflict is a claim with TTL:
- Note the `expires_at` timestamp
- Wait for it to expire
- Then claim with `mempalace_claim_path`

### Strategy 2: Negotiate via Handoff
If you need the file now:
1. Push a handoff to the other session: `mempalace_push_handoff`
2. Ask them to release the claim
3. Wait for them to complete or cancel

### Strategy 3: Take Over
If the other session is unresponsive:
1. Accept any pending handoff they left
2. Wait for their claims to expire
3. Claim the files

### Strategy 4: Work Around
If possible, work on a different file or wing while waiting.

## List All Active Claims

```
mempalace_list_claims()
```

Returns all active claims across all sessions:
```json
{
  "claims": [
    {
      "session_id": "session-abc",
      "target_type": "file",
      "target_id": "/src/auth.py",
      "expires_at": "2026-04-17T10:00:00Z"
    }
  ]
}
```

## Always Check Before Editing

Before any edit, run:
```
mempalace_conflict_check(path="/path/to/file.py", session_id="your-session-id")
```

If `has_conflict: true`, do NOT edit the file until:
- The conflict is resolved, OR
- The owner's claim expires

## Claim TTL Defaults

| Edit Type | TTL |
|-----------|-----|
| Quick fix | 300s |
| Standard edit | 600s |
| Large refactor | 1800s |
| Multi-file | 3600s |

If you're doing a large refactor, use a longer TTL to avoid conflicts during the work.