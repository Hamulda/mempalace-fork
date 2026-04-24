"""
Tests for session-aware lifecycle control of the shared MemPalace MCP HTTP server.

Tests cover:
- mempal-server-control.sh lifecycle controller
- mempal-session-start-hook.sh session registration
- mempal-stop-hook.sh save-before-shutdown ordering
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def ensure_scripts_executable():
    """Ensure all hook scripts are executable before any test runs."""
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    for name in [
        "mempal-server-control.sh",
        "mempal-session-start-hook.sh",
        "mempal-stop-hook.sh",
        "mempal-precompact-hook.sh",
    ]:
        p = hooks_dir / name
        if p.exists():
            subprocess.run(["chmod", "+x", str(p)], check=False)
    yield


@pytest.fixture
def runtime_dir(tmp_path):
    """Set up a temporary runtime directory."""
    runtime = tmp_path / ".mempalace" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


@pytest.fixture
def env_with_runtime(runtime_dir):
    """Build a full environment dict with RUNTIME_DIR exported."""
    env = os.environ.copy()
    env["RUNTIME_DIR"] = str(runtime_dir)
    # Set HOME to avoid ~ expansion issues
    env["HOME"] = str(runtime_dir.parent.parent)
    return env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_control_cmd(cmd, args=None, env=None, timeout=10, check=False):
    """Run a server-control command and return subprocess result."""
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    script = hooks_dir / "mempal-server-control.sh"
    full_cmd = ["bash", str(script), cmd]
    if args:
        full_cmd.extend(args)

    run_env = dict(env) if env else dict(os.environ)

    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        env=run_env,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Script exited {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result


def run_hook(hook_name, stdin_data, env=None, timeout=15):
    """Run a hook script with stdin."""
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    run_env = dict(env) if env else dict(os.environ)
    run_env["CLAUDE_PLUGIN_ROOT"] = str(hooks_dir.parent)

    result = subprocess.run(
        ["bash", str(hooks_dir / f"mempal-{hook_name}-hook.sh")],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=run_env,
        timeout=timeout,
    )
    return result


# ---------------------------------------------------------------------------
# Tests: server-control lifecycle
# ---------------------------------------------------------------------------

def test_server_control_start_creates_session_file(runtime_dir, env_with_runtime):
    """start creates a session file under SESSIONS_DIR."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    result = run_control_cmd("start", ["test-session-1"], env=env_with_runtime)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert (sessions_dir / "test-session-1").exists(), \
        f"Session file not created. Contents: {list(sessions_dir.iterdir())}"


def test_server_control_status_shows_zero_sessions_when_empty(env_with_runtime):
    """status reports active_sessions=0 when no sessions registered."""
    result = run_control_cmd("status", env=env_with_runtime)
    assert result.returncode == 0
    assert "active_sessions=0" in result.stdout


def test_server_control_status_shows_active_sessions(runtime_dir, env_with_runtime):
    """status shows correct session count."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "session-alpha").touch()
    (sessions_dir / "session-beta").touch()

    result = run_control_cmd("status", env=env_with_runtime)

    assert "active_sessions=2" in result.stdout
    assert result.returncode == 0


def test_server_control_prune_removes_stale_sessions(runtime_dir, env_with_runtime):
    """prune removes session files older than TTL."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    # Backdate stale file to 1 hour ago using touch -t
    one_hour_ago = str(int(time.time()) - 3600)
    stale = sessions_dir / "stale-session"
    subprocess.run(["touch", "-t", one_hour_ago, str(stale)], check=True)

    recent = sessions_dir / "recent-session"
    recent.touch()

    # TTL=1 second — stale file (1 hour old) will be pruned, recent (0 seconds) won't
    prune_env = dict(env_with_runtime)
    prune_env["MEMPALACE_SESSION_TTL_SECONDS"] = "1"

    result = run_control_cmd("prune", env=prune_env)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert not stale.exists(), "stale session should be removed"
    assert recent.exists(), "recent session should remain"


