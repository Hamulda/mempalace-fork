"""
IPC edge-case tests for the MemPalace embed daemon.

Covers socket-level edge cases: daemon death mid-response,
truncated body, and garbage before protocol.

Run: pytest tests/test_embed_daemon_ipc_edge.py -v
"""

import json
import os
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed")


def _make_sock_path(tmp_path: str, suffix: str) -> str:
    return str(tmp_path / f"mp_embed_{suffix}_{os.getpid()}.sock")


def _wait_ready(proc: subprocess.Popen, deadline: float = 60.0) -> None:
    """Spin until READY is emitted on stdout."""
    while time.monotonic() < deadline:
        line = proc.stdout.readline().decode().strip()
        if line == "READY":
            return
        if proc.poll() is not None:
            raise RuntimeError(f"Daemon exited before READY: {proc.stderr.read().decode(errors='ignore')}")
    raise RuntimeError("Daemon did not emit READY within timeout")


def _send_raw_header_body(sock_path: str, header_bytes: bytes, body_bytes: bytes, timeout: float = 5.0) -> None:
    """Send raw bytes on a Unix socket without framing protocol."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    s.sendall(header_bytes + body_bytes)
    s.close()


def _recv_with_interrupt(sock_path: str, timeout: float = 10.0) -> None:
    """Send a valid embedding request but close socket before reading response."""
    payload = json.dumps({"texts": ["edge case test"]}).encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    s.sendall(len(payload).to_bytes(4, "big") + payload)
    s.close()  # close before reading response — daemon mid-write


@pytest.mark.slow
def test_daemon_dies_mid_response(tmp_path):
    """Daemon is killed while writing a response body — client gets an error, not a hang."""
    sock_path = _make_sock_path(tmp_path, "death")
    env = {**os.environ, "MEMPALACE_EMBED_SOCK": sock_path}

    proc = subprocess.Popen(
        [sys.executable, "-m", "mempalace.embed_daemon"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        _wait_ready(proc)

        # Send a request, then kill daemon while it's still writing
        payload = json.dumps({"texts": ["kill me mid write"]}).encode("utf-8")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(sock_path)
        s.sendall(len(payload).to_bytes(4, "big") + payload)

        # Read the length header
        raw_len = b""
        while len(raw_len) < 4:
            raw_len += s.recv(4 - len(raw_len))
        msg_len = int.from_bytes(raw_len, "big")

        # Kill daemon while we're mid-body-read
        proc.terminate()
        proc.wait(timeout=5)

        # Now try to read the body — should get an error
        data = b""
        exc_type = None
        try:
            while len(data) < msg_len:
                chunk = s.recv(min(65536, msg_len - len(data)))
                if not chunk:
                    break
                data += chunk
        except (ConnectionResetError, OSError, BrokenPipeError, TimeoutError) as e:
            exc_type = type(e).__name__
        finally:
            s.close()

        # Any of these are acceptable — the key is we got an error, not hanging forever
        assert exc_type is not None, (
            f"Expected an exception during body recv after daemon death, got none. "
            f"Received data: {len(data)} bytes."
        )

    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


@pytest.mark.slow
def test_truncated_body_close(tmp_path):
    """Send a valid header for 1000 bytes but close the socket after 500 body bytes."""
    sock_path = _make_sock_path(tmp_path, "trunc")
    env = {**os.environ, "MEMPALACE_EMBED_SOCK": sock_path}

    proc = subprocess.Popen(
        [sys.executable, "-m", "mempalace.embed_daemon"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        _wait_ready(proc)

        # Send header claiming 1000-byte body, then close after 500 bytes
        header = (1000).to_bytes(4, "big")
        body = b"X" * 500

        exc_type = None
        try:
            _send_raw_header_body(sock_path, header, body, timeout=5.0)
        except (ConnectionResetError, OSError, BrokenPipeError, TimeoutError) as e:
            exc_type = type(e).__name__

        # Daemon should have closed or rejected the malformed request
        assert exc_type is not None, (
            f"Expected an exception sending truncated body, got none. "
            f"Daemon should have closed the connection."
        )

    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


@pytest.mark.slow
def test_garbage_before_protocol(tmp_path):
    """Send raw garbage bytes before closing — daemon should reject gracefully."""
    sock_path = _make_sock_path(tmp_path, "garbage")
    env = {**os.environ, "MEMPALACE_EMBED_SOCK": sock_path}

    proc = subprocess.Popen(
        [sys.executable, "-m", "mempalace.embed_daemon"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        _wait_ready(proc)

        # Send garbage and close immediately
        garbage = b"GARBAGE1234567890XXXX"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        try:
            s.connect(sock_path)
            s.sendall(garbage)
        finally:
            s.close()

        # Give daemon a moment to process and reject
        time.sleep(0.5)

        # Daemon should still be alive (rejected gracefully), send a valid request to confirm
        payload = json.dumps({"texts": ["still alive?"]}).encode("utf-8")
        s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s2.settimeout(10.0)
        try:
            s2.connect(sock_path)
            s2.sendall(len(payload).to_bytes(4, "big") + payload)

            raw_len = b""
            while len(raw_len) < 4:
                raw_len += s2.recv(4 - len(raw_len))
            msg_len = int.from_bytes(raw_len, "big")

            data = b""
            while len(data) < msg_len:
                chunk = s2.recv(min(65536, msg_len - len(data)))
                if not chunk:
                    break
                data += chunk

            resp = json.loads(data.decode("utf-8"))
            # After garbage, a valid subsequent request should still work
            assert resp.get("error") is None, f"Valid request after garbage failed: {resp.get('error')}"
        finally:
            s2.close()

    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass