---
description: Show MemPalace palace status — wings, rooms, drawer counts, and server health.
allowed-tools: Bash, Read
---

# MemPalace Status

## Check Server Health

```bash
curl http://127.0.0.1:8765/health
# → {"status": "ok", "service": "mempalace"}
```

## Check Palace Status

In Claude Code, use the MCP tool directly:

```
mempalace_status
```

Or from shell:

```bash
mempalace status
```

## What You'll See

- **Wings**: technical, identity, memory, ...
- **Rooms per wing**: decisions, code, preferences, sessions, ...
- **Drawer counts**: total memories stored
- **Session count**: active Claude Code sessions

## Shared Server Architecture

One `mempalace serve` process serves all 6 parallel Claude Code sessions.
Session coordinators ensure safe concurrent access to claims, handoffs, and decisions.
