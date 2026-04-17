---
skill: decision-capture
trigger: when you've made an architectural decision
---

# Decision Capture

When you've made an architectural decision, capture it for future reference.

## Why Capture Decisions?

- Avoid revisiting the same权衡
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

```
mempalace_capture_decision(
  session_id="your-session-id",
  decision="Use JWT instead of sessions for auth",
  rationale="Sessions don't scale across multiple servers. JWT is stateless and works better with horizontal scaling.",
  alternatives=["Keep sessions with sticky sessions", "Use cookies with server-side validation"],
  category="architecture",
  confidence=4
)
```

## Categories

| Category | Description |
|----------|-------------|
| `architecture` | System design, patterns, frameworks |
| `api` | API design, protocol choices |
| `testing` | Test strategy, coverage approaches |
| `deployment` | Infrastructure, CI/CD, hosting |
| `data` | Data model, storage choices |
| `other` | Anything not covered above |

## Confidence Scale

| Confidence | Meaning |
|------------|---------|
| 1 | Unsure, might revisit |
| 2 | Leaning toward this choice |
| 3 | Confident, would choose again |
| 4 | Very confident, good evidence |
| 5 | Certain, no other reasonable choice |

## Alternatives Field

Always document alternatives, even if quickly:
- What else was considered?
- Why was the chosen option better?
- What would need to change for you to choose differently?

## Superseding Decisions

If you later make a better decision that supersedes an old one:

```
mempalace_supersede_decision(
  decision_id="old-decision-uuid",
  superseding_decision_id="new-decision-uuid",
  session_id="your-session-id"
)
```

This marks the old decision as superseded and links it to the new one.