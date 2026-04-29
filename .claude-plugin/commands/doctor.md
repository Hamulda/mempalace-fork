---
description: Diagnose MemPalace server and palace health — shared server status, runtime dir, sessions, palace path, backend, Python version, Lance availability, Chroma import check.
allowed-tools: Bash, Read
---

# MemPalace Doctor

## Full Health Check

```bash
echo "=== Shared Server ===" && curl -s http://127.0.0.1:8765/health | python3 -c "import json,sys; d=json.load(sys.stdin); print('status:', d.get('status')); print('version:', d.get('version')); print('transport:', d.get('transport')); print('shared_server_mode:', d.get('shared_server_mode')); print('palace_path:', d.get('palace_path')); print('backend:', d.get('backend')); print('memory_pressure:', d.get('memory_pressure'))"
echo "" && echo "=== Runtime Directory ===" && ls -la ~/.mempalace/runtime/ 2>/dev/null || echo "runtime dir: not found"
echo "" && echo "=== Server PID ===" && cat ~/.mempalace/runtime/server.pid 2>/dev/null || echo "server.pid: not found"
echo "" && echo "=== Session Files ===" && ls ~/.mempalace/runtime/sessions/ 2>/dev/null | wc -l | tr -d ' ' && echo " active sessions"
echo "" && echo "=== Palace Path ===" && curl -s http://127.0.0.1:8765/health | python3 -c "import json,sys; print(json.load(sys.stdin).get('palace_path','?'))"
echo "" && echo "=== Backend ===" && curl -s http://127.0.0.1:8765/health | python3 -c "import json,sys; print(json.load(sys.stdin).get('backend','?'))"
echo "" && echo "=== Python Version ===" && python3 --version
echo "" && echo "=== LanceDB Available ===" && python3 -c "import lancedb; print('lancedb', lancedb.__version__)" 2>/dev/null || echo "lancedb: not installed"
echo "" && echo "=== Chroma NOT Imported ===" && python3 -c "import sys; mods=[k for k in sys.modules.keys() if 'chroma' in k.lower()]; exit(len(mods))" && echo "chroma: clean" || echo "WARNING: chroma modules present"
```

## Individual Checks

**Shared server health:**
```bash
curl http://127.0.0.1:8765/health
```

**Runtime directory:**
```bash
ls -la ~/.mempalace/runtime/
```

**Session count:**
```bash
ls ~/.mempalace/runtime/sessions/ 2>/dev/null | wc -l
```

**Server PID:**
```bash
cat ~/.mempalace/runtime/server.pid
```

**Palace path:**
```bash
curl -s http://127.0.0.1:8765/health | python3 -c "import json,sys; print(json.load(sys.stdin).get('palace_path','?'))"
```

**Backend:**
```bash
curl -s http://127.0.0.1:8765/health | python3 -c "import json,sys; print(json.load(sys.stdin).get('backend','?'))"
```

**Python version (3.14 target):**
```bash
python3 --version
```

**LanceDB availability:**
```bash
python3 -c "import lancedb; print('lancedb', lancedb.__version__)"
```

**Chroma not imported:**
```bash
python3 -c "import sys; mods=[k for k in sys.modules.keys() if 'chroma' in k.lower()]; exit(len(mods))" && echo "chroma not loaded" || echo "WARNING: chroma modules present"
```

## Common Issues

| Symptom | Check | Fix |
|---------|-------|-----|
| MCP tools return connection error | `curl http://127.0.0.1:8765/health` | Start server: `mempalace serve --host 127.0.0.1 --port 8765` |
| `connection refused` | server.pid missing | Server not running — ensure hooks registered in `settings.json` |
| Stale sessions | Session count high | Sessions auto-expire via TTL (6h) — no manual prune needed |
| Slow searches | memory_pressure | Check `memory_pressure` in health output — nominal is good |
| Backend shows `chroma` | health backend field | Only LanceDB is supported — Chroma is not supported |
| Wrong Python version | python3 --version | Requires Python 3.14 — use pyenv or virtualenv |

## If All Checks Pass but Slow

- Use `mempalace_search_code` with `limit: 5-10` instead of full searches
- Avoid `mempalace_export_claude_md` with large room filters
- Rerank only for complex semantic queries (FTS5 is fast for keyword matching)
