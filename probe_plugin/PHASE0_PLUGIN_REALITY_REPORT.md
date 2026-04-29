# PHASE0_PLUGIN_REALITY_REPORT
**Date**: 2026-04-27
**Audit**: Claude plugin layer — MemPalace hooks + lifecycle registration

---

## Verdict: HOOKS ARE NOT REGISTERED

The plugin ships all hook scripts and documents the registration JSON, but **nothing auto-registers them**.
The README claims "auto-save on Stop, PreCompact preservation, SessionStart lifecycle control" — this is **not true**
unless the user manually pastes the hook JSON into `~/.claude/settings.json`.

---

## Evidence

### plugin.json — No hooks field

```
.claude-plugin/plugin.json:
{
  "name": "mempalace",
  "skills": "./skills/",
  "commands": "./commands/"
  // ← NO "hooks" field. No such field exists in Claude plugin schema.
}
```

Claude Code plugin packages cannot declare hooks in their manifest. The plugin schema supports only:
- `skills`
- `commands`
- (and implicitly `mcpServers` via `.mcp.json`)

### hooks/hooks.json — Documentation, not auto-registration

```
.claude-plugin/hooks/hooks.json:
{ "description": "MemPalace auto-save...", "hooks": { SessionStart: [...], Stop: [...], PreCompact: [...] } }
```

This file is **a template** — it shows the exact JSON the user must paste into `settings.json`. It is
NOT read automatically by Claude Code. The `${CLAUDE_PLUGIN_ROOT}` variable substitution is also a
convention the user must manually resolve when pasting.

### settings.json — MemPalace hooks absent

```
~/.claude/settings.json:
SessionStart → claudekit-hooks run create-checkpoint  (NOT MemPalace)
Stop         → claudekit-hooks run create-checkpoint + self-review  (NOT MemPalace)
PreCompact   → (not present)
```

**MemPalace hooks are entirely absent from settings.json.** The claudekit-hooks are not MemPalace hooks.

### Hook scripts — Fully implemented, correctly structured

| File | Purpose | Status |
|------|---------|--------|
| `mempal-session-start-hook.sh` | Derive session ID → `server-control start` → `mempalace hook run` | ✅ Correct |
| `mempal-stop-hook.sh` | Derive session ID → `mempalace hook run` → `server-control stop` | ✅ Correct |
| `mempal-precompact-hook.sh` | Bounded `mempalace hook run --hook precompact` | ✅ Correct |
| `mempal-server-control.sh` | Full session refcounting, graceful shutdown, flock | ✅ Correct |

All three hook scripts are well-implemented with proper timeout handling, fallback transports,
and correct error handling (no `set -e` that would block Claude Code startup/shutdown).

### .mcp.json — Correct

```
{
  "mempalace": {
    "transport": "http",
    "url": "http://127.0.0.1:8765/mcp"
  }
}
```
Points to the shared HTTP endpoint. ✅ Correct.

### Docs vs Reality — Conflicting claims

| Document | Claim | Reality |
|----------|-------|---------|
| README.md line 8 (pre-patch) | "Hooks: Auto-save on Stop, PreCompact preservation, SessionStart lifecycle control" | **FALSE** — hooks not registered; now patched with "(optional, requires manual registration in settings.json)" |
| README.md line 15 (pre-patch) | "Shared MemPalace MCP server managed by plugin hooks (auto-started on first session)" | **FALSE** — no auto-start without hook registration; now patched |
| README.md line 31-42 (pre-patch) | Full lifecycle diagram with SessionStart/Stop/PreCompact | **DOCUMENTED BUT NOT WIRED**; now qualified with hook registration requirement |
| init.md | "Follow these steps in order" + manual `mempalace serve` | ✅ Correct for manual mode |
| help.md | "Server Not Running?" section | Correct for manual mode |

---

## Consistency Analysis

### Commands (consistent with each other)
- `/mempalace:init` — manual setup instructions (pip + `mempalace serve` + `mempalace init`). ✅ Correct.
- `/mempalace:help` — same manual setup. ✅ Correct.
- `/mempalace:status` — MCP tool call. ✅ Correct.

