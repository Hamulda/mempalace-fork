# PHASE6: Plugin Lifecycle Runtime Seal Report

**Date:** 2026-04-29
**Mission:** Seal the Claude Code plugin lifecycle runtime behavior for MemPalace.
**Result:** ✅ VERIFIED AND SEALED

---

## 1. Audit Summary — Plugin Files

### 1.1 .claude-plugin/README.md
- ✅ Correctly describes hooks as **optional** and requiring manual `settings.json` registration.
- ✅ Correctly describes **one shared HTTP server** at `http://127.0.0.1:8765`.
- ✅ Explicitly states: "No MCP servers started by plugin — plugin provides skills + hooks".
- ✅ Session refcount lifecycle: SessionStart registers, Stop unregisters, last session triggers graceful shutdown.
- ✅ Server lifecycle section clearly labeled: "Automatic — Recommended, Requires Hook Registration".
- ✅ Grace period: ~20 seconds to avoid restart churn.

### 1.2 .claude-plugin/.mcp.json
- ✅ Points to `http://127.0.0.1:8765/mcp` (localhost, port 8765, /mcp suffix).
- ✅ Transport: `http` (not stdio).
- ✅ File is the mcpServers dict directly (not wrapped in an extra key).
- ✅ No per-session stdio configuration.

### 1.3 .claude-plugin/plugin.json
- ✅ Has no `mcpServers` key.
- ✅ Defines `skills` and `commands` paths only.
- ✅ Name: "mempalace", version: "3.1.0".
- ✅ Correctly scoped as skills + hooks only.

### 1.4 .claude-plugin/hooks/hooks.json
- ✅ Defines `SessionStart`, `Stop`, and `PreCompact` hooks.
- ✅ All three hooks point to `bash ${CLAUDE_PLUGIN_ROOT}/hooks/<hook-name>.sh`.
- ✅ This is the correct pattern for hook registration in `settings.json`.

### 1.5 .claude-plugin/hooks/mempal-server-control.sh
- ✅ Manages a single shared HTTP server at `http://127.0.0.1:8765/mcp`.
- ✅ Session refcount via `sessions/<session_id>` files.
- ✅ Graceful shutdown after `GRACE_PERIOD_SECONDS` (default 20s).
- ✅ Lock-based mutual exclusion via `control.lock` directory with flock semantics.
- ✅ `safe_session_id`: strips non-alphanumeric to empty, then hashes via Python → `id-<sha256>` prefix.
- ✅ `stop_server`: verifies process command line before killing — only kills `mempalace.*serve` processes.
- ✅ No LaunchAgent, no Docker, no external network.
- ✅ All timing constants are tunable via env vars (not hardcoded).

### 1.6 Commands (.claude-plugin/commands/*.md)
- ✅ `init.md`: Correctly describes shared server setup. Does not claim automatic hook registration.
- ✅ `help.md`: Describes manual server start for non-hook users. Correct.
- ✅ `status.md`: References MCP tool `mempalace_status` and CLI `mempalace status`.
- ✅ `doctor.md`: References `mempalace doctor` CLI.
- ✅ `mine.md`: References `mempalace mine` CLI.
- ✅ `search.md`: References MCP tool `mempalace_search` and CLI.
- ✅ **No command file claims automatic hook registration**.

---

## 2. Truth Consistency Verification

| Check | Expected | Actual | Status |
|-------|----------|--------|--------|
| README: hooks optional | Manual registration in settings.json | "hooks (optional, requires manual registration in settings.json)" | ✅ |
| README: one shared HTTP | Single server, port 8765 | "single shared HTTP server" / "http://127.0.0.1:8765" | ✅ |
| .mcp.json URL | http://127.0.0.1:8765/mcp | `"url": "http://127.0.0.1:8765/mcp"` | ✅ |
| plugin.json: no stdio mcpServers | No mcpServers key | Keys: name, version, description, skills, commands | ✅ |
| Commands: no false auto-registration claims | No claim of automatic hook setup | init.md correctly defers to manual settings.json registration | ✅ |
| hooks.json: SessionStart/Stop/PreCompact | All 3 hooks defined | All 3 present | ✅ |
| Control script: PID safety | Verify process is mempalace before kill | grep check for `mempalace.*serve` pattern | ✅ |
| Control script: safe_session_id | Strip to alnum, hash empty | tr -cd 'a-zA-Z0-9_-', python hash fallback | ✅ |

---

## 3. Test Coverage: test_plugin_lifecycle.py

**Location:** `tests/test_plugin_lifecycle.py`
**Run:** `pytest tests/test_plugin_lifecycle.py -q` → **13 passed in 9.55s**

### Test Cases

| # | Test | What it verifies |
|---|------|-----------------|
| 1 | `test_start_creates_session_file` | `start sessionA` → `sessions/sessionA` file exists |
| 2 | `test_start_second_session_keeps_first` | Starting sessionB does not remove sessionA |
| 3 | `test_status_shows_correct_active_sessions` | `status` reports `active_sessions=2` after two starts |
| 4 | `test_stop_one_session_leaves_other_active` | `stop sessionA` leaves sessionB intact |
| 5 | `test_stop_last_session_zero_active` | `stop` last session → zero active after grace |
| 6 | `test_prune_removes_stale_sessions` | `prune` removes sessions older than TTL |
| 7 | `test_safe_session_id_sanitizes_weird_ids` | `////etc/passwd////` → `etcpasswd`; `/////` → `id-<hash>` |
| 8 | `test_stale_pid_not_mempalace_not_killed` | Stale PID for non-mempalace process is not killed |
| 9 | `test_status_output_machine_readable` | All 5 required fields present: server_running, pid, active_sessions, session_files, health |
| 10 | `test_concurrent_session_refcount_integrity` | Multiple start/stop cycles keep refcount accurate |
| 11 | `test_mcp_json_has_correct_url` | `.mcp.json` has correct `http://127.0.0.1:8765/mcp` |
| 12 | `test_plugin_json_has_no_stdio_servers` | `plugin.json` has no `mcpServers` key |
| 13 | `test_new_session_during_grace_cancels_shutdown` | New session during grace period prevents shutdown |

