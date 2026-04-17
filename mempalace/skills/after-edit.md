---
skill: after-edit
trigger: after completing an edit
---

# After Edit — Release and Log

After completing an edit, follow this protocol:

## Step 1: Release the Claim
When done editing, release the claim:

```
mempalace_release_claim(path="/path/to/file.py", session_id="your-session-id")
```

Note: Claims do NOT auto-expire at TTL — you must release explicitly.

## Step 2: Log to Diary
Record what was changed:

```
mempalace_diary_write(
  agent_name="YourName",
  entry="Fixed session expiry bug in auth.py. Changed token refresh from 30min to 24h.",
  topic="bug-fix"
)
```

## Step 3: Check Architectural Decisions
If the change was architectural (not just a bug fix), capture the decision:

```
mempalace_capture_decision(
  session_id="your-session-id",
  decision="Changed token refresh from 30min to 24h for security",
  rationale="Shorter refresh windows reduce token theft window",
  alternatives=["Keep 30min with rotation", "Use hardware keys"],
  category="security",
  confidence=4
)
```

## Step 4: Push Handoff if Scope > Single File
If the change affects multiple files or requires context for the next session:

```
mempalace_push_handoff(
  from_session_id="your-session-id",
  summary="Refactored auth module — moved token handling to separate class",
  touched_paths=["src/auth.py", "src/token.py", "tests/test_auth.py"],
  blockers=[],
  next_steps=["Update API docs", "Add integration tests for token rotation"],
  confidence=4,
  priority="normal"
)
```

## When NOT to Push Handoff
- Single file, isolated change
- Bug fix with no context needed
- Trivial refactor (renaming, formatting)

## When TO Push Handoff
- Multi-file refactor
- Architectural change requiring explanation
- Work that another session should continue
- Knowledge that would be lost if session dies