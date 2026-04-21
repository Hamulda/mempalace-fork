"""
conftest.py — Shared fixtures for MemPalace tests.

Provides isolated palace and knowledge graph instances so tests never
touch the user's real data or leak temp files on failure.

HOME is redirected to a temp directory at module load time — before any
mempalace imports — so that module-level initialisations write to a
throwaway location instead of the real user profile.
"""

import os
import shutil
import tempfile
import threading
import unittest.mock

# ── Isolate HOME before any mempalace imports ──────────────────────────
_original_env = {}
_session_tmp = tempfile.mkdtemp(prefix="mempalace_session_")

for _var in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
    _original_env[_var] = os.environ.get(_var)

os.environ["HOME"] = _session_tmp
os.environ["USERPROFILE"] = _session_tmp
os.environ["HOMEDRIVE"] = os.path.splitdrive(_session_tmp)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_session_tmp)[1] or _session_tmp

# ── Deterministic mock embeddings ───────────────────────────────────────
# Fast, deterministic mock — bypasses MLX, fastembed, and MemoryGuard.
# Tests that intentionally test real embedding behavior should be marked @pytest.mark.slow.


def _mock_embed_texts(texts):
    """Deterministic fake embeddings — no MLX, no memory pressure, no daemon."""
    import hashlib
    dim = 256
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:dim]) + [0.0] * (dim - len(h))
        result.append(vec)
    return result


# Now it is safe to import mempalace modules that trigger initialisation.
import chromadb  # noqa: E402
import pytest  # noqa: E402

from mempalace.config import MempalaceConfig  # noqa: E402
from mempalace.knowledge_graph import KnowledgeGraph  # noqa: E402


# ── Deterministic mock embeddings ───────────────────────────────────────
# Fast, deterministic mock — bypasses MLX, fastembed, and MemoryGuard.
# Tests that intentionally test real embedding behavior should be marked @pytest.mark.slow.


def _mock_embed_texts(texts):
    """Deterministic fake embeddings — no MLX, no memory pressure, no daemon."""
    import hashlib
    dim = 256
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:dim]) + [0.0] * (dim - len(h))
        result.append(vec)
    return result


@pytest.fixture(scope="session", autouse=True)
def _mock_embed_for_all_tests():
    """Patch _embed_texts for the entire test session — no slow MLX/daemon needed."""
    import sys

    # Patch any already-loaded lance modules
    for mod_name in list(sys.modules.keys()):
        if "mempalace.backends.lance" in mod_name:
            mod = sys.modules[mod_name]
            if hasattr(mod, "_embed_texts"):
                mod._embed_texts = _mock_embed_texts

    # Also patch the module-level reference so new imports get the mock
    try:
        import mempalace.backends.lance as lance_mod
        lance_mod._embed_texts = _mock_embed_texts
        # Also patch the socket-based variant and fallback
        if hasattr(lance_mod, "_embed_via_socket"):
            lance_mod._embed_via_socket = lambda *args, **kwargs: _mock_embed_texts(*args, **kwargs)
        if hasattr(lance_mod, "_embed_texts_fallback"):
            lance_mod._embed_texts_fallback = _mock_embed_texts
    except ImportError:
        pass  # lance not available in this environment

    # Disable MEMPALACE_COALESCE_MS to avoid 500ms window delays in tests
    os.environ.setdefault("MEMPALACE_COALESCE_MS", "0")

    yield


@pytest.fixture(scope="session", autouse=True)
def _isolate_home():
    """Ensure HOME points to a temp dir for the entire test session.

    The env vars were already set at module level (above) so that
    module-level initialisations are captured.  This fixture simply
    restores the originals on teardown and cleans up the temp dir.
    """
    yield
    for var, orig in _original_env.items():
        if orig is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = orig
    shutil.rmtree(_session_tmp, ignore_errors=True)