### Fast Timing Overrides Used in Tests
- `GRACE_PERIOD_SECONDS=1` (not 20s)
- `STARTUP_WAIT_SECONDS=1` (not 10s)
- `LOCK_MAX_WAIT=2` (not 30s)
- `MEMPALACE_SESSION_TTL_SECONDS=2` (not 21600s)

### Isolation Guarantees
- `HOME` overridden to pytest temp dir — no real `~/.mempalace` accessed.
- `RUNTIME_DIR` overridden to temp path — no real runtime state touched.
- No real MemPalace servers started — `mempalace` binary replaced with fake in temp `PATH`.
- No real processes killed — stale PID test verifies `os.getpid()` survives the script run.

---

## 4. Abort Condition Check

| Condition | Finding |
|-----------|---------|
| Hook schema uncertainty | ❌ NOT APPLICABLE — no unsupported schema invented |
| Real process killed in test | ❌ NOT APPLICABLE — all process operations are staged |
| Real runtime directory modified | ❌ NOT APPLICABLE — all runtime operations use temp paths |
| Control script has real safety bug | ❌ NOT FOUND — PID verification (`grep -qE 'mempalace.*serve'`) is correct |

---

## 5. Key Findings

### Finding 1: `safe_session_id` hash fallback only triggers on empty result
- `tr -cd 'a-zA-Z0-9_-'` on `"////etc/passwd////"` yields `"etcpasswd"` — non-empty, no hash.
- Only `""` (all non-alnum chars) triggers the Python hash fallback → `id-<sha256[:12]>`.
- **Test correctly validates both paths.**

### Finding 2: `stop_server` has proper PID safety
- Reads PID from `server.pid`, verifies `is_pid_running`.
- Uses `timeout 1 ps -p $pid -o command=` to get command line.
- Only kills if command matches `mempalace.*serve` or `python.*mempalace.*serve`.
- Stale PID for non-MempPalace process → removes PID file, does NOT kill.
- **Verified by `test_stale_pid_not_mempalace_not_killed`.**

### Finding 3: Grace period correctly interruptible
- `cmd_stop` releases lock during grace, checks for new sessions every second.
- If new session appears, returns early without calling `stop_server`.
- `shutdown_if_idle` follows the same pattern.
- **Tested by `test_new_session_during_grace_cancels_shutdown`.**

### Finding 4: `.mcp.json` is the mcpServers dict directly
- Does NOT have a `"mcpServers"` wrapper key.
- Structure: `{"mempalace": {"transport": "http", "url": "http://127.0.0.1:8765/mcp"}}`.
- This is the correct Claude Code plugin format.

---

## 6. Document-vs-Runtime Gap Analysis

| Document | Claim | Runtime Reality | Status |
|----------|-------|-----------------|--------|
| README "Server Lifecycle" | "only if hooks are registered in settings.json" | `hooks.json` exists; registration is manual in `settings.json` | ✅ ALIGNED |
| README "No MCP servers started by plugin" | Plugin provides skills + hooks, MCP tools connect to shared server | `plugin.json` has no `mcpServers` key | ✅ ALIGNED |
| README "one shared HTTP server" | Single `mempalace serve` process for all sessions | `mempalace-server-control.sh` uses session refcount | ✅ ALIGNED |
| init.md "one shared server" | "Run one server for all 6 parallel Claude Code sessions" | Confirmed | ✅ ALIGNED |
| status.md "Shared Server Architecture" | "One mempalace serve process serves all 6 sessions" | Confirmed | ✅ ALIGNED |
| Key Design Constraints README | "No plugin.json mcpServers" | `plugin.json` has no `mcpServers` key | ✅ ALIGNED |

---

## 7. Run Checks

### Check 1: pytest
```
pytest tests/test_plugin_lifecycle.py -q
→ 13 passed in 9.55s ✅
```

### Check 2: control script status (no runtime)
```bash
bash .claude-plugin/hooks/mempal-server-control.sh status
→ server_running=false, pid=none, active_sessions=0, health=fail ✅
```

### Check 3: .mcp.json valid JSON
```python
import json; json.load(open('.claude-plugin/.mcp.json'))
→ {"mempalace": {"transport": "http", "url": "http://127.0.0.1:8765/mcp"}} ✅
```

---

## 8. Conclusion

**Plugin lifecycle is sealed and verified.**

- Session refcount: ✅ Correct
- Shared server management: ✅ Correct  
- Graceful shutdown interruptibility: ✅ Correct
- PID safety (non-mempalace process protection): ✅ Correct
- `safe_session_id` (strip + hash fallback): ✅ Correct
- Machine-readable status output: ✅ Correct (5 fields)
- Docs match runtime truth: ✅ All consistent
- MCP endpoint unchanged: ✅ `http://127.0.0.1:8765/mcp`
- No real runtime state touched by tests: ✅ Verified
- No real processes killed by tests: ✅ Verified

**ABORT CONDITIONS: None triggered.**