#!/bin/bash
#===============================================================================
# mempal-stop-hook.sh — MemPalace Stop hook with lifecycle control
#
# CRITICAL ORDERING:
#   1. Derive SESSION_ID from hook JSON
#   2. Run mempalace hook save (while server is still alive) — via HTTP preferred
#   3. Unregister session via server-control stop (may shutdown server)
#
# The save must happen BEFORE any server shutdown so memory state is preserved.
#===============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_CONTROL="$SCRIPT_DIR/mempal-server-control.sh"

#-------------------------------------------------------------------------------
# Derive SESSION_ID from stdin JSON (same logic as session-start-hook)
#-------------------------------------------------------------------------------
derive_session_id() {
    python3 -c "
import sys, json, hashlib

try:
    data = json.loads(sys.stdin.read())
except (json.JSONDecodeError, EOFError):
    print('unknown', end='')
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

h = hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:12]
print(f'unknown-{h}', end='')
"
}

INPUT=$(cat)

SESSION_ID=$(derive_session_id <<< "$INPUT")
if [[ -z "$SESSION_ID" ]]; then
    SESSION_ID="unknown-$(date +%s)"
fi

MCP_HOST="http://127.0.0.1:8765"

#---------------------------------------------------------------------------
# STEP 1: Run the save hook while server is still alive (if health is ok)
#---------------------------------------------------------------------------
if curl -sf --max-time 1 "$MCP_HOST/health" > /dev/null 2>&1; then
    printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook stop --harness claude-code --transport http
else
    # Server unreachable — still call CLI fallback so save logic runs
    echo "WARNING: MCP server not reachable, running stop hook via CLI" >&2
    printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook stop --harness claude-code
fi

#---------------------------------------------------------------------------
# STEP 2: Unregister session and possibly shutdown server
#---------------------------------------------------------------------------
if [[ -x "$SERVER_CONTROL" ]]; then
    bash "$SERVER_CONTROL" stop "$SESSION_ID" 2>&2 || true
else
    echo "WARNING: $SERVER_CONTROL not found or not executable, session not unregistered" >&2
fi

exit 0