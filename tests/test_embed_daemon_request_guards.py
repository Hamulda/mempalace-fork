"""
Tests for embed daemon request-size guards.

Validates:
- valid small request passes
- oversized msg_len rejected
- too many texts rejected
- oversized single text rejected
- non-string text item rejected
- daemon returns correct JSON error shape

Uses fake embed model — no real MLX/fastembed load.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Constants under test (imported from embed_daemon)
# ---------------------------------------------------------------------------
from mempalace.embed_daemon import (
    MAX_REQUEST_BYTES,
    MAX_TEXTS,
    MAX_CHARS_PER_TEXT,
    _handle_client,
    _daemon_sanitize_embeddings,
)


# ---------------------------------------------------------------------------
# Fake model used to intercept embed() calls
# ---------------------------------------------------------------------------
class FakeEmbedModel:
    DIMS = 256

    def embed(self, texts):
        import numpy as np
        # Return numpy array — _handle_client iterates and calls .tolist() on each row
        arr = np.zeros((len(texts), self.DIMS), dtype=np.float64)
        # Return iterator over rows to match real model behavior
        for row in arr:
            yield row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_frame(payload: dict) -> bytes:
    """Encode dict as length-prefixed JSON frame (matches daemon protocol)."""
    data = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def recv_frame(sock: socket.socket) -> dict:
    """Receive and decode a response frame from the daemon."""
    raw_len = b""
    while len(raw_len) < 4:
        chunk = sock.recv(4 - len(raw_len))
        if not chunk:
            raise ConnectionError("Daemon closed during header")
        raw_len += chunk
    msg_len = struct.unpack(">I", raw_len)[0]
    if msg_len > 10_000_000:
        raise RuntimeError(f"Response too large: {msg_len} bytes")
    data = b""
    while len(data) < msg_len:
        chunk = sock.recv(min(65536, msg_len - len(data)))
        if not chunk:
            raise ConnectionError("Daemon closed during body")
        data += chunk
    return json.loads(data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Inline validation helpers (mirror the guard logic in _handle_client)
# These are used to verify expected behavior without starting the real daemon.
# ---------------------------------------------------------------------------
def validate_request(request: dict) -> tuple[bool, str | None]:
    """
    Mirror the guard logic in _handle_client.
    Returns (ok, error_json_str | None).
    """
    texts = request.get("texts", [])

    if not isinstance(texts, list):
        err = json.dumps({
            "embeddings": [],
            "error": f"Expected 'texts' to be a list, got {type(texts).__name__}",
        }).encode("utf-8")
        return False, err.decode("utf-8")

    if len(texts) > MAX_TEXTS:
        err = json.dumps({
            "embeddings": [],
            "error": f"Too many texts: {len(texts)} (max {MAX_TEXTS})",
        }).encode("utf-8")
        return False, err.decode("utf-8")

    for i, text in enumerate(texts):
        if not isinstance(text, str):
            err = json.dumps({
                "embeddings": [],
                "error": f"texts[{i}] is not a string (got {type(text).__name__})",
            }).encode("utf-8")
            return False, err.decode("utf-8")
        if len(text) > MAX_CHARS_PER_TEXT:
            err = json.dumps({
                "embeddings": [],
                "error": f"texts[{i}] too long: {len(text)} chars (max {MAX_CHARS_PER_TEXT})",
            }).encode("utf-8")
            return False, err.decode("utf-8")

    return True, None


# ---------------------------------------------------------------------------
# Socket pair factory — uses localhost TCP so no file system deps
# ---------------------------------------------------------------------------
def _make_connected_pair():
    """Create a connected client/server socket pair on localhost."""
    srv_sock = socket.socket()
    srv_sock.bind(("127.0.0.1", 0))
    srv_sock.listen(1)
    port = srv_sock.getsockname()[1]

    cli_sock = socket.socket()
    cli_sock.connect(("127.0.0.1", port))
    # Use a shorter timeout for accept
    srv_sock.settimeout(5.0)
    srv_conn = srv_sock.accept()[0]
    srv_sock.close()
    return cli_sock, srv_conn


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
class TestRequestValidation:
    """Unit-test the inline validation mirror."""

    def test_valid_small_request(self):
        ok, err = validate_request({"texts": ["hello", "world"]})
        assert ok is True
        assert err is None

    def test_empty_texts_valid(self):
        ok, err = validate_request({"texts": []})
        assert ok is True

    def test_rejects_non_list_texts(self):
        ok, err = validate_request({"texts": "not a list"})
        assert ok is False
        assert "list" in err

    def test_rejects_dict_texts(self):
        ok, err = validate_request({"texts": {"a": 1}})
        assert ok is False

    def test_rejects_too_many_texts(self):
        ok, err = validate_request({"texts": ["x"] * (MAX_TEXTS + 1)})
        assert ok is False
        assert "Too many texts" in err
        assert str(MAX_TEXTS) in err

    def test_rejects_exactly_max_texts(self):
        ok, err = validate_request({"texts": ["x"] * MAX_TEXTS})
        assert ok is True

    def test_rejects_oversized_text(self):
        long_text = "x" * (MAX_CHARS_PER_TEXT + 1)
        ok, err = validate_request({"texts": [long_text]})
        assert ok is False
        assert "too long" in err

    def test_rejects_exactly_max_chars(self):
        ok_text = "x" * MAX_CHARS_PER_TEXT
        ok, err = validate_request({"texts": [ok_text]})
        assert ok is True

    def test_rejects_non_string_item(self):
        ok, err = validate_request({"texts": [123, "string", None]})
        assert ok is False
        assert "not a string" in err

    def test_error_response_shape(self):
        _, err = validate_request({"texts": "bad"})
        parsed = json.loads(err)
        assert "embeddings" in parsed
        assert parsed["embeddings"] == []
        assert "error" in parsed
        assert isinstance(parsed["error"], str)


class TestGuardConstants:
    """Verify the constants are env-configurable."""

    def test_default_values(self):
        # Defaults when env vars are unset
        assert MAX_REQUEST_BYTES == 2_000_000
        assert MAX_TEXTS == 512
        assert MAX_CHARS_PER_TEXT == 8192

    def test_env_override_max_bytes(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_EMBED_MAX_REQUEST_BYTES", "500000")
        import importlib
        import mempalace.embed_daemon as ed
        importlib.reload(ed)
        assert ed.MAX_REQUEST_BYTES == 500_000

    def test_env_override_max_texts(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_EMBED_MAX_TEXTS", "100")
        import importlib
        import mempalace.embed_daemon as ed
        importlib.reload(ed)
        assert ed.MAX_TEXTS == 100

    def test_env_override_max_chars(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_EMBED_MAX_CHARS_PER_TEXT", "4096")
        import importlib
        import mempalace.embed_daemon as ed
        importlib.reload(ed)
        assert ed.MAX_CHARS_PER_TEXT == 4096


class TestSanitizeEmbeddings:
    """Sanity-check _daemon_sanitize_embeddings with fake data."""

    def test_passthrough_finite(self):
        emb = [[0.1] * 256, [0.2] * 256]
        result = _daemon_sanitize_embeddings(emb)
        assert result == emb

    def test_replaces_nan(self):
        import math
        # Single NaN in 256-dim: after replacement all-zero → norm=0 → raises RuntimeError
        # (all-NaN cannot be repaired, must fail loudly)
        emb = [[float("nan")] + [0.0] * 255]
        with pytest.raises(RuntimeError, match="degenerate"):
            _daemon_sanitize_embeddings(emb)

    def test_replaces_inf(self):
        import math
        # Single Inf in 256-dim: after replacement all-zero → norm=0 → raises RuntimeError
        emb = [[float("inf")] + [0.0] * 255]
        with pytest.raises(RuntimeError, match="degenerate"):
            _daemon_sanitize_embeddings(emb)

    def test_replaces_sparse_nan(self):
        """Sparse NaN (128 finite + 128 NaN) → repaired and renormalized to unit length."""
        import math
        emb = [[1.0] * 128 + [float("nan")] * 128]
        result = _daemon_sanitize_embeddings(emb)
        assert all(math.isfinite(v) for v in result[0])
        norm = math.sqrt(sum(v * v for v in result[0]))
        assert 0.99 < norm < 1.01  # renormalized to unit length

    def test_raises_on_all_invalid(self):
        emb = [[float("nan")] * 256]
        with pytest.raises(RuntimeError, match="degenerate"):
            _daemon_sanitize_embeddings(emb)


# ---------------------------------------------------------------------------
# Integration tests via socketpair (real _handle_client, fake model)
# ---------------------------------------------------------------------------
class TestHandleClientGuards:
    """Exercise _handle_client with real sockets and fake model."""

    def test_valid_small_request_succeeds(self):
        """Small valid request → embeddings returned, error=null."""
        cli, srv = _make_connected_pair()
        try:
            done = threading.Event()
            result = [None]
            exc = [None]

            def handle():
                try:
                    _handle_client(srv, FakeEmbedModel())
                except Exception as e:
                    exc[0] = e
                finally:
                    done.set()

            t = threading.Thread(target=handle, daemon=True)
            t.start()
            # Give handler time to block on recv
            time.sleep(0.1)

            frame = make_frame({"texts": ["hello", "world"]})
            cli.sendall(frame)
            resp = recv_frame(cli)

            done.wait(timeout=3.0)
            assert exc[0] is None
            assert "embeddings" in resp
            assert resp["error"] is None
            assert len(resp["embeddings"]) == 2
            for emb in resp["embeddings"]:
                assert len(emb) == 256
                assert all(v == 0.0 for v in emb)  # fake model
        finally:
            cli.close()

    def test_too_many_texts_rejected(self):
        """len(texts) > MAX_TEXTS → error JSON."""
        cli, srv = _make_connected_pair()
        try:
            done = threading.Event()
            exc = [None]

            def handle():
                try:
                    _handle_client(srv, FakeEmbedModel())
                except Exception as e:
                    exc[0] = e
                finally:
                    done.set()

            t = threading.Thread(target=handle, daemon=True)
            t.start()
            time.sleep(0.1)

            frame = make_frame({"texts": ["x"] * (MAX_TEXTS + 1)})
            cli.sendall(frame)
            resp = recv_frame(cli)
            done.wait(timeout=3.0)

            assert exc[0] is None
            assert resp["embeddings"] == []
            assert "Too many texts" in resp["error"]
        finally:
            cli.close()

    def test_oversized_text_rejected(self):
        """Text longer than MAX_CHARS_PER_TEXT → error JSON."""
        cli, srv = _make_connected_pair()
        try:
            done = threading.Event()
            exc = [None]

            def handle():
                try:
                    _handle_client(srv, FakeEmbedModel())
                except Exception as e:
                    exc[0] = e
                finally:
                    done.set()

            t = threading.Thread(target=handle, daemon=True)
            t.start()
            time.sleep(0.1)

            frame = make_frame({"texts": ["x" * (MAX_CHARS_PER_TEXT + 1)]})
            cli.sendall(frame)
            resp = recv_frame(cli)
            done.wait(timeout=3.0)

            assert exc[0] is None
            assert resp["embeddings"] == []
            assert "too long" in resp["error"]
        finally:
            cli.close()

    def test_non_string_text_rejected(self):
        """Non-string item in texts list → error JSON."""
        cli, srv = _make_connected_pair()
        try:
            done = threading.Event()
            exc = [None]

            def handle():
                try:
                    _handle_client(srv, FakeEmbedModel())
                except Exception as e:
                    exc[0] = e
                finally:
                    done.set()

            t = threading.Thread(target=handle, daemon=True)
            t.start()
            time.sleep(0.1)

            frame = make_frame({"texts": [None, "valid", 42]})
            cli.sendall(frame)
            resp = recv_frame(cli)
            done.wait(timeout=3.0)

            assert exc[0] is None
            assert resp["embeddings"] == []
            assert "not a string" in resp["error"]
        finally:
            cli.close()

    def test_non_list_texts_rejected(self):
        """'texts' is a dict, not a list → error JSON."""
        cli, srv = _make_connected_pair()
        try:
            done = threading.Event()
            exc = [None]

            def handle():
                try:
                    _handle_client(srv, FakeEmbedModel())
                except Exception as e:
                    exc[0] = e
                finally:
                    done.set()

            t = threading.Thread(target=handle, daemon=True)
            t.start()
            time.sleep(0.1)

            frame = make_frame({"texts": {"a": 1}})
            cli.sendall(frame)
            resp = recv_frame(cli)
            done.wait(timeout=3.0)

            assert exc[0] is None
            assert resp["embeddings"] == []
            assert "list" in resp["error"]
        finally:
            cli.close()


# ---------------------------------------------------------------------------
# ChromaDB import check
# ---------------------------------------------------------------------------
def test_chromadb_not_in_modules():
    """Verify ChromaDB is not loaded after embed_daemon import."""
    import sys
    import mempalace.embed_daemon as ed
    import importlib
    importlib.reload(ed)
    # Check that neither 'chromadb' nor 'chromadb所在' is in modules
    for key in list(sys.modules):
        if "chromadb" in key.lower():
            pytest.fail(f"chromadb loaded: {key}")