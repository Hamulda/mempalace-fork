# MemPalace MCP Setup

## Quick Start

**Install + start in one line:**
```bash
pip install mempalace && python -m mempalace.fastmcp_server &
```

**Verify it's running:**
```bash
curl http://127.0.0.1:8765/health
# → {"status": "ok", "service": "mempalace"}
```

## Two Ways to Connect

### Option A — Claude Code Plugin (recommended)

```bash
# Install once
claude plugin marketplace add hamulda/mempalace-fork  # or your fork URL
claude plugin install --scope user mempalace

# Restart Claude Code — MemPalace tools appear automatically
```

The plugin path is recommended because it handles MCP registration persistently and does not require manual `claude mcp add` commands.

### Option B — Manual MCP registration

```bash
claude mcp add mempalace -- python -m mempalace.fastmcp_server
```

Works for any MCP-compatible tool (Cursor, Continue, Zed, etc.).

## Session Coordination (multi-session)

When running MemPalace for 6× parallel Claude Code sessions, the server runs in **shared mode** with session coordinators active:

- **SessionRegistry** — tracks active sessions, prevents split-brain
- **WriteCoordinator** — coalesces concurrent writes via WAL
- **ClaimsManager** — enforces file-level mutual exclusion
- **HandoffManager** — atomic handoff between sessions
- **DecisionTracker** — captures architectural decisions

These are auto-activated when you use `mempalace serve` (CLI) or `python -m mempalace.fastmcp_server` (HTTP transport). Stdio mode (default for development) does not activate coordinators.

## Backend

Default backend is **LanceDB** (local, zero API calls). ChromaDB is available via `MEMPALACE_BACKEND=chroma` but LanceDB is recommended for reliability.

## Troubleshooting

**Port 8765 already in use:**
```bash
pkill -f mempalace.fastmcp_server
python -m mempalace.fastmcp_server &
```

**Palace not found:**
```bash
mempalace init ~/projects/myapp
python -m mempalace.fastmcp_server
```

**MCP tools not appearing in Claude Code:** Restart Claude Code after installing the plugin or adding the MCP server.
