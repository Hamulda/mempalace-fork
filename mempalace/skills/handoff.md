---
skill: handoff
trigger: when doing a handoff to another session
---

# Handoff Protocol

## When to Use Which

**Primary (use almost always):**
- `mempalace_publish_handoff` — atomic handoff + release all claims in one call

**Expert (only when primary won't do):**
- `mempalace_push_handoff` — create handoff without releasing claims
- `mempalace_release_claim` — release individual claims separately
- `mempalace_complete_handoff` — mark handoff done without publish flow

**Why prefer publish_handoff?**
- Atomic: claims released ONLY if handoff is created
- Simpler: one call instead of two
- Structured: returns `workflow_state` with `next_tool = mempalace_diary_write`

## When to Handoff

- Multi-file refactor affecting multiple wings/rooms
- Architectural change requiring explanation
- Work that needs to continue in a different session
- Context that would be lost if your session ends

## How to Push a Handoff

```python
# Minimal call — only summary is required, everything else has a sensible default
mempalace_push_handoff(
  summary="Refactored the auth module to use JWT instead of sessions"
)

# With more detail (all fields are optional)
mempalace_push_handoff(
  summary="Refactored the auth module to use JWT instead of sessions",
  touched_paths=["src/auth.py", "src/middleware.py", "tests/test_auth.py"],
  blockers=["Need to update API docs for new token format"],
  next_steps=["Update API docs", "Add integration tests for JWT rotation"],
  confidence=5,          # 1-5, default is 3; only override for high/low confidence
  priority="high",        # "high", "normal", or "low"; default is "normal"
  to_session_id=None      # None = broadcast (any session can accept), or specific session ID
)
```

**session_id is auto-detected** — `from_session_id` is no longer required. The tool
automatically resolves it from the Claude Code session context. Pass it explicitly
only when you need to override (e.g., impersonating another session).

## Handoff Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `summary` | **Yes** | — | What was done / what the work is |
| `from_session_id` | No | auto-detected | Who is handing off |
| `touched_paths` | No | `[]` | Files affected by this work |
| `blockers` | No | `[]` | What's preventing completion |
| `next_steps` | No | `[]` | What needs to happen next |
| `confidence` | No | `3` | How confident you are (1-5) |
| `priority` | No | `"normal"` | high / normal / low |
| `to_session_id` | No | `None` | Specific recipient, or None=broadcast |

## Priority Levels

- **high**: Critical, blocking other work, needs immediate attention
- **normal**: Standard handoff, no urgent timeline (default)
- **low**: Low priority, can wait

## Claim Release

Note: Claim release is NOT automatic when you push a handoff.
You must explicitly call `mempalace_release_claim` when done:

```
mempalace_release_claim(path="/src/auth.py")
```

session_id is auto-detected here too — no need to pass it explicitly.

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
