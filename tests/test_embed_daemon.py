"""
Tests for the MemPalace embedding daemon.

Run: pytest tests/test_embed_daemon.py -v -s
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed — run: pip install 'mempalace[lance]'")


def _send_embedding_request(sock_path: str, texts: list[str], timeout: float = 60.0) -> dict:
    """Send embedding request to daemon, return parsed response."""
    payload = json.dumps({"texts": texts}).encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    s.sendall(len(payload).to_bytes(4, "big") + payload)

    raw_len = b""
    while len(raw_len) < 4:
        raw_len += s.recv(4 - len(raw_len))
    msg_len = int.from_bytes(raw_len, "big")

    data = b""
    while len(data) < msg_len:
        chunk = s.recv(min(65536, msg_len - len(data)))
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data.decode("utf-8"))


def test_daemon_starts_and_embeds():
    """Daemon starts, accepts request, returns valid vectors."""
    sock_path = f"/tmp/mp_embed_test_{os.getpid()}.sock"
    env = {**os.environ, "MEMPALACE_EMBED_SOCK": sock_path}

    proc = subprocess.Popen(
        [sys.executable, "-m", "mempalace.embed_daemon"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        # Wait for READY
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            line = proc.stdout.readline().decode().strip()
            if line == "READY":
                ready = True
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="ignore")
                pytest.fail(f"Daemon exited before READY: {stderr}")
        assert ready, "Daemon did not emit READY within 30s"

        # Send embedding request
        texts = ["Hello world", "LanceDB vector database for AI agents"]
        response = _send_embedding_request(sock_path, texts)

        assert response["error"] is None, f"Error returned: {response['error']}"
        assert len(response["embeddings"]) == 2
        assert len(response["embeddings"][0]) == 384  # bge-small-en-v1.5 dims
        assert len(response["embeddings"][1]) == 384

    finally:
        proc.terminate()
        proc.wait(timeout=5)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


def test_daemon_concurrent_clients():
    """Six clients send requests simultaneously to one daemon."""
    sock_path = f"/tmp/mp_embed_concur_{os.getpid()}.sock"
    env = {**os.environ, "MEMPALACE_EMBED_SOCK": sock_path}

    proc = subprocess.Popen(
        [sys.executable, "-m", "mempalace.embed_daemon"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.stdout.readline().decode().strip() == "READY":
                break

        results: dict = {}
        errors: dict = {}

        def client_request(client_id: int) -> None:
            try:
                texts = [f"Memory from session {client_id}, item {i}" for i in range(5)]
                resp = _send_embedding_request(sock_path, texts, timeout=90.0)
                results[client_id] = len(resp["embeddings"])
            except Exception as e:
                errors[client_id] = str(e)

        threads = [threading.Thread(target=client_request, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert not errors, f"Client errors: {errors}"
        assert all(v == 5 for v in results.values()), f"Unexpected result counts: {results}"

    finally:
        proc.terminate()
        proc.wait(timeout=5)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


def test_auto_start_from_lance():
    """LanceCollection auto-starts daemon on first add() via _start_daemon_if_needed."""
    sock_path = f"/tmp/mp_embed_auto_{os.getpid()}.sock"

    # Set custom socket env before importing lance module
    original_env = os.environ.get("MEMPALACE_EMBED_SOCK")
    os.environ["MEMPALACE_EMBED_SOCK"] = sock_path

    try:
        # Force reimport of lance module to pick up env change
        import importlib
        import mempalace.backends.lance
        importlib.reload(mempalace.backends.lance)

        from mempalace.backends.lance import LanceBackend

        backend = LanceBackend()
        col = backend.get_collection(f"/tmp/mp_palace_{os.getpid()}", "auto_test", create=True)

        # This should auto-start the daemon
        col.add(
            documents=["Auto-started daemon test document"],
            ids=["auto_1"],
            metadatas=[{"wing": "test"}],
        )

        assert col.count() == 1

        # Query should work too
        results = col.query(query_texts=["Auto-started daemon"], n_results=1)
        assert "auto_1" in results["ids"][0]

    finally:
        os.environ.pop("MEMPALACE_EMBED_SOCK", None)
        if original_env is not None:
            os.environ["MEMPALACE_EMBED_SOCK"] = original_env
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
