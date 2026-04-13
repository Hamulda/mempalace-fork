---
name: mempalace
description: MemPalace persistent memory — mine, search, and recall context across sessions.
allowed-tools: Read
---

# MemPalace

Searchable memory palace for AI sessions. Mine projects, store decisions, recall context.

## Auto-trigger rules

**Call `mempalace_search` without being asked when:**
- User asks about past decisions: "what did we decide", "jak jsme to implementovali"
- User mentions a project or technology they may have worked on before
- User says "I don't remember", "zapomněl jsem", "kde jsme přestali"

**Call `mempalace_add_drawer` without being asked when:**
- User says "remember this", "ulož to", "poznamenej si"
- Key architectural decision just made
- Session exceeds 20 exchanges with code or design content

## Tool reference

| Tool | When to use | Key params |
|------|-------------|------------|
| `mempalace_search` | Semantic search across all memories | `query`, `limit=5`, `wing`, `room` |
| `mempalace_add_drawer` | Store a single memory manually | `content`, `wing`, `room` |
| `mempalace_check_duplicate` | Check if content already exists before storing | `content`, `threshold=0.9` |
| `mempalace_status` | Palace overview: counts, wings, size | — |
| `mempalace_list_wings` | List all wings | — |
| `mempalace_list_rooms` | List rooms, optionally filtered by wing | `wing=None` |
| `mempalace_kg_query` | Query knowledge graph entities | `query` |
| `mempalace_kg_stats` | Knowledge graph statistics | — |
| `mempalace_diary_write` | Write to agent diary | `content`, `agent_name` |
| `mempalace_diary_read` | Read agent diary | `agent_name`, `last_n=10` |
| `mempalace_export_claude_md` | Export palace as Claude.md | `project_path`, `output_dir` |

## Organization patterns

| Content | Wing | Room |
|---------|------|------|
| Architectural decisions | technical | decisions |
| Code and implementations | technical | code |
| Debates and conclusions | technical | discussions |
| User personal preferences | identity | preferences |
| Memory/session notes | memory | sessions |

## Server not running?

If MemPalace MCP tools return an error, start the server:
<!-- Bash permitted only for server startup -->
```bash
python3 -m mempalace.fastmcp_server
```
Server URL: `http://127.0.0.1:8765/mcp`
