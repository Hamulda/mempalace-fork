# MemPalace Claude Code Plugin

A Claude Code plugin that gives your AI a persistent memory system powered by MemPalace — the local, LanceDB-backed memory palace with session coordination for 6× parallel Claude Code sessions.

## What This Plugin Does

- **Skills**: `/mempalace:help`, `/mempalace:init`, `/mempalace:search`, `/mempalace:mine`, `/mempalace:status`
- **Hooks**: Auto-save on Stop, PreCompact preservation, SessionStart health check
- **No MCP servers started by plugin** — plugin provides UX, MCP tools connect to a shared server

## Prerequisites

- Python 3.9+
- MemPalace installed: `pip install git+https://github.com/Hamulda/mempalace-fork`
- Shared MemPalace MCP server running on `http://127.0.0.1:8765/mcp`

## Installation

```bash
# Install MemPalace Python package first
pip install git+https://github.com/Hamulda/mempalace-fork

# Install the Claude Code plugin
claude plugin marketplace add hamulda/mempalace-fork
claude plugin install --scope user mempalace
```

## Starting the Shared MCP Server

Run **one** shared server for all Claude Code sessions:

```bash
mempalace serve --host 127.0.0.1 --port 8765
```

Or:

```bash
python -m mempalace serve --host 127.0.0.1 --port 8765
```

The server starts on `http://127.0.0.1:8765/mcp` with all session coordinators active
(SessionRegistry, WriteCoordinator, ClaimsManager, HandoffManager, DecisionTracker).

## Verify

```bash
curl http://127.0.0.1:8765/health
# → {"status": "ok", "service": "mempalace"}
```

## Available Slash Commands

| Command | Description |
|---------|-------------|
| `/mempalace:help` | Show tools, skills, and architecture |
| `/mempalace:init` | Install package, initialize palace, configure server |
| `/mempalace:search` | Search memories across the palace |
| `/mempalace:mine` | Mine projects and conversations |
| `/mempalace:status` | Palace overview — wings, rooms, counts |

## Hooks

- **Stop** — Auto-saves conversation context every 15 messages
- **PreCompact** — Preserves memories before context compaction
- **SessionStart** — Verifies MCP server is reachable

Set `MEMPAL_DIR` environment variable to auto-ingest a directory on each save.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Claude Code (session 1..6)                         │
│  /mempalace:search → MCP call → http://localhost:8765│
└─────────────────────────────────────────────────────┘
                     │
                     ▼ (shared HTTP)
┌─────────────────────────────────────────────────────┐
│  mempalace serve (1 process, port 8765)             │
│  ├── SessionRegistry (shared across sessions)       │
│  ├── WriteCoordinator (WAL coalescing)              │
│  ├── ClaimsManager (file-level mutual exclusion)    │
│  ├── HandoffManager (atomic handoff)               │
│  └── DecisionTracker (architectural decisions)       │
└─────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│  LanceDB (~/.mempalace/)                           │
└─────────────────────────────────────────────────────┘
```

## Full Documentation

See the main [README](../README.md).