### Commands vs README
- init.md/help.md describe **manual server mode** (user runs `mempalace serve`).
- README.md describes **automatic hook-driven mode** (SessionStart starts server).
- **Both cannot be true simultaneously.** README claims automatic; commands describe manual.

---

## What Is Missing

The MemPalace hooks are not registered in `~/.claude/settings.json`. The user must manually add:

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

---

## Patch Assessment

**Can we patch the plugin manifest to auto-register hooks?** No. Claude plugin.json does not support a `hooks` field.

**Can we patch the README to remove false claims?** Yes — minimal patch: clarify that automatic lifecycle requires manual settings.json registration, OR update README to match the actual (manual) behavior.

**Can we add a static check?** Yes — a check that fails if README claims hooks are automatic but settings.json has no MemPalace hook registration. This satisfies SUCCESS CRITERIA item 6.

---

## ABORT CONDITIONS

- ❌ Did not delete any plugin files
- ❌ Did not change port 8765
- ❌ Did not introduce LaunchAgent or background services
- ❌ Did not invent Claude plugin schema that doesn't exist

---

## Canonical File for Plugin Lifecycle

**The canonical runtime owner for shared MCP server lifecycle is:**
```
~/.mempalace/runtime/control.lock   (flock mutual exclusion)
~/.mempalace/runtime/server.pid     (server PID)
~/.mempalace/runtime/sessions/     (session refcount files)
~/.claude/plugins/marketplaces/mempalace/.claude-plugin/hooks/mempal-server-control.sh  (lifecycle controller)
```

The three hook scripts (`mempal-*-hook.sh`) are the **entry points** that invoke the controller.
The controller is the **runtime owner**.

---

## Post-Audit Actions Taken

### README.md patched (3 locations)

1. **Line 8**: `**Hooks**: Auto-save...` → `**Hooks** (optional, requires manual registration in settings.json): Auto-save...`
2. **Line 15**: `Shared MemPalace MCP server managed by plugin hooks (auto-started on first session)` → `Shared MemPalace MCP server (start manually with mempalace serve..., or enable auto-start by registering the hooks in settings.json)`
3. **Server Lifecycle section (line 28)**: Added "Requires Hook Registration" qualifier and clarification that without registration server must be run manually.
4. **Hooks section**: Added complete settings.json snippet with absolute paths for all 3 hook scripts.

### probe_plugin/PHASE0_STATIC_CHECK.sh created

Static check that verifies:
- Hook scripts are present (all 4 pass)
- settings.json has MemPalace hook registration
- README contains hook registration instructions

Exit codes: 0=pass, 1=fail (unqualified claims + no registration), 2=scripts missing

**Current result**: ✅ PASS — README documents registration procedure; no false claims

### CLAUDE.md (repo) — Note for future review

The main CLAUDE.md in the repo root is checked at session start for user instructions. It contains RTK instructions (not MemPalace-specific lifecycle claims). The auto-trigger rules in CLAUDE.md for `mempalace_search` and `mempalace_add_drawer` are correct and do not claim hook-based lifecycle.

The MEMORY.md also correctly records the lifecycle hook hardening fixes from 2026-04-25 in `mempalace_lifecycle_final_polish_apr252026` — those are the actual hook scripts in the repo (which ARE properly implemented).

So the picture is: the hook **scripts** are well-implemented and tested, but they were never **registered** in settings.json, so they never ran automatically.

---

## Recommended Actions (minimal)

1. **settings.json**: User must add MemPalace hook entries manually (requires user approval).
2. **CLAUDE.md (repo root)**: Consider adding a note that automatic lifecycle requires hook registration in settings.json. This is out of scope for this phase but worth noting.
3. **probe_plugin/PHASE0_STATIC_CHECK.sh**: Static check added — if README is changed back to claiming automatic lifecycle without registration instructions, the check will fail.
