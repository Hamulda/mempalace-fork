# MemPalace MCP Setup — Canonical Guide

## Target Setup: 6 Parallel Claude Code Sessions + 1 Shared Server

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

## Install

```bash
# 1. Install Python package
pip install git+https://github.com/Hamulda/mempalace-fork

# 2. Initialize palace
mempalace init ~/palace

# 3. Start shared MCP server
mempalace serve --host 127.0.0.1 --port 8765
```

## Connect Claude Code

```bash
# Install plugin (provides skills + hooks only)
claude plugin marketplace add hamulda/mempalace-fork
claude plugin install --scope user mempalace
```

Plugin connects to MCP server via `.claude-plugin/.mcp.json` (already configured for `http://127.0.0.1:8765/mcp`).

## Verify

```bash
curl http://127.0.0.1:8765/health
# → {"status": "ok", "service": "mempalace"}
```

Then in Claude Code: `/mempalace:status`

## Auto-start (Optional)

To start the server automatically on login, use `launchd` on macOS:

```bash
# Create plist at ~/Library/LaunchAgents/com.mempalace.server.plist
# See: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Concepts/CreatingLaunchdPlists.html
```

Or add to your shell profile:

```bash
# ~/.zshrc or ~/.bashrc
alias mempalace-start='mempalace serve --host 127.0.0.1 --port 8765'
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `fastmcp` not found | `pip install git+https://github.com/Hamulda/mempalace-fork` |
| Server not running | `mempalace serve --host 127.0.0.1 --port 8765` |
| Wrong port | Check `.mcp.json` port matches server port |
| Plugin installed but no tools | `/reload-plugins` in Claude Code |
| Tools not appearing | Restart Claude Code after plugin install |

## Port Canonicalization

- Default server port: **8765**
- `.mcp.json` always: `http://127.0.0.1:8765/mcp`
- `mempalace serve` default: `--port 8765`
- No other port is canonical for shared-server mode.
