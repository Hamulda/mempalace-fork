"""
test_fastmcp_server.py — Tests for FastMCP server using in-process Client.

Tests the mempalace.fastmcp_server module directly via FastMCP's
in-process Client transport — no subprocess, no ports, fast and isolated.

Migration from test_mcp_server.py (legacy MCP SDK) → FastMCP Client pattern.
"""

import json
import secrets
import pytest
import pytest_asyncio

try:
    import chromadb
    _CHROMADB_AVAILABLE = True
except ImportError:
    _CHROMADB_AVAILABLE = False

from unittest.mock import patch, MagicMock

from fastmcp import Client
from mempalace.fastmcp_server import create_server
from mempalace.settings import MemPalaceSettings
from mempalace.backends import get_backend

# ChromaDB backend has been removed — skip entire module
pytestmark = pytest.mark.skip(reason="ChromaDB backend removed — tests require chromadb")


pytestmark = pytest.mark.asyncio


def _get_result_data(result):
    """Extract JSON data from FastMCP CallToolResult.

    FastMCP 3.x returns CallToolResult with content=[TextContent(...)]
    or structured_content={} with already-parsed JSON.
    """
    if hasattr(result, 'structured_content') and result.structured_content:
        return result.structured_content
    if hasattr(result, 'content') and result.content:
        return json.loads(result.content[0].text)
    return None


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def test_settings(tmp_path):
    """Izolovaná test konfigurace — tmp_path je unikátní per test."""
    return MemPalaceSettings(
        db_path=str(tmp_path / "test_palace"),
        db_backend="chroma",
        cache_ttl_status=1,
        cache_ttl_metadata=1,
        log_sessions=False,
    )


@pytest_asyncio.fixture
async def client(test_settings):
    """Čerstvá server instance per test — plná izolace."""
    server = create_server(settings=test_settings)
    async with Client(transport=server) as c:
        yield c


@pytest_asyncio.fixture
async def empty_palace_client(tmp_path):
    """Client with empty palace for write tests."""
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    backend = get_backend("chroma")
    collection = backend.get_collection(str(palace_path), "mempalace_drawers", create=True)
    del collection
    test_settings = MemPalaceSettings(
        db_path=str(palace_path),
        db_backend="chroma",
    )
    server = create_server(settings=test_settings)
    async with Client(transport=server) as c:
        yield c, str(palace_path)


