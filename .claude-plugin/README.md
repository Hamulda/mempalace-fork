# MemPalace Claude Code Plugin

A Claude Code plugin that gives your AI a persistent memory system powered by MemPalace — the local, LanceDB-backed memory palace with session coordination for up to 6× parallel Claude Code sessions.

## What This Plugin Does

- **Skills**: `/mempalace:help`, `/mempalace:init`, `/mempalace:search`, `/mempalace:mine`, `/mempalace:status`
- **Hooks** (optional, requires manual registration in `settings.json`): Auto-save on Stop, PreCompact preservation, SessionStart lifecycle control
- **No MCP servers started by plugin** — plugin provides skills + hooks, MCP tools connect to a shared HTTP server managed by the plugin

## Prerequisites

- Python 3.14 (the target version; 3.9+ is the minimum)
- MemPalace installed: `pip install git+https://github.com/Hamulda/mempalace-fork`
- Shared MemPalace MCP server (start manually with `mempalace serve --host 127.0.0.1 --port 8765`, or enable auto-start by registering the hooks in `settings.json` — see Hooks section below)

## Installation

```bash
# Install MemPalace Python package first
pip install git+https://github.com/Hamulda/mempalace-fork

# Install the Claude Code plugin
claude plugin marketplace add hamulda/mempalace-fork
claude plugin install --scope user mempalace
```

## Server Lifecycle (Automatic — Recommended, Requires Hook Registration)

The plugin can manage a **single shared HTTP server** across all Claude Code sessions using session refcounting — but **only if hooks are registered in `settings.json`** (see below). Without registration, run `mempalace serve` manually.

```
SessionStart (hook — requires settings.json registration)
  → registers session ID
  → starts server if not already running
  → calls mempalace hook run (session-start inject)

Stop (hook)
  → calls mempalace hook run (auto-save while server alive)
  → unregisters session ID
  → if zero sessions remain → graceful server shutdown after ~20s grace period
```

Runtime state is kept in `~/.mempalace/runtime/`:
- `server.pid` — current server process ID
- `sessions/<session_id>` — session refcount files (TTL: 6 hours)
- `control.lock` — flock-based mutual exclusion for all mutations

The server binds to `http://127.0.0.1:8765` and is shared by all Claude Code sessions on the machine. Opening a new session while another is open reuses the same server. Closing one session does not shut down the server. Closing the last session triggers graceful shutdown after a 20-second grace period to avoid restart churn.

### Crash / Stale Session Recovery

Session files have a 6-hour TTL (configurable via `MEMPALACE_SESSION_TTL_SECONDS`). If Claude Code crashes without running the Stop hook, the stale session file expires automatically and does not prevent server shutdown.

## Manual Server Mode (Optional)

If you need a persistent server for debugging or external MCP clients (non-Claude-Code):

```bash
mempalace serve --host 127.0.0.1 --port 8765
```

This bypasses the plugin's session-aware lifecycle. The plugin hooks still run normally, but the server is already running so SessionStart registers the session without spawning a new process.

## Verify

```bash
curl http://127.0.0.1:8765/health
# → {"status": "ok", "service": "mempalace"}
```

Then in Claude Code: `/mempalace:status`

## Available Slash Commands

| Command | Description |
|---------|-------------|
| `/mempalace:help` | Show tools, skills, and architecture |
| `/mempalace:init` | Install package, initialize palace, configure server |
| `/mempalace:search` | Search memories across the palace |
| `/mempalace:mine` | Mine projects and conversations |
| `/mempalace:status` | Palace overview — wings, rooms, counts |
| `/mempalace:doctor` | Diagnose server health — health endpoint, process, sessions |

## Hooks

- **SessionStart** — Registers session, starts shared server if needed, injects relevant memories
- **Stop** — Runs auto-save (while server is alive), unregisters session, may shut down server
- **PreCompact** — Preserves memories before context compaction (does NOT affect session refcount)

**To enable:** Add the following to your `~/.claude/settings.json` under the `hooks` key. Replace `{PLUGIN_ROOT}` with the path to this plugin (or use the absolute path shown below):

```json
{
  "SessionStart": [
    {
      "matcher": "*",
      "hooks": [
        {
          "type": "command",
          "command": "bash /Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/.claude-plugin/hooks/mempal-session-start-hook.sh"
        }
      ]
    }
  ],
  "Stop": [
    {
      "matcher": "*",
      "hooks": [
        {
          "type": "command",
          "command": "bash /Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/.claude-plugin/hooks/mempal-stop-hook.sh"
        }
      ]
    }
  ],
  "PreCompact": [
    {
      "matcher": "*",
      "hooks": [
        {
          "type": "command",
          "command": "bash /Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/.claude-plugin/hooks/mempal-precompact-hook.sh"
        }
      ]
    }
  ]
}
```

Set `MEMPAL_DIR` environment variable to auto-ingest a directory on each save.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Claude Code session 1..6 (localhost)              │
│  Hooks → mempal-server-control.sh (session refcount)│
│  MCP tools → http://127.0.0.1:8765/mcp             │
└─────────────────────────────────────────────────────┘
                     │
                     ▼ (shared HTTP)
┌─────────────────────────────────────────────────────┐
│  mempalace serve (1 process, port 8765)             │
│  ├── SessionRegistry (shared across sessions)        │
│  ├── WriteCoordinator (WAL coalescing)               │
│  ├── ClaimsManager (file-level mutual exclusion)     │
│  ├── HandoffManager (atomic handoff)                │
│  └── DecisionTracker (architectural decisions)        │
└─────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│  LanceDB (~/.mempalace/)                           │
└─────────────────────────────────────────────────────┘
```

## Key Design Constraints

- **One shared server** — plugin never spawns multiple MCP servers; session refcount ensures single process
- **No plugin.json mcpServers** — the plugin is skills + hooks only; no MCP server configuration in plugin.json
- **Localhost only** — server binds 127.0.0.1 only, never exposed externally
- **Graceful shutdown** — Stop hook auto-save runs before any server termination
- **No LaunchAgent** — server lifecycle is entirely hook-driven

## Full Documentation

See the main [README](../README.md).
