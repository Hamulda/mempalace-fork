"""
Tests for Phase 3b Claim Enforcement in Write Paths.

Verifies that write tools honor claim semantics:
- valid owner can write (claim_mode=strict, advisory)
- conflicting write is blocked in strict mode
- conflicting write is allowed with warning in advisory mode
- unclaimed write is allowed (no active claim)
- non-shared mode (no ClaimsManager) falls back to allowing writes
- session_id=None falls back to allowing writes

Run: pytest tests/test_claim_enforcement.py -v
"""

import tempfile
import pytest
from unittest.mock import MagicMock


palace_path_factory = tempfile.mkdtemp(prefix="mempalace_ce_")


def _pp():
    return tempfile.mkdtemp(prefix="mempalace_ce_")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockSettings:
    """Minimal settings stub for write tool registration."""
    def __init__(self, palace_path: str):
        self.db_path = palace_path
        self.effective_collection_name = "test_collection"
        self.wal_dir = palace_path
        self.palace_path = palace_path
        self.timeout_write = 30
        self.timeout_read = 15
        self.timeout_embed = 60


class _MockClaimsManager:
    """In-memory ClaimsManager fake for testing."""
    def __init__(self):
        self._claims: dict[tuple, dict] = {}  # (target_type, target_id) -> claim

    def claim(self, target_type, target_id, session_id, ttl_seconds=600, payload=None):
        key = (target_type, target_id)
        existing = self._claims.get(key)
        if existing and existing["expires_at"] > _now() and existing["session_id"] != session_id:
            return {"acquired": False, "owner": existing["session_id"]}
        self._claims[key] = {
            "session_id": session_id,
            "expires_at": _expires_at(ttl_seconds),
            "payload": payload or {},
        }
        return {"acquired": True, "owner": session_id, "expires_at": self._claims[key]["expires_at"]}

    def check_conflicts(self, target_type, target_id, session_id):
        key = (target_type, target_id)
        c = self._claims.get(key)
        if c is None:
            return {"has_conflict": False}
        if c["expires_at"] <= _now():
            return {"has_conflict": False}
        if c["session_id"] == session_id:
            return {"has_conflict": False, "owner": session_id, "is_self": True}
        return {"has_conflict": True, "owner": c["session_id"], "expires_at": c["expires_at"]}

    def release_claim(self, target_type, target_id, session_id):
        key = (target_type, target_id)
        c = self._claims.get(key)
        if c is None or c["expires_at"] <= _now():
            return {"success": False, "error": "no_active_claim"}
        if c["session_id"] != session_id:
            return {"success": False, "error": "not_owner", "owner": c["session_id"]}
        del self._claims[key]
        return {"success": True}

    def cleanup_expired(self):
        return {"removed": 0}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _expires_at(ttl_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


# ---------------------------------------------------------------------------
# Test: _claim_check behavior in isolation
# ---------------------------------------------------------------------------

class TestClaimCheckUnit:
    """Unit tests for _claim_check helper."""

    def test_claim_check_no_session_id_proceeds(self):
        """session_id=None → fail-open, no error returned."""
        from mempalace.server._write_tools import _claim_check
        server = MagicMock()
        server._claims_manager = MagicMock()
        result = _claim_check(server, "/src/main.py", None, "strict")
        assert result is None

    def test_claim_check_no_claims_manager_proceeds(self):
        """Non-shared mode (no ClaimsManager) → fail-open."""
        from mempalace.server._write_tools import _claim_check
        server = MagicMock(spec=[])
        result = _claim_check(server, "/src/main.py", "session-a", "strict")
        assert result is None

    def test_claim_check_no_active_claim_proceeds(self):
        """No claim on target → proceed (no conflict)."""
        from mempalace.server._write_tools import _claim_check
        mgr = _MockClaimsManager()
        server = MagicMock()
        server._claims_manager = mgr
        result = _claim_check(server, "/src/main.py", "session-a", "strict")
        assert result is None

    def test_claim_check_self_claim_proceeds(self):
        """Session owns the claim → proceed."""
        from mempalace.server._write_tools import _claim_check
        mgr = _MockClaimsManager()
        mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        server = MagicMock()
        server._claims_manager = mgr
        result = _claim_check(server, "/src/main.py", "session-a", "strict")
        assert result is None

    def test_claim_check_other_session_strict_blocks(self):
        """Other session holds claim + strict mode → error dict."""
        from mempalace.server._write_tools import _claim_check
        mgr = _MockClaimsManager()
        mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        server = MagicMock()
        server._claims_manager = mgr
        result = _claim_check(server, "/src/main.py", "session-b", "strict")
        assert result is not None
        assert result["error"] == "claim_conflict"
        assert result["owner"] == "session-a"
        assert "hint" in result

    def test_claim_check_other_session_advisory_proceeds(self):
        """Other session holds claim + advisory mode → warning dict (not None)."""
        from mempalace.server._write_tools import _claim_check
        mgr = _MockClaimsManager()
        mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        server = MagicMock()
        server._claims_manager = mgr
        result = _claim_check(server, "/src/main.py", "session-b", "advisory")
        assert result is not None  # returns warning dict, not None
        assert result["warning"] == "claim_advisory_write"
        assert result["owner"] == "session-a"
        assert "message" in result

    def test_claim_check_unknown_mode_defaults_to_advisory(self):
        """Unknown claim_mode → treated as advisory, returns warning dict."""
        from mempalace.server._write_tools import _claim_check
        mgr = _MockClaimsManager()
        mgr.claim("file", "/src/main.py", "session-a", ttl_seconds=600)
        server = MagicMock()
        server._claims_manager = mgr
        # "banana" is not a valid mode — should fall back to advisory
        result = _claim_check(server, "/src/main.py", "session-b", "banana")
        assert result is not None
        assert result["warning"] == "claim_advisory_write"


# ---------------------------------------------------------------------------
# Test: write tool integration (functional, mocked backend)
# ---------------------------------------------------------------------------

class TestWriteToolsClaimIntegration:
    """Functional tests wiring ClaimsManager into write tool signatures."""

    @pytest.fixture
    def mock_server_and_backend(self):
        """Create mock server with ClaimsManager + minimal backend."""
        from mempalace.server._write_tools import register_write_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = _pp()
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()

        claims_mgr = _MockClaimsManager()
        server._claims_manager = claims_mgr

        # Mock backend that returns a fake collection
        fake_col = MagicMock()
        fake_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        fake_col.upsert.return_value = None
        fake_col.delete.return_value = None
        fake_col.query.return_value = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[0.0]]}

        backend = MagicMock()
        backend.get_collection.return_value = fake_col

        config = MagicMock()
        config.palace_path = tmp

        # Memory guard that always allows writes
        mem_guard = MagicMock()
        mem_guard.should_pause_writes.return_value = False

        # Capture registered tool functions directly.
        # server.tool() is a decorator that registers the tool; we replace it with
        # an identity decorator so the actual function objects are preserved.
        captured = {}
        _original_tool = server.tool

        def capture_tool_decorator(**kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator
        server.tool = capture_tool_decorator

        register_write_tools(server, backend, config, settings, mem_guard)

        # Restore original
        server.tool = _original_tool

        return server, claims_mgr, fake_col, settings, captured

    def test_add_drawer_valid_owner_strict_succeeds(self, mock_server_and_backend):
        """Owner with valid claim + strict → write succeeds."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        # session-a claims the path
        target = f"{settings.palace_path}/wing_test/room_test"
        claims_mgr.claim("file", target, "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_add_drawer"]
        result = tool(
            dummy_ctx,
            wing="wing_test", room="room_test", content="test content",
            source_file=None, added_by="test",
            session_id="session-a", claim_mode="strict",
        )
        assert result.get("success") is True
        assert fake_col.upsert.called

    def test_add_drawer_conflict_strict_blocked(self, mock_server_and_backend):
        """session-b tries to write to path claimed by session-a in strict → blocked."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        target = f"{settings.palace_path}/wing_test/room_test"
        claims_mgr.claim("file", target, "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_add_drawer"]
        result = tool(
            dummy_ctx,
            wing="wing_test", room="room_test", content="test content",
            source_file=None, added_by="test",
            session_id="session-b", claim_mode="strict",
        )
        assert result.get("error") == "claim_conflict"
        assert result.get("owner") == "session-a"
        assert not fake_col.upsert.called

    def test_add_drawer_conflict_advisory_succeeds(self, mock_server_and_backend):
        """session-b writes to claimed path in advisory mode → allowed with warning."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        target = f"{settings.palace_path}/wing_test/room_test"
        claims_mgr.claim("file", target, "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_add_drawer"]
        result = tool(
            dummy_ctx,
            wing="wing_test", room="room_test", content="test content",
            source_file=None, added_by="test",
            session_id="session-b", claim_mode="advisory",
        )
        assert result.get("success") is True
        assert fake_col.upsert.called
        assert "claim_warning" in result

    def test_add_drawer_no_session_id_succeeds(self, mock_server_and_backend):
        """session_id=None → fail-open, write proceeds."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        claims_mgr.claim("file", f"{settings.palace_path}/wing_test/room_test", "session-a")

        dummy_ctx = MagicMock()
        tool = captured["mempalace_add_drawer"]
        result = tool(
            dummy_ctx,
            wing="wing_test", room="room_test", content="test content",
            source_file=None, added_by="test",
            session_id=None, claim_mode="strict",
        )
        assert result.get("success") is True
        assert fake_col.upsert.called

    def test_add_drawer_no_claims_manager_succeeds(self, mock_server_and_backend):
        """Non-shared mode (no _claims_manager) → fail-open."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend
        del server._claims_manager  # simulate non-shared mode

        dummy_ctx = MagicMock()
        tool = captured["mempalace_add_drawer"]
        result = tool(
            dummy_ctx,
            wing="wing_test", room="room_test", content="test content",
            source_file=None, added_by="test",
            session_id="session-a", claim_mode="strict",
        )
        assert result.get("success") is True
        assert fake_col.upsert.called

    def test_delete_drawer_conflict_strict_blocked(self, mock_server_and_backend):
        """Delete in strict mode with conflicting claim → blocked."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        # Pre-populate the drawer so delete finds it
        fake_col.get.return_value = {
            "ids": ["drawer_abc123"],
            "documents": ["some content"],
            "metadatas": [{"wing": "w", "room": "r"}],
        }

        claims_mgr.claim("file", "drawer_abc123", "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_delete_drawer"]
        result = tool(
            dummy_ctx,
            drawer_id="drawer_abc123",
            session_id="session-b", claim_mode="strict",
        )
        assert result.get("error") == "claim_conflict"
        assert not fake_col.delete.called

    def test_delete_drawer_conflict_advisory_succeeds(self, mock_server_and_backend):
        """Delete in advisory mode with conflicting claim → allowed."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        fake_col.get.return_value = {
            "ids": ["drawer_abc123"],
            "documents": ["some content"],
            "metadatas": [{"wing": "w", "room": "r"}],
        }

        claims_mgr.claim("file", "drawer_abc123", "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_delete_drawer"]
        result = tool(
            dummy_ctx,
            drawer_id="drawer_abc123",
            session_id="session-b", claim_mode="advisory",
        )
        assert result.get("success") is True
        assert fake_col.delete.called

    def test_diary_write_valid_owner_succeeds(self, mock_server_and_backend):
        """Diary write with own claim → succeeds."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        # room='diary' is hardcoded in the tool, so target is wing/diary not wing/room_diary
        target = f"{settings.palace_path}/wing_agent_name/diary"
        claims_mgr.claim("file", target, "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_diary_write"]
        result = tool(
            dummy_ctx,
            agent_name="agent_name", entry="test diary entry",
            topic="general",
            session_id="session-a", claim_mode="strict",
        )
        assert result.get("success") is True
        assert fake_col.upsert.called

    def test_diary_write_conflict_strict_blocked(self, mock_server_and_backend):
        """Diary write in strict mode with conflicting claim → blocked."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        target = f"{settings.palace_path}/wing_agent_name/diary"
        claims_mgr.claim("file", target, "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_diary_write"]
        result = tool(
            dummy_ctx,
            agent_name="agent_name", entry="test diary entry",
            topic="general",
            session_id="session-b", claim_mode="strict",
        )
        assert result.get("error") == "claim_conflict"
        assert not fake_col.upsert.called

    def test_remember_code_conflict_strict_blocked(self, mock_server_and_backend):
        """Code memory write in strict mode with conflicting claim → blocked."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        target = f"{settings.palace_path}/wing_code/room_notes"
        claims_mgr.claim("file", target, "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_remember_code"]
        result = tool(
            dummy_ctx,
            code="print('hello')", description="hello example",
            wing="wing_code", room="room_notes",
            source_file=None, added_by="test",
            session_id="session-b", claim_mode="strict",
        )
        assert result.get("error") == "claim_conflict"
        assert not fake_col.upsert.called

    def test_remember_code_no_conflict_succeeds(self, mock_server_and_backend):
        """Code memory write with no competing claim → succeeds."""
        server, claims_mgr, fake_col, settings, captured = mock_server_and_backend

        dummy_ctx = MagicMock()
        tool = captured["mempalace_remember_code"]
        result = tool(
            dummy_ctx,
            code="print('hello')", description="hello example",
            wing="wing_code", room="room_notes",
            source_file=None, added_by="test",
            session_id="session-a", claim_mode="strict",
        )
        assert result.get("success") is True
        assert fake_col.upsert.called


# ---------------------------------------------------------------------------
# Test: backward compatibility — existing calls without new params work
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Verify write tools remain backward-compatible (no new required params)."""

    def test_add_drawer_callable_without_session_params(self, tmp_path):
        """Call add_drawer with just the original 5 positional args → no TypeError."""
        import sys
        from unittest.mock import MagicMock
        from mempalace.server._write_tools import register_write_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = str(tmp_path)
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()
        # No _claims_manager → non-shared mode (backward compat)

        fake_col = MagicMock()
        fake_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        fake_col.upsert.return_value = None

        backend = MagicMock()
        backend.get_collection.return_value = fake_col

        config = MagicMock()
        config.palace_path = tmp

        mem_guard = MagicMock()
        mem_guard.should_pause_writes.return_value = False

        captured = {}
        _orig_tool = server.tool
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_write_tools(server, backend, config, settings, mem_guard)

        server.tool = _orig_tool

        # ctx is required by FastMCP tool signature; pass a dummy
        dummy_ctx = MagicMock()

        tool = captured["mempalace_add_drawer"]
        # Call with original 5 args only — no session_id, no claim_mode
        result = tool(dummy_ctx, wing="wing_compat", room="room_compat", content="backward compat test")
        # Should succeed (fail-open in non-shared mode)
        assert result.get("success") is True

    def test_delete_drawer_callable_without_session_params(self, tmp_path):
        """delete_drawer with original signature → no TypeError."""
        from unittest.mock import MagicMock
        from mempalace.server._write_tools import register_write_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = str(tmp_path)
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()

        fake_col = MagicMock()
        fake_col.get.return_value = {
            "ids": ["drawer_xyz"],
            "documents": ["content"],
            "metadatas": [{"wing": "w", "room": "r"}],
        }
        fake_col.delete.return_value = None

        backend = MagicMock()
        backend.get_collection.return_value = fake_col

        config = MagicMock()
        config.palace_path = tmp

        mem_guard = MagicMock()
        mem_guard.should_pause_writes.return_value = False

        captured = {}
        _orig_tool = server.tool
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_write_tools(server, backend, config, settings, mem_guard)

        server.tool = _orig_tool

        dummy_ctx = MagicMock()

        tool = captured["mempalace_delete_drawer"]
        # Original signature: only drawer_id
        result = tool(dummy_ctx, drawer_id="drawer_xyz")
        assert result.get("success") is True

    def test_diary_write_callable_without_session_params(self, tmp_path):
        """diary_write with original signature → no TypeError."""
        from unittest.mock import MagicMock
        from mempalace.server._write_tools import register_write_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = str(tmp_path)
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()

        fake_col = MagicMock()
        fake_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        fake_col.upsert.return_value = None

        backend = MagicMock()
        backend.get_collection.return_value = fake_col

        config = MagicMock()
        config.palace_path = tmp

        mem_guard = MagicMock()
        mem_guard.should_pause_writes.return_value = False

        captured = {}
        _orig_tool = server.tool
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_write_tools(server, backend, config, settings, mem_guard)

        server.tool = _orig_tool

        dummy_ctx = MagicMock()

        tool = captured["mempalace_diary_write"]
        # Original signature: agent_name, entry, topic=...
        result = tool(dummy_ctx, agent_name="TestAgent", entry="Test entry", topic="general")
        assert result.get("success") is True


# ---------------------------------------------------------------------------
# Test: shared server mode instantiation
# ---------------------------------------------------------------------------

class TestSharedServerMode:
    """Verify factory instantiates ClaimsManager in shared_server_mode."""

    def test_create_server_instantiates_claims_manager_in_shared_mode(self, tmp_path):
        """create_server(shared_server_mode=True) → _claims_manager attached."""
        from mempalace.server.factory import create_server

        # Mock settings pointing to tmp palace
        from mempalace.settings import MemPalaceSettings
        original_pp = os.environ.get("MEMPALACE_PALACE_PATH")

        palace = str(tmp_path / "palace")
        os.environ["MEMPALACE_PALACE_PATH"] = palace

        try:
            settings = MemPalaceSettings()
            settings.palace_path = palace
            settings.db_path = str(tmp_path / "db")
            settings.transport = "stdio"  # not http

            mcp = create_server(settings=settings, shared_server_mode=True)

            assert hasattr(mcp, "_claims_manager")
            assert mcp._claims_manager is not None
            # Verify it's a real ClaimsManager
            from mempalace.claims_manager import ClaimsManager
            assert isinstance(mcp._claims_manager, ClaimsManager)
            mcp._claims_manager.close()
        finally:
            if original_pp:
                os.environ["MEMPALACE_PALACE_PATH"] = original_pp
            elif "MEMPALACE_PALACE_PATH" in os.environ:
                del os.environ["MEMPALACE_PALACE_PATH"]

    def test_create_server_no_claims_manager_in_stdio_mode(self, tmp_path):
        """create_server(shared_server_mode=False, transport=stdio) → no _claims_manager."""
        from mempalace.server.factory import create_server
        from mempalace.settings import MemPalaceSettings

        original_pp = os.environ.get("MEMPALACE_PALACE_PATH")
        palace = str(tmp_path / "palace2")
        os.environ["MEMPALACE_PALACE_PATH"] = palace

        try:
            settings = MemPalaceSettings()
            settings.palace_path = palace
            settings.db_path = str(tmp_path / "db2")
            settings.transport = "stdio"

            mcp = create_server(settings=settings, shared_server_mode=False)

            # No _claims_manager in non-shared stdio mode
            assert not hasattr(mcp, "_claims_manager") or mcp._claims_manager is None
        finally:
            if original_pp:
                os.environ["MEMPALACE_PALACE_PATH"] = original_pp
            elif "MEMPALACE_PALACE_PATH" in os.environ:
                del os.environ["MEMPALACE_PALACE_PATH"]

    def test_create_server_claims_manager_via_http_transport(self, tmp_path):
        """transport='http' → ClaimsManager instantiated even without shared_server_mode."""
        from mempalace.server.factory import create_server
        from mempalace.settings import MemPalaceSettings

        original_pp = os.environ.get("MEMPALACE_PALACE_PATH")
        palace = str(tmp_path / "palace3")
        os.environ["MEMPALACE_PALACE_PATH"] = palace

        try:
            settings = MemPalaceSettings()
            settings.palace_path = palace
            settings.db_path = str(tmp_path / "db3")
            settings.transport = "http"

            mcp = create_server(settings=settings, shared_server_mode=False)

            assert hasattr(mcp, "_claims_manager")
            assert mcp._claims_manager is not None
            from mempalace.claims_manager import ClaimsManager
            assert isinstance(mcp._claims_manager, ClaimsManager)
            mcp._claims_manager.close()
        finally:
            if original_pp:
                os.environ["MEMPALACE_PALACE_PATH"] = original_pp
            elif "MEMPALACE_PALACE_PATH" in os.environ:
                del os.environ["MEMPALACE_PALACE_PATH"]


# ---------------------------------------------------------------------------
# Test: consolidation write path (merge deletes)
# ---------------------------------------------------------------------------

class TestConsolidateWritePath:
    """consolidate tool deletes duplicates — same claim semantics apply."""

    def test_consolidate_merge_deletes_conflict_strict(self, tmp_path):
        """consolidate merge= with conflicting claim in strict → blocked before any delete."""
        from unittest.mock import MagicMock
        from mempalace.server._write_tools import register_write_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = str(tmp_path)
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()

        claims_mgr = _MockClaimsManager()
        server._claims_manager = claims_mgr

        fake_col = MagicMock()
        # Simulate 2 duplicate drawers
        fake_col.query.return_value = {
            "ids": [["dup1", "dup2"]],
            "documents": [["doc1", "doc2"]],
            "metadatas": [[{"wing": "w", "room": "r", "timestamp": "2026-01-01T00:00:00Z"},
                          {"wing": "w", "room": "r", "timestamp": "2026-01-02T00:00:00Z"}]],
            "distances": [[0.0, 0.1]],
        }
        fake_col.get.side_effect = [
            # First call: get keeper (dup1)
            {"ids": ["dup1"], "documents": ["doc1"], "metadatas": [{"wing": "w", "room": "r", "timestamp": "2026-01-01T00:00:00Z"}]},
            # Second call: get dup2 for timestamp
            {"ids": ["dup2"], "documents": ["doc2"], "metadatas": [{"wing": "w", "room": "r", "timestamp": "2026-01-02T00:00:00Z"}]},
        ]
        fake_col.delete.return_value = None

        backend = MagicMock()
        backend.get_collection.return_value = fake_col

        config = MagicMock()
        config.palace_path = tmp

        mem_guard = MagicMock()
        mem_guard.should_pause_writes.return_value = False

        captured = {}
        _orig_tool = server.tool
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_write_tools(server, backend, config, settings, mem_guard)

        server.tool = _orig_tool

        dummy_ctx = MagicMock()

        # Claim the consolidate target path (what the tool checks)
        consolidate_target = f"{tmp}/_consolidate/any topic"
        claims_mgr.claim("file", consolidate_target, "session-a", ttl_seconds=600)

        tool = captured["mempalace_consolidate"]
        # merge=True + strict mode + session-b (not owner) → blocked
        result = tool(dummy_ctx, topic="any topic", merge=True, threshold=0.85, session_id="session-b", claim_mode="strict")
        assert result.get("error") == "claim_conflict"
        # No deletes should have happened
        assert fake_col.delete.call_count == 0


class TestErrorContract:
    """Verify error response shape is consistent and actionable."""

    def test_claim_conflict_error_has_required_fields(self, tmp_path):
        """strict-mode conflict error must have: error, owner, target_id, hint."""
        from unittest.mock import MagicMock
        from mempalace.server._write_tools import register_write_tools
        from mempalace.server._infrastructure import make_status_cache

        tmp = str(tmp_path)
        settings = _MockSettings(tmp)
        settings.db_path = tmp
        settings.palace_path = tmp

        server = MagicMock()
        server._status_cache = make_status_cache()
        server._claims_manager = _MockClaimsManager()

        fake_col = MagicMock()
        fake_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        fake_col.upsert.return_value = None

        backend = MagicMock()
        backend.get_collection.return_value = fake_col

        config = MagicMock()
        config.palace_path = tmp

        mem_guard = MagicMock()
        mem_guard.should_pause_writes.return_value = False

        captured = {}
        _orig_tool = server.tool
        def capture(**kwargs):
            def dec(fn):
                captured[fn.__name__] = fn
                return fn
            return dec
        server.tool = capture

        register_write_tools(server, backend, config, settings, mem_guard)

        server.tool = _orig_tool

        # Set up conflict
        mgr = server._claims_manager
        mgr.claim("file", f"{settings.palace_path}/w/r", "session-a", ttl_seconds=600)

        dummy_ctx = MagicMock()
        tool = captured["mempalace_add_drawer"]
        result = tool(dummy_ctx, wing="w", room="r", content="c", session_id="session-b", claim_mode="strict")

        assert result.get("error") == "claim_conflict"
        assert "owner" in result
        assert "target_id" in result
        assert "hint" in result
        # hint should be actionable
        assert "TTL" in result["hint"] or "handoff" in result["hint"].lower()


import os