@pytest.fixture(scope="function", autouse=True)
def _cleanup_resources():
    """
    Clean up singleton resources after each test function.

    Runs after every test to prevent cross-test contamination:
    - MemoryGuard daemon thread stopped and singleton reset
    - SymbolIndex instances closed and cleared
    - QueryCache cleared
    - ChromaDB client reset
    """
    yield
    # Stop MemoryGuard daemon thread and reset singleton
    try:
        from mempalace.memory_guard import MemoryGuard
        MemoryGuard.get().stop()
    except Exception:
        pass
    # Reset class-level singleton state
    try:
        from mempalace.memory_guard import MemoryGuard
        MemoryGuard._instance = None
        MemoryGuard._started.clear()
        # Fresh _stop event for next test's get()
        MemoryGuard._stop = threading.Event()
    except Exception:
        pass
    # Close all SymbolIndex instances
    try:
        from mempalace.symbol_index import SymbolIndex
        with SymbolIndex._instances_lock:
            for idx in list(SymbolIndex._instances.values()):
                idx._close()
            SymbolIndex._instances.clear()
    except Exception:
        pass
    # Clear query cache
    try:
        from mempalace.query_cache import get_query_cache
        cache = get_query_cache()
        cache.clear()
    except Exception:
        pass
    # Reset ChromaDB client singleton
    try:
        import chromadb
        chromadb.reset_client()
    except Exception:
        pass


@pytest.fixture
def tmp_dir():
    """Create and auto-cleanup a temporary directory."""
    d = tempfile.mkdtemp(prefix="mempalace_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def palace_path(tmp_dir):
    """Path to an empty palace directory inside tmp_dir."""
    p = os.path.join(tmp_dir, "palace")
    os.makedirs(p)
    return p


@pytest.fixture
def config(tmp_dir, palace_path):
    """A MempalaceConfig pointing at the temp palace."""
    cfg_dir = os.path.join(tmp_dir, "config")
    os.makedirs(cfg_dir)
    import json

    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump({"palace_path": palace_path}, f)
    return MempalaceConfig(config_dir=cfg_dir)


@pytest.fixture
def collection_chroma(palace_path):
    """A ChromaDB collection pre-seeded in the temp palace.

    DEPRECATED: This fixture exists only for tests that still use ChromaDB.
    All new tests should use LanceDB via the canonical write/read paths.
    """
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers")
    yield col
    try:
        client.delete_collection("mempalace_drawers")
    except Exception:
        pass
    try:
        client.close()
    except Exception:
        pass
    del client


# Alias for backward compatibility with existing tests
collection = collection_chroma


@pytest.fixture
def seeded_collection(collection):
    """Collection with a handful of representative drawers."""
    collection.add(
        ids=[
            "drawer_proj_backend_aaa",
            "drawer_proj_backend_bbb",
            "drawer_proj_frontend_ccc",
            "drawer_notes_planning_ddd",
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
            {
                "wing": "project",
                "room": "backend",
                "source_file": "auth.py",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-01T00:00:00",
            },
            {
                "wing": "project",
                "room": "backend",
                "source_file": "db.py",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-02T00:00:00",
            },
            {
                "wing": "project",
                "room": "frontend",
                "source_file": "App.tsx",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-03T00:00:00",
            },
            {
                "wing": "notes",
                "room": "planning",
                "source_file": "sprint.md",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-04T00:00:00",
            },
        ],
    )
    return collection


@pytest.fixture
def kg(tmp_dir):
    """An isolated KnowledgeGraph using a temp SQLite file."""
    db_path = os.path.join(tmp_dir, "test_kg.sqlite3")
    return KnowledgeGraph(db_path=db_path)


@pytest.fixture
def seeded_kg(kg):
    """KnowledgeGraph pre-loaded with sample triples."""
    kg.add_entity("Alice", entity_type="person")
    kg.add_entity("Max", entity_type="person")
    kg.add_entity("swimming", entity_type="activity")
    kg.add_entity("chess", entity_type="activity")

    kg.add_triple("Alice", "parent_of", "Max", valid_from="2015-04-01")
    kg.add_triple("Max", "does", "swimming", valid_from="2025-01-01")
    kg.add_triple("Max", "does", "chess", valid_from="2024-06-01")
    kg.add_triple("Alice", "works_at", "Acme Corp", valid_from="2020-01-01", valid_to="2024-12-31")
    kg.add_triple("Alice", "works_at", "NewCo", valid_from="2025-01-01")

    return kg
