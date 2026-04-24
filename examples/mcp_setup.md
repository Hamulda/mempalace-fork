# MemPalace MCP Setup — Canonical Guide

## Two Server Modes

MemPalace supports two server modes depending on your workflow:

| Mode | How it works | Use when |
|------|-------------|----------|
| **A: Automatic lifecycle (recommended)** | Plugin hooks start/stop server around Claude Code sessions | Standard Claude Code usage |
| **B: Manual long-running server** | You start `mempalace serve` before working | Debugging, external MCP clients |

Both modes use the same shared HTTP server (`http://127.0.0.1:8765/mcp`) and the same LanceDB storage. The difference is whether the plugin or you control the server process.

---

## Mode A: Automatic Lifecycle (Recommended)

Plugin hooks automatically start the shared server on first Claude Code session start and shut it down after the last session exits.

```
Session 1 starts → server starts → (Session 2 starts → reuses server) → (Session 1 closes → server stays) → (Session 2 closes → server shuts down after ~20s grace period)
```

**Setup:**

```bash
# 1. Install Python package
pip install git+https://github.com/Hamulda/mempalace-fork

# 2. Initialize palace
mempalace init ~/palace

# 3. Install plugin (hooks manage server lifecycle automatically)
claude plugin marketplace add hamulda/mempalace-fork
claude plugin install --scope user mempalace

# 4. Restart Claude Code
# SessionStart hook starts the shared server automatically on first session
```

**How it works internally:**

1. `SessionStart` hook reads the session JSON, derives a session ID
2. Calls `mempal-server-control.sh start <session_id>` — registers session, starts server if health check fails
3. Calls `mempalace hook run --hook session-start` via HTTP
4. `Stop` hook calls `mempalace hook run --hook stop` first (while server alive for auto-save), then `mempal-server-control.sh stop <session_id>`
5. Last session exit triggers graceful server shutdown after a 20-second grace period

**Crash recovery:** Session files expire after 6 hours (TTL, configurable via `MEMPALACE_SESSION_TTL_SECONDS`). Stale sessions from crashed Claude Code instances don't block server shutdown.

---

## Mode B: Manual Long-Running Server

Start the server yourself before working. Useful for debugging or when external MCP clients (non-Claude-Code) need persistent access.

```bash
# 1. Install Python package
pip install git+https://github.com/Hamulda/mempalace-fork

# 2. Initialize palace
mempalace init ~/palace

# 3. Start server (manually — keeps running until you stop it)
mempalace serve --host 127.0.0.1 --port 8765

# 4. Install plugin (hooks use existing server, don't spawn new ones)
claude plugin marketplace add hamulda/mempalace-fork
claude plugin install --scope user mempalace
```

Plugin connects to MCP server via `.claude-plugin/.mcp.json` (already configured for `http://127.0.0.1:8765/mcp`).

**Verify:**

```bash
curl http://127.0.0.1:8765/health
# → {"status": "ok", "service": "mempalace"}
```

Then in Claude Code: `/mempalace:status`

---

## Architecture — Shared Server

```
┌──────────────────────────────────────────┐
│  Claude Code session 1..6 (localhost)    │
│  MCP tools → http://127.0.0.1:8765/mcp  │
└──────────────────────────────────────────┘
                    │
                    ▼ (shared HTTP)
┌──────────────────────────────────────────┐
│  mempalace serve --port 8765 (1 process)  │
│  SessionRegistry, ClaimsManager, WAL...   │
└──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────┐
│  LanceDB (~/.mempalace/)                  │
└──────────────────────────────────────────┘
```

One server process, multiple Claude Code sessions. Session coordination (ClaimsManager, WriteCoordinator, HandoffManager) works across all parallel sessions.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `fastmcp` not found | `pip install git+https://github.com/Hamulda/mempalace-fork` |
| Server not running | Claude Code plugin starts it automatically (Mode A); or run `mempalace serve --host 127.0.0.1 --port 8765` (Mode B) |
| Wrong port | Check `.mcp.json` port matches server port |
| Plugin installed but no tools | `/reload-plugins` in Claude Code |
| Tools not appearing | Restart Claude Code after plugin install |
| Server won't shut down | Check `~/.mempalace/runtime/sessions/` for stale session files; set `MEMPALANCE_SESSION_TTL_SECONDS=0` to force immediate cleanup |

## Port Canonicalization

- Default server port: **8765**
- `.mcp.json` always: `http://127.0.0.1:8765/mcp`
- `mempalace serve` default: `--port 8765`
- No other port is canonical for shared-server mode.

## Session Lifecycle Debugging

Check server and session status:

```bash
bash .claude-plugin/hooks/mempal-server-control.sh status
# server_running=true/false
# pid=<pid or none>
# active_sessions=<count>
# session_files=<list>
# health=ok/fail
```

Manually prune stale sessions:

```bash
bash .claude-plugin/hooks/mempal-server-control.sh prune
```
