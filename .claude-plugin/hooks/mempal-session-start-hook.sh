#!/bin/bash
# MemPalace SessionStart Hook — thin wrapper with HTTP server fast-path
INPUT=$(cat)
MCP_HOST="http://127.0.0.1:8765"

if curl -sf --max-time 1 "$MCP_HOST/health" > /dev/null 2>&1; then
    printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook session-start --harness claude-code --transport http
else
    printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook session-start --harness claude-code
fi