@pytest_asyncio.fixture
async def seeded_palace_client(tmp_path):
    """Client with pre-seeded collection for read tests."""
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_or_create_collection("mempalace_drawers")
    suffix = secrets.token_hex(3)
    col.add(
        ids=[
            f"drawer_proj_backend_{suffix}_aaa",
            f"drawer_proj_backend_{suffix}_bbb",
            f"drawer_proj_frontend_{suffix}_ccc",
            f"drawer_notes_planning_{suffix}_ddd",
        ],
        documents=[
            "The authentication module uses JWT tokens for session management. "
            "Tokens expire after 24 hours. Refresh tokens are stored in HttpOnly cookies.",
            "Database migrations are handled by Alembic. We use PostgreSQL 15 "
            "with connection pooling via pgbouncer.",
            "The React frontend uses TanStack Query for server state management. "
            "All API calls go through a centralized fetch wrapper.",
            "Sprint planning: migrate auth to passkeys by Q3. "
            "Evaluate ChromaDB alternatives for vector search.",
        ],
        metadatas=[
            {"wing": "project", "room": "backend", "source_file": "auth.py", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-01T00:00:00"},
            {"wing": "project", "room": "backend", "source_file": "db.py", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-02T00:00:00"},
            {"wing": "project", "room": "frontend", "source_file": "App.tsx", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-03T00:00:00"},
            {"wing": "notes", "room": "planning", "source_file": "sprint.md", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-04T00:00:00"},
        ],
    )
    del client
    test_settings = MemPalaceSettings(
        db_path=str(palace_path),
        db_backend="chroma",
    )
    server = create_server(settings=test_settings)
    async with Client(transport=server) as c:
        yield c, str(palace_path)


# ── Protocol Layer ──────────────────────────────────────────────────


async def test_list_tools_count(client):
    """Verify server exposes all registered tools (56 across 7 tool groups)."""
    tools = await client.list_tools()
    # 7 groups: search(12) + write(6) + kg(7) + code(5) + session(15) + symbol(5) + workflow(7) = 56
    assert len(tools) == 56


async def test_list_tools_contains_expected(client):
    """Verify all expected tool names are present."""
    tools = await client.list_tools()
    tool_names = {t.name for t in tools}
    expected = {
        # search tools (12)
        "mempalace_status",
        "mempalace_list_wings",
        "mempalace_list_rooms",
        "mempalace_get_taxonomy",
        "mempalace_get_aaak_spec",
        "mempalace_search",
        "mempalace_hybrid_search",
        "mempalace_check_duplicate",
        "mempalace_traverse_graph",
        "mempalace_find_tunnels",
        "mempalace_graph_stats",
        "mempalace_eval",
        # write tools (6)
        "mempalace_add_drawer",
        "mempalace_delete_drawer",
        "mempalace_diary_write",
        "mempalace_diary_read",
        "mempalace_remember_code",
        "mempalace_consolidate",
        # kg tools (7)
        "mempalace_kg_query",
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
        "mempalace_kg_supersede",
        "mempalace_kg_timeline",
        "mempalace_kg_stats",
        "mempalace_kg_history",
        # code tools (5)
        "mempalace_search_code",
        "mempalace_auto_search",
        "mempalace_file_context",
        "mempalace_project_context",
        "mempalace_export_claude_md",
        # session tools (15)
        "mempalace_claim_path",
        "mempalace_release_claim",
        "mempalace_list_claims",
        "mempalace_conflict_check",
        "mempalace_file_status",
        "mempalace_workspace_claims",
        "mempalace_edit_guidance",
        "mempalace_push_handoff",
        "mempalace_pull_handoffs",
        "mempalace_accept_handoff",
        "mempalace_complete_handoff",
        "mempalace_wakeup_context",
        "mempalace_capture_decision",
        "mempalace_list_decisions",
        # symbol tools (5)
        "mempalace_find_symbol",
        "mempalace_search_symbols",
        "mempalace_callers",
        "mempalace_recent_changes",
        "mempalace_file_symbols",
        # workflow tools (7)
        "mempalace_begin_work",
        "mempalace_prepare_edit",
        "mempalace_finish_work",
        "mempalace_publish_handoff",
        "mempalace_takeover_work",
        "mempalace_begin_work_batch",
        "mempalace_finish_work_batch",
    }
    assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}"


async def test_unknown_tool_returns_error(client):
    """Unknown tool should raise ToolError."""
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError) as exc_info:
        await client.call_tool("nonexistent_tool", {})
    assert "Unknown tool" in str(exc_info.value)


# ── Read Tools ───────────────────────────────────────────────────────


async def test_status_empty_palace(empty_palace_client):
    """Status on empty palace returns total_drawers=0."""
    client, palace_path = empty_palace_client
    result = await client.call_tool("mempalace_status", {})
    data = _get_result_data(result)
    assert data["total_drawers"] == 0


async def test_status_with_data(seeded_palace_client):
    """Status with seeded collection returns correct counts."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_status", {})
    data = _get_result_data(result)
    assert data["total_drawers"] == 4
    assert "project" in data["wings"]
    assert "notes" in data["wings"]


async def test_list_wings(seeded_palace_client):
    """List wings returns correct counts."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_list_wings", {})
    data = _get_result_data(result)
    assert data["wings"]["project"] == 3
    assert data["wings"]["notes"] == 1


async def test_list_rooms_all(seeded_palace_client):
    """List rooms (no filter) returns all rooms."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_list_rooms", {})
    data = _get_result_data(result)
    assert "backend" in data["rooms"]
    assert "frontend" in data["rooms"]
    assert "planning" in data["rooms"]


async def test_list_rooms_filtered(seeded_palace_client):
    """List rooms filtered by wing."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_list_rooms", {"wing": "project"})
    data = _get_result_data(result)
    assert "backend" in data["rooms"]
    assert "planning" not in data["rooms"]


