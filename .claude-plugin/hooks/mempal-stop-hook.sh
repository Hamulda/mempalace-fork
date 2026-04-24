#!/bin/bash
#===============================================================================
# mempal-stop-hook.sh — MemPalace Stop hook with lifecycle control
#
# CRITICAL ORDERING (no set -e — save failure never blocks unregister):
#   1. Derive SESSION_ID from hook JSON
#   2. Run mempalace hook save best-effort (server still alive)
#   3. ALWAYS call server-control stop "$SESSION_ID" — even if save fails
#
# The save must happen BEFORE any server shutdown so memory state is preserved.
#===============================================================================

set -uo pipefail  # no -e: save failure must not prevent unregister

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_CONTROL="$SCRIPT_DIR/mempal-server-control.sh"

#-------------------------------------------------------------------------------
# Derive SESSION_ID — reads stdin once, uses raw content for fallback hash
#-------------------------------------------------------------------------------
derive_session_id() {
    python3 -c "
import sys, json, hashlib

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except (json.JSONDecodeError, EOFError):
    pass
    # Use raw for hash even on parse failure
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    print(f'unknown-{h}', end='')
    sys.exit(0)

for key in ('session_id', 'sessionId', 'session.id',
            'transcript_path', 'cwd', 'timestamp'):
    parts = key.split('.')
    val = data
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part, None)
        else:
            val = None
        if val is None:
            break
    if val is not None and str(val).strip():
        s = str(val).strip()
        safe = ''.join(c if c.isalnum() or c in '_-' else '-' for c in s)
        print(safe, end='')
        sys.exit(0)

cwd = str(data.get('cwd', '') or '')
ts = str(data.get('timestamp', '') or '')
if cwd or ts:
    h = hashlib.sha256((cwd + ts).encode()).hexdigest()[:12]
    print(f'fallback-{h}', end='')
    sys.exit(0)

h = hashlib.sha256(raw.encode()).hexdigest()[:12]
print(f'unknown-{h}', end='')
"
}

INPUT=$(cat)

SESSION_ID=$(derive_session_id <<< "$INPUT")
if [[ -z "$SESSION_ID" ]]; then
    SESSION_ID="unknown-$(date +%s)"
fi

MCP_HOST="http://127.0.0.1:8765"
SAVE_STATUS="not_attempted"

#---------------------------------------------------------------------------
# STEP 1: Run the save hook best-effort while server is still alive
#---------------------------------------------------------------------------
if curl -sf --max-time 1 "$MCP_HOST/health" > /dev/null 2>&1; then
    SAVE_OUTPUT=$(printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook stop --harness claude-code --transport http 2>&1) && SAVE_STATUS="ok" || SAVE_STATUS="failed"
else
    SAVE_OUTPUT=$(printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook stop --harness claude-code 2>&1) && SAVE_STATUS="ok" || SAVE_STATUS="failed"
fi

if [[ "$SAVE_STATUS" == "failed" ]]; then
    echo "WARNING: mempalace hook save failed (server may already be stopping), proceeding to unregister" >&2
fi

#---------------------------------------------------------------------------
# STEP 2: ALWAYS unregister session — even if save failed (no set -e)
#---------------------------------------------------------------------------
if [[ -x "$SERVER_CONTROL" ]]; then
    STOP_OUTPUT=$("$SERVER_CONTROL" stop "$SESSION_ID" 2>&1) && STOP_STATUS="ok" || STOP_STATUS="failed"
    if [[ "$STOP_STATUS" == "failed" ]]; then
        echo "WARNING: server-control stop failed: $STOP_OUTPUT" >&2
    fi
else
    echo "WARNING: $SERVER_CONTROL not found or not executable, session not unregistered" >&2
fi

exit 0