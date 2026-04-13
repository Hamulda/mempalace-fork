#!/bin/bash
# MemPalace MCP Server — Dev s hot reload
# Použij pro vývoj = změny se projeví bez restartu

export MEMPALACE_TRANSPORT="${MEMPALACE_TRANSPORT:-http}"
export MEMPALACE_HOST="${MEMPALACE_HOST:-127.0.0.1}"
export MEMPALACE_PORT="${MEMPALACE_PORT:-8765}"
export MEMPALACE_LOG_SESSIONS="${MEMPALACE_LOG_SESSIONS:-true}"

echo "Starting MemPalace MCP dev server on http://${MEMPALACE_HOST}:${MEMPALACE_PORT}/mcp"
echo "Hot reload enabled (watching for changes)"
echo "Press Ctrl+C to stop"
echo ""

cd "$(dirname "$0")/.." || exit 1

# Use fastmcp run with reload
exec fastmcp run mempalace.fastmcp_server \
    --transport streamable-http \
    --host "$MEMPALACE_HOST" \
    --port "$MEMPALACE_PORT" \
    --reload
