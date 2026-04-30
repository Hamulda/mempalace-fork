#!/bin/bash
#===============================================================================
# mempal-server-control.sh — Session-aware lifecycle controller for MemPalace MCP HTTP server
#
# Manages a single shared server via session refcounting.
# Runtime state: ~/.mempalace/runtime/
#
# Commands:
#   start <session_id>     Register session, start server if not running
#   stop  <session_id>     Unregister session, shutdown if zero sessions remain
#   status                 Print server status (machine-readable)
#   prune                  Remove stale session files older than TTL
#   shutdown-if-idle       Graceful shutdown if no sessions remain (internal)
#
# Compatible with: bash 3.2+ (macOS), zsh, bash 4+
#===============================================================================

set -uo pipefail  # no -e so lock acquisition can fail gracefully

RUNTIME_DIR="${RUNTIME_DIR:-$HOME/.mempalace/runtime}"
SESSIONS_DIR="$RUNTIME_DIR/sessions"
SERVER_PID_FILE="$RUNTIME_DIR/server.pid"
SERVER_LOG="$RUNTIME_DIR/server.log"
SERVER_ERR_LOG="$RUNTIME_DIR/server.err.log"
LOCK_FILE="$RUNTIME_DIR/control.lock"

# Tunables
export MEMPALACE_SESSION_TTL_SECONDS="${MEMPALACE_SESSION_TTL_SECONDS:-21600}"  # 6 hours
HEALTH_URL="http://127.0.0.1:8765/health"
STARTUP_WAIT_SECONDS="${STARTUP_WAIT_SECONDS:-10}"
GRACE_PERIOD_SECONDS="${GRACE_PERIOD_SECONDS:-20}"
LOCK_MAX_WAIT="${LOCK_MAX_WAIT:-30}"

#-------------------------------------------------------------------------------
# Helpers
#-------------------------------------------------------------------------------
log_msg() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $$ $*"
}

runtime_dir() {
    mkdir -p "$RUNTIME_DIR"
    mkdir -p "$SESSIONS_DIR"
}