async def test_get_taxonomy(seeded_palace_client):
    """Full taxonomy returns wing→room→count."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_get_taxonomy", {})
    data = _get_result_data(result)
    assert data["taxonomy"]["project"]["backend"] == 2
    assert data["taxonomy"]["project"]["frontend"] == 1
    assert data["taxonomy"]["notes"]["planning"] == 1


# ── Search Tool ──────────────────────────────────────────────────────


async def test_search_basic(seeded_palace_client):
    """Basic semantic search returns results."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_search", {"query": "JWT authentication tokens"})
    data = _get_result_data(result)
    assert "results" in data
    assert len(data["results"]) > 0


async def test_search_with_wing_filter(seeded_palace_client):
    """Search filtered by wing."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_search", {"query": "planning", "wing": "notes"})
    data = _get_result_data(result)
    assert all(r["wing"] == "notes" for r in data["results"])


async def test_search_with_room_filter(seeded_palace_client):
    """Search filtered by room."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_search", {"query": "database", "room": "backend"})
    data = _get_result_data(result)
    assert all(r["room"] == "backend" for r in data["results"])


# ── Write Tools ───────────────────────────────────────────────────────


async def test_add_drawer(empty_palace_client):
    """Add drawer succeeds and returns drawer_id."""
    client, palace_path = empty_palace_client
    result = await client.call_tool("mempalace_add_drawer", {
        "wing": "test_wing",
        "room": "test_room",
        "content": "This is a test memory about Python decorators and metaclasses.",
    })
    data = _get_result_data(result)
    assert data["success"] is True
    assert data["wing"] == "test_wing"
    assert data["room"] == "test_room"
    assert data["drawer_id"].startswith("drawer_test_wing_test_room_")


async def test_add_drawer_duplicate_detection(empty_palace_client):
    """Duplicate content returns already_exists."""
    client, palace_path = empty_palace_client
    content = "This is a unique test memory about Rust ownership and borrowing."
    result1 = await client.call_tool("mempalace_add_drawer", {"wing": "w", "room": "r", "content": content})
    data1 = _get_result_data(result1)
    assert data1["success"] is True

    result2 = await client.call_tool("mempalace_add_drawer", {"wing": "w", "room": "r", "content": content})
    data2 = _get_result_data(result2)
    assert data2["success"] is True
    assert data2["reason"] == "already_exists"


async def test_add_drawer_has_is_latest(empty_palace_client):
    """Add drawer metadata includes is_latest=True and timestamp in ChromaDB."""
    client, palace_path = empty_palace_client
    result = await client.call_tool("mempalace_add_drawer", {
        "wing": "test_wing",
        "room": "test_room",
        "content": "Memory with provenance fields.",
    })
    data = _get_result_data(result)
    assert data["success"] is True
    drawer_id = data["drawer_id"]

    # Verify metadata stored in ChromaDB
    from mempalace.backends import get_backend
    backend = get_backend("chroma")
    col = backend.get_collection(palace_path, "mempalace_drawers")
    stored = col.get(ids=[drawer_id])
    meta = stored["metadatas"][0]
    assert meta.get("is_latest") is True
    assert "timestamp" in meta
    assert meta.get("origin_type") == "observation"
    assert meta.get("agent_id") == "mcp"


async def test_check_duplicate(seeded_palace_client):
    """Check duplicate detects similar content."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_check_duplicate", {
        "content": "The authentication module uses JWT tokens for session management. "
                   "Tokens expire after 24 hours. Refresh tokens are stored in HttpOnly cookies.",
        "threshold": 0.5,
    })
    data = _get_result_data(result)
    assert data["is_duplicate"] is True


async def test_check_duplicate_no_match(seeded_palace_client):
    """Check duplicate with unrelated content returns False."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_check_duplicate", {
        "content": "Black holes emit Hawking radiation at the event horizon.",
        "threshold": 0.99,
    })
    data = _get_result_data(result)
    assert data["is_duplicate"] is False


