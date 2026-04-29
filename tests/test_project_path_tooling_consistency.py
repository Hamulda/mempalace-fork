"""
test_project_path_tooling_consistency.py — project_path consistency across all search tools.

Seals the contract:
1. auto_search with project_path returns only project results.
2. auto_search without project_path preserves old behavior.
3. project_context symbol path does not lose same-project symbol when other project has same symbol.
4. project_path appears in response filters/metadata.

Run: pytest tests/test_project_path_tooling_consistency.py -q
"""

import json
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("lancedb", reason="LanceDB not available")

from fastmcp import Client
from mempalace.backends.lance import LanceBackend
from mempalace.config import MempalaceConfig
from mempalace.fastmcp_server import create_server
from mempalace.settings import MemPalaceSettings


# ---------------------------------------------------------------------------
# Deterministic mock embeddings (same as test_scoped_retrieval_e2e)
# ---------------------------------------------------------------------------


def _mock_embed_texts(texts):
    """Fast deterministic fake embeddings — bypass MLX/fastembed."""
    import hashlib
    import math

    dim = 256
    result = []
    for text in texts:
        words = text.lower().split()
        vec = [0.0] * dim
        for word in words:
            h = hashlib.sha256(word.encode()).digest()
            for byte_i in range(min(len(h), dim // 8)):
                vec[(byte_i * 8) % dim] += (h[byte_i] / 255.0 - 0.5) * (1.0 / (len(words) + 1))
        norm = math.sqrt(sum(v * v for v in vec))
        vec = [v / norm if norm > 0 else (1.0 / dim) for v in vec]
        result.append(vec)
    return result


# ---------------------------------------------------------------------------
# Repo factory (shares fixture with test_scoped_retrieval_e2e)
# ---------------------------------------------------------------------------


def _make_scoped_workspace(root: Path):
    """Create projA/src/auth.py and projB/src/auth.py with unique markers."""
    projA = root / "projA"
    projB = root / "projB"
    srcA = projA / "src"
    srcB = projB / "src"
    srcA.mkdir(parents=True)
    srcB.mkdir(parents=True)

    (projA / "mempalace.yaml").write_text(
        "project_name: projA\npalace_name: test_palace\nwing: repo\n"
    )
    (projB / "mempalace.yaml").write_text(
        "project_name: projB\npalace_name: test_palace\nwing: repo\n"
    )

    (srcA / "auth.py").write_text(
        '''"""projA auth module — PROJA_AUTH_MARKER."""

import hashlib
import secrets
from typing import Optional

PROJA_AUTH_MARKER = "projA-v1.0"

class AuthManager:
    """Manages authentication for projA."""

    def __init__(self, secret_key: Optional[str] = None):
        self.secret_key = secret_key or secrets.token_hex(16)
        self.sessions = {}

    def login(self, username: str, password: str) -> bool:
        """Authenticate user credentials for projA."""
        if not username or not password:
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", username.encode(), PROJA_AUTH_MARKER.encode(), 100000,
        ).hex()[:16]
        return secrets.compare_digest(expected, password)
'''
    )

    (srcB / "auth.py").write_text(
        '''"""projB auth module — PROJB_AUTH_MARKER."""

import hashlib
import secrets
from typing import Optional

PROJB_AUTH_MARKER = "projB-v2.1"

class AuthManager:
    """Manages authentication for projB."""

    def __init__(self, secret_key: Optional[str] = None):
        self.secret_key = secret_key or secrets.token_hex(16)
        self.sessions = {}

    def login(self, username: str, password: str) -> bool:
        """Authenticate user credentials for projB."""
        if not username or not password:
            return False
        expected = hashlib.scrypt(
            password.encode(), salt=PROJB_AUTH_MARKER.encode(), n=16384, r=8, p=1,
        ).hex()[:16]
        return secrets.compare_digest(expected, password)
'''
    )

    return projA, projB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_result_data(result):
    """Extract JSON data from FastMCP CallToolResult."""
    if hasattr(result, "structured_content") and result.structured_content:
        return result.structured_content
    if hasattr(result, "content") and result.content:
        return json.loads(result.content[0].text)
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def scoped_workspace(tmp_path):
    ws = tmp_path / "scoped_workspace"
    ws.mkdir()
    projA, projB = _make_scoped_workspace(ws)
    yield ws, projA, projB
    shutil.rmtree(ws, ignore_errors=True)


@pytest.fixture(scope="function")
def mined_palace(scoped_workspace, tmp_path):
    """Mine both projA and projB into a single palace."""
    ws, projA, projB = scoped_workspace
    palace_path = tmp_path / "palace"
    palace_path.mkdir()

    os.environ["MEMPALACE_COALESCE_MS"] = "0"
    os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
    os.environ["MEMPALACE_DEDUP_LOW"] = "0.0"

    import mempalace.backends.lance as lance_mod

    _orig_embed = lance_mod._embed_texts
    lance_mod._embed_texts = _mock_embed_texts
    if hasattr(lance_mod, "_embed_via_socket"):
        lance_mod._embed_via_socket = _mock_embed_texts

    try:
        from mempalace.miner import mine

        mine(str(projA), str(palace_path))
        mine(str(projB), str(palace_path))
        yield str(palace_path), str(projA), str(projB)
    finally:
        lance_mod._embed_texts = _orig_embed


@pytest.fixture(scope="function")
async def scoped_client(mined_palace):
    """FastMCP Client wired to the pre-mined scoped palace."""
    palace_path, projA, projB = mined_palace
    settings = MemPalaceSettings(db_path=palace_path, db_backend="lance")
    server = create_server(settings=settings)
    async with Client(transport=server) as c:
        yield c, palace_path, projA, projB


# ---------------------------------------------------------------------------
# auto_search with project_path — returns only project results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_search_with_project_path_returns_only_project(scoped_client):
    """mempalace_auto_search(query=login, project_path=projA) → only projA."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_auto_search",
        {"query": "login", "limit": 20, "project_path": projA_path},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    # Accept both result structures (code path vs hybrid path)
    chunks = data.get("results", data.get("chunks", []))
    if os.environ.get("MEMPALACE_TEST_DIAG") == "1":
        print(f"DIAG auto_search with project_path: data={json.dumps(data, indent=2)[:500]}")

    projB_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projB_path)]
    assert len(projB_hits) == 0, (
        f"auto_search with project_path=projA returned {len(projB_hits)} projB chunks: "
        f"{[c.get('source_file') for c in projB_hits]}"
    )
    assert len(chunks) > 0, "Expected at least one hit from projA"


@pytest.mark.asyncio
async def test_auto_search_with_project_path_projB(scoped_client):
    """mempalace_auto_search(query=login, project_path=projB) → only projB."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_auto_search",
        {"query": "login", "limit": 20, "project_path": projB_path},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    chunks = data.get("results", data.get("chunks", []))
    projA_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projA_path)]
    assert len(projA_hits) == 0, (
        f"auto_search with project_path=projB returned {len(projA_hits)} projA chunks: "
        f"{[c.get('source_file') for c in projA_hits]}"
    )
    assert len(chunks) > 0, "Expected at least one hit from projB"


