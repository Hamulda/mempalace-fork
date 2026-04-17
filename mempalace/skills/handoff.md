---
skill: handoff
trigger: when doing a handoff to another session
---

# Handoff Protocol

When you need to hand off work to another session, use this protocol.

## When to Handoff

- Multi-file refactor affecting multiple wings/rooms
- Architectural change requiring explanation
- Work that needs to continue in a different session
- Context that would be lost if your session ends

## How to Push a Handoff

```
mempalace_push_handoff(
  from_session_id="your-session-id",
  summary="Refactored the auth module to use JWT instead of sessions",
  touched_paths=["src/auth.py", "src/middleware.py", "tests/test_auth.py"],
  blockers=["Need to update API docs for new token format"],
  next_steps=["Update API docs", "Add integration tests for JWT rotation"],
  confidence=4,
  priority="high",
  to_session_id="other-session-id"  # Optional: null for broadcast
)
```

## Handoff Fields

| Field | Description |
|-------|-------------|
| `from_session_id` | Who is handing off |
| `summary` | What was done / what the work is |
| `touched_paths` | Files affected by this work |
| `blockers` | What's preventing completion |
| `next_steps` | What needs to happen next |
| `confidence` | How confident you are (1-5) |
| `priority` | high / normal / low |
| `to_session_id` | Optional — specific session to receive. Null = broadcast |

## Priority Levels

- **high**: Critical, blocking other work, needs immediate attention
- **normal**: Standard handoff, no urgent timeline
- **low**: Low priority, can wait

## Claim Release

Note: Claim release is NOT automatic when you push a handoff.
You must explicitly call `mempalace_release_claim` when done:

```
mempalace_release_claim(path="/src/auth.py", session_id="your-session-id")
```

## Handoff Status Flow

```
pending → accepted → completed
       ↘ cancelled
       ↘ expired (TTL exceeded)
```

- **pending**: Waiting for acceptor
- **accepted**: Another session has taken ownership
- **completed**: Work acknowledged and done
- **cancelled**: Handoff withdrawn by sender
- **expired**: TTL exceeded without acceptance

## After Pushing a Handoff

1. Release any claims you hold on affected files
2. Log the handoff in your diary: `mempalace_diary_write`
3. If blockers remain, document them clearly in the handoff payload