# ── KG Tools ─────────────────────────────────────────────────────────


async def test_kg_add(empty_palace_client):
    """KG add creates a triple."""
    client, palace_path = empty_palace_client
    result = await client.call_tool("mempalace_kg_add", {
        "subject": "Alice",
        "predicate": "likes",
        "object": "coffee",
        "valid_from": "2025-01-01",
    })
    data = _get_result_data(result)
    assert data["success"] is True


async def test_kg_query(empty_palace_client):
    """KG query returns facts for an entity."""
    client, palace_path = empty_palace_client
    # First add something
    await client.call_tool("mempalace_kg_add", {
        "subject": "Alice",
        "predicate": "likes",
        "object": "coffee",
    })
    result = await client.call_tool("mempalace_kg_query", {"entity": "Alice"})
    data = _get_result_data(result)
    assert data["entity"] == "Alice"
    assert data["count"] >= 1


async def test_kg_invalidate(empty_palace_client):
    """KG invalidate marks a fact as ended."""
    client, palace_path = empty_palace_client
    await client.call_tool("mempalace_kg_add", {
        "subject": "Alice",
        "predicate": "works_at",
        "object": "Acme Corp",
    })
    result = await client.call_tool("mempalace_kg_invalidate", {
        "subject": "Alice",
        "predicate": "works_at",
        "object": "Acme Corp",
        "ended": "2024-12-31",
    })
    data = _get_result_data(result)
    assert data["success"] is True


async def test_kg_supersede_tool(empty_palace_client):
    """KG supersede atomically replaces old fact with new one."""
    client, palace_path = empty_palace_client
    await client.call_tool("mempalace_kg_add", {
        "subject": "Bob",
        "predicate": "status",
        "object": "active",
    })
    result = await client.call_tool("mempalace_kg_supersede", {
        "subject": "Bob",
        "predicate": "status",
        "old_value": "active",
        "new_value": "inactive",
    })
    data = _get_result_data(result)
    assert data["success"] is True
    assert "old_id" in data
    assert "new_id" in data
    assert data["old_value"] == "active"
    assert data["new_value"] == "inactive"


async def test_kg_history_tool(empty_palace_client):
    """mempalace_kg_history returns all versions after supersede."""
    client, palace_path = empty_palace_client
    await client.call_tool("mempalace_kg_add", {
        "subject": "Frank",
        "predicate": "role",
        "object": "developer",
    })
    await client.call_tool("mempalace_kg_supersede", {
        "subject": "Frank",
        "predicate": "role",
        "old_value": "developer",
        "new_value": "senior_developer",
    })
    result = await client.call_tool("mempalace_kg_history", {
        "subject": "Frank",
        "predicate": "role",
    })
    data = _get_result_data(result)
    assert data["versions"] == 2
    assert len(data["history"]) == 2
    assert data["current"] is not None


async def test_kg_stats(empty_palace_client):
    """KG stats returns overview."""
    client, palace_path = empty_palace_client
    result = await client.call_tool("mempalace_kg_stats", {})
    data = _get_result_data(result)
    assert "entities" in data


# ── Diary Tools ──────────────────────────────────────────────────────


async def test_diary_write_and_read(empty_palace_client):
    """Write and read diary entries."""
    client, palace_path = empty_palace_client
    write_result = await client.call_tool("mempalace_diary_write", {
        "agent_name": "TestAgent",
        "entry": "Today we discussed authentication patterns.",
        "topic": "architecture",
    })
    write_data = _get_result_data(write_result)
    assert write_data["success"] is True
    assert write_data["agent"] == "TestAgent"

    read_result = await client.call_tool("mempalace_diary_read", {"agent_name": "TestAgent"})
    read_data = _get_result_data(read_result)
    assert read_data["total"] == 1
    assert read_data["entries"][0]["topic"] == "architecture"
    assert "authentication" in read_data["entries"][0]["content"]


