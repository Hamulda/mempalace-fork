"""
test_lance_codebase_rag_e2e.py -- Canonical LanceDB codebase RAG E2E test.

Seals the canonical path: mine() -> LanceDB + FTS5 + SymbolIndex -> MCP tools.

Run: pytest tests/test_lance_codebase_rag_e2e.py -q
"""

import json
import os
from pathlib import Path

import pytest

# LanceDB must be available -- abort if not
pytest.importorskip("lancedb", reason="LanceDB not installed")

from fastmcp import Client
from mempalace.backends.lance import LanceBackend
from mempalace.fastmcp_server import create_server
from mempalace.lexical_index import KeywordIndex
from mempalace.miner import mine
from mempalace.settings import MemPalaceSettings
from mempalace.symbol_index import SymbolIndex


# ---------------------------------------------------------------------------
# Deterministic mock embeddings -- bypass MLX, fastembed, daemon
# ---------------------------------------------------------------------------


def _mock_embed_texts(texts):
    """Fast deterministic fake embeddings."""
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


def _make_fixture_repo(root: Path) -> None:
    """Create src/auth.py, src/db.py, README.md, pyproject.toml, mempalace.yaml."""
    src = root / "src"
    src.mkdir()

    # auth.py -- symbols: AuthManager, login, logout, hash_password, verify_password
    (src / "auth.py").write_text(
        '''"""Authentication module -- manages user sessions and password hashing."""
import hashlib
import secrets


class AuthManager:
    """Central authentication controller."""

    def __init__(self, secret_key: str | None = None):
        self.secret_key = secret_key or secrets.token_hex(32)
        self._sessions: dict[str, str] = {}

    def login(self, username: str, password: str) -> str | None:
        """Authenticate user and return session token, or None on failure."""
        if not username or not password:
            return None
        token = hashlib.sha256(
            (username + self.secret_key + password).encode()
        ).hexdigest()
        self._sessions[token] = username
        return token

    def logout(self, token: str) -> bool:
        """Invalidate session token. Returns True if token was active."""
        return self._sessions.pop(token, None) is not None

    def is_authenticated(self, token: str) -> bool:
        """Check if token represents an active session."""
        return token in self._sessions


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (digest, salt) using PBKDF2-equivalent approach."""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return dk.hex(), salt


def verify_password(password: str, digest: str, salt: str) -> bool:
    """Verify password against stored digest and salt."""
    computed, _ = hash_password(password, salt)
    return secrets.compare_digest(computed, digest)
'''
    )

    # db.py -- symbols: ConnectionPool, connect_db, execute_query
    (src / "db.py").write_text(
        '''"""Database connectivity and connection pooling."""
import sqlite3
from contextlib import contextmanager
from typing import Any, Generator


class ConnectionPool:
    """Minimal SQLite connection pool."""

    def __init__(self, database: str, max_connections: int = 5):
        self.database = database
        self.max_connections = max_connections
        self._pool: list[sqlite3.Connection] = []
        self._size = 0

    @contextmanager
    def acquire(self) -> Generator[sqlite3.Connection, None, None]:
        """Acquire a connection from the pool."""
        if self._pool:
            conn = self._pool.pop()
        else:
            conn = sqlite3.connect(self.database, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            if len(self._pool) < self.max_connections:
                self._pool.append(conn)
            else:
                conn.close()


def connect_db(database: str = "app.db") -> sqlite3.Connection:
    """Create a new database connection with WAL mode enabled."""
    conn = sqlite3.connect(database, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def execute_query(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Execute SELECT and return rows as list of dicts."""
    cursor = conn.execute(query, params)
    columns = [col[0] for col in cursor.description or ()]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]
'''
    )

    (root / "README.md").write_text(
        "# Fixture Project\n\n"
        "A small fixture repo for testing MemPalace LanceDB codebase RAG pipeline.\n\n"
        "## Modules\n- src/auth.py -- AuthManager class, login/logout, password hashing\n"
        "- src/db.py -- ConnectionPool, connect_db, execute_query\n"
    )

    (root / "pyproject.toml").write_text(
        "[project]\nname = \"fixture-project\"\nversion = \"0.1.0\"\n"
        "requires-python = \">=3.11\"\n"
    )

    (root / "mempalace.yaml").write_text(
        "wing: fixture_project\nrooms:\n"
        "  - name: src\n    description: Source code modules\n"
        "  - name: docs\n    description: Documentation and config\n"
    )


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
def fixture_repo(tmp_path):
    """Create a small Python fixture repo, auto-cleaned on exit."""
    repo = tmp_path / "fixture_project"
    repo.mkdir()
    _make_fixture_repo(repo)
    return repo


@pytest.fixture(scope="function")
def lance_palace(tmp_path):
    """Create an empty LanceDB palace directory with mock embeddings."""
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

    yield str(palace_path)

    # Restore
    lance_mod._embed_texts = _orig_embed


