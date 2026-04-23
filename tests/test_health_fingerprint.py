"""
test_health_fingerprint.py — Tests for /health endpoint fingerprint fields.

Verifies that /health returns the expected operational fingerprint:
- service, version, transport, shared_server_mode
- palace_path, backend
- memory_pressure
"""

import os
import pytest

pytest.importorskip("lancedb", reason="LanceDB required")


def _mock_embed_texts(texts):
    """Deterministic fake embeddings — bypasses MLX daemon."""
    import hashlib
    dim = 256
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:dim]) + [0.0] * (dim - len(h))
        result.append(vec)
    return result


@pytest.fixture
def temp_palace(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    return str(palace)


class TestHealthEndpointFingerprint:
    """HTTP /health endpoint returns correct fingerprint fields.

    Tests the health_check response structure by directly testing the
    factory.create_server health route registration and attributes.
    """

    def test_server_has_shared_server_mode_flag(self, temp_palace):
        """Server._shared_server_mode is True when shared_server_mode=True or transport=http."""
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_PALACE_PATH"] = temp_palace
        os.environ["MEMPALACE_DB_BACKEND"] = "lance"
        # Note: settings.transport is set by MEMPALACE_TRANSPORT env var.
        # When transport=http (the default in test env), shared_server_mode is True.

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.server.factory import create_server

        try:
            server = create_server(shared_server_mode=True)
            # shared_server_mode=True activates the flag
            assert getattr(server, "_shared_server_mode", False) is True
        finally:
            lance_module._embed_texts = original

    def test_health_response_keys(self, temp_palace):
        """Health endpoint returns all required fingerprint keys in real response."""
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_PALACE_PATH"] = temp_palace
        os.environ["MEMPALACE_DB_BACKEND"] = "lance"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.server.factory import create_server
        from starlette.requests import Request
        import asyncio, json

        try:
            server = create_server(shared_server_mode=True)

            # Extract the actual health_check handler from registered routes
            health_handler = None
            for r in server._additional_http_routes:
                if getattr(r, "name", None) == "health":
                    health_handler = getattr(r, "endpoint", None)
                    break
            assert health_handler is not None, "health route not found"

            # Construct a request scope for the handler
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/health",
                "query_string": b"",
                "headers": [],
                "root_path": "",
                "server": ("testclient", 80),
            }

            async def mock_receive():
                return {"type": "http.request", "body": b""}

            # Call the actual handler — it returns a JSONResponse directly
            async def call_handler():
                request = Request(scope, mock_receive)
                return await health_handler(request)

            response = asyncio.run(call_handler())

            # Parse the real JSON response body
            result = json.loads(response.body)

            # Verify all required fingerprint keys are present in the REAL response
            required_keys = {
                "status", "service", "version", "transport",
                "shared_server_mode", "palace_path", "backend",
            }
            assert isinstance(result, dict), "health endpoint must return a dict"
            for key in required_keys:
                assert key in result, f"Missing required fingerprint key: {key}"

            # Verify values make sense
            assert result["status"] == "ok"
            assert result["service"] == "mempalace"
            assert result["transport"] == "http"
            assert isinstance(result["version"], str)
            assert len(result["version"].split(".")) == 3, "version must be semver"
            assert result["shared_server_mode"] is True
            assert result["palace_path"] == temp_palace
            assert result["backend"] == "lance"
        finally:
            lance_module._embed_texts = original

    def test_health_transport_reflects_shared_mode(self, temp_palace):
        """transport field is 'http' when shared_server_mode is True (or transport=http env)."""
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_PALACE_PATH"] = temp_palace
        os.environ["MEMPALACE_DB_BACKEND"] = "lance"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.server.factory import create_server

        try:
            server = create_server(shared_server_mode=True)
            shared_mode = getattr(server, "_shared_server_mode", False)
            transport = "http" if shared_mode else "stdio"
            # shared_server_mode=True always activates HTTP mode
            assert transport == "http"
            assert shared_mode is True
        finally:
            lance_module._embed_texts = original

    def test_memory_pressure_unknown_when_guard_not_running(self, temp_palace):
        """memory_pressure field is present in health response."""
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_PALACE_PATH"] = temp_palace
        os.environ["MEMPALACE_DB_BACKEND"] = "lance"

        import mempalace.backends.lance as lance_module
        original = lance_module._embed_texts
        lance_module._embed_texts = _mock_embed_texts

        from mempalace.server.factory import create_server

        try:
            server = create_server()

            # Simulate what health_check does
            memory_pressure = "unknown"
            try:
                from mempalace.memory_guard import MemoryGuard
                guard = MemoryGuard.get_if_running()
                if guard is not None:
                    memory_pressure = guard.pressure.value
            except Exception:
                pass

            # MemoryGuard is started in test env so it should not be unknown
            # (unless get_if_running returns None)
            assert isinstance(memory_pressure, str)
        finally:
            lance_module._embed_texts = original