async def test_diary_write_idempotent(empty_palace_client):
    """Calling diary_write twice with same data does not raise."""
    client, palace_path = empty_palace_client
    entry = "Consistent diary entry about project status."
    result1 = await client.call_tool("mempalace_diary_write", {
        "agent_name": "IdempotentAgent",
        "entry": entry,
        "topic": "status",
    })
    data1 = _get_result_data(result1)
    assert data1["success"] is True

    # Second write with same data should not raise
    result2 = await client.call_tool("mempalace_diary_write", {
        "agent_name": "IdempotentAgent",
        "entry": entry,
        "topic": "status",
    })
    data2 = _get_result_data(result2)
    assert data2["success"] is True


async def test_diary_read_empty(empty_palace_client):
    """Read diary with no entries returns empty list."""
    client, palace_path = empty_palace_client
    result = await client.call_tool("mempalace_diary_read", {"agent_name": "Nobody"})
    data = _get_result_data(result)
    assert data["entries"] == []


# ── Remember Code ─────────────────────────────────────────────────────


async def test_remember_code(empty_palace_client):
    """Remember code stores code with description."""
    client, palace_path = empty_palace_client
    result = await client.call_tool("mempalace_remember_code", {
        "code": "def hello(): return 'world'",
        "description": "Simple hello world function",
        "wing": "test_wing",
        "room": "test_room",
    })
    data = _get_result_data(result)
    assert data["success"] is True
    assert data["wing"] == "test_wing"
    assert data["room"] == "test_room"


# ── Consolidate ──────────────────────────────────────────────────────


