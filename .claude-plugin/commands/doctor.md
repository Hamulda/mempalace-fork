---
description: Diagnose MemPalace server health — check health endpoint, palace status, and server process.
allowed-tools: Bash, Read
---

# MemPalace Doctor

## Run All Checks

```bash
echo "=== Health ===" && curl -s http://127.0.0.1:8765/health && echo "" && echo "=== Palace Status ===" && curl -s http://127.0.0.1:8765/health | jq -r '.palace_path // empty' 2>/dev/null && echo "" && echo "=== Server Process ===" && cat ~/.mempalace/runtime/server.pid 2>/dev/null && echo "" && echo "=== Session Count ===" && ls ~/.mempalace/runtime/sessions/ 2>/dev/null | wc -l | tr -d ' '
```

## Individual Checks

**Health endpoint:**
```bash
curl http://127.0.0.1:8765/health
```

**Server PID:**
```bash
cat ~/.mempalace/runtime/server.pid
```

**Active sessions:**
```bash
ls ~/.mempalace/runtime/sessions/ | wc -l
```

**Server-control status:**
```bash
bash ~/.claude/plugins/marketplaces/mempalace/.claude-plugin/hooks/mempal-server-control.sh status 2>/dev/null || echo "server-control: unavailable"
```

## Common Issues

| Symptom | Check | Fix |
|---------|-------|-----|
| MCP tools return connection error | Health check | Start server: `mempalace serve --host 127.0.0.1 --port 8765` |
| `connection refused` | Server PID missing | Server not running — start it or check hooks registration |
| Stale sessions | Session count high | Sessions auto-expire via TTL (6h) — no manual prune needed |
| Slow searches | Memory pressure | Check `memory_guard` in `mempalace_status` output |

## If All Checks Pass but Slow

- Use `mempalace_search_code` with `limit: 5` instead of full searches
- Avoid large exports (`mempalace_export_claude_md` without filters)
- Rerank only for complex semantic queries
