---
skill: takeover
trigger: when taking over work from another session
---

# Takeover Protocol

When another session stops and you need to take over their work, follow this protocol.

## Step 1: Pull Pending Handoffs

See what's been left for you:

```python
# With your session ID (directed + broadcast handoffs for you)
mempalace_pull_handoffs()

# Without session ID — pulls all broadcast handoffs (anyone can pick up)
mempalace_pull_handoffs(session_id=None)
```

**session_id is auto-detected** — no need to pass it explicitly unless you want
to pull for a specific session context.

## Step 2: Get Full Wakeup Context

For complete picture including recent changes and hot spots:

```python
# All arguments are optional — session_id and project_root are auto-detected
mempalace_wakeup_context()

# Or with explicit overrides
mempalace_wakeup_context(
  session_id="your-session-id",
  project_root="/path/to/project"
)
```

This returns:
- Active claims you hold
- Pending handoffs for you
- Recent decisions you made
- Recent repo changes (files changed recently)
- Hot spots (most-frequently changed files)
- Active symbols from claimed files
- Relevant decisions (linked to your file context)
- Session info from registry
- Next checks (validation steps)

## Step 3: Check Recent Changes Impact

Before diving in, understand what's changed recently:

```
mempalace_recent_changes(n=10)
```

This tells you which files have been active — helpful for understanding context.

## Step 4: Accept the Handoff

When you decide to take over work from a handoff:

```python
# session_id auto-detected — just pass the handoff_id
mempalace_accept_handoff(handoff_id="handoff-uuid")
```

## Step 5: Symbol Context (if handoff mentions specific files)

If the handoff mentions specific files, get symbol context:

```
mempalace_file_symbols(file_path="/path/to/handoff-file.py")
mempalace_find_symbol(symbol_name="function_in_handoff_file")
```

## Step 6: Check Claims of Other Session

If no handoff was pushed but you see claims from another session:

```
mempalace_list_claims()
```

Check `expires_at` on their claims to know when they'll expire.

## Hot Spot Awareness

The wakeup context includes `hot_spots` — files that have changed frequently recently.
If the takeover involves a hot spot file, be extra careful about concurrent edits.

## After Takeover

1. Claim the files you need with `mempalace_claim_path`
2. Complete the `next_steps` from the handoff
3. When done, push a handoff if there's more to do
4. Mark the original handoff as complete with `mempalace_complete_handoff`