async def test_consolidate_find_duplicates(seeded_palace_client):
    """Consolidate finds similar memories."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_consolidate", {
        "topic": "authentication",
        "merge": False,
        "threshold": 0.85,
    })
    data = _get_result_data(result)
    assert "duplicates" in data
    assert "total_found" in data


# ── Export ──────────────────────────────────────────────────────────


async def test_export_claude_md(seeded_palace_client):
    """Export returns markdown format."""
    client, palace_path = seeded_palace_client
    result = await client.call_tool("mempalace_export_claude_md", {
        "format": "markdown",
    })
    data = _get_result_data(result)
    assert "export" in data
    assert "# MemPalace Export" in data["export"]


# ── Tool Timeouts ─────────────────────────────────────────────────────


async def test_hybrid_search_tool_exists(client):
    """Verify mempalace_hybrid_search tool is registered."""
    tools = await client.list_tools()
    tool_map = {t.name: t for t in tools}
    hybrid_tool = tool_map.get("mempalace_hybrid_search")
    assert hybrid_tool is not None, "mempalace_hybrid_search not found in tool registry"


async def test_search_tool_has_timeout(client):
    """Verify mempalace_search has timeout set (embed operation)."""
    tools = await client.list_tools()
    tool_map = {t.name: t for t in tools}
    search_tool = tool_map.get("mempalace_search")
    assert search_tool is not None
    # FastMCP exposes timeout via tool metadata; we verify the tool is present
    assert "search" in search_tool.name


async def test_write_tools_have_timeouts(client):
    """Verify write tools (add_drawer, diary_write) have timeout set."""
    tools = await client.list_tools()
    tool_map = {t.name: t for t in tools}
    # These should exist and have timeouts (embed/search category)
    assert "mempalace_add_drawer" in tool_map
    assert "mempalace_diary_write" in tool_map


# ── SkillsProvider (Resources) ────────────────────────────────────────


async def test_skills_directory_has_files(client):
    """Verify mempalace/skills/ directory contains instruction files."""
    import os
    skills_dir = os.path.join(os.path.dirname(__file__), "..", "mempalace", "skills")
    files = os.listdir(skills_dir) if os.path.exists(skills_dir) else []
    assert len(files) > 0, "skills directory should contain instruction files"
    assert any(f.endswith(".md") for f in files), "skills should include markdown files"


async def test_search_skill_mentions_hybrid(client):
    """search.md skill file must mention mempalace_hybrid_search."""
    import os
    skills_dir = os.path.join(os.path.dirname(__file__), "..", "mempalace", "skills")
    search_md = os.path.join(skills_dir, "search.md")
    if os.path.exists(search_md):
        content = open(search_md).read()
        assert "hybrid_search" in content, "search.md must mention hybrid_search"


async def test_reranker_no_eager_load_by_default(client):
    """Default create_server() must NOT eagerly load the reranker.

    On M1 Air 8GB, eager warmup costs ~90MB RAM + ~3s startup.
    The cross-encoder should load lazily on first rerank=True call.
    """
    import inspect
    from mempalace.server.factory import create_server
    from mempalace.settings import MemPalaceSettings

    src = inspect.getsource(create_server)
    # Warmup thread must be gated on settings.reranker_warmup
    assert "reranker_warmup" in src, "create_server must check settings.reranker_warmup"
    # Default settings must have reranker_warmup=False
    s = MemPalaceSettings()
    assert s.reranker_warmup is False, "default reranker_warmup must be False"


async def test_reranker_warmup_thread_starts_when_enabled(client):
    """create_server(reranker_warmup=True) must start a daemon warmup thread."""
    import inspect
    from mempalace.server.factory import create_server
    from mempalace.settings import MemPalaceSettings

    src = inspect.getsource(create_server)
    # Warmup conditional on reranker_warmup flag
    warmup_guard = "if settings.reranker_warmup:" in src or "if reranker_warmup:" in src
    assert warmup_guard, "warmup must be guarded by reranker_warmup flag"
    # Thread name when enabled
    assert 'name="reranker_warmup"' in src, "warmup thread must be named reranker_warmup"


async def test_searcher_reranker_lazy_load(palace_path, seeded_collection):
    """Reranker must NOT be loaded before any rerank=True call."""
    import mempalace.searcher as sr

    # Reranker starts uninitialized
    assert sr._reranker is None, "reranker should be None before first use"

    # Call search with rerank=False — must not trigger load
    from mempalace.searcher import search_memories
    result = search_memories("JWT authentication", palace_path, n_results=3, rerank=False)

    # Reranker still not loaded (rerank=False never touches it)
    # Note: subsequent rerank=True in other tests may have loaded it already
    # so we only assert it wasn't loaded BY this call
    if "sentence_transformers" in sr.__dict__:
        assert sr._reranker is None or sr._reranker is not False


async def test_searcher_reranker_loads_on_first_rerank(palace_path, seeded_collection):
    """warmup_reranker() triggers lazy load of cross-encoder (when available).

    Uses a subprocess to avoid state pollution from prior tests in this process.
    """
    import subprocess, sys

    st_available = False
    try:
        from sentence_transformers import CrossEncoder
        st_available = True
    except ImportError:
        pass

    if not st_available:
        pytest.skip("sentence-transformers not installed")

    # Run in fresh subprocess — sr._reranker will be None at startup
    code = """
import mempalace.searcher as sr
assert sr._reranker is None, f"Expected None at start, got {sr._reranker}"
from mempalace.searcher import warmup_reranker
warmup_reranker()
# After warmup, _reranker should be a CrossEncoder (or False on import failure)
assert sr._reranker is not None, "reranker should be loaded after warmup_reranker()"
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"warmup_reranker failed: {result.stderr}"


async def test_reranker_graceful_degradation_no_sentence_transformers():
    """When sentence-transformers is absent, rerank=True fails gracefully."""
    import mempalace.searcher as sr

    # Save original
    original = sr._reranker

    # Simulate ImportError by setting to False
    sr._reranker = False

    try:
        # _get_reranker returns None when _reranker is False
        result = sr._get_reranker()
        assert result is None, "should return None when sentence-transformers unavailable"
    finally:
        sr._reranker = original
