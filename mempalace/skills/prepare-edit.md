---
skill: prepare-edit
trigger: before making edits on a file (especially hot or complex files)
---

# Prepare Edit — Get Context Before Editing

Use `mempalace_prepare_edit` to get symbol context and recent change info before editing.

## Single-Tool Workflow

```python
# One call does: file_symbols + recent_changes + hot_spot detection
mempalace_prepare_edit(path="/src/auth.py")
```

Returns:
```json
{
  "ok": true,
  "phase": "prepare_edit:done",
  "action": "prepare_edit",
  "path": "/src/auth.py",
  "session_id": "claude-code-123",
  "symbols_count": 4,
  "hotspot": true,
  "next_actions": [
    {"action": "mempalace_conflict_check", "reason": "Hot file changed 5x recently", "priority": "high"},
    {"action": "mempalace_search", "reason": "Search /src/auth.py content before editing", "priority": "medium"}
  ],
  "context_snippets": {
    "symbols": [
      {"name": "authenticate_user", "type": "function", "line": 10},
      {"name": "TokenCache", "type": "class", "line": 25}
    ],
    "recent_change": {
      "file_path": "/src/auth.py",
      "change_count": 5,
      "last_modified": "2026-04-20"
    }
  }
}
```

## When to Use

**Always call before editing** on:
- Files with 3+ recent changes (hot spots)
- Files with complex symbol structure (many classes/functions)
- Files you're taking over from another session
- Multi-file refactors

## Hot Spot Handling

If `hotspot: true`:
- Run `mempalace_conflict_check` before editing
- Consider using shorter TTL (300s) for quick fixes
- Coordinate with other sessions if same file appears in their wakeup context

## Next Actions

The `next_actions` list tells you what to do next:
- `mempalace_search` — understand existing content
- `mempalace_conflict_check` — verify no concurrent edit (hot files only)
- `mempalace_file_symbols` — get full symbol list for complex files

## Failure Modes

| failure_mode | Root Cause | Resolution |
|-------------|-----------|------------|
| `symbol_index_unavailable` | SymbolIndex not built | Run `mempalace mine` first |
| `project_not_git` | No git history | Skip hot_spot check, use symbols only |