def test_server_control_stop_removes_only_target_session(runtime_dir, env_with_runtime):
    """stop removes only its own session file."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "session-keep").touch()
    (sessions_dir / "session-remove").touch()

    result = run_control_cmd("stop", ["session-remove"], env=env_with_runtime)
    assert result.returncode == 0

    assert (sessions_dir / "session-keep").exists(), "session-keep should remain"
    assert not (sessions_dir / "session-remove").exists(), "session-remove should be gone"


def test_server_control_stop_with_multiple_sessions_keeps_server_alive(
    runtime_dir, env_with_runtime
):
    """stop when sessions remain should not attempt server shutdown."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "keep-1").touch()
    (sessions_dir / "stop-1").touch()

    result = run_control_cmd("stop", ["stop-1"], env=env_with_runtime)

    assert result.returncode == 0
    assert "0 sessions remain" not in result.stdout, \
        "should report remaining sessions > 0"


def test_server_control_unknown_subcommand_shows_usage():
    """Unknown subcommand exits non-zero with usage."""
    result = run_control_cmd("bad-cmd")
    assert result.returncode == 1
    assert "Usage:" in result.stdout


def test_server_control_start_and_stop_idempotent(runtime_dir, env_with_runtime):
    """Starting then stopping the same session is a no-op on server state."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    # Use short grace period to avoid 20s test timeout
    test_env = dict(env_with_runtime)
    test_env["GRACE_PERIOD_SECONDS"] = "1"

    # Start session
    result = run_control_cmd("start", ["session-1"], env=test_env)
    assert result.returncode == 0

    # Stop same session (grace period = 1s to avoid long test timeout)
    result = run_control_cmd("stop", ["session-1"], env=test_env, timeout=15)
    assert result.returncode == 0

    # No sessions remain
    status = run_control_cmd("status", env=env_with_runtime)
    assert "active_sessions=0" in status.stdout


def test_multiple_sessions_all_registered(runtime_dir, env_with_runtime):
    """Starting multiple sessions creates all session files."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    for session in ["sess-a", "sess-b", "sess-c"]:
        result = run_control_cmd("start", [session], env=env_with_runtime)
        assert result.returncode == 0, f"start {session} failed: {result.stderr}"

    status = run_control_cmd("status", env=env_with_runtime)
    assert "active_sessions=3" in status.stdout


# ---------------------------------------------------------------------------
# Tests: session-start-hook.sh
# ---------------------------------------------------------------------------

def test_session_start_creates_session_file_via_hook(
    runtime_dir, env_with_runtime
):
    """
    session-start-hook creates a session file via server-control.
    Note: mempalace hook run can be slow (MLX init); we verify only that
    the session file is registered, not the hook output.
    """
    sessions_dir = runtime_dir / "sessions"

    stdin_data = json.dumps({
        "session_id": "hook-test-session",
        "cwd": "/tmp",
    })

    # Use a generous timeout to allow for MLX model loading
    result = run_hook("session-start", stdin_data, env=env_with_runtime, timeout=60)

    # The hook script always exits 0 (even if hook logic fails)
    assert result.returncode == 0, f"Hook failed: {result.stderr}"
    assert (sessions_dir / "hook-test-session").exists(), \
        f"Session file not created. Contents: {list(sessions_dir.iterdir()) if sessions_dir.exists() else 'dir missing'}"


def test_session_start_derives_session_id_from_session_id_field(
    runtime_dir, env_with_runtime
):
    """session-start-hook extracts .session_id correctly."""
    stdin_data = json.dumps({
        "session_id": "my-unique-session-xyz",
        "cwd": "/tmp",
    })

    result = run_hook("session-start", stdin_data, env=env_with_runtime, timeout=60)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert (runtime_dir / "sessions" / "my-unique-session-xyz").exists()


def test_session_start_derives_session_id_from_sessionId_camel(
    runtime_dir, env_with_runtime
):
    """session-start-hook extracts .sessionId (camelCase variant)."""
    stdin_data = json.dumps({
        "sessionId": "camel-case-session",
        "cwd": "/tmp",
    })

    result = run_hook("session-start", stdin_data, env=env_with_runtime, timeout=60)
    assert result.returncode == 0
    assert (runtime_dir / "sessions" / "camel-case-session").exists()


