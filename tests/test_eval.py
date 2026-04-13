"""
test_eval.py -- Tests for mempalace_eval tool.
"""

import json
import pytest
import pytest_asyncio
from fastmcp import Client

from mempalace.fastmcp_server import create_server
from mempalace.settings import MemPalaceSettings

pytestmark = pytest.mark.asyncio


def _get_result_data(result):
    """Extract JSON data from FastMCP CallToolResult."""
    if hasattr(result, 'structured_content') and result.structured_content:
        return result.structured_content
    if hasattr(result, 'content') and result.content:
        return json.loads(result.content[0].text)
    return None


class TestEvalReturnsSummary:
    async def test_eval_returns_summary(self, palace_path, seeded_collection):
        """mempalace_eval returns eval_summary and per_query."""
        from mempalace.fastmcp_server import create_server
        from mempalace.settings import MemPalaceSettings
        from fastmcp import Client

        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_eval",
                {"queries": ["JWT authentication", "database schema"], "expected_wing": None, "n_results": 3},
            )
            data = _get_result_data(result)
            assert "eval_summary" in data, "result should contain eval_summary"
            assert "per_query" in data, "result should contain per_query"
            assert len(data["per_query"]) == 2, "should have 2 per_query entries"


class TestEvalMax10Queries:
    async def test_eval_max_10_queries(self, palace_path, seeded_collection):
        """Passing 15 queries should result in queries_tested == 10."""
        from mempalace.fastmcp_server import create_server
        from mempalace.settings import MemPalaceSettings
        from fastmcp import Client

        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        queries_15 = [f"query {i}" for i in range(15)]
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_eval",
                {"queries": queries_15, "expected_wing": None, "n_results": 3},
            )
            data = _get_result_data(result)
            assert data["eval_summary"]["queries_tested"] == 10, "max 10 queries per eval call"


class TestEvalDiagnosisLow:
    async def test_eval_diagnosis_low(self, palace_path, collection):
        """Empty palace returns 'Low similarity' diagnosis."""
        from mempalace.fastmcp_server import create_server
        from mempalace.settings import MemPalaceSettings
        from fastmcp import Client

        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_eval",
                {"queries": ["xyzzy nonexistent"], "expected_wing": None, "n_results": 5},
            )
            data = _get_result_data(result)
            assert "diagnosis" in data
            assert "Low similarity" in data["diagnosis"]


class TestEvalWingPrecision:
    async def test_eval_wing_precision(self, palace_path, seeded_collection):
        """With expected_wing, result contains wing_precision."""
        from mempalace.fastmcp_server import create_server
        from mempalace.settings import MemPalaceSettings
        from fastmcp import Client

        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_eval",
                {"queries": ["project planning", "code architecture"], "expected_wing": "project", "n_results": 5},
            )
            data = _get_result_data(result)
            assert "eval_summary" in data
            assert "wing_precision" in data["eval_summary"], "wing_precision should be in eval_summary"


def _get_result_data(result):
    """Extract data dict from MCP result — handles both raw dict and wrapped responses."""
    if isinstance(result, dict):
        return result
    if hasattr(result, "data"):
        import json
        if isinstance(result.data, str):
            return json.loads(result.data)
        return result.data
    return result
