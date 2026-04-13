#!/bin/bash
# MemPalace SessionStart Hook — inject relevant memories as session context
# Python has its own timeout; || true ensures exit 0 always
INPUT=$(cat)
printf "%s" "$INPUT" | python3 -m mempalace hook run --hook session-start --harness claude-code 2>/dev/null || true
