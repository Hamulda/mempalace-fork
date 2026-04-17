---
skill: before-edit
trigger: before claiming a file to edit
---

# Before Edit — Claim Path

Before claiming a file to edit, always run conflict check first.

## Protocol

### Step 1: Conflict Check
```
mempalace_conflict_check(path="/path/to/file.py", session_id="your-session-id")
```

### Step 2: If Conflict Detected
```json
{
  "has_conflict": true,
  "owner": "other-session-id",
  "expires_at": "2026-04-17T10:00:00Z"
}
```

When conflict detected:
- Call `mempalace_claim_path` with a note explaining your intent
- Wait for the claim to expire, or coordinate via handoff
- Never edit a file with an active claim from another session

### Step 3: If No Conflict
```
mempalace_claim_path(path="/path/to/file.py", session_id="your-session-id", ttl_seconds=600, note="fixing bug in auth flow")
```

### Step 4: Log Intent to WriteCoordinator
Use the WriteCoordinator to log your write intent for crash recovery:
```
write_coordinator.log_intent(session_id, operation="edit", target_type="file", target_id="/path/to/file.py")
```

## TTL Guidance

| Edit Type | TTL |
|-----------|-----|
| Quick fix (< 5 min) | 300 seconds |
| Standard edit | 600 seconds |
| Large refactor | 1800 seconds |
| Multi-file change | 3600 seconds |

## Claim Payload

When claiming with a note, include:
- What you're changing (brief description)
- Why you're changing it (bug fix, refactor, feature)
- Expected duration (if known)

Example:
```
mempalace_claim_path(
  path="/src/auth.py",
  session_id="claude-code-123",
  ttl_seconds=600,
  note="fixing session expiry bug — auth flow refactor"
)
```