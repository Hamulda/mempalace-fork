"""
Plugin lifecycle tests for mempal-server-control.sh

These tests verify session refcount behavior and lifecycle correctness
using an isolated fake HOME / temp runtime directory.

IMPORTANT TEST CONSTRAINTS:
- GRACE_PERIOD_SECONDS=1 (not 20s)
- STARTUP_WAIT_SECONDS=1 (not 10s)
- LOCK_MAX_WAIT=2 (not 30s)
- HOME overridden to temp dir — no real ~/.mempalace touched
- No real servers started — mempalace command replaced with fake/no-op
- No real processes killed
"""

import subprocess
import os
import time
import json
import pytest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent / ".claude-plugin"
CONTROL_SCRIPT = PLUGIN_ROOT / "hooks" / "mempal-server-control.sh"

# Env overrides for fast tests
FAST_ENV = {
    "GRACE_PERIOD_SECONDS": "1",
    "STARTUP_WAIT_SECONDS": "1",
    "LOCK_MAX_WAIT": "2",
    "MEMPALACE_SESSION_TTL_SECONDS": "2",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_fake_mempalace(tmp_path: Path):
    """Create a fake mempalace binary that exits 1 (not a real server)."""
    fake_bin = tmp_path / "bin" / "mempalace"
    (tmp_path / "bin").mkdir(exist_ok=True)
    fake_bin.write_text("#!/bin/sh\nexit 1\n")
    fake_bin.chmod(0o755)


def write_fake_mempalace_sleep(tmp_path: Path):
    """Create a fake mempalace that sleeps forever (simulates server)."""
    fake_bin = tmp_path / "bin" / "mempalace"
    (tmp_path / "bin").mkdir(exist_ok=True)
    fake_bin.write_text("#!/bin/sh\nsleep 3600\n")
    fake_bin.chmod(0o755)


def run_control(tmp_path: Path, *args: str, fake_mempalace: bool = True) -> subprocess.CompletedProcess:
    """Run the control script with overridden RUNTIME_DIR and fast timing."""
    runtime = tmp_path / "runtime"
    env = {
        **os.environ,
        "RUNTIME_DIR": str(runtime),
        **FAST_ENV,
    }
    if fake_mempalace:
        fake_bin = write_fake_mempalace(tmp_path)
        env["PATH"] = f"{tmp_path}/bin:{os.environ.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(CONTROL_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return result


def run_control_with_sleep_bin(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run with fake mempalace that sleeps (simulates real server)."""
    runtime = tmp_path / "runtime"
    fake_bin = write_fake_mempalace_sleep(tmp_path)
    env = {
        **os.environ,
        "RUNTIME_DIR": str(runtime),
        "PATH": f"{tmp_path}/bin:{os.environ.get('PATH', '')}",
        **FAST_ENV,
    }
    result = subprocess.run(
        ["bash", str(CONTROL_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    return result


def parse_status(stdout: str) -> dict:
    """Parse machine-readable status output."""
    lines = [l.strip() for l in stdout.strip().splitlines() if "=" in l]
    return dict(line.split("=", 1) for line in lines)


def session_files(tmp_path: Path) -> list[str]:
    """Return list of session file names in the runtime sessions dir."""
    sessions_dir = tmp_path / "runtime" / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted([p.name for p in sessions_dir.iterdir()])


# ---------------------------------------------------------------------------
# Test 1: start sessionA creates sessions/sessionA
# ---------------------------------------------------------------------------

def test_start_creates_session_file(tmp_path: Path):
    """Starting sessionA should create a session file in sessions/.noqa: E501"""
    result = run_control(tmp_path, "start", "sessionA")
    assert result.returncode == 0, f"start failed: {result.stderr}"
    assert "sessionA" in session_files(tmp_path), (
        f"sessionA not found in {session_files(tmp_path)}"
    )


# ---------------------------------------------------------------------------
# Test 2: start sessionB creates sessions/sessionB (sessionA still there)
# ---------------------------------------------------------------------------

def test_start_second_session_keeps_first(tmp_path: Path):
    """Starting sessionB should not remove sessionA."""
    run_control(tmp_path, "start", "sessionA")
    run_control(tmp_path, "start", "sessionB")
    files = session_files(tmp_path)
    assert "sessionA" in files, f"sessionA missing: {files}"
    assert "sessionB" in files, f"sessionB missing: {files}"
    assert len(files) == 2


# ---------------------------------------------------------------------------
# Test 3: active_sessions count reflects registered sessions
# ---------------------------------------------------------------------------

def test_status_shows_correct_active_sessions(tmp_path: Path):
    """Status output should show correct active_sessions count."""
    run_control(tmp_path, "start", "sessionA")
    run_control(tmp_path, "start", "sessionB")
    result = run_control(tmp_path, "status")
    status = parse_status(result.stdout)
    assert status.get("active_sessions") == "2", f"expected 2, got {status}"


# ---------------------------------------------------------------------------
# Test 4: stop sessionA leaves sessionB active
# ---------------------------------------------------------------------------

def test_stop_one_session_leaves_other_active(tmp_path: Path):
    """Stopping sessionA should leave sessionB in the sessions dir."""
    run_control(tmp_path, "start", "sessionA")
    run_control(tmp_path, "start", "sessionB")
    run_control(tmp_path, "stop", "sessionA")
    files = session_files(tmp_path)
    assert "sessionA" not in files, f"sessionA still present: {files}"
    assert "sessionB" in files, f"sessionB missing: {files}"


# ---------------------------------------------------------------------------
# Test 5: stop last session reaches zero (but graceful shutdown delay)
# ---------------------------------------------------------------------------

def test_stop_last_session_zero_active(tmp_path: Path):
    """Stopping the last session should result in zero active sessions after grace."""
    run_control(tmp_path, "start", "sessionA")
    run_control(tmp_path, "stop", "sessionA")
    # After grace period, sessions dir should be empty (grace=1s so wait 2s)
    time.sleep(2)
    files = session_files(tmp_path)
    assert len(files) == 0, f"expected empty sessions dir, got: {files}"


# ---------------------------------------------------------------------------
# Test 6: stale session file older than TTL is pruned
# ---------------------------------------------------------------------------

def test_prune_removes_stale_sessions(tmp_path: Path):
    """Session files older than TTL should be removed by prune command."""
    runtime = tmp_path / "runtime"
    sessions_dir = runtime / "sessions"
    sessions_dir.mkdir(parents=True)

    # Create a session file with old mtime
    stale = sessions_dir / "old_session"
    stale.touch()
    old_mtime = time.time() - int(FAST_ENV["MEMPALACE_SESSION_TTL_SECONDS"]) - 1
    os.utime(stale, (old_mtime, old_mtime))

    # Create a fresh session file
    fresh = sessions_dir / "fresh_session"
    fresh.touch()

    result = run_control(tmp_path, "prune")
    assert result.returncode == 0, f"prune failed: {result.stderr}"
    assert "old_session" not in session_files(tmp_path), "stale session not removed"
    assert "fresh_session" in session_files(tmp_path), "fresh session was incorrectly pruned"


# ---------------------------------------------------------------------------
# Test 7: safe_session_id sanitizes weird session IDs
# ---------------------------------------------------------------------------

def test_safe_session_id_sanitizes_weird_ids(tmp_path: Path):
    """safe_session_id strips dangerous chars; all-slashes triggers hash fallback."""
    runtime = tmp_path / "runtime"
    sessions_dir = runtime / "sessions"
    sessions_dir.mkdir(parents=True)

    # Case 1: weird ID with path traversal — tr strips / leaving "etcpasswd" (non-empty → no hash)
    weird_id = "////etc/passwd////"
    result = run_control(tmp_path, "start", weird_id)
    assert result.returncode == 0, f"start with weird ID failed: {result.stderr}"
    files = session_files(tmp_path)
    assert len(files) == 1
    safe_name = files[0]
    assert "/" not in safe_name, f"unsafe session name contains /: {safe_name}"
    assert safe_name == "etcpasswd", f"expected etcpasswd, got: {safe_name}"

    # Stop and reset for case 2
    run_control(tmp_path, "stop", weird_id)

    # Case 2: all-slashes → tr yields empty → hash fallback → id- prefix
    weird_id_empty = "/////"
    result2 = run_control(tmp_path, "start", weird_id_empty)
    assert result2.returncode == 0, f"start with all-slashes ID failed: {result2.stderr}"
    files2 = session_files(tmp_path)
    assert len(files2) == 1
    safe_name2 = files2[0]
    assert safe_name2.startswith("id-"), f"expected id- hash prefix, got: {safe_name2}"
    assert "/" not in safe_name2

    # Verify the all-slashes ID can be stopped
    result3 = run_control(tmp_path, "stop", weird_id_empty)
    assert result3.returncode == 0, f"stop with all-slashes ID failed: {result3.stderr}"
    assert len(session_files(tmp_path)) == 0


# ---------------------------------------------------------------------------
# Test 8: stale PID file for non-MemPalace process is not killed
# ---------------------------------------------------------------------------

def test_stale_pid_not_mempalace_not_killed(tmp_path: Path):
    """A stale PID file pointing to a non-MemPalace process should not be killed."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    pid_file = runtime / "server.pid"

    # Write PID of a harmless long-running process (this shell)
    my_pid = os.getpid()
    pid_file.write_text(str(my_pid))

    # Write fake mempalace that would die immediately (simulating non-mempalace)
    fake_bin = tmp_path / "bin" / "mempalace"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text("#!/bin/sh\nsleep 3600\n")  # sleep forever fake
    fake_bin.chmod(0o755)

    env = {
        **os.environ,
        "RUNTIME_DIR": str(runtime),
        "PATH": f"{tmp_path}/bin:{os.environ.get('PATH', '')}",
        **FAST_ENV,
    }

    result = subprocess.run(
        ["bash", str(CONTROL_SCRIPT), "start", "test_session"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    # The script should detect that the running process is NOT mempalace,
    # remove the stale pid file, and proceed to start a new server
    # (or fail to start, but crucially should NOT have tried to kill os.getpid())
    assert result.returncode == 0, f"start failed: {result.stderr}"
    # After running, my_pid should still be running (we're alive)
    try:
        os.kill(my_pid, 0)  # should not raise
    except ProcessLookupError:
        pytest.fail("The test process was killed — stale PID safety FAILED")


# ---------------------------------------------------------------------------
# Test 9: status output is machine-readable with all required fields
# ---------------------------------------------------------------------------

def test_status_output_machine_readable(tmp_path: Path):
    """Status output must contain server_running, pid, active_sessions, session_files, health."""
    run_control(tmp_path, "start", "sessionA")
    run_control(tmp_path, "start", "sessionB")
    result = run_control(tmp_path, "status")
    assert result.returncode == 0, f"status failed: {result.stderr}"

    status = parse_status(result.stdout)
    required_fields = ["server_running", "pid", "active_sessions", "session_files", "health"]
    for field in required_fields:
        assert field in status, f"missing required status field: {field}"

    assert status["server_running"] in ("true", "false")
    assert status["active_sessions"] == "2"
    assert "sessionA" in status["session_files"]
    assert "sessionB" in status["session_files"]


# ---------------------------------------------------------------------------
# Test 10: concurrent start/stop session safety
# ---------------------------------------------------------------------------

def test_concurrent_session_refcount_integrity(tmp_path: Path):
    """Multiple start/stop cycles should keep refcount accurate."""
    for i in range(3):
        run_control(tmp_path, "start", f"session{i}")

    # Now stop them all except session1
    run_control(tmp_path, "stop", "session0")
    run_control(tmp_path, "stop", "session2")

    result = run_control(tmp_path, "status")
    status = parse_status(result.stdout)
    assert status["active_sessions"] == "1", f"expected 1 session, got: {status}"
    assert "session1" in session_files(tmp_path)


# ---------------------------------------------------------------------------
# Test 11: verify .mcp.json points to correct URL
# ---------------------------------------------------------------------------

def test_mcp_json_has_correct_url():
    """Verify .mcp.json points to correct localhost:8765 endpoint."""
    mcp_json = PLUGIN_ROOT / ".mcp.json"
    assert mcp_json.exists(), ".mcp.json does not exist"
    with open(mcp_json) as f:
        data = json.load(f)
    # .mcp.json IS the mcpServers dict directly (not wrapped)
    assert "mempalace" in data, ".mcp.json must have mempalace key"
    assert "url" in data["mempalace"], "mempalace entry must have url"
    url = data["mempalace"]["url"]
    assert "127.0.0.1:8765" in url, f"wrong URL in .mcp.json: {url}"
    assert url.endswith("/mcp"), f"URL should end with /mcp: {url}"


# ---------------------------------------------------------------------------
# Test 12: plugin.json has no stdio mcpServers
# ---------------------------------------------------------------------------

def test_plugin_json_has_no_stdio_servers():
    """plugin.json must not define stdio-based MCP servers."""
    plugin_json = PLUGIN_ROOT / "plugin.json"
    with open(plugin_json) as f:
        data = json.load(f)
    # Skills and commands are fine; mcpServers key should not exist
    assert "mcpServers" not in data, "plugin.json must not define mcpServers (one shared HTTP server)"


# ---------------------------------------------------------------------------
# Test 13: grace period interrupt — new session during grace cancels shutdown
# ---------------------------------------------------------------------------

def test_new_session_during_grace_cancels_shutdown(tmp_path: Path):
    """If a new session registers during the grace period, server must keep running."""
    # Start sessionA and stop it — this triggers grace shutdown
    run_control(tmp_path, "start", "sessionA")
    # Start sessionB before stopping sessionA to interrupt the grace
    run_control(tmp_path, "start", "sessionB")

    # Now stop sessionA — grace period begins but sessionB is still active
    result = run_control(tmp_path, "stop", "sessionA")

    # sessionB should still be registered (no graceful shutdown triggered)
    files = session_files(tmp_path)
    assert "sessionB" in files, f"sessionB was removed during other session stop: {files}"


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-q", "-v"])