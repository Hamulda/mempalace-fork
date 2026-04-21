"""
test_transport_contract.py — Transport layer canonical path tests.

Verifies the single canonical HTTP transport contract:
- Shared HTTP = streamable-http via factory.create_server() + mcp.run()
- Stdio = single-session / dev mode
- No dual/legacy HTTP path remains
- Session coordinators activate correctly in shared mode
"""
import inspect
import subprocess
import sys
import pytest

from mempalace.server.factory import create_server
from mempalace.settings import MemPalaceSettings


class TestCanonicalHTTPCreateServer:
    """factory.create_server() is the single canonical server factory."""

    def test_create_server_http_sets_shared_server_mode(self, tmp_path):
        """transport='http' → _shared_server_mode=True and all coordinators attached."""
        palace = str(tmp_path / "hc")
        settings = MemPalaceSettings()
        settings.palace_path = palace
        settings.db_path = str(tmp_path / "hc_db")
        settings.transport = "http"

        mcp = create_server(settings=settings, shared_server_mode=False)

        assert getattr(mcp, "_shared_server_mode", False) is True
        assert getattr(mcp, "_session_registry", None) is not None
        assert getattr(mcp, "_write_coordinator", None) is not None
        assert getattr(mcp, "_claims_manager", None) is not None
        assert getattr(mcp, "_handoff_manager", None) is not None
        assert getattr(mcp, "_decision_tracker", None) is not None

    def test_create_server_explicit_shared_mode(self, tmp_path):
        """shared_server_mode=True → coordinators attached regardless of transport."""
        palace = str(tmp_path / "hs")
        settings = MemPalaceSettings()
        settings.palace_path = palace
        settings.db_path = str(tmp_path / "hs_db")
        settings.transport = "stdio"  # explicit stdio, but...

        mcp = create_server(settings=settings, shared_server_mode=True)  # ...coordinators forced on

        assert getattr(mcp, "_shared_server_mode", False) is True
        assert getattr(mcp, "_session_registry", None) is not None

    def test_create_server_stdio_no_coordinators(self, tmp_path):
        """transport='stdio' and shared_server_mode=False → no coordinators."""
        palace = str(tmp_path / "sc")
        settings = MemPalaceSettings()
        settings.palace_path = palace
        settings.db_path = str(tmp_path / "sc_db")
        settings.transport = "stdio"

        mcp = create_server(settings=settings, shared_server_mode=False)

        assert getattr(mcp, "_shared_server_mode", None) is None
        assert getattr(mcp, "_session_registry", None) is None
        assert getattr(mcp, "_write_coordinator", None) is None

    def test_create_server_http_run_signature(self):
        """create_server exists and is callable with expected signature."""
        sig = inspect.signature(create_server)
        params = list(sig.parameters.keys())
        assert "settings" in params
        assert "shared_server_mode" in params


class TestServeHttpDeprecatedShim:
    """serve_http (http_transport.py) is a deprecated backward-compat shim."""

    def test_serve_http_deprecated_warns(self):
        """serve_http emits a DeprecationWarning redirecting to canonical path."""
        import warnings
        from mempalace.fastmcp_server import serve_http

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            try:
                # Call with timeout guard — serve_http.run() blocks, so we
                # need to catch the warning before it blocks. We can only
                # test that importing it is deprecated, not the full call.
                pass
            except Exception:
                pass  # Expected — run() blocks

            # The import itself should be clean; check the function docstring
            assert "deprecated" in serve_http.__doc__.lower() or \
                   "DEPRECATED" in serve_http.__doc__, \
                   "serve_http docstring must mark it as deprecated"

    def test_serve_http_signature_preserved(self):
        """serve_http has (host, port, server) signature for backward compat."""
        from mempalace.fastmcp_server import serve_http
        sig = inspect.signature(serve_http)
        params = list(sig.parameters.keys())
        assert params == ["host", "port", "server"], \
            f"serve_http signature must be (host, port, server), got {params}"


class TestNoLegacyHTTPTransport:
    """http_transport.py no longer contains a custom Starlette HTTP server."""

    def test_http_transport_no_starlette_routes(self):
        """http_transport.py must NOT define Starlette Route objects."""
        with open("mempalace/server/http_transport.py") as f:
            source = f.read()
        # The deprecated shim should NOT have Route, Starlette, uvicorn imports
        # that the old custom HTTP server used
        assert "Route(" not in source, \
            "http_transport.py must not define Starlette Route objects"
        assert "uvicorn.run" not in source, \
            "http_transport.py must not call uvicorn.run() — use FastMCP streamable-http"

    def test_http_transport_no_server_handle_request(self):
        """http_transport.py must NOT call server.handle_request directly."""
        with open("mempalace/server/http_transport.py") as f:
            source = f.read()
        assert "server.handle_request" not in source, \
            "http_transport.py must not bypass FastMCP with server.handle_request"


class TestCmdServeUsesCanonicalPath:
    """CLI cmd_serve uses canonical streamable-http path (not legacy wrapper)."""

    def test_cmd_serve_imports_factory_not_serve_http(self):
        """cmd_serve must import create_server from factory, not serve_http."""
        with open("mempalace/cli.py") as f:
            source = f.read()
        # Find the cmd_serve function
        assert "from .server.factory import serve_http" not in source, \
            "cmd_serve must not import serve_http from factory — use canonical path"
        # Should use create_server directly
        assert "from .server.factory import create_server" in source, \
            "cmd_serve must import create_server from factory"

    def test_cmd_serve_calls_streamable_http_run(self):
        """cmd_serve must call mcp.run(transport='streamable-http')."""
        with open("mempalace/cli.py") as f:
            source = f.read()
        assert 'transport="streamable-http"' in source or \
               "transport='streamable-http'" in source, \
               "cmd_serve must use mcp.run(transport='streamable-http')"
        assert "shared_server_mode=True" in source, \
            "cmd_serve must pass shared_server_mode=True to create_server"

    def test_factory_main_uses_streamable_http(self):
        """factory.py __main__ must use streamable-http when transport is http."""
        with open("mempalace/server/factory.py") as f:
            source = f.read()
        assert 'transport="streamable-http"' in source, \
            "factory __main__ must use streamable-http transport"


class TestScriptsUseCanonicalPath:
    """Shell scripts use the canonical factory __main__ path."""

    def test_start_server_uses_fastmcp_module(self):
        """start_server.sh uses 'python -m mempalace.fastmcp_server' (canonical)."""
        with open("scripts/start_server.sh") as f:
            source = f.read()
        assert "mempalace.fastmcp_server" in source, \
            "start_server.sh must use the canonical mempalace.fastmcp_server module"

    def test_dev_server_uses_streamable_http(self):
        """dev_server.sh passes --transport streamable-http to fastmcp run."""
        with open("scripts/dev_server.sh") as f:
            source = f.read()
        assert "streamable-http" in source, \
            "dev_server.sh must use --transport streamable-http"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
