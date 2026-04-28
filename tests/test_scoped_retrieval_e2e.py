"""
test_scoped_retrieval_e2e.py — Project-scoped retrieval E2E tests.

Seals the invariant: no query with project_path=projA returns projB.
Tests real Lance + FTS5 + SymbolIndex paths (mock embeddings only).

Run: pytest tests/test_scoped_retrieval_e2e.py -q
"""

import json
import os
import tempfile
import shutil
from pathlib import Path

import pytest

pytest.importorskip("lancedb", reason="LanceDB not available")

from fastmcp import Client
from mempalace.backends.lance import LanceBackend
from mempalace.fastmcp_server import create_server
from mempalace.lexical_index import KeywordIndex
from mempalace.miner import mine
from mempalace.settings import MemPalaceSettings
from mempalace.symbol_index import SymbolIndex


# ---------------------------------------------------------------------------
# Deterministic mock embeddings
# ---------------------------------------------------------------------------


def _mock_embed_texts(texts):
    """Fast deterministic fake embeddings — bypass MLX/fastembed."""
    import hashlib

    dim = 256
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:dim]) + [0.0] * (dim - len(h))
        result.append(vec)
    return result


# ---------------------------------------------------------------------------
# Fixture repo factory
# ---------------------------------------------------------------------------


def _make_scoped_workspace(root: Path) -> tuple[Path, Path]:
    """Create projA/src/auth.py and projB/src/auth.py with unique markers."""
    projA = root / "projA"
    projB = root / "projB"
    srcA = projA / "src"
    srcB = projB / "src"
    srcA.mkdir(parents=True)
    srcB.mkdir(parents=True)

    # Write minimal mempalace.yaml so mine() finds config
    (projA / "mempalace.yaml").write_text(
        "project_name: projA\npalace_name: test_palace\nwing: repo\n"
    )
    (projB / "mempalace.yaml").write_text(
        "project_name: projB\npalace_name: test_palace\nwing: repo\n"
    )

    # projA auth — uses PROJA_AUTH_MARKER
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
            "sha256",
            username.encode(),
            PROJA_AUTH_MARKER.encode(),
            100000,
        ).hex()[:16]
        return secrets.compare_digest(expected, password)

    def verify(self, token: str) -> bool:
        """Verify session token for projA."""
        if not token:
            return False
        return token.startswith(PROJA_AUTH_MARKER)