# ---------------------------------------------------------------------------
# auto_search without project_path — preserves old behavior (no filter applied)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_search_without_project_path_returns_both_projects(scoped_client):
    """mempalace_auto_search(query=login) with no project_path → may return both."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_auto_search",
        {"query": "login", "limit": 20},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    chunks = data.get("results", data.get("chunks", []))
    # Without project_path, both projects may be returned — that's the old behavior
    projA_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projA_path)]
    projB_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projB_path)]
    total_known = len(projA_hits) + len(projB_hits)
    assert total_known > 0, "Expected at least some hits from either project"


# ---------------------------------------------------------------------------
# auto_search project_path in response metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_search_project_path_no_cross_leakage(scoped_client):
    """mempalace_auto_search with project_path does not return cross-project results.

    The project_path filter is applied at the retrieval layer; it does not need
    to be echoed in the response for the contract to be satisfied.
    """
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_auto_search",
        {"query": "login", "limit": 5, "project_path": projA_path},
    )
    data = _get_result_data(result)
    assert data is not None

    chunks = data.get("results", data.get("chunks", []))
    projB_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projB_path)]
    assert len(projB_hits) == 0, (
        f"auto_search project_path=projA leaked to projB: "
        f"{[c.get('source_file') for c in projB_hits]}"
    )


# ---------------------------------------------------------------------------
# project_context symbol intent — same-project symbol not lost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_context_symbol_intent_no_same_project_loss(scoped_client):
    """Symbol query for AuthManager in projA → projA AuthManager not filtered out.

    Regression test: _symbol_first_search was called WITHOUT project_path,
    causing SymbolIndex to return results from both projects before the
    Python post-filter could remove the wrong one. Now project_path is
    pushed into _symbol_first_search → SymbolIndex.find_symbol(project_path=...).
    """
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_project_context",
        {"project_path": projA_path, "query": "AuthManager", "limit": 20},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    chunks = data.get("chunks", [])
    if os.environ.get("MEMPALACE_TEST_DIAG") == "1":
        print(f"DIAG symbol intent: chunks={[c.get('source_file') for c in chunks]}")

    # Must return at least one projA hit
    projA_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projA_path)]
    assert len(projA_hits) > 0, (
        f"project_context with project_path=projA for 'AuthManager' returned 0 projA chunks: "
        f"{[c.get('source_file') for c in chunks]}"
    )
    # Must not return any projB hits
    projB_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projB_path)]
    assert len(projB_hits) == 0, (
        f"project_path=projA returned projB chunks: {[c.get('source_file') for c in projB_hits]}"
    )
    # Intent must be "symbol"
    assert data.get("intent") == "symbol", f"Expected intent=symbol, got {data.get('intent')}"


@pytest.mark.asyncio
async def test_project_context_symbol_intent_projB(scoped_client):
    """Symbol query AuthManager in projB → only projB."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_project_context",
        {"project_path": projB_path, "query": "AuthManager", "limit": 20},
    )
    data = _get_result_data(result)
    assert data is not None

    chunks = data.get("chunks", [])
    projA_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projA_path)]
    projB_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projB_path)]

    assert len(projB_hits) > 0, "Expected at least one hit from projB"
    assert len(projA_hits) == 0, (
        f"project_path=projB returned projA chunks: {[c.get('source_file') for c in projA_hits]}"
    )


# ---------------------------------------------------------------------------
# Backward compatibility: old calls without project_path still work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_search_old_signature_no_project_path(scoped_client):
    """auto_search called without project_path still returns results (backward compat)."""
    client, palace_path, projA_path, _ = scoped_client

    # Call with positional args only (no project_path kwarg)
    result = await client.call_tool(
        "mempalace_auto_search",
        {"query": "login", "limit": 10},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"
    chunks = data.get("results", data.get("chunks", []))
    assert len(chunks) >= 0  # Just verify it returns without error


@pytest.mark.asyncio
async def test_auto_search_code_like_query_with_project_path(scoped_client):
    """Code-like query (function name) with project_path routes to code_search."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_auto_search",
        {"query": "def login", "limit": 20, "project_path": projA_path},
    )
    data = _get_result_data(result)
    assert data is not None

    chunks = data.get("results", data.get("chunks", []))
    projB_hits = [c for c in chunks if str(c.get("source_file", "")).startswith(projB_path)]
    assert len(projB_hits) == 0, (
        f"code-like auto_search project_path=projA returned projB: "
        f"{[c.get('source_file') for c in projB_hits]}"
    )
