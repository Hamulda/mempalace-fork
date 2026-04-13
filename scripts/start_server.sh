#!/bin/bash
# MemPalace MCP Server — Streamable HTTP
# Spouštěj PŘED otevřením Claude Code sessions
#
# Usage: ./scripts/start_server.sh
# Nebo:  MEMPALACE_TRANSPORT=http MEMPALACE_HOST=127.0.0.1 MEMPALACE_PORT=8765 python -m mempalace.fastmcp_server

export MEMPALACE_TRANSPORT="${MEMPALACE_TRANSPORT:-http}"
export MEMPALACE_HOST="${MEMPALACE_HOST:-127.0.0.1}"
export MEMPALACE_PORT="${MEMPALACE_PORT:-8765}"

echo "Starting MemPalace MCP server on http://${MEMPALACE_HOST}:${MEMPALACE_PORT}/mcp"
echo "Transport: ${MEMPALACE_TRANSPORT}"
echo "Press Ctrl+C to stop"
echo ""

cd "$(dirname "$0")/.." || exit 1
python -m mempalace.fastmcp_server
