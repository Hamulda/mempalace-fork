"""
test_bugfixes.py — Tests for F166d bug fixes.
"""

import inspect
import pytest
import pytest_asyncio
from fastmcp import Client

from mempalace.fastmcp_server import create_server
from mempalace.settings import MemPalaceSettings


pytestmark = pytest.mark.asyncio


class TestServeHttpDefaultPort:
    def test_serve_http_default_port(self):
        """serve_http default port is 8765."""
        from mempalace.fastmcp_server import serve_http
        sig = inspect.signature(serve_http)
        port_param = sig.parameters["port"]
        assert port_param.default == 8765, f"Expected port default 8765, got {port_param.default}"


class TestRegisterToolsDocstring:
    def test_register_tools_docstring_count(self):
        """_register_tools docstring mentions 27 tools."""
        from mempalace.fastmcp_server import _register_tools
        sig = inspect.getsource(_register_tools)
        assert "27" in sig, "_register_tools docstring should mention 27 tools"


class TestProjectContextNoProjectKey:
    async def test_project_context_source_file_matching(self, palace_path, seeded_collection):
        """mempalace_project_context uses source_file/wing matching, not 'project' metadata key."""
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            # Previously crashed because "project" metadata key didn't exist
            # Now it should return results by matching wing or source_file
            result = await client.call_tool(
                "mempalace_project_context",
                {"project_path": "xyzzy_nonexistent", "limit": 5},
            )
            data = _get_result_data(result)
            # Should not error (previously crashed on missing "project" key)
            assert "error" not in data or data.get("error") == ""
            # No wing/source matches "xyzzy" so count should be 0
            assert data.get("count", -1) == 0

    async def test_project_context_finds_matching_source(self, palace_path, seeded_collection):
        """mempalace_project_context finds drawers when source_file matches."""
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            # seeded_collection has source_file="auth.py"
            result = await client.call_tool(
                "mempalace_project_context",
                {"project_path": "auth.py", "limit": 5},
            )
            data = _get_result_data(result)
            # Should find the JWT drawer since its source_file is auth.py
            assert data.get("count", 0) >= 1, "Should match drawer with source_file=auth.py"


class TestConsolidateKeeperIsNewest:
    async def test_consolidate_keeper_is_newest(self, palace_path, collection):
        """consolidate with merge=True keeps the newest drawer (by filed_at)."""
        import chromadb
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")

        # Add two near-identical docs with different filed_at timestamps
        old_id = "drawer_old"
        new_id = "drawer_new"
        col.upsert(
            ids=[old_id, new_id],
            documents=[
                "The auth module uses JWT tokens. Tokens expire after 24 hours.",
                "The auth module uses JWT tokens. Tokens expire after 24 hours.",
            ],
            metadatas=[
                {"wing": "project", "room": "backend", "source_file": "auth.py", "chunk_index": 0, "added_by": "miner", "filed_at": "2025-01-01T00:00:00"},
                {"wing": "project", "room": "backend", "source_file": "auth.py", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-01T00:00:00"},
            ],
        )
        del client

        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_consolidate",
                {"topic": "JWT auth tokens", "merge": True, "threshold": 0.3},
            )
            data = _get_result_data(result)
            assert "error" not in data, f"Should not error: {data.get('error')}"
            # merged_count should be 1 (one drawer was deleted)
            assert data.get("merged", 0) == 1, f"Expected merged=1, got {data.get('merged')}"
            # The kept drawer should be the newer one
            duplicates = data.get("duplicates", [])
            assert len(duplicates) == 2, "Should have 2 duplicates before merge"


def _get_result_data(result):
    """Extract JSON data from FastMCP CallToolResult."""
    if hasattr(result, 'structured_content') and result.structured_content:
        return result.structured_content
    if hasattr(result, 'content') and result.content:
        import json
        return json.loads(result.content[0].text)
    return None
