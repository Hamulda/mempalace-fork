---
description: Search MemPalace memories — semantic search with wing/room filtering.
allowed-tools: Bash, Read
---

# MemPalace Search

## MCP Tool (Primary)

Use `mempalace_search` directly in Claude Code:

```
mempalace_search(query="your question", wing="technical", room="decisions")
```

Omit wing/room for full palace search.

## CLI

```bash
mempalace search "why did we switch to GraphQL"
```

## Organization

| Content | Wing | Room |
|---------|------|------|
| Architectural decisions | technical | decisions |
| Code and implementations | technical | code |
| Debates and conclusions | technical | discussions |
| User preferences | identity | preferences |

## Workflow Integration

Search before starting new work:
```
mempalace_file_status  → check what's claimed
mempalace_search       → check past decisions
mempalace_begin_work   → start session
```
