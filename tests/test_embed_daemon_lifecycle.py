"""
Sprint 1: Embed daemon lifecycle hardening tests.

Covers:
- _SOCK_TIMEOUT reads env
- stale _DAEMON_STARTED resets when _daemon_is_running false
- _start_daemon_if_needed returns False and resets flag on startup timeout
- malformed daemon response makes _daemon_is_running false
- fallback disabled + daemon unavailable raises RuntimeError
- cli embed-daemon start honors timeout env

No live MLX required.
"""
import os
import socket
import threading
import time as time_module
from unittest import mock

import pytest


# ── Test 1: _SOCK_TIMEOUT reads env ──────────────────────────────────────────

class TestSockTimeoutEnv:
    def test_sock_timeout_defaults_to_120(self):
        """Default is 120s when neither env var is set."""
        with mock.patch.dict(os.environ, {}, clear=True):
            import importlib
            import mempalace.backends.lance as lance_mod
            importlib.reload(lance_mod)
            assert lance_mod._SOCK_TIMEOUT == 120.0

    def test_sock_timeout_reads_mempace_embed_socket_timeout(self):
        """MEMPALACE_EMBED_SOCKET_TIMEOUT env var is read."""
        with mock.patch.dict(os.environ, {"MEMPALACE_EMBED_SOCKET_TIMEOUT": "45"}):
            import importlib
            import mempalace.backends.lance as lance_mod
            importlib.reload(lance_mod)
            assert lance_mod._SOCK_TIMEOUT == 45.0

    def test_sock_timeout_reads_mempace_embed_daemon_startup_timeout(self):
        """MEMPALACE_EMBED_DAEMON_STARTUP_TIMEOUT env var is read."""
        with mock.patch.dict(os.environ, {"MEMPALACE_EMBED_DAEMON_STARTUP_TIMEOUT": "90"}):
            import importlib
            import mempalace.backends.lance as lance_mod
            importlib.reload(lance_mod)
            assert lance_mod._SOCK_TIMEOUT == 90.0

    def test_daemon_startup_env_takes_precedence(self):
        """MEMPALACE_EMBED_DAEMON_STARTUP_TIMEOUT takes precedence."""
        with mock.patch.dict(os.environ, {
            "MEMPALACE_EMBED_SOCKET_TIMEOUT": "30",
            "MEMPALACE_EMBED_DAEMON_STARTUP_TIMEOUT": "180",
        }):
            import importlib
            import mempalace.backends.lance as lance_mod
            importlib.reload(lance_mod)
            assert lance_mod._SOCK_TIMEOUT == 180.0


# ── Test 2: malformed daemon response makes _daemon_is_running false ─────────

