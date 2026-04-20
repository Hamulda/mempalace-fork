---
skill: decision-capture
trigger: when you've made an architectural decision
---

# Decision Capture

When you've made an architectural decision, capture it for future reference.

## Why Capture Decisions?

- Avoid revisiting the same trade-offs
- Onboard future sessions faster
- Track rationale for architectural choices
- Enable superseding when better decisions are made

## When to Capture

Capture decisions when:
- Choosing between multiple implementation approaches
- Making architectural changes (new patterns, framework choices)
- Trade-off decisions with significant trade-offs
- Infrastructure or deployment decisions
- Data model or schema changes

## How to Capture

```python
# Minimal — only decision and rationale are required
mempalace_capture_decision(
  decision="Use JWT instead of sessions for auth",
  rationale="Sessions don't scale across multiple servers. JWT is stateless and works better with horizontal scaling. Affects /src/auth.py token handling and /src/middleware.py."
)

# Full — all optional fields with defaults
mempalace_capture_decision(
  decision="Use JWT instead of sessions for auth",
  rationale="Sessions don't scale across multiple servers.",
  alternatives=["Keep sessions with sticky sessions", "Use cookies with server-side validation"],
  category="architecture",     # default: "general"
  confidence=4,                # default: 3
  # session_id is auto-detected — no need to pass it
)
```

**session_id is auto-detected** from the Claude Code harness context. Pass it explicitly
only when you need to override.

## Include File Context in Rationale

Always include affected file paths in rationale — this makes decisions more useful for takeover:

```
rationale="Changed token refresh from 30min to 24h — affects /src/auth.py (token handling), /src/middleware.py (session validation), and /tests/test_auth.py"
```

This allows `mempalace_list_decisions` to surface decisions relevant to files you're currently editing.

## Categories

| Category | Description |
|----------|-------------|
| `architecture` | System design, patterns, frameworks |
| `api` | API design, protocol choices |
| `testing` | Test strategy, coverage approaches |
| `deployment` | Infrastructure, CI/CD, hosting |
| `data` | Data model, storage choices |
| `general` | Anything not covered above (default) |

## Confidence Scale

| Confidence | Meaning |
|------------|---------|
| 1 | Unsure, might revisit |
| 2 | Leaning toward this choice |
| 3 | Confident, would choose again (default) |
| 4 | Very confident, good evidence |
| 5 | Certain, no other reasonable choice |

## Alternatives Field

Always document alternatives, even if quickly:
- What else was considered?
- Why was the chosen option better?
- What would need to change for you to choose differently?

## Relevant Decisions in Wakeup

When `mempalace_wakeup_context` returns decisions, it filters by file context from your active claims.
If you're editing `/src/auth.py` and there's a decision with `/src/auth.py` in its rationale, it appears in `relevant_decisions`.

## Superseding Decisions

If you later make a better decision that supersedes an old one, use `mempalace_kg_supersede`
for KG facts, or simply capture a new decision with the same topic.

Note: there is no separate `mempalace_supersede_decision` tool — capture a new decision
and let future sessions see both (old and new) via `mempalace_list_decisions`.
