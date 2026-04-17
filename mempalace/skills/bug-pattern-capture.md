---
skill: bug-pattern-capture
trigger: when you find a recurring bug pattern
---

# Bug Pattern Capture

When you find a recurring bug pattern, document it in the palace for future reference.

## Why Document Bug Patterns?

- Avoid fixing the same bug multiple times
- Help future sessions recognize the pattern
- Document what triggers it, what the symptom is, what fixed it

## How to Document

Use `mempalace_remember_code` with `wing=bug_pattern`:

```
mempalace_remember_code(
  code="""\
def get_session(user_id):
    session = cache.get(user_id)
    if not session:
        session = Session(user_id)
        cache.set(user_id, session, ttl=300)
    return session  # BUG: cache.get returns None on timeout, not Session
""",
  description="Session cache returns None instead of creating new session when TTL expires",
  wing="bug_pattern",
  room="session_cache_bug",
  source_file="/src/session.py",
  added_by="claude-code-123"
)
```

## Bug Pattern Structure

When documenting, include:
- **Trigger**: What causes the bug to appear?
- **Symptom**: What does it look like when it happens?
- **Fix**: What was done to resolve it?
- **Source file**: Where did the bug live?

## Use Wing=bug_pattern

The `bug_pattern` wing is reserved for recurring bug documentation:

```
wing="bug_pattern"
room="<descriptive-name>"
```

Examples:
- `wing=bug_pattern, room=auth_token_expiry`
- `wing=bug_pattern, room=race_condition_db_write`
- `wing=bug_pattern, room=memory_leak_loop`

## Alternative: Use mempalace_kg_query

For structured bug tracking, use the knowledge graph:

```
mempalace_kg_add(
  subject="auth_token_expiry",
  predicate="has_symptom",
  object="Returns None instead of new token",
  source_closet="/src/auth.py:142"
)

mempalace_kg_add(
  subject="auth_token_expiry",
  predicate="has_fix",
  object="Check for None and create new token",
  source_closet="/src/auth.py:145"
)
```

## Bug Pattern Categories

| Pattern | Description |
|---------|-------------|
| `race_condition` | Concurrent access causing wrong state |
| `null_pointer` | Missing None checks |
| `off_by_one` | Index/loop boundary errors |
| `cache_invalidation` | Stale data in cache |
| `async_await` | Missing await causing None |
| `timestamp_timezone` | Timezone handling bugs |

## When to Create a Bug Pattern Entry

- The same bug has appeared 2+ times
- The fix is non-obvious
- Future sessions might make the same mistake
- The bug is in a complex area with context dependencies