---
skill: repo-wakeup
trigger: when waking up in a project with active sessions
---

# Repo Wake-Up

When you wake up in a project with active MemPalace sessions, follow this checklist.

## Step 1: Get Full Wakeup Context

```python
# Full auto-detection — session_id and project_root from environment
mempalace_wakeup_context()
```

This returns:
- Active claims you hold
- Pending handoffs for you
- Recent decisions you made
- Recent repo changes (last 20 files changed)
- Hot spots (files changed most in last 30 days)
- Active symbols from claimed files
- Relevant decisions (linked to your file context)
- Next checks (validation steps based on context)

**session_id and project_root are auto-detected** from Claude Code harness context
and `PROJECT_ROOT` env var respectively. No arguments needed for the common case.

## Step 2: Check Recent Changes

```
mempalace_recent_changes(n=10)
```

This shows:
- Files that changed in recent commits
- Hot spots (most-frequently changed files)
- Languages with recent activity

This is especially useful for understanding what other sessions have been working on.

## Step 3: Check Active Claims

```
mempalace_list_claims()
```

See which files are currently claimed by other sessions.

## Step 4: Pull Pending Handoffs

```
mempalace_pull_handoffs()
```

See what other sessions left for you. **session_id is auto-detected.**

## Step 5: Symbol Context for Active Claims

If you have active claims, understand what's in those files:

```
mempalace_file_symbols(file_path="/path/to/file.py")
```

Or find a specific symbol:

```
mempalace_find_symbol(symbol_name="function_name")
```

## Step 6: Check for Conflicts

```python
# path is required, session_id is auto-detected
mempalace_conflict_check(path="/path/to/file.py")
```

Call on any files you plan to edit.

## Hot Spots in Wakeup Context

The wakeup context now includes `hot_spots` — files changed most in the last 30 days.
If you see a file you need to edit in hot_spots, coordinate carefully to avoid conflicts.

## Session Resume vs Takeover

**Resume**: Your previous session left context. Use `mempalace_wakeup_context` to restore state.

**Takeover**: Another session stopped. Use `mempalace_pull_handoffs` to see what's pending, then accept the handoff to take ownership.
