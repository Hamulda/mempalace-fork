---
skill: repo-wakeup
trigger: when waking up in a project with active sessions
---

# Repo Wake-Up

When you wake up in a project with active MemPalace sessions, follow this checklist:

## Step 1: Check Active Claims
Call `mempalace_list_claims` to see which files are currently claimed by other sessions.

```
mempalace_list_claims
```

## Step 2: Pull Pending Handoffs
Call `mempalace_pull_handoffs` for your session to see what other sessions left for you.

```
mempalace_pull_handoffs(session_id="your-session-id")
```

## Step 3: Check for Conflicts
Call `mempalace_conflict_check` on any files you plan to edit.

```
mempalace_conflict_check(path="/path/to/file.py", session_id="your-session-id")
```

## Step 4: Get Full Wakeup Context
For complete context bundle including active claims, pending handoffs, recent decisions:

```
mempalace_wakeup_context(session_id="your-session-id", project_root="/path/to/project")
```

## Step 5: Load Recent Decisions
Check what architectural decisions were made recently:

```
mempalace_list_decisions(session_id="your-session-id", category="architecture")
```

## After Loading Context

- If there are pending handoffs: review the `blockers` and `next_steps` fields
- If there are active claims by others: wait or coordinate via handoff
- If you have claims from a previous session: check if they're still valid
- If conflicts exist: use `mempalace_claim_path` with a note to negotiate ownership

## Session Resume vs Takeover

**Resume**: Your previous session left context. Use `mempalace_wakeup_context` to restore state.

**Takeover**: Another session stopped. Use `mempalace_pull_handoffs` to see what's pending, then accept the handoff to take ownership.