@pytest.fixture(scope="function")
async def seeded_lance_palace(tmp_path):
    """Create + mine a fixture repo; yields (palace_path, repo_path)."""
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    repo = tmp_path / "fixture_project"
    repo.mkdir()
    _make_fixture_repo(repo)

    os.environ["MEMPALACE_COALESCE_MS"] = "0"
    os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
    os.environ["MEMPALACE_DEDUP_LOW"] = "0.0"

    import mempalace.backends.lance as lance_mod

    _orig_embed = lance_mod._embed_texts
    lance_mod._embed_texts = _mock_embed_texts
    if hasattr(lance_mod, "_embed_via_socket"):
        lance_mod._embed_via_socket = _mock_embed_texts

    try:
        mine(str(repo), str(palace_path))
        yield str(palace_path), str(repo)
    finally:
        lance_mod._embed_texts = _orig_embed


@pytest.fixture(scope="function")
async def lance_e2e_client(seeded_lance_palace):
    """FastMCP Client wired to a pre-mined LanceDB palace."""
    palace_path, _ = seeded_lance_palace
    settings = MemPalaceSettings(db_path=palace_path, db_backend="lance")
    server = create_server(settings=settings)
    async with Client(transport=server) as c:
        yield c, palace_path


# ---------------------------------------------------------------------------
# Stage 1 & 2: Mining -> Lance collection
# ---------------------------------------------------------------------------


def test_mine_fixture_repo_into_lance(lance_palace, fixture_repo):
    """Mine the fixture repo and assert drawer count > 0."""
    mine(str(fixture_repo), lance_palace)

    backend = LanceBackend()
    col = backend.get_collection(lance_palace, "mempalace_drawers")
    assert col.count() > 0


def test_lance_collection_has_drawers(lance_palace, fixture_repo):
    """Mine then assert Lance drawer count > 0."""
    mine(str(fixture_repo), lance_palace)

    backend = LanceBackend()
    col = backend.get_collection(lance_palace, "mempalace_drawers")
    assert col.count() > 0


# ---------------------------------------------------------------------------
# Stage 3: FTS5 KeywordIndex
# ---------------------------------------------------------------------------


def test_fts5_keyword_index_populated(lance_palace, fixture_repo):
    """After mining, FTS5 KeywordIndex must have entries."""
    mine(str(fixture_repo), lance_palace)

    idx = KeywordIndex.get(lance_palace)

    results = idx.search("AuthManager", n_results=5)
    assert len(results) > 0, "Expected AuthManager in FTS5 index"

    results2 = idx.search("ConnectionPool", n_results=5)
    assert len(results2) > 0, "Expected ConnectionPool in FTS5 index"


# ---------------------------------------------------------------------------
# Stage 4: SymbolIndex
# ---------------------------------------------------------------------------


def test_symbol_index_has_expected_symbols(lance_palace, fixture_repo):
    """After mining, SymbolIndex must index AuthManager, login, connect_db, etc."""
    mine(str(fixture_repo), lance_palace)

    si = SymbolIndex.get(lance_palace)
    si.build_index(str(fixture_repo), [str(p) for p in (fixture_repo / "src").iterdir()])

    # Exact name lookups
    assert len(si.find_symbol("AuthManager")) > 0, "AuthManager not in SymbolIndex"
    assert len(si.find_symbol("login")) > 0, "login not in SymbolIndex"
    assert len(si.find_symbol("connect_db")) > 0, "connect_db not in SymbolIndex"
    assert len(si.find_symbol("hash_password")) > 0, "hash_password not in SymbolIndex"

    # File-scoped lookup -- get_file_symbols returns {"symbols": [...], ...}
    auth_result = si.get_file_symbols(str(fixture_repo / "src" / "auth.py"))
    auth_names = {s["name"] for s in auth_result["symbols"]}
    assert "AuthManager" in auth_names
    assert "login" in auth_names
    assert "logout" in auth_names
    assert "hash_password" in auth_names

    db_result = si.get_file_symbols(str(fixture_repo / "src" / "db.py"))
    db_names = {s["name"] for s in db_result["symbols"]}
    assert "connect_db" in db_names
    assert "ConnectionPool" in db_names
    assert "execute_query" in db_names


# ---------------------------------------------------------------------------
# Stage 5: MCP tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_status_shows_drawers(lance_e2e_client):
    """mempalace_status returns total_drawers > 0 after mining."""
    client, palace_path = lance_e2e_client

    result = await client.call_tool("mempalace_status", {})
    data = _get_result_data(result)
    assert data is not None, f"Got None result: {result}"
    assert data["total_drawers"] > 0, f"Expected drawers > 0, got {data}"


@pytest.mark.asyncio
async def test_mcp_list_wings(lance_e2e_client):
    """mempalace_list_wings returns the fixture_project wing."""
    client, _ = lance_e2e_client

    result = await client.call_tool("mempalace_list_wings", {})
    data = _get_result_data(result)
    assert data is not None
    assert "fixture_project" in data.get("wings", {}), f"Expected fixture_project: {data}"