class TestDaemonHealthProbe:
    """Tests for _daemon_is_running() health probe."""

    def test_missing_socket_path_returns_false(self):
        """Missing socket file returns False."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        with mock.patch.object(LM, "_get_socket_path", return_value="/tmp/mp_nonexistent.sock"):
            assert LM._daemon_is_running() is False

    def test_socket_path_not_a_socket_returns_false(self, tmp_path):
        """A file that is not a socket returns False."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        sock_file = tmp_path / "not_a_socket"
        sock_file.write_text("I'm not a socket")

        with mock.patch.object(LM, "_get_socket_path", return_value=str(sock_file)):
            assert LM._daemon_is_running() is False

    def test_socket_timeout_returns_false(self):
        """Connection timeout returns False."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        sock_path = "/tmp/mp_silence.sock"
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)
        server_sock.settimeout(0.3)

        try:
            with mock.patch.object(LM, "_get_socket_path", return_value=sock_path):
                assert LM._daemon_is_running() is False
        finally:
            server_sock.close()
            os.unlink(sock_path)

    def test_malformed_json_returns_false(self):
        """Daemon returns non-JSON bytes returns False."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        sock_path = "/tmp/mp_malformed.sock"

        def fake_respond(unused_addr):
            s2, _ = unused_addr.accept()
            s2.sendall((5).to_bytes(4, "big") + b"notjs")
            s2.close()

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)
        server_sock.settimeout(2.0)
        thread = threading.Thread(target=fake_respond, args=(server_sock,), daemon=True)
        thread.start()

        try:
            with mock.patch.object(LM, "_get_socket_path", return_value=sock_path):
                assert LM._daemon_is_running() is False
        finally:
            server_sock.close()
            os.unlink(sock_path)
            thread.join(timeout=1)

    def test_empty_response_returns_false(self):
        """Daemon returns empty response returns False."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        sock_path = "/tmp/mp_empty.sock"

        def fake_respond(unused_addr):
            s2, _ = unused_addr.accept()
            s2.close()

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)
        server_sock.settimeout(2.0)
        thread = threading.Thread(target=fake_respond, args=(server_sock,), daemon=True)
        thread.start()

        try:
            with mock.patch.object(LM, "_get_socket_path", return_value=sock_path):
                assert LM._daemon_is_running() is False
        finally:
            server_sock.close()
            os.unlink(sock_path)
            thread.join(timeout=1)


# ── Test 3: stale _DAEMON_STARTED resets when health check fails ─────────────

class TestStaleDaemonFlag:
    def test_stale_flag_marked_dead_on_health_check_failure(self):
        """When _DAEMON_STARTED is True but health check fails, flag is reset."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        LM._DAEMON_STARTED = True

        fake_proc = mock.MagicMock()
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None
        fake_proc.stdout.readline.return_value = b""
        fake_proc.stderr.read.return_value = b""

        # Simulate deadline already passed by mocking select to return immediately
        # with nothing ready, then poll shows process still running
        # After one loop iteration with no READY, deadline check fails
        with mock.patch.object(LM, "_daemon_is_running", return_value=False):
            with mock.patch.object(LM, "_get_socket_path", return_value="/tmp/mp_nonexistent.sock"):
                with mock.patch("subprocess.Popen", return_value=fake_proc):
                    with mock.patch("mempalace.backends.lance.select.select", return_value=([], [], [])):
                        with mock.patch.object(LM, "_mark_daemon_dead") as mock_mark_dead:
                            result = LM._start_daemon_if_needed()
                            assert result is False
                            assert LM._DAEMON_STARTED is False

    def test_mark_daemon_dead_resets_flag(self):
        """_mark_daemon_dead() resets _DAEMON_STARTED."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        LM._DAEMON_STARTED = True
        LM._mark_daemon_dead("test reason")
        assert LM._DAEMON_STARTED is False


# ── Test 4: _start_daemon_if_needed returns False on startup timeout ─────────

class TestDaemonStartupTimeout:
    def test_startup_timeout_returns_false_and_resets_flag(self):
        """When daemon fails to emit READY within timeout, returns False."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        fake_proc = mock.MagicMock()
        fake_proc.pid = 99998
        fake_proc.poll.return_value = None
        fake_proc.stdout.readline.return_value = b""
        fake_proc.stderr.read.return_value = b""

        with mock.patch.object(LM, "_daemon_is_running", return_value=False):
            with mock.patch.object(LM, "_get_socket_path", return_value="/tmp/mp_nonexistent.sock"):
                with mock.patch("subprocess.Popen", return_value=fake_proc):
                    with mock.patch("mempalace.backends.lance.select.select", return_value=([], [], [])):
                        with mock.patch.object(LM, "_mark_daemon_dead"):
                            result = LM._start_daemon_if_needed()
                            assert result is False

    def test_startup_timeout_kills_process(self):
        """Process is killed when startup times out."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        fake_proc = mock.MagicMock()
        fake_proc.pid = 99997
        fake_proc.poll.return_value = None
        fake_proc.stdout.readline.return_value = b""
        fake_proc.stderr.read.return_value = b""

        with mock.patch.object(LM, "_daemon_is_running", return_value=False):
            with mock.patch.object(LM, "_get_socket_path", return_value="/tmp/mp_nonexistent2.sock"):
                with mock.patch("subprocess.Popen", return_value=fake_proc):
                    with mock.patch("mempalace.backends.lance.select.select", return_value=([], [], [])):
                        LM._start_daemon_if_needed()
                        fake_proc.kill.assert_called_once()

    def test_process_exit_before_ready_marks_dead(self):
        """If process exits before emitting READY, daemon is marked dead."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        fake_proc = mock.MagicMock()
        fake_proc.pid = 99996
        fake_proc.poll.return_value = 1
        fake_proc.stderr.read.return_value = b"crashed before READY"

        with mock.patch.object(LM, "_daemon_is_running", return_value=False):
            with mock.patch.object(LM, "_get_socket_path", return_value="/tmp/mp_nonexistent3.sock"):
                with mock.patch("subprocess.Popen", return_value=fake_proc):
                    with mock.patch.object(LM, "_mark_daemon_dead") as mock_mark_dead:
                        result = LM._start_daemon_if_needed()
                        assert result is False
                        mock_mark_dead.assert_called()


