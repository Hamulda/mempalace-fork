"""
Tests for embed_daemon doctor command (protocol validation).

Run: pytest tests/test_embed_daemon_doctor.py -v
"""

import json
import math
import os
import socket
import threading
from unittest.mock import patch, MagicMock

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed")


class MockDaemon:
    """Mock embed daemon that speaks the socket protocol."""

    def __init__(self, sock_path: str, response_fn=None):
        self.sock_path = sock_path
        self.response_fn = response_fn or (lambda texts: {"embeddings": [], "error": None})
        self._stop = False
        self._thread = None

    def start(self):
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.sock_path)
        server.listen(1)
        server.settimeout(1.0)

        def run():
            while not self._stop:
                try:
                    conn, _ = server.accept()
                    self._handle(conn)
                except socket.timeout:
                    continue
                except Exception:
                    pass
            server.close()
            try:
                os.unlink(self.sock_path)
            except Exception:
                pass

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _handle(self, conn):
        try:
            raw_len = b""
            while len(raw_len) < 4:
                chunk = conn.recv(4 - len(raw_len))
                if not chunk:
                    return
                raw_len += chunk
            msg_len = int.from_bytes(raw_len, "big")
            data = b""
            while len(data) < msg_len:
                chunk = conn.recv(min(65536, msg_len - len(data)))
                if not chunk:
                    return
                data += chunk
            request = json.loads(data.decode("utf-8"))
            texts = request.get("texts", [])
            response = self.response_fn(texts)
            payload = json.dumps(response).encode("utf-8")
            conn.sendall(len(payload).to_bytes(4, "big") + payload)
        except Exception:
            pass
        finally:
            conn.close()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=2)


# Valid 256-dim vector generator
def make_vec(dim: int = 256, finite: bool = True) -> list:
    if dim == 256:
        base = [0.1] * 255 + [0.15]
    elif dim == 384:
        base = [0.1] * 383 + [0.15]
    else:
        base = [0.1] * dim
    if not finite:
        base[0] = float("nan")
    return base


class TestEmbedDaemonDoctor:
    """Tests for run_embed_doctor() validation logic."""

    def _run_doctor(self, mock_daemon: MockDaemon):
        """Start mock daemon, run doctor, return (ok, caps)."""
        mock_daemon.start()

        from mempalace.embed_daemon import run_embed_doctor

        with patch("mempalace.embed_daemon.get_socket_path", return_value=mock_daemon.sock_path):
            with patch("mempalace.embed_daemon.get_pid_path", return_value=mock_daemon.sock_path.replace(".sock", ".pid")):
                ok = run_embed_doctor()

        mock_daemon.stop()
        return ok

    def test_healthy_returns_true(self):
        def healthy_response(texts):
            return {
                "embeddings": [make_vec(256) for _ in texts],
                "error": None,
            }

        sock = f"/tmp/mp_doctor_healthy_{os.getpid()}.sock"
        mock = MockDaemon(sock, response_fn=healthy_response)
        ok = self._run_doctor(mock)
        assert ok is True

    def test_socket_missing_returns_false(self):
        from mempalace.embed_daemon import run_embed_doctor

        nonexistent = "/tmp/nonexistent_mempalace_daemon_socket_12345.sock"
        with patch("mempalace.embed_daemon.get_socket_path", return_value=nonexistent):
            with patch("mempalace.embed_daemon.get_pid_path", return_value=nonexistent.replace(".sock", ".pid")):
                ok = run_embed_doctor()
        assert ok is False

    def test_malformed_json_returns_false(self):
        server_path = f"/tmp/mp_malformed_{os.getpid()}.sock"
        if os.path.exists(server_path):
            os.unlink(server_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(server_path)
        server.listen(1)
        server.settimeout(1.0)

        def bad_server():
            try:
                conn, _ = server.accept()
                # Send raw bytes that aren't valid JSON
                conn.sendall(b"0005hello")
                conn.close()
            except Exception:
                pass

        t = threading.Thread(target=bad_server, daemon=True)
        t.start()

        from mempalace.embed_daemon import run_embed_doctor

        with patch("mempalace.embed_daemon.get_socket_path", return_value=server_path):
            with patch("mempalace.embed_daemon.get_pid_path", return_value=server_path.replace(".sock", ".pid")):
                ok = run_embed_doctor()

        t.join(timeout=2)
        server.close()
        try:
            os.unlink(server_path)
        except Exception:
            pass

        assert ok is False

    def test_384_dim_returns_false(self):
        def response_384(texts):
            # Return 384-dim vectors (wrong — should be 256)
            return {
                "embeddings": [make_vec(384) for _ in texts],
                "error": None,
            }

        sock = f"/tmp/mp_384_{os.getpid()}.sock"
        mock = MockDaemon(sock, response_fn=response_384)
        ok = self._run_doctor(mock)
        assert ok is False

    def test_nan_vector_returns_false(self):
        def response_nan(texts):
            return {
                "embeddings": [make_vec(256, finite=False) for _ in texts],
                "error": None,
            }

        sock = f"/tmp/mp_nan_{os.getpid()}.sock"
        mock = MockDaemon(sock, response_fn=response_nan)
        ok = self._run_doctor(mock)
        assert ok is False

    def test_all_zero_vector_returns_false(self):
        def response_zero(texts):
            return {
                "embeddings": [[0.0] * 256 for _ in texts],
                "error": None,
            }

        sock = f"/tmp/mp_zero_{os.getpid()}.sock"
        mock = MockDaemon(sock, response_fn=response_zero)
        ok = self._run_doctor(mock)
        assert ok is False

    def test_wrong_count_returns_false(self):
        def response_wrong_count(texts):
            # Ask for 10 but only return 5
            return {
                "embeddings": [make_vec(256) for _ in range(5)],
                "error": None,
            }

        sock = f"/tmp/mp_count_{os.getpid()}.sock"
        mock = MockDaemon(sock, response_fn=response_wrong_count)
        ok = self._run_doctor(mock)
        assert ok is False

    def test_stale_pid_process_dead_returns_false(self):
        server_path = f"/tmp/mp_stale_{os.getpid()}.sock"
        if os.path.exists(server_path):
            os.unlink(server_path)

        # Socket exists but PID points to dead process
        pid_path = server_path.replace(".sock", ".pid")
        with open(pid_path, "w") as f:
            f.write("99999999")  # unlikely PID

        from mempalace.embed_daemon import run_embed_doctor

        with patch("mempalace.embed_daemon.get_socket_path", return_value=server_path):
            with patch("mempalace.embed_daemon.get_pid_path", return_value=pid_path):
                ok = run_embed_doctor()

        try:
            os.unlink(server_path)
        except Exception:
            pass
        try:
            os.unlink(pid_path)
        except Exception:
            pass

        assert ok is False

    def test_inf_vector_returns_false(self):
        def response_inf(texts):
            vec = [0.1] * 256
            vec[0] = float("inf")
            return {"embeddings": [vec for _ in texts], "error": None}

        sock = f"/tmp/mp_inf_{os.getpid()}.sock"
        mock = MockDaemon(sock, response_fn=response_inf)
        ok = self._run_doctor(mock)
        assert ok is False