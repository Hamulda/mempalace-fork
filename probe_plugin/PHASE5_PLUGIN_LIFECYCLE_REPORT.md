# PHASE5_PLUGIN_LIFECYCLE_REPORT
**Date**: 2026-04-27
**Mission**: Harden Claude plugin lifecycle — single shared MemPalace HTTP MCP server, session-refcounted

---

## Verdict: LIFECYCLE IS CORRECTLY IMPLEMENTED AND TESTED

The session refcounting controller works correctly across all 9 test cases (28 assertions, 0 failures).
The only gap is that hook registration in `settings.json` is **manual** — not auto-registered by the plugin package.

---

## What Was Tested

### Test Suite: `probe_plugin/PHASE5_LIFECYCLE_TEST.py`

| # | Test | Result |
|---|------|--------|
| 1 | `start session A` → session file created | ✅ PASS |
| 2 | `start session B` → server not restarted (already healthy) | ✅ PASS |
| 3 | `stop session A` → session removed, server continues with B | ✅ PASS |
| 4 | `stop session B` → zero sessions, graceful shutdown after grace | ✅ PASS |
| 5 | `prune` → removes stale sessions older than TTL | ✅ PASS |
| 6 | stale PID file → non-mempalace process not killed | ✅ PASS |
| 7 | `status` → machine-readable key=value output | ✅ PASS |
| 8 | grace period → new session aborts shutdown | ✅ PASS |
| 9 | concurrent `start` with same session ID → idempotent | ✅ PASS |

**Total: 28 assertions, 0 failures**

---

## Controller Behavior Verified

| Behavior | Expected | Actual |
|----------|----------|--------|
| First `start` registers session + spawns server | ✅ | ✅ |
| Second `start` sees healthy server, no restart | ✅ | ✅ |
| `stop` of non-last session: server keeps running | ✅ | ✅ |
| Last `stop`: grace period (~20s) then shutdown | ✅ | ✅ |
| Grace period: new session aborts shutdown | ✅ | ✅ |
| `prune`: removes sessions older than TTL (default 6h) | ✅ | ✅ |
| `stop_server` with stale PID: refuses to kill non-MemPalace | ✅ | ✅ |
| `status`: machine-readable `key=value` format | ✅ | ✅ |
| Concurrent `start` with same ID: idempotent | ✅ | ✅ |

---

## Hook Scripts — Status

| Script | Purpose | Status |
|--------|---------|--------|
| `mempal-server-control.sh` | Session refcounting controller | ✅ Correct |
| `mempal-session-start-hook.sh` | SessionStart entry point | ✅ Correct |
| `mempal-stop-hook.sh` | Stop hook entry point | ✅ Correct |
| `mempal-precompact-hook.sh` | PreCompact hook entry point | ✅ Correct |

All hooks are:
- **Non-blocking**: `set -uo pipefail` (no `set -e`), exit 0 always
- **Timeout-bounded**: stop=30s, precompact=55s, session-start=unbounded but fast
- **Fallback transports**: HTTP first, CLI fallback for `mempalace hook run`
- **Session ID safe**: `safe_session_id()` with deterministic SHA256 fallback

---

## `.mcp.json` — HTTP Endpoint Only

```json
{
  "mempalace": {
    "transport": "http",
    "url": "http://127.0.0.1:8765/mcp"
  }
}
```

✅ Points to shared HTTP MCP endpoint. No `mcpServers` in `plugin.json`. No per-session stdio servers.

---

## Documentation Consistency

| Document | Claim | Status |
|----------|-------|--------|
| `README.md` | Hooks optional, require manual `settings.json` registration | ✅ Consistent |
| `commands/init.md` | Manual setup: pip + `mempalace serve` + `mempalace init` | ✅ Consistent |
| `commands/help.md` | Same manual setup, "Server Not Running?" section | ✅ Consistent |
| `init.md` vs `help.md` | Both describe manual server mode | ✅ Consistent |
| `README` vs `init.md` | README: auto-start via hooks (requires registration); init: manual serve | ✅ Both valid modes |

**No contradiction remains.** The README accurately describes the optional automatic lifecycle (with hook registration requirement), and `init.md`/`help.md` describe the manual fallback.

---

## Hook Registration — Manual Process

**Plugin cannot auto-register hooks.** `plugin.json` schema supports only:
- `skills`
- `commands`

No `hooks` field exists in the Claude plugin manifest.

**User must manually add to `~/.claude/settings.json`** (as documented in README hooks section):

```json
{
  "SessionStart": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "bash /Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/.claude-plugin/hooks/mempal-session-start-hook.sh" }] }],
  "Stop": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "bash /Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/.claude-plugin/hooks/mempal-stop-hook.sh" }] }],
  "PreCompact": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "bash /Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/.claude-plugin/hooks/mempal-precompact-hook.sh" }] }]
}
```

---

## Docs Fixes Applied

1. **No new edits needed** — Phase0 audit already patched README to remove false automatic-lifecycle claims. Current state is accurate.
2. **`/mempalace:init`** — says "plugin lifecycle is canonical" in spirit; the command itself correctly describes manual serve as the setup path (which is the correct fallback when hooks aren't registered).
3. **`/mempalace:help`** — correctly describes manual serve. No contradiction with README.

---

## Files Created/Modified

| File | Change |
|------|--------|
| `probe_plugin/PHASE5_LIFECYCLE_TEST.py` | **Created** — 9 integration tests, 28 assertions, all pass |
| `probe_plugin/PHASE5_PLUGIN_LIFECYCLE_REPORT.md` | **Created** — this report |

---

## ABORT CONDITIONS — NONE TRIGGERED

| Condition | Status |
|-----------|--------|
| No git commands used | ✅ |
| No guessing of unsupported Claude plugin schema | ✅ |
| `.mcp.json` unchanged (HTTP endpoint only) | ✅ |
| No LaunchAgent introduced | ✅ |
| Port 8765 preserved | ✅ |
| Controller script unchanged (not needed) | ✅ |

---

## Runtime State Ownership

```
~/.mempalace/runtime/
├── server.pid       — server PID (written at spawn, cleared at shutdown)
├── control.lock/    — flock directory (lock.token + lock.pid)
├── sessions/        — session refcount files (TTL: 6h)
├── server.log       — stdout of server process
└── server.err.log   — stderr of server process
```

Canonical runtime owner: `mempal-server-control.sh` (invoked by hook entry points).

---

## Success Criteria — Final Status

| Criterion | Status |
|-----------|--------|
| Hook controller behavior verified (start/stop/prune/status) | ✅ 9/9 tests pass |
| Session refcounting (A→B→stop A→stop B→grace→shutdown) | ✅ Verified |
| stale PID does not kill unrelated process | ✅ Verified |
| stale session TTL prune | ✅ Verified |
| `.mcp.json` remains HTTP endpoint only | ✅ Confirmed |
| No contradiction between README and commands | ✅ Resolved |
| Plugin layer has tests | ✅ `PHASE5_LIFECYCLE_TEST.py` |
| Static checks for hook registration | ✅ `PHASE0_STATIC_CHECK.sh` |
| Runtime lifecycle is single-server + session-refcounted | ✅ Confirmed |

---

## Recommendations

1. **User must register hooks manually** in `settings.json` — no automatic path exists via plugin schema. This is documented.
2. **No code changes needed** — controller, hook scripts, and docs are all correct.
3. **Consider**: Add `probe_plugin/PHASE5_LIFECYCLE_TEST.py` to CI as a lightweight check (runs in <5s, no network required).