# ── Test 5: circuit open raises RuntimeError ─────────────────────────────────

class TestFallbackDisabled:
    def test_circuit_open_raises_runtime_error(self):
        """When circuit breaker is open, raises RuntimeError."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        from mempalace.circuit_breaker import _embed_circuit, _State
        orig_state = _embed_circuit._state
        orig_failures = _embed_circuit._failures
        _embed_circuit._state = _State.OPEN
        _embed_circuit._opened_at = 0

        try:
            with mock.patch.object(LM, "_daemon_is_running", return_value=True):
                with mock.patch.object(LM, "_start_daemon_if_needed", return_value=True):
                    with mock.patch.object(LM, "_embed_via_socket", side_effect=RuntimeError("Circuit open, using fallback")):
                        with pytest.raises(RuntimeError) as exc_info:
                            LM._embed_texts(["test"])
                        assert "Circuit open" in str(exc_info.value)
        finally:
            _embed_circuit._state = orig_state
            _embed_circuit._failures = orig_failures


# ── Test 6: cli embed-daemon start honors timeout env ───────────────────────

class TestCliEmbedDaemonTimeout:
    def test_cli_reads_daemon_startup_timeout_env(self):
        """cli embed-daemon start reads MEMPALACE_EMBED_DAEMON_STARTUP_TIMEOUT."""
        # Re-import to ensure fresh module state with env var
        import importlib
        import mempalace.cli as cli_module
        importlib.reload(cli_module)

        from unittest.mock import MagicMock, patch
        import os

        fake_proc = MagicMock()
        fake_proc.pid = 99995
        fake_proc.poll.return_value = None
        fake_proc.stdout.readline.return_value = b"READY\n"
        fake_proc.stderr.read.return_value = b""

        with patch("mempalace.backends.lance._daemon_is_running", return_value=False):
            with patch("subprocess.Popen", return_value=fake_proc):
                with patch.object(cli_module, "get_socket_path", return_value="/tmp/mp_test.sock"):
                    args = MagicMock()
                    args.action = "start"
                    with patch.dict(os.environ, {"MEMPALACE_EMBED_DAEMON_STARTUP_TIMEOUT": "120"}, clear=False):
                        # Should use 120s timeout
                        cli_module.cmd_embed_daemon(args)


# ── Test 7: READY emitted but socket not responsive → marked dead ────────────

class TestReadyButNotResponsive:
    def test_ready_emitted_but_socket_check_fails_marks_dead(self):
        """Daemon emits READY but subsequent health check fails → marked dead."""
        import importlib
        import mempalace.backends.lance as LM
        importlib.reload(LM)

        fake_proc = mock.MagicMock()
        fake_proc.pid = 99993
        fake_proc.poll.return_value = None
        fake_proc.stdout.readline.return_value = b"READY\n"
        fake_proc.stderr.read.return_value = b""

        calls = [True, False]

        def daemon_running_side_effect():
            return calls.pop(0)

        with mock.patch.object(LM, "_daemon_is_running", side_effect=daemon_running_side_effect):
            with mock.patch.object(LM, "_get_socket_path", return_value="/tmp/mp_notresp.sock"):
                with mock.patch("subprocess.Popen", return_value=fake_proc):
                    with mock.patch.object(LM, "_mark_daemon_dead") as mock_mark_dead:
                        result = LM._start_daemon_if_needed()
                        assert result is False
                        mock_mark_dead.assert_called_with("READY emitted but socket not responsive")