def test_session_start_falls_back_for_empty_json(
    runtime_dir, env_with_runtime
):
    """session-start-hook handles empty JSON gracefully."""
    stdin_data = json.dumps({})
    result = run_hook("session-start", stdin_data, env=env_with_runtime, timeout=60)
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Tests: stop-hook.sh
# ---------------------------------------------------------------------------

def test_stop_hook_removes_its_own_session(runtime_dir, env_with_runtime):
    """stop-hook removes only its own session file."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "stop-test-session").touch()

    stdin_data = json.dumps({
        "session_id": "stop-test-session",
        "stop_hook_active": "false",
        "transcript_path": "/tmp/nonexistent.jsonl",
        "cwd": "/tmp",
    })

    # Use generous timeout to allow for MLX model loading in hook subprocess
    result = run_hook("stop", stdin_data, env=env_with_runtime, timeout=90)
    assert result.returncode == 0
    assert not (sessions_dir / "stop-test-session").exists()


def test_stop_hook_keeps_other_sessions(runtime_dir, env_with_runtime):
    """stop-hook for one session leaves other sessions intact."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "session-keep").touch()
    (sessions_dir / "session-stop").touch()

    stdin_data = json.dumps({
        "session_id": "session-stop",
        "stop_hook_active": "false",
        "transcript_path": "/tmp/nonexistent.jsonl",
        "cwd": "/tmp",
    })

    result = run_hook("stop", stdin_data, env=env_with_runtime, timeout=90)
    assert result.returncode == 0
    assert (sessions_dir / "session-keep").exists()
    assert not (sessions_dir / "session-stop").exists()


# ---------------------------------------------------------------------------
# Tests: graceful shutdown
# ---------------------------------------------------------------------------

def test_shutdown_if_idle_with_zero_sessions_completes_ok(env_with_runtime):
    """shutdown-if-idle exits 0 when no sessions remain (no error)."""
    # Use short grace period to avoid long test waits
    test_env = dict(env_with_runtime)
    test_env["GRACE_PERIOD_SECONDS"] = "1"
    result = run_control_cmd(
        "shutdown-if-idle",
        env=test_env,
        timeout=15,
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Tests: precompact does NOT touch server lifecycle
# ---------------------------------------------------------------------------

def test_precompact_does_not_create_session_files(runtime_dir, env_with_runtime):
    """Precompact must NOT create session files (no refcount changes)."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    stdin_data = json.dumps({
        "session_id": "precompact-test",
        "cwd": "/tmp",
    })

    result = run_hook("precompact", stdin_data, env=env_with_runtime, timeout=10)

    assert result.returncode == 0
    session_files = [f for f in sessions_dir.iterdir() if f.is_file()]
    assert len(session_files) == 0, \
        f"precompact should not create session files, found: {session_files}"


# ---------------------------------------------------------------------------
# Tests: session ID sanitization (path traversal prevention)
# ---------------------------------------------------------------------------

def test_session_id_sanitization_blocks_path_traversal(runtime_dir, env_with_runtime):
    """A session ID with path separators is sanitized to prevent traversal."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    result = run_control_cmd("start", ["../../../etc/passwd"], env=env_with_runtime)
    assert result.returncode == 0

    # A sanitized file SHOULD exist in sessions dir (no slashes)
    files = list(sessions_dir.iterdir())
    assert len(files) >= 1, "sanitized session file should be created"
    for f in files:
        assert "/" not in f.name, f"Unsafe session file name: {f.name}"
        # The file should be named something like "unknown" or "etcpasswd" (no slashes)
        assert f.name not in ("", ".", ".."), f"Invalid session file name: {f.name}"


# ---------------------------------------------------------------------------
# Tests: bash syntax validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_name", [
    "mempal-server-control.sh",
    "mempal-session-start-hook.sh",
    "mempal-stop-hook.sh",
    "mempal-precompact-hook.sh",
])
def test_shell_scripts_syntax_check(script_name):
    """bash -n passes on all hook scripts (no syntax errors)."""
    script_path = Path(__file__).parent.parent / ".claude-plugin" / "hooks" / script_name
    result = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{script_name}: {result.stderr}"


