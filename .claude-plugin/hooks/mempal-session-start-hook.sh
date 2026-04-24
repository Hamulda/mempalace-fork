#!/bin/bash
#===============================================================================
# mempal-session-start-hook.sh — MemPalace SessionStart hook with lifecycle control
#
# 1. Derives SESSION_ID from hook JSON
# 2. Registers session with lifecycle controller (server-control start)
# 3. Calls mempalace hook run via HTTP (with CLI fallback)
#===============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_CONTROL="$SCRIPT_DIR/mempal-server-control.sh"

#-------------------------------------------------------------------------------
# Derive SESSION_ID from stdin JSON using Python (robust, order: session_id,
# sessionId, session.id, transcript_path, cwd+timestamp, hash of full input)
#-------------------------------------------------------------------------------
derive_session_id() {
    python3 -c "
import sys, json, hashlib, os

try:
    data = json.loads(sys.stdin.read())
except (json.JSONDecodeError, EOFError):
    print('unknown', end='')
    sys.exit(0)

# Try field names in priority order
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
        # sanitize to safe filename chars
        safe = ''.join(c if c.isalnum() or c in '_-' else '-' for c in s)
        print(safe, end='')
        sys.exit(0)

# Fallback: cwd + timestamp hash
cwd = str(data.get('cwd', '') or '')
ts = str(data.get('timestamp', '') or '')
if cwd or ts:
    h = hashlib.sha256((cwd + ts).encode()).hexdigest()[:12]
    print(f'fallback-{h}', end='')
    sys.exit(0)

# Last resort: hash entire input
h = hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:12]
print(f'unknown-{h}', end='')
"
}

# Read INPUT once
INPUT=$(cat)

# Derive session ID
SESSION_ID=$(derive_session_id <<< "$INPUT")
if [[ -z "$SESSION_ID" ]]; then
    SESSION_ID="unknown-$(date +%s)"
fi

# Register session + start server if needed
if [[ -x "$SERVER_CONTROL" ]]; then
    if ! bash "$SERVER_CONTROL" start "$SESSION_ID" 2>&2; then
        echo "WARNING: server-control start failed, server may not be running" >&2
    fi
else
    echo "WARNING: $SERVER_CONTROL not found or not executable" >&2
fi

# Call mempalace hook run via HTTP if server is healthy, else CLI fallback
MCP_HOST="http://127.0.0.1:8765"

if curl -sf --max-time 1 "$MCP_HOST/health" > /dev/null 2>&1; then
    printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook session-start --harness claude-code --transport http
else
    echo "WARNING: MCP server not reachable, falling back to CLI transport" >&2
    printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook session-start --harness claude-code
fi

exit 0