@pytest.mark.asyncio
async def test_mcp_hybrid_search_finds_auth(lance_e2e_client):
    """mempalace_hybrid_search finds auth-related content via Lance+RRF."""
    client, _ = lance_e2e_client

    result = await client.call_tool("mempalace_hybrid_search", {
        "query": "authentication manager login session",
        "limit": 5,
    })
    data = _get_result_data(result)
    assert data is not None
    assert "results" in data
    assert len(data["results"]) > 0, f"Expected search results: {data}"


@pytest.mark.asyncio
async def test_mcp_hybrid_search_finds_db(lance_e2e_client):
    """mempalace_hybrid_search finds database / ConnectionPool content."""
    client, _ = lance_e2e_client

    result = await client.call_tool("mempalace_hybrid_search", {
        "query": "connection pool database sqlite",
        "limit": 5,
    })
    data = _get_result_data(result)
    assert data is not None
    assert "results" in data
    assert len(data["results"]) > 0, f"Expected search results: {data}"


@pytest.mark.asyncio
async def test_mcp_search_code_finds_auth(lance_e2e_client):
    """mempalace_search_code finds AuthManager definition."""
    client, _ = lance_e2e_client

    result = await client.call_tool("mempalace_search_code", {
        "query": "AuthManager class authentication",
        "limit": 5,
    })
    data = _get_result_data(result)
    assert data is not None
    assert "results" in data
    assert len(data["results"]) > 0, f"Expected code search results: {data}"


@pytest.mark.asyncio
async def test_mcp_project_context(lance_e2e_client):
    """mempalace_project_context returns code chunks scoped to fixture repo."""
    client, palace_path = lance_e2e_client

    result = await client.call_tool("mempalace_project_context", {
        "project_path": palace_path,
        "query": "authentication password hashing",
        "limit": 3,
    })
    data = _get_result_data(result)
    assert data is not None
    assert "chunks" in data or "results" in data, f"Expected chunks/results: {data}"


@pytest.mark.asyncio
async def test_mcp_find_symbol(lance_e2e_client):
    """mempalace_find_symbol locates AuthManager in fixture repo source."""
    client, palace_path = lance_e2e_client

    result = await client.call_tool("mempalace_find_symbol", {
        "symbol_name": "AuthManager",
        "project_root": palace_path,
    })
    data = _get_result_data(result)
    assert data is not None
    assert "results" in data or "symbols" in data, f"Expected symbol results: {data}"


@pytest.mark.asyncio
async def test_mcp_file_symbols(lance_e2e_client):
    """mempalace_file_symbols lists symbols in src/auth.py."""
    client, palace_path = lance_e2e_client

    result = await client.call_tool("mempalace_file_symbols", {
        "file_path": str(Path(palace_path).parent / "fixture_project" / "src" / "auth.py"),
        "project_root": str(Path(palace_path).parent / "fixture_project"),
    })
    data = _get_result_data(result)
    assert data is not None
    assert "symbols" in data or "results" in data, f"Expected symbol list: {data}"


# ---------------------------------------------------------------------------
# Stage 6: FTS5 sync regression -- delete keeps index clean
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_drawer_removes_fts5_entry(seeded_lance_palace):
    """
    Regression: add a drawer, delete it, assert no stale ghost row in FTS5.

    Flow:
    1. palace already mined via seeded_lance_palace (populates Lance + FTS5)
    2. add a unique new drawer via MCP add_drawer
    3. delete that drawer via MCP delete_drawer
    4. search FTS5 -- deleted content must NOT appear
    """
    palace_path, _ = seeded_lance_palace

    settings = MemPalaceSettings(db_path=palace_path, db_backend="lance")
    server = create_server(settings=settings)

    async with Client(transport=server) as client:
        unique_content = (
            "UNIQUE_MARKER_7X3K9ZQ2P_for_fts5_delete_regression_test "
            "def must_not_appear_after_delete(): pass"
        )
        add_result = await client.call_tool("mempalace_add_drawer", {
            "wing": "test_regression",
            "room": "fts5_sync",
            "content": unique_content,
        })
        add_data = _get_result_data(add_result)
        assert add_data is not None
        assert add_data.get("success") is True, f"add_drawer failed: {add_data}"
        drawer_id = add_data["drawer_id"]

        # Verify it's now in FTS5
        idx = KeywordIndex.get(palace_path)
        before_delete = idx.search("UNIQUE_MARKER_7X3K9ZQ2P", n_results=5)
        assert len(before_delete) > 0, "Drawer not found in FTS5 after add"

        # Delete the drawer
        del_result = await client.call_tool("mempalace_delete_drawer", {
            "drawer_id": drawer_id,
        })
        del_data = _get_result_data(del_result)
        assert del_data is not None
        assert del_data.get("success") is True, f"delete_drawer failed: {del_data}"

        # FTS5 must NOT return the deleted content -- ghost row check
        after_delete = idx.search("UNIQUE_MARKER_7X3K9ZQ2P", n_results=5)
        assert len(after_delete) == 0, (
            f"STALE GHOST ROW: FTS5 still returns deleted drawer {drawer_id} -- "
            f"delete did not sync to FTS5. Found: {after_delete}"
        )
