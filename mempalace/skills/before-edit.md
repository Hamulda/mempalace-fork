---
skill: before-edit
trigger: before claiming a file to edit
---

# Before Edit — Claim Path

Before claiming a file to edit, follow this protocol:

## Step 1: Check Recent Changes (if known)

If you know the file path, check if it's a hot spot:
```
mempalace_recent_changes(project_root="/path", n=5)
```
This tells you if the file has been changing frequently.

## Step 2: Symbol Lookup (optional but recommended)

If editing a function or class, find its current location and signature:
```
mempalace_find_symbol(symbol_name="my_function")
mempalace_file_symbols(file_path="/path/to/file.py")
```

This helps you understand the current scope before making changes.

## Step 3: Conflict Check

Check for active claims before editing:
```
mempalace_conflict_check(path="/path/to/file.py", session_id="your-session-id")
```

### If Conflict Detected
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

### If No Conflict
```
mempalace_claim_path(path="/path/to/file.py", session_id="your-session-id", ttl_seconds=600, note="fixing bug in auth flow")
```

## Step 4: Log Intent to WriteCoordinator

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

## Wakeup Context Enrichment

After claiming, your session's wakeup context will include:
- active_claims (what you're holding)
- active_symbols (symbols in the claimed files)
- next_checks (validation steps)