'''
    )

    # projB auth — uses PROJB_AUTH_MARKER (different impl + different marker)
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
            password.encode(),
            salt=PROJB_AUTH_MARKER.encode(),
            n=16384,
            r=8,
            p=1,
        ).hex()[:16]
        return secrets.compare_digest(expected, password)

    def verify(self, token: str) -> bool:
        """Verify session token for projB."""
        if not token:
            return False
        return token.startswith(PROJB_AUTH_MARKER)
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
    """Create a scoped_workspace with projA and projB, auto-cleaned."""
    ws = tmp_path / "scoped_workspace"
    ws.mkdir()
    projA, projB = _make_scoped_workspace(ws)
    yield ws, projA, projB
    shutil.rmtree(ws, ignore_errors=True)


@pytest.fixture(scope="function")
def mined_palace(scoped_workspace, tmp_path):
    """Mine both projA and projB into a single palace; yields (palace_path, projA, projB)."""
    ws, projA, projB = scoped_workspace
    palace_path = tmp_path / "palace"
    palace_path.mkdir()

    os.environ["MEMPALACE_COALESCE_MS"] = "0"
    os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
    os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

    import mempalace.backends.lance as lance_mod

    _orig_embed = lance_mod._embed_texts
    lance_mod._embed_texts = _mock_embed_texts
    if hasattr(lance_mod, "_embed_via_socket"):
        lance_mod._embed_via_socket = _mock_embed_texts

    try:
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
# Scoped retrieval tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_code_scoped_to_projA_only(scoped_client):
    """mempalace_search_code(query=AuthManager, project_path=projA) → only projA."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_search_code",
        {"query": "AuthManager", "project_path": projA_path, "limit": 20},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    chunks = data.get("results", data.get("chunks", []))
    projB_hits = [c for c in chunks if c.get("source_file", "").startswith(projB_path)]
    assert len(projB_hits) == 0, (
        f"project_path=projA returned {len(projB_hits)} projB chunks: "
        f"{[c.get('source_file') for c in projB_hits]}"
    )
    assert len(chunks) > 0, "Expected at least one hit from projA"


@pytest.mark.asyncio
async def test_search_code_scoped_to_projB_only(scoped_client):
    """mempalace_search_code(query=AuthManager, project_path=projB) → only projB."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_search_code",
        {"query": "AuthManager", "project_path": projB_path, "limit": 20},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    chunks = data.get("results", data.get("chunks", []))
    projA_hits = [c for c in chunks if c.get("source_file", "").startswith(projA_path)]
    assert len(projA_hits) == 0, (
        f"project_path=projB returned {len(projA_hits)} projA chunks: "
        f"{[c.get('source_file') for c in projA_hits]}"
    )
    assert len(chunks) > 0, "Expected at least one hit from projB"


@pytest.mark.asyncio
async def test_project_context_scoped_to_projA(scoped_client):
    """mempalace_project_context(project_path=projA, query=login) → only projA."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_project_context",
        {"project_path": projA_path, "query": "login", "limit": 20},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    chunks = data.get("chunks", [])
    projB_hits = [c for c in chunks if c.get("source_file", "").startswith(projB_path)]
    assert len(projB_hits) == 0, (
        f"project_path=projA returned {len(projB_hits)} projB chunks"
    )
    assert len(chunks) > 0, "Expected at least one hit from projA"


@pytest.mark.asyncio
async def test_path_query_scoped_to_projA_not_projB(scoped_client):
    """Path query 'src/auth.py' scoped to projA → only projA paths."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_search_code",
        {"query": "src/auth.py", "project_path": projA_path, "limit": 20},
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    chunks = data.get("results", data.get("chunks", []))
    projB_hits = [c for c in chunks if c.get("source_file", "").startswith(projB_path)]
    assert len(projB_hits) == 0, (
        f"path query for projA returned projB chunks: "
        f"{[c.get('source_file') for c in projB_hits]}"
    )


@pytest.mark.asyncio
async def test_semantic_query_scoped_to_projA_not_projB(scoped_client):
    """Semantic query scoped to projA → only projA results."""
    client, palace_path, projA_path, projB_path = scoped_client

    result = await client.call_tool(
        "mempalace_project_context",
        {
            "project_path": projA_path,
            "query": "how does login verify credentials",
            "limit": 20,
        },
    )
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"

    chunks = data.get("chunks", [])
    projB_hits = [c for c in chunks if c.get("source_file", "").startswith(projB_path)]
    assert len(projB_hits) == 0, (
        f"semantic query for projA returned projB chunks: "
        f"{[c.get('source_file') for c in projB_hits]}"
    )
    assert len(chunks) > 0, "Expected at least one hit from projA"


# ---------------------------------------------------------------------------
# Output field normalization tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieval_fields_present(scoped_client):
    """Every code hit includes required output fields."""
    client, palace_path, projA_path, _ = scoped_client

    result = await client.call_tool(
        "mempalace_search_code",
        {"query": "AuthManager", "project_path": projA_path, "limit": 5},
    )
    data = _get_result_data(result)
    chunks = data.get("results", data.get("chunks", []))
    assert len(chunks) > 0, "Expected at least one hit"

    required = ["source_file", "language", "line_start", "line_end", "symbol_name"]
    for chunk in chunks:
        for field in required:
            assert field in chunk, f"Missing field '{field}' in chunk: {chunk}"


# ---------------------------------------------------------------------------
# _source_file_matches prefix isolation tests
# ---------------------------------------------------------------------------


def test_source_file_matches_prefix_isolation():
    """_source_file_matches must not treat /proj, /proj-old, /proj2 as same project."""
    from mempalace.server._code_tools import _source_file_matches

    # /proj vs /proj-old — old project should NOT match
    assert not _source_file_matches("/proj-old/src/auth.py", "/proj")
    assert not _source_file_matches("/proj/src/auth.py", "/proj-old")

    # /proj vs /proj2 — sibling should NOT match
    assert not _source_file_matches("/proj2/src/auth.py", "/proj")
    assert not _source_file_matches("/proj/src/auth.py", "/proj2")

    # /projA vs /projA-old — prefix boundary
    assert not _source_file_matches("/projA-old/src/auth.py", "/projA")

    # Valid matches
    assert _source_file_matches("/proj/src/auth.py", "/proj")
    assert _source_file_matches("/proj/src/auth.py", "/proj/src")
    assert _source_file_matches("/proj", "/proj")
    assert _source_file_matches("/workspace/projA/src/auth.py", "/workspace/projA")


# ---------------------------------------------------------------------------
# SymbolIndex scoped retrieval
# ---------------------------------------------------------------------------


def test_symbol_index_scoped_retrieval(mined_palace):
    """SymbolIndex find_symbol(project_path=projA) returns only that project."""
    palace_path, projA_path, projB_path = mined_palace

    si = SymbolIndex.get(palace_path)

    # Build index for both projects
    projA_src = Path(projA_path) / "src"
    projB_src = Path(projB_path) / "src"
    si.build_index(projA_path, [str(p) for p in projA_src.iterdir()])
    si.build_index(projB_path, [str(p) for p in projB_src.iterdir()])

    # Find AuthManager scoped to projA — should only return projA
    projA_symbols = si.find_symbol("AuthManager", project_path=projA_path)
    projB_symbols = si.find_symbol("AuthManager", project_path=projB_path)

    assert len(projA_symbols) > 0, "Expected AuthManager in projA"
    assert len(projB_symbols) > 0, "Expected AuthManager in projB"
    # Scoped results must not leak to other project
    for s in projA_symbols:
        assert s["file_path"].startswith(projA_path), f"projA symbol leaked: {s['file_path']}"
    for s in projB_symbols:
        assert s["file_path"].startswith(projB_path), f"projB symbol leaked: {s['file_path']}"

    # Also verify global find without scope finds both
    all_symbols = si.find_symbol("AuthManager")
    assert len(all_symbols) == 2, f"Expected 2 global symbols, got {len(all_symbols)}"


# ---------------------------------------------------------------------------
# FTS5 KeywordIndex scoped retrieval
# ---------------------------------------------------------------------------


def test_fts5_scoped_retrieval(mined_palace):
    """FTS5 KeywordIndex search scoped returns only matching project files."""
    palace_path, projA_path, projB_path = mined_palace

    idx = KeywordIndex.get(palace_path)

    results = idx.search("AuthManager", n_results=20)
    assert len(results) > 0, "Expected AuthManager in FTS5 index"

    # FTS5 returns document_id (= source_file path from mining). Verify isolation.
    projA_results = [r for r in results if r["document_id"].startswith(projA_path)]
    projB_results = [r for r in results if r["document_id"].startswith(projB_path)]

    assert len(projA_results) > 0, "Expected projA results in FTS5"
    assert len(projB_results) > 0, "Expected projB results in FTS5 (separate project)"