# safe_session_id — sanitize raw session id to safe filename characters.
# If result is empty, returns a deterministic sha256 hash of the raw input.
# Bash 3.2 compatible (no associative arrays, no regex).
safe_session_id() {
    local raw="$1"
    local safe
    safe=$(echo "$raw" | tr -cd 'a-zA-Z0-9_-')
    if [[ -z "$safe" ]]; then
        # Deterministic fallback: sha256 hash of raw input via stdin (no injection risk)
        safe=$(python3 -c "
import hashlib, sys
raw = sys.stdin.read()
print('id-' + hashlib.sha256(raw.encode()).hexdigest()[:12])
" <<< "$raw")
    fi
    echo "$safe"
}

# acquire_lock — bash 3.x compatible. Blocks up to LOCK_MAX_WAIT real seconds.
# Creates $LOCK_FILE as a directory containing lock.token and lock.pid files.
# lock.pid is written INSIDE this function (not by caller) to avoid token/pid race.
acquire_lock() {
    local max_wait="${LOCK_MAX_WAIT:-30}"
    local start now elapsed

    # Ensure lock dir exists
    mkdir -p "$LOCK_FILE" || return 1

    start=$(date +%s)

    # Wait for lock
    while true; do
        # Try to atomically claim the lock using noclobber redirect
        local token="$$-$(date +%s%N)"
        if (set -C; echo "$token" > "$LOCK_FILE/lock.token" 2>/dev/null); then
            # We got the lock — write our PID before releasing
            echo "$$" > "$LOCK_FILE/lock.pid"
            echo "$token"
            return 0
        fi

        # Check if lock holder process is still alive
        local holder_pid
        holder_pid=$(cat "$LOCK_FILE/lock.pid" 2>/dev/null || echo "")
        if [[ -n "$holder_pid" ]]; then
            if ! kill -0 "$holder_pid" 2>/dev/null; then
                # Stale lock — holder is dead, clean up and retry
                rm -f "$LOCK_FILE/lock.token" "$LOCK_FILE/lock.pid"
                continue
            fi
        else
            # No PID recorded but lock.token exists — check token age to avoid
            # deleting a freshly-created token that hasn't had PID written yet.
            # Use a short stale threshold (2s) so the race window is bounded.
            local token_age
            token_age=$(stat -f %m "$LOCK_FILE/lock.token" 2>/dev/null || stat -c %Y "$LOCK_FILE/lock.token" 2>/dev/null || echo 0)
            now=$(date +%s)
            if [[ $((now - token_age)) -gt 2 ]]; then
                # Token older than 2s with no PID — treat as stale orphan
                rm -f "$LOCK_FILE/lock.token" "$LOCK_FILE/lock.pid"
                continue
            fi
            # Token is fresh (≤2s) — it may be mid-creation; wait and retry
        fi

        now=$(date +%s)
        elapsed=$((now - start))
        if [[ "$elapsed" -ge "$max_wait" ]]; then
            echo "ERROR: could not acquire lock after ${max_wait}s real time" >&2
            return 1
        fi

        sleep 0.2
    done
}

# release_lock — bash 3.x compatible
release_lock() {
    local token="$1"
    rm -f "$LOCK_FILE/lock.token" "$LOCK_FILE/lock.pid" 2>/dev/null || true
}

is_server_healthy() {
    curl -sf --max-time 1 "$HEALTH_URL" > /dev/null 2>&1
}

read_pid() {
    if [[ -f "$SERVER_PID_FILE" ]]; then
        cat "$SERVER_PID_FILE" 2>/dev/null || echo ""
    else
        echo ""
    fi
}

is_pid_running() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

#-------------------------------------------------------------------------------
# do_prune — called inside lock
#-------------------------------------------------------------------------------
do_prune() {
    local ttl="$MEMPALACE_SESSION_TTL_SECONDS"
    local now
    now=$(date +%s)
    local pruned=0

    if [[ ! -d "$SESSIONS_DIR" ]]; then
        return 0
    fi

    for session_file in "$SESSIONS_DIR"/*; do
        [[ -f "$session_file" ]] || continue
        local mtime age
        # macOS: stat -f %m  |  Linux: stat -c %Y
        mtime=$(stat -f %m "$session_file" 2>/dev/null || stat -c %Y "$session_file" 2>/dev/null || echo 0)
        age=$((now - mtime))
        if [[ "$age" -gt "$ttl" ]]; then
            rm -f "$session_file"
            pruned=$((pruned + 1))
            log_msg "pruned stale session: $(basename "$session_file") (age=${age}s)"
        fi
    done

    log_msg "prune done: removed $pruned stale sessions"
}

#-------------------------------------------------------------------------------
# active_session_count
#-------------------------------------------------------------------------------
active_session_count() {
    if [[ ! -d "$SESSIONS_DIR" ]]; then
        echo 0
        return
    fi
    local count
    count=$(ls -1 "$SESSIONS_DIR"/ 2>/dev/null | grep -v '^$' | wc -l | tr -d ' ')
    echo "${count:-0}"
}

#-------------------------------------------------------------------------------
# register_session
#-------------------------------------------------------------------------------
register_session() {
    local session_id="$1"
    local safe_id
    safe_id=$(safe_session_id "$session_id")

    touch "$SESSIONS_DIR/$safe_id"
    log_msg "registered session: $session_id → $SESSIONS_DIR/$safe_id"
}

#-------------------------------------------------------------------------------
# unregister_session
#-------------------------------------------------------------------------------
unregister_session() {
    local session_id="$1"
    local safe_id
    safe_id=$(safe_session_id "$session_id")

    local path="$SESSIONS_DIR/$safe_id"
    if [[ -f "$path" ]]; then
        rm -f "$path"
        log_msg "unregistered session: $session_id → $path"
    fi
}

#-------------------------------------------------------------------------------
# start_server — PATH-robust: prefers mempalace binary, falls back to python -m
#-------------------------------------------------------------------------------
start_server() {
    log_msg "starting mempalace server..."

    if is_server_healthy; then
        log_msg "start_server: server already healthy"
        return 0
    fi

    # Disable GPU for MacBook Air M1 (UMA — GPU=CPU, slow)
    export PYTORCH_MPS_GPU_BACKEND="${PYTORCH_MPS_GPU_BACKEND:-}"

    # Use project .venv python (has all dependencies: fastmcp, mempalace, lancedb)
    # pyenv shims resolve to system-managed Python which lacks these packages
    # Hardcode project root to avoid SCRIPT_DIR reference issues with set -u
    PYTHON_BIN="/Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/.venv/bin/python3"
    if [[ ! -x "$PYTHON_BIN" ]]; then
        # Fallback: try pyenv-resolved python (may lack packages)
        if command -v python3 >/dev/null 2>&1; then
            PYTHON_BIN="python3"
        else
            PYTHON_BIN="/Users/vojtechhamada/.pyenv/versions/3.14/bin/python3"
        fi
    fi

    # Prefer mempalace binary if available; otherwise use python -m
    if command -v mempalace >/dev/null 2>&1; then
        SERVE_CMD="mempalace serve --host 127.0.0.1 --port 8765"
        log_msg "start_server: using mempalace binary"
    else
        SERVE_CMD="$PYTHON_BIN -m mempalace serve --host 127.0.0.1 --port 8765"
        log_msg "start_server: using $PYTHON_BIN -m mempalace"
    fi

    nohup sh -c "exec $SERVE_CMD" \
        >> "$SERVER_LOG" \
        2>> "$SERVER_ERR_LOG" &

    local pid=$!
    echo "$pid" > "$SERVER_PID_FILE"
    log_msg "server spawned with pid $pid"

    local waited=0
    while [[ "$waited" -lt "$STARTUP_WAIT_SECONDS" ]]; do
        if is_server_healthy; then
            log_msg "server healthy after ${waited}s"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if is_pid_running "$pid"; then
        log_msg "server started but health check timed out after ${STARTUP_WAIT_SECONDS}s"
        return 0
    else
        log_msg "ERROR: server exited immediately (pid $pid died)"
        return 1
    fi
}

#-------------------------------------------------------------------------------
# stop_server — only kills MemPalace server process (PID-safe verification)
#-------------------------------------------------------------------------------
stop_server() {
    local pid
    pid=$(read_pid)

    if [[ -z "$pid" ]] || ! is_pid_running "$pid"; then
        log_msg "stop_server: no running pid found, cleaning up state"
        rm -f "$SERVER_PID_FILE"
        return 0
    fi

    # Verify pid is actually a MemPalace server before killing
    # ps -p "$pid" -o command= returns full command line of pid (no header)
    # Wrap with timeout(1) to prevent hanging on zombie/waitpid-stuck processes
    local cmd_line
    if command -v timeout >/dev/null 2>&1; then
        cmd_line=$(timeout 1 ps -p "$pid" -o command= 2>/dev/null || echo "")
    elif command -v gtimeout >/dev/null 2>&1; then
        cmd_line=$(gtimeout 1 ps -p "$pid" -o command= 2>/dev/null || echo "")
    else
        # macOS without GNU timeout — use perl alarm wrapper inline
        cmd_line=$(perl -e '
use strict;
alarm 1;
exec("ps", "-p", $ARGV[0], "-o", "command=");
print "";  # fallback if alarm fires
' -- "$pid" 2>/dev/null || echo "")
    fi
    if [[ -z "$cmd_line" ]]; then
        log_msg "stop_server: pid $pid vanished, cleaning up"
        rm -f "$SERVER_PID_FILE"
        return 0
    fi

    # Allow only mempalace serve or python...mempalace...serve patterns
    local is_mempalace=0
    if echo "$cmd_line" | grep -qE 'mempalace.?serve|python.*mempalace.*serve'; then
        is_mempalace=1
    fi

    if [[ "$is_mempalace" -eq 0 ]]; then
        log_msg "stop_server: pid $pid command='$cmd_line' does not look like MemPalace server, removing stale pid file"
        rm -f "$SERVER_PID_FILE"
        return 0
    fi

    log_msg "stopping server pid $pid (command: $cmd_line)..."

    if kill -TERM "$pid" 2>/dev/null; then
        local waited=0
        while [[ "$waited" -lt 5 ]]; do
            if ! is_pid_running "$pid"; then
                log_msg "server pid $pid terminated gracefully"
                rm -f "$SERVER_PID_FILE"
                return 0
            fi
            sleep 1
            waited=$((waited + 1))
        done
    fi

    if is_pid_running "$pid"; then
        log_msg "graceful termination failed, sending SIGKILL to $pid"
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
        rm -f "$SERVER_PID_FILE"
    fi

    return 0
}

#-------------------------------------------------------------------------------
# shutdown_if_idle — caller holds lock; releases during grace, reacquires after
#-------------------------------------------------------------------------------
shutdown_if_idle() {
    do_prune
    local count
    count=$(active_session_count)

    if [[ "$count" -gt 0 ]]; then
        log_msg "shutdown_if_idle: $count sessions still active, leaving server running"
        return 0
    fi

    # Release lock during grace period so new sessions can register
    log_msg "shutdown_if_idle: zero sessions, releasing lock for ${GRACE_PERIOD_SECONDS}s grace period..."
    release_lock_from_shutdown

    # Incremental sleep with session-check on each second (for fast test response)
    local grace_remaining="$GRACE_PERIOD_SECONDS"
    while [[ "$grace_remaining" -gt 0 ]]; do
        sleep 1
        grace_remaining=$((grace_remaining - 1))
        # Check if a new session has registered (without acquiring lock — just file check)
        if [[ -d "$SESSIONS_DIR" ]] && ls -1 "$SESSIONS_DIR"/ 2>/dev/null | grep -q .; then
            log_msg "shutdown_if_idle: session registered during grace period, keeping server"
            return 0
        fi
    done

    # Reacquire lock to proceed with shutdown
    local lock_token
    lock_token=$(acquire_lock) || {
        log_msg "shutdown_if_idle: could not reacquire lock, aborting shutdown"
        return 1
    }

    do_prune
    count=$(active_session_count)
    if [[ "$count" -gt 0 ]]; then
        log_msg "shutdown_if_idle: sessions appeared during grace period, keeping server"
        release_lock "$lock_token"
        return 0
    fi

    log_msg "shutdown_if_idle: still zero sessions after grace, initiating graceful shutdown"
    stop_server
    release_lock "$lock_token"
}

release_lock_from_shutdown() {
    # Called from shutdown_if_idle to release lock before sleep
    rm -f "$LOCK_FILE/lock.token" "$LOCK_FILE/lock.pid" 2>/dev/null || true
}

#-------------------------------------------------------------------------------
# CMD: start
#-------------------------------------------------------------------------------
cmd_start() {
    local session_id="${1:-}"
    [[ -z "$session_id" ]] && { echo "ERROR: start requires <session_id>" >&2; exit 1; }

    runtime_dir

    local lock_token
    lock_token=$(acquire_lock) || { echo "ERROR: could not acquire lock" >&2; exit 1; }

    do_prune
    register_session "$session_id"

    if is_server_healthy; then
        log_msg "start: server already healthy (pid=$(read_pid))"
    else
        local pid_before
        pid_before=$(read_pid)
        if [[ -n "$pid_before" ]] && ! is_pid_running "$pid_before"; then
            log_msg "start: stale pid $pid_before not running"
            rm -f "$SERVER_PID_FILE"
        fi
        if ! start_server; then
            log_msg "ERROR: start failed to spawn server"
            unregister_session "$session_id"
            release_lock "$lock_token"
            exit 1
        fi
    fi

    release_lock "$lock_token"
    exit 0
}

#-------------------------------------------------------------------------------
# CMD: stop
#-------------------------------------------------------------------------------
cmd_stop() {
    local session_id="${1:-}"
    [[ -z "$session_id" ]] && { echo "ERROR: stop requires <session_id>" >&2; exit 1; }

    local lock_token
    lock_token=$(acquire_lock) || { echo "ERROR: could not acquire lock" >&2; exit 1; }

    unregister_session "$session_id"

    # Prune stale sessions before counting — stale files must not keep server alive
    do_prune

    local count
    count=$(active_session_count)
    log_msg "stop: session unregistered, $count sessions remain after prune"

    if [[ "$count" -eq 0 ]]; then
        # Release lock during grace so new sessions can register and prevent shutdown
        release_lock "$lock_token"

        local grace_remaining="$GRACE_PERIOD_SECONDS"
        while [[ "$grace_remaining" -gt 0 ]]; do
            sleep 1
            grace_remaining=$((grace_remaining - 1))
            # Check if a new session has registered (no lock needed for file check)
            if [[ -d "$SESSIONS_DIR" ]] && ls -1 "$SESSIONS_DIR"/ 2>/dev/null | grep -q .; then
                log_msg "stop: new session registered during grace, keeping server"
                return 0
            fi
        done

        # Reacquire to verify still zero
        lock_token=$(acquire_lock) || {
            log_msg "stop: grace period interrupted by lock acquisition, server continuing"
            return 0
        }
        do_prune
        count=$(active_session_count)
        if [[ "$count" -gt 0 ]]; then
            log_msg "stop: sessions appeared during grace, keeping server"
            release_lock "$lock_token"
            return 0
        fi
        log_msg "stop: still zero sessions after grace, shutting down server"
        stop_server
        release_lock "$lock_token"
    else
        log_msg "stop: $count sessions active, server continues running"
        release_lock "$lock_token"
    fi

    exit 0
}

#-------------------------------------------------------------------------------
# CMD: status
#-------------------------------------------------------------------------------
cmd_status() {
    local pid
    pid=$(read_pid)
    local running="false"
    local health="fail"

    if [[ -n "$pid" ]] && is_pid_running "$pid" && is_server_healthy; then
        running="true"
        health="ok"
    fi

    local count=0
    local session_list=""
    if [[ -d "$SESSIONS_DIR" ]]; then
        count=$(ls -1 "$SESSIONS_DIR"/ 2>/dev/null | wc -l | tr -d ' ')
        session_list=$(ls -1 "$SESSIONS_DIR"/ 2>/dev/null | tr '\n' ' ' | sed 's/ $//')
        [[ -z "$session_list" ]] && session_list="(none)"
    fi

    echo "server_running=$running"
    echo "pid=${pid:-none}"
    echo "active_sessions=$count"
    echo "session_files=${session_list:-error}"
    echo "health=$health"
}

#-------------------------------------------------------------------------------
# CMD: prune
#-------------------------------------------------------------------------------
cmd_prune() {
    local lock_token
    lock_token=$(acquire_lock) || { echo "ERROR: could not acquire lock" >&2; exit 1; }
    do_prune
    release_lock "$lock_token"
    exit 0
}

#-------------------------------------------------------------------------------
# CMD: shutdown-if-idle
#-------------------------------------------------------------------------------
cmd_shutdown_if_idle() {
    local lock_token
    lock_token=$(acquire_lock) || { echo "ERROR: could not acquire lock" >&2; exit 1; }
    shutdown_if_idle
    release_lock "$lock_token"
    exit 0
}

#-------------------------------------------------------------------------------
# Dispatch
#-------------------------------------------------------------------------------
SUBCMD="${1:-}"
case "$SUBCMD" in
    start)
        shift; cmd_start "$@"
        ;;
    stop)
        shift; cmd_stop "$@"
        ;;
    status)
        cmd_status
        ;;
    prune)
        cmd_prune
        ;;
    shutdown-if-idle)
        cmd_shutdown_if_idle
        ;;
    *)
        echo "Usage: $0 {start|stop|status|prune|shutdown-if-idle} [<session_id>]"
        echo ""
        echo "  start            <session_id>   Register session, start server if needed"
        echo "  stop             <session_id>   Unregister session, shutdown if idle"
        echo "  status                          Print machine-readable status"
        echo "  prune                           Remove stale sessions (TTL-based)"
        echo "  shutdown-if-idle                 Shutdown if zero sessions (internal)"
        exit 1
        ;;
esac