#!/bin/bash
# MemPalace PreCompact Hook — SYNCHRONOUS, always blocks until save completes
INPUT=$(cat)
MCP_HOST="http://127.0.0.1:8765"

if curl -sf --max-time 1 "$MCP_HOST/health" > /dev/null 2>&1; then
    # Synchronous HTTP call — waits for completion (timeout 55s)
    printf '%s' "$INPUT" | timeout 55s python3 -m mempalace hook run \
        --hook precompact --harness claude-code --transport http
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        # HTTP failed — fallback to CLI (safety net)
        printf '%s' "$INPUT" | python3 -m mempalace hook run \
            --hook precompact --harness claude-code
    fi
else
    printf '%s' "$INPUT" | python3 -m mempalace hook run \
        --hook precompact --harness claude-code
fi