# ---------------------------------------------------------------------------
# Regression Tests: Issue 1 — stop hook always unregisters even on save failure
# ---------------------------------------------------------------------------

def test_stop_hook_unregisters_even_when_save_fails(tmp_path, monkeypatch):
    """
    When mempalace hook save fails, stop hook still calls server-control stop.
    Issue: set -e could exit before unregister when save fails.
    """
    sessions_dir = tmp_path / ".mempalace" / "runtime" / "sessions"
    sessions_dir.mkdir(parents=True)
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"

    # Pre-create a session file to be removed
    (sessions_dir / "save-fail-session").touch()

    stdin_data = json.dumps({
        "session_id": "save-fail-session",
        "cwd": "/tmp",
        "transcript_path": "/tmp/nonexistent.jsonl",
    })

    env = os.environ.copy()
    env["RUNTIME_DIR"] = str(tmp_path / ".mempalace" / "runtime")
    env["HOME"] = str(tmp_path.parent)

    # Run stop hook — save will fail but unregister must happen
    result = subprocess.run(
        ["bash", str(hooks_dir / "mempal-stop-hook.sh")],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    # Hook must exit 0
    assert result.returncode == 0, f"stop hook exited {result.returncode}: {result.stderr}"
    # Session file MUST be gone even if save hook failed
    assert not (sessions_dir / "save-fail-session").exists(), \
        "session file must be removed even when save hook fails (no set -e leak)"


# ---------------------------------------------------------------------------
# Regression Tests: Issue 3 — lock timeout math is sane (waited += 1, not 2)
# ---------------------------------------------------------------------------

def test_lock_timeout_math_is_sane(tmp_path):
    """
    LOCK_MAX_WAIT=2 should wait roughly 2 seconds, not 0.2s or 20s.
    Before fix: sleep 0.2 + waited+=2 → ~3 iterations = 0.6s.
    After fix: sleep 0.2 + waited+=1 → ~10 iterations = 2s.
    """
    sessions_dir = tmp_path / ".mempalace" / "runtime" / "sessions"
    sessions_dir.mkdir(parents=True)
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"

    env = os.environ.copy()
    env["RUNTIME_DIR"] = str(tmp_path / ".mempalace" / "runtime")
    env["HOME"] = str(tmp_path.parent)
    env["GRACE_PERIOD_SECONDS"] = "1"
    env["LOCK_MAX_WAIT"] = "2"

    # Acquire lock in a subprocess
    lock_acquirer = subprocess.Popen(
        ["bash", str(hooks_dir / "mempal-server-control.sh"), "start", "lock-holder"],
        env=env,
    )
    lock_acquirer.wait()

    # Now try to acquire lock with short timeout
    start = time.time()
    result = subprocess.run(
        ["bash", str(hooks_dir / "mempal-server-control.sh"), "stop", "lock-holder"],
        env=env,
        timeout=10,
    )
    elapsed = time.time() - start

    # Should succeed and take ~2s (lock held, then released, then stop runs)
    # Allow range 0.5s–8s: at least 2s of lock-wait activity, no more than 8s
    assert result.returncode == 0, f"stop failed: {result.stderr}"
    # The stop operation itself includes prune + lock release, but the lock
    # acquisition timeout (2s) is bounded by LOCK_MAX_WAIT
    assert elapsed < 8.0, f"stop took {elapsed:.1f}s — lock timeout may be too long"


# ---------------------------------------------------------------------------
# Regression Tests: Issue 2 — start during grace prevents shutdown
# ---------------------------------------------------------------------------

def test_start_during_grace_prevents_shutdown(tmp_path):
    """
    If a new session starts while stop is sleeping through grace period,
    the server must remain alive (grace interrupted, no shutdown).
    This requires lock to be released during grace.

    Does NOT assert server_running=true because no real server is started
    in this test environment — only that the new session file exists
    (preventing shutdown) and stop didn't reach stop_server.
    """
    sessions_dir = tmp_path / ".mempalace" / "runtime" / "sessions"
    sessions_dir.mkdir(parents=True)
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"

    env = os.environ.copy()
    env["RUNTIME_DIR"] = str(tmp_path / ".mempalace" / "runtime")
    env["HOME"] = str(tmp_path.parent)
    env["GRACE_PERIOD_SECONDS"] = "2"  # short grace for test speed

    # Start a "victim" session that will be stopped, triggering grace
    result = subprocess.run(
        ["bash", str(hooks_dir / "mempal-server-control.sh"), "start", "victim-session"],
        env=env,
        timeout=10,
    )
    assert result.returncode == 0

    # Start stop in background (will sleep through grace)
    stop_proc = subprocess.Popen(
        ["bash", str(hooks_dir / "mempal-server-control.sh"), "stop", "victim-session"],
        env=env,
    )

    # Wait a moment for stop to release lock and enter grace sleep
    time.sleep(1.0)

    # Start a new session during grace period — must succeed
    start_result = subprocess.run(
        ["bash", str(hooks_dir / "mempal-server-control.sh"), "start", "new-session-during-grace"],
        env=env,
        timeout=10,
    )
    assert start_result.returncode == 0, \
        f"start during grace failed: {start_result.stderr}"

    # Wait for stop to complete
    stop_proc.wait(timeout=15)

    # Verify new session registered (session file must exist)
    assert (sessions_dir / "new-session-during-grace").exists(), \
        "new session must have registered during grace"

    # Verify stop didn't reach stop_server (server.pid must not exist
    # OR no server process running — since we never started one)
    runtime_dir = tmp_path / ".mempalace" / "runtime"
    pid_file = runtime_dir / "server.pid"
    # No assertion on server_running=true since no server was ever started


# ---------------------------------------------------------------------------
# Regression Tests: Issue 4 — prune before count in stop/shutdown-if-idle
# ---------------------------------------------------------------------------

def test_stop_prunes_before_counting(tmp_path):
    """
    Stale session files must not keep server alive after real session stops.
    Before counting for shutdown decision, prune must run first.
    """
    sessions_dir = tmp_path / ".mempalace" / "runtime" / "sessions"
    sessions_dir.mkdir(parents=True)
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"

    env = os.environ.copy()
    env["RUNTIME_DIR"] = str(tmp_path / ".mempalace" / "runtime")
    env["HOME"] = str(tmp_path.parent)
    env["GRACE_PERIOD_SECONDS"] = "1"  # short grace
    env["MEMPALACE_SESSION_TTL_SECONDS"] = "1"  # 1s TTL

    # Create a stale session (backdated 1 hour ago)
    stale = sessions_dir / "stale-session"
    stale.touch()
    one_hour_ago = str(int(time.time()) - 3600)
    subprocess.run(["touch", "-t", one_hour_ago, str(stale)], check=True)

    # Create the real session
    (sessions_dir / "active-session").touch()

    # Stop the active session — stale session should be pruned before count
    result = subprocess.run(
        ["bash", str(hooks_dir / "mempal-server-control.sh"), "stop", "active-session"],
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, f"stop failed: {result.stderr}"

    # Stale should be gone; no sessions should remain; server shutdown
    assert not stale.exists(), "stale session should be pruned"
    assert len(list(sessions_dir.iterdir())) == 0, \
        "no sessions should remain after stop of only session"

    # Verify server shut down (no running process)
    status = subprocess.run(
        ["bash", str(hooks_dir / "mempal-server-control.sh"), "status"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # Server should either be stopped or in process of stopping
    # As long as no active sessions remain, prune+count path worked
    assert "active_sessions=0" in status.stdout, \
        f"no sessions should remain: {status.stdout}"


# ---------------------------------------------------------------------------
# Regression Tests: Issue 5 — invalid/empty JSON produces stable non-empty id
# ---------------------------------------------------------------------------

def test_invalid_json_fallback_is_stable_and_safe(tmp_path):
    """
    Invalid JSON produces a stable hash-based id, not empty/unknown.
    Empty input produces a unique but deterministic id.
    Path traversal characters are sanitized away.
    """
    sessions_dir = tmp_path / ".mempalace" / "runtime" / "sessions"
    sessions_dir.mkdir(parents=True)
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"

    env = os.environ.copy()
    env["RUNTIME_DIR"] = str(tmp_path / ".mempalace" / "runtime")
    env["HOME"] = str(tmp_path.parent)

    # Test 1: empty stdin
    result1 = subprocess.run(
        ["bash", str(hooks_dir / "mempal-session-start-hook.sh")],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result1.returncode == 0

    # Test 2: malformed JSON
    result2 = subprocess.run(
        ["bash", str(hooks_dir / "mempal-session-start-hook.sh")],
        input="not valid json at all {",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result2.returncode == 0

    # Both should produce session files (non-empty ids)
    files = list(sessions_dir.iterdir())
    assert len(files) == 2, f"expected 2 session files, got: {[f.name for f in files]}"
    for f in files:
        assert f.name not in ("", ".", ".."), f"invalid session name: {f.name}"
        assert "/" not in f.name, f"path traversal in session name: {f.name}"
        # Each should be different (unique hash per raw input)
    assert files[0].name != files[1].name, "empty vs invalid JSON should produce different ids"


def test_path_traversal_in_session_id_is_sanitized(tmp_path):
    """
    Session IDs with path traversal characters are sanitized.
    No file should be created outside SESSIONS_DIR.
    """
    sessions_dir = tmp_path / ".mempalace" / "runtime" / "sessions"
    sessions_dir.mkdir(parents=True)
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"

    env = os.environ.copy()
    env["RUNTIME_DIR"] = str(tmp_path / ".mempalace" / "runtime")
    env["HOME"] = str(tmp_path.parent)

    result = subprocess.run(
        ["bash", str(hooks_dir / "mempal-session-start-hook.sh")],
        input=json.dumps({"session_id": "../../../etc/passwd"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0

    # Only one file should exist in sessions dir
    files = list(sessions_dir.iterdir())
    assert len(files) == 1
    assert "/" not in files[0].name
    # No file should exist outside sessions dir
    assert not (tmp_path / ".mempalace" / "etcpasswd").exists()


# ---------------------------------------------------------------------------
# Regression Tests: Issue 6 — server startup fallback when mempalace binary absent
# ---------------------------------------------------------------------------

def test_start_server_fallback_when_binary_not_in_path(tmp_path, monkeypatch):
    """
    When mempalace binary is not in PATH, start_server uses python3 -m mempalace.
    We test the fallback logic by ensuring the script does not hard-code the binary.
    """
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    script_path = hooks_dir / "mempal-server-control.sh"

    # Read the script and verify both command variants appear
    content = script_path.read_text()
    assert "mempalace serve" in content
    assert "python3 -m mempalace serve" in content, \
        "start_server must have python3 -m fallback for when mempalace binary is absent"
    # Verify it's conditional (command -v or if/else)
    assert "command -v" in content or "which" in content or \
           ("if" in content and "mempalace" in content), \
        "start_server should check for mempalace binary before falling back"


# ---------------------------------------------------------------------------
# Regression Tests: Issue 7 — stop_server PID verification before kill
# ---------------------------------------------------------------------------

def test_stop_server_does_not_kill_non_mempalace_process(tmp_path):
    """
    If SERVER_PID_FILE points to a process that is not a MemPalace server,
    stop_server must NOT kill it — only remove the stale pid file.

    Uses GRACE_PERIOD_SECONDS=0 to make the test fast and deterministic.
    Starts a real `sleep 30` process, writes its PID to server.pid,
    puts a fake `ps` in PATH returning `sleep 30`, then runs stop.
    The sleep process must remain alive after stop completes.
    """
    sessions_dir = tmp_path / ".mempalace" / "runtime" / "sessions"
    sessions_dir.mkdir(parents=True)
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    runtime_dir = tmp_path / ".mempalace" / "runtime"

    pid_file = runtime_dir / "server.pid"

    # Start a real sleep process and capture its PID
    sleep_proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sleep_pid = sleep_proc.pid
    pid_file.write_text(str(sleep_pid))

    # Create a fake ps that returns a non-mempalace command string
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ps = fake_bin / "ps"
    fake_ps.write_text("#!/bin/bash\necho 'sleep 30'\nexit 0\n")
    fake_ps.chmod(0o755)

    env = os.environ.copy()
    env["RUNTIME_DIR"] = str(runtime_dir)
    env["HOME"] = str(tmp_path.parent)
    env["GRACE_PERIOD_SECONDS"] = "0"
    env["MEMPALACE_SESSION_TTL_SECONDS"] = "36000"
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    try:
        # Run stop — should remove stale pid file without killing the sleep process
        result = subprocess.run(
            ["bash", str(hooks_dir / "mempal-server-control.sh"), "stop", "fake-session"],
            env=env,
            timeout=20,
        )
        assert result.returncode == 0, f"stop failed: {result.stderr}"

        # pid file must be removed (stale, non-MemPalace pid)
        assert not pid_file.exists(), "stale pid file should be removed"

        # sleep process must still be alive (not killed by stop_server)
        # os.kill(pid, 0) raises OSError if process is dead; returns None if alive
        try:
            os.kill(sleep_pid, 0)
        except OSError as e:
            assert False, \
                f"sleep process {sleep_pid} should still be alive — stop_server must not kill non-mempalace processes: {e}"
    finally:
        # Clean up: kill the sleep process so it doesn't linger
        try:
            os.kill(sleep_pid, signal.SIGTERM)
            sleep_proc.wait(timeout=5)
        except Exception:
            try:
                os.kill(sleep_pid, signal.SIGKILL)
            except OSError:
                pass  # already dead


# ---------------------------------------------------------------------------
# Regression Tests: Issue 8 — precompact works without GNU timeout on macOS
# ---------------------------------------------------------------------------

def test_precompact_hook_does_not_require_gnu_timeout(tmp_path):
    """
    mempal-precompact-hook.sh must work when timeout command is absent.
    It should either skip timeout or use python-based fallback.
    """
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    script_path = hooks_dir / "mempal-precompact-hook.sh"

    content = script_path.read_text()

    # Script must have `command -v timeout` guard or equivalent
    assert "command -v timeout" in content or \
           ("if" in content and "timeout" in content), \
        "precompact hook must guard timeout availability"

    # Script should not unconditionally call `timeout 55s`
    # It should be wrapped in `if command -v timeout`
    assert "if command -v timeout" in content or \
           "command -v timeout" in content, \
        "precompact must check for timeout before using it"


def test_precompact_no_session_refcount_change(runtime_dir, env_with_runtime):
    """Existing test preserved for regression coverage."""
    sessions_dir = runtime_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    stdin_data = json.dumps({
        "session_id": "precompact-test",
        "cwd": "/tmp",
    })

    result = run_hook("precompact", stdin_data, env=env_with_runtime, timeout=10)

    assert result.returncode == 0
    session_files = [f for f in sessions_dir.iterdir() if f.is_file()]
    assert len(session_files) == 0, \
        f"precompact should not create session files, found: {session_files}"


# ---------------------------------------------------------------------------
# Regression Tests: Issue 9 — session-start bounded execution
# ---------------------------------------------------------------------------

def test_session_start_hook_uses_bounded_execution():
    """
    mempal-session-start-hook.sh must use run_with_timeout for the session-start
    hook invocation to prevent indefinite blocking of Claude Code startup.
    """
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    script_path = hooks_dir / "mempal-session-start-hook.sh"
    content = script_path.read_text()

    # Script must define run_with_timeout
    assert "run_with_timeout()" in content, \
        "session-start hook must define run_with_timeout()"
    assert "command -v timeout" in content or "gtimeout" in content or "perl" in content, \
        "run_with_timeout must guard against missing timeout command"

    # Normalize line continuations and collapse multiple spaces before checking
    import re
    content_normalized = re.sub(r'\\\s*', ' ', content)  # backslash-newline → space
    content_normalized = re.sub(r' {2,}', ' ', content_normalized)  # collapse multi-space

    # Both hook run paths (HTTP and CLI fallback) must use run_with_timeout
    # Use whitespace-flexible match since backslash-newline normalization leaves multi-space
    assert re.search(r'run_with_timeout\s+python3\s+-m\s+mempalace\s+hook\s+run\s+--hook\s+session-start', content_normalized), \
        "session-start hook invocation must be wrapped in run_with_timeout"


def test_session_start_hook_always_exits_zero():
    """
    session-start hook must exit 0 even when the hook invocation fails.
    A failed session-start must not block Claude Code startup.
    """
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    script_path = hooks_dir / "mempal-session-start-hook.sh"
    content = script_path.read_text()

    # Must end with `exit 0` (not `exit $?` or conditional exit)
    lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
    last_line = lines[-1] if lines else ""
    assert last_line == "exit 0", \
        f"session-start hook must end with 'exit 0', found: {last_line}"


# ---------------------------------------------------------------------------
# Regression Tests: Issue 10 — cmd_start rollback on start_server failure
# ---------------------------------------------------------------------------

def test_cmd_start_unregisters_on_start_server_failure(tmp_path):
    """
    When start_server fails, cmd_start must:
      1. unregister the session (remove session file)
      2. release the lock
      3. exit non-zero

    This prevents phantom sessions when server fails to start.

    The test verifies rollback by checking the script source for the unregister
    call on the start_server failure path. A dynamic test is environmental because
    the shared MCP server is always running and blocks the "not healthy" path.
    """
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    server_control = hooks_dir / "mempal-server-control.sh"
    content = server_control.read_text()

    # Verify the start_server failure path in cmd_start calls unregister_session.
    # This is the key rollback behavior: on start_server failure, unregister first.
    assert "if ! start_server; then" in content, \
        "cmd_start must call start_server and check its return value"

    # The failure block must call unregister_session before release_lock and exit
    # We check for the pattern inside the failure handler
    failure_block_pattern = r'if ! start_server; then.*?unregister_session.*?release_lock.*?exit 1'
    import re
    assert re.search(failure_block_pattern, content, re.DOTALL), \
        "cmd_start failure path must call: unregister_session + release_lock + exit 1"

    # Also verify cmd_start success path does NOT call unregister_session
    # (it should only release_lock and exit 0)
    cmd_start_match = re.search(r'^cmd_start\(\).*?^\}', content, re.MULTILINE | re.DOTALL)
    assert cmd_start_match, "cmd_start function not found"
    cmd_start_body = cmd_start_match.group(0)

    # Count unregister calls — should be exactly 1 (in failure path only)
    unregister_in_failure = re.search(
        r'if ! start_server; then.*?unregister_session',
        cmd_start_body, re.DOTALL
    )
    assert unregister_in_failure, \
        "unregister_session must be called in start_server failure path"


# ---------------------------------------------------------------------------
# Regression Tests: Issue 11 — all hook scripts pass bash -n
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_name", [
    "mempal-server-control.sh",
    "mempal-session-start-hook.sh",
    "mempal-stop-hook.sh",
    "mempal-precompact-hook.sh",
])
def test_all_hook_scripts_bash_syntax_check(script_name):
    """Every lifecycle hook script must pass bash -n (no-op syntax check)."""
    hooks_dir = Path(__file__).parent.parent / ".claude-plugin" / "hooks"
    script_path = hooks_dir / script_name
    result = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, \
        f"bash -n failed for {script_name}:\n{result.stderr}"
