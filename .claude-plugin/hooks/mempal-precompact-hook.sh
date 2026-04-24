#!/bin/bash
# MemPalace PreCompact Hook — SYNCHRONOUS, always blocks until save completes
# macOS bash 3.2 compatible — uses timeout/gtimeout if available, else perl alarm
INPUT=$(cat)
MCP_HOST="http://127.0.0.1:8765"

# Resolve python3 path for perl alarm wrapper
resolve_python() {
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    else
        echo "/usr/bin/python3"
    fi
}
PYTHON_CMD=$(resolve_python)

run_with_timeout() {
    local cmd="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout 55s "$cmd" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout 55s "$cmd" "$@"
    else
        # macOS fallback: use perl alarm (works on all Unix)
        perl -e '
use strict;
use POSIX qw(strftime);
$SIG{ALRM} = sub {
    print STDERR "[precompact-hook] timeout after 55s\n";
    exit(124);
};
alarm 55;
exec(@ARGV) or die "exec failed: $!";
' -- "$cmd" "$@"
    fi
}

# Try HTTP first, then CLI fallback
if curl -sf --max-time 1 "$MCP_HOST/health" > /dev/null 2>&1; then
    run_with_timeout "$PYTHON_CMD" -m mempalace hook run \
        --hook precompact --harness claude-code --transport http <<< "$INPUT"
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ]; then
        run_with_timeout "$PYTHON_CMD" -m mempalace hook run \
            --hook precompact --harness claude-code <<< "$INPUT"
    fi
else
    run_with_timeout "$PYTHON_CMD" -m mempalace hook run \
        --hook precompact --harness claude-code <<< "$INPUT"
fi
