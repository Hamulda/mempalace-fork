#!/bin/bash
#===============================================================================
# PHASE0_STATIC_CHECK.sh — Verify MemPalace plugin hook registration vs README claims
#
# FAILS if:
#   - README claims automatic hooks but settings.json has no MemPalace hooks registered
#
# EXIT CODES:
#   0  = all checks pass OR hooks are intentionally not registered (manual mode documented)
#   1  = README claims automatic hooks but hooks are NOT registered (silent failure)
#   2  = cannot determine state (settings.json unreadable, etc.)
#===============================================================================

set -uo pipefail

PLUGIN_ROOT="${PLUGIN_ROOT:-/Users/vojtechhamada/.claude/plugins/marketplaces/mempalace}"
SETTINGS_FILE="${SETTINGS_FILE:-$HOME/.claude/settings.json}"
README="$PLUGIN_ROOT/.claude-plugin/README.md"

HOOK_SCRIPTS=(
    "$PLUGIN_ROOT/.claude-plugin/hooks/mempal-session-start-hook.sh"
    "$PLUGIN_ROOT/.claude-plugin/hooks/mempal-stop-hook.sh"
    "$PLUGIN_ROOT/.claude-plugin/hooks/mempal-precompact-hook.sh"
    "$PLUGIN_ROOT/.claude-plugin/hooks/mempal-server-control.sh"
)

echo "=== PHASE0_STATIC_CHECK: MemPalace Plugin Hook Registration ==="
echo ""

#--- 1. Verify hook scripts exist ---
echo "[1] Hook scripts present..."
all_present=true
for script in "${HOOK_SCRIPTS[@]}"; do
    if [[ -f "$script" ]]; then
        echo "  OK  $(basename "$script")"
    else
        echo "  MISSING $script"
        all_present=false
    fi
done
if ! $all_present; then
    echo "FAIL: Some hook scripts are missing"
    exit 2
fi

#--- 2. Check if settings.json has MemPalace hooks registered ---
echo ""
echo "[2] Checking settings.json for MemPalace hook registration..."

if [[ ! -f "$SETTINGS_FILE" ]]; then
    echo "  INFO: settings.json not found — assuming manual mode"
    SETTINGS_HOOKS_REGISTERED=false
else
    if grep -q 'mempal-session-start-hook\|mempal-stop-hook\|mempal-precompact\|mempalace.*hook.*run' "$SETTINGS_FILE" 2>/dev/null; then
        SETTINGS_HOOKS_REGISTERED=true
        echo "  OK  MemPalace hooks found in settings.json"
    else
        SETTINGS_HOOKS_REGISTERED=false
        echo "  MISSING  No MemPalace hooks in settings.json"
    fi
fi

#--- 3. Check README: has registration instructions? has unqualified claims? ---
echo ""
echo "[3] Checking README for hook registration instructions..."

README_HAS_REGISTRATION=false
if [[ -f "$README" ]]; then
    if grep -qi 'settings\.json.*SessionStart\|mempal-session-start-hook\.sh\|"SessionStart".*command.*mempal' "$README" 2>/dev/null; then
        README_HAS_REGISTRATION=true
        echo "  OK  README contains hook registration instructions"
    else
        echo "  FAIL  README has no hook registration instructions"
    fi
fi

#--- 4. Verdict ---
echo ""
echo "=== RESULT ==="

if $SETTINGS_HOOKS_REGISTERED; then
    echo "  PASS  MemPalace hooks are registered in settings.json"
    echo "  (Automatic lifecycle is wired)"
    exit 0
else
    if $README_HAS_REGISTRATION; then
        echo "  PASS  README documents manual hook registration procedure"
        echo "  (No false claims; user must add hooks to settings.json per README)"
        exit 0
    else
        echo "  FAIL  README claims automatic hooks but provides no hook registration instructions"
        echo "  and hooks are NOT registered in settings.json."
        echo ""
        echo "  TO FIX: Add MemPalace hooks to settings.json (see hooks/hooks.json in plugin)"
        exit 1
    fi
fi
