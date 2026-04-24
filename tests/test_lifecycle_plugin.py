"""
Tests for session-aware lifecycle control of the shared MemPalace MCP HTTP server.

Tests cover:
- mempal-server-control.sh lifecycle controller
- mempal-session-start-hook.sh session registration
- mempal-stop-hook.sh save-before-shutdown ordering
"""

import json
import os
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
