---
description: Set up MemPalace — install package, start shared server, initialize palace, verify.
allowed-tools: Bash, Read
---

# MemPalace Init — Canonical Setup

Follow these steps in order.

## Step 1 — Install Python Package

```bash
pip install git+https://github.com/Hamulda/mempalace-fork
```

## Step 2 — Start Shared MCP Server

Run **one** server for all 6 parallel Claude Code sessions:

```bash
mempalace serve --host 127.0.0.1 --port 8765
```

Default port is 8765. The server runs in shared mode with session coordinators active.

## Step 3 — Initialize Your Palace

```bash
mempalace init ~/palace
```

Choose your wings and rooms. Or let MemPalace auto-detect from folder structure.

## Step 4 — Verify

```bash
curl http://127.0.0.1:8765/health
# → {"status": "ok", "service": "mempalace"}
```

Then in Claude Code:
```
/mempalace:status
```

## Troubleshooting

**fastmcp not found**: `pip install git+https://github.com/Hamulda/mempalace-fork`
**Server not running**: `mempalace serve --host 127.0.0.1 --port 8765`
**Wrong port**: Change port in `.claude-plugin/.mcp.json` to match server port
