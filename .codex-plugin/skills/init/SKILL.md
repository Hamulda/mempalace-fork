---
name: init
description: Initialize a new MemPalace — guided setup for your AI memory palace with LanceDB backend.
allowed-tools: Bash, Read, Write, Edit
---

# MemPalace Init

Run the following command and follow the returned instructions step by step:

```bash
mempalace instructions init
```

**One-time setup for Claude Code plugin:**
```bash
claude plugin marketplace add hamulda/mempalace-fork   # or milla-jovovich/mempalace
claude plugin install --scope user mempalace
# Restart Claude Code, then verify with: /skills
```

**Manual MCP setup (non-plugin path):**
```bash
claude mcp add mempalace -- python -m mempalace.fastmcp_server
```

**First time?** Run `mempalace init ~/projects/yourproject` to create your palace, then `mempalace mine` to populate it.
