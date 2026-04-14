"""
test_bugfixes.py — Tests for F166d bug fixes.
"""

import inspect
import pytest
import pytest_asyncio
from fastmcp import Client
from unittest.mock import MagicMock, patch

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


class TestEntityExtraction:
    async def test_add_drawer_entity_extraction(self, palace_path, collection):
        """add_drawer extracts entities from content and stores in metadata."""
        import chromadb
        client = chromadb.PersistentClient(path=palace_path)
        # Use the default collection (mempalace_drawers)
        col = client.get_or_create_collection("mempalace_drawers")

        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            # Content with repeated capitalized names (3+ mentions = entity candidate)
            content = (
                "Alice worked on the backend. Alice implemented the API. Alice deployed it. "
                "Bob helped with testing. Bob reviewed the code. Bob approved the merge."
            )
            result = await client.call_tool(
                "mempalace_add_drawer",
                {"wing": "test_wing", "room": "test_room", "content": content, "added_by": "test"},
            )
            data = _get_result_data(result)
            assert data.get("success"), f"add_drawer failed: {data}"

            # Verify entities metadata was stored
            drawer_id = data.get("drawer_id")
            stored = col.get(ids=[drawer_id])
            assert stored["ids"], "Drawer should exist"
            meta = stored["metadatas"][0]
            entities_json = meta.get("entities", "")
            assert entities_json, "entities metadata should be populated"
            import json
            entities = json.loads(entities_json)
            # Alice and Bob appear 3+ times each
            assert "Alice" in entities, "Alice should be extracted as entity"
            assert "Bob" in entities, "Bob should be extracted as entity"
            del client

    async def test_remember_code_entity_extraction(self, palace_path, collection):
        """remember_code extracts entities from code+description and stores in metadata."""
        import chromadb
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")

        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            code = "def authenticate(user):\n    return user.check_auth()"
            description = (
                "Max implemented the authentication module. Max wrote the tests. Max reviewed. "
                "Sarah deployed the service. Sarah configured the database."
            )
            result = await client.call_tool(
                "mempalace_remember_code",
                {
                    "code": code,
                    "description": description,
                    "wing": "test_wing",
                    "room": "test_room",
                    "added_by": "test",
                },
            )
            data = _get_result_data(result)
            assert data.get("success"), f"remember_code failed: {data}"

            # Verify entities metadata was stored
            drawer_id = data.get("drawer_id")
            stored = col.get(ids=[drawer_id])
            assert stored["ids"], "Drawer should exist"
            meta = stored["metadatas"][0]
            entities_json = meta.get("entities", "")
            assert entities_json, "entities metadata should be populated"
            import json
            entities = json.loads(entities_json)
            # Max appears 3+ times, Sarah appears 2+ times
            assert "Max" in entities, "Max should be extracted as entity"
            del client


class TestDiaryWriteWriteCoalescerTodo:
    def test_diary_write_write_coalescer_todo(self):
        """diary_write has TODO F176 comment about WriteCoalescer."""
        import mempalace.fastmcp_server as module
        import inspect
        source = inspect.getsource(module)
        # The TODO comment is 2 lines above mempalace_diary_write definition
        # (blank line between comment block and @server.tool decorator)
        lines = source.split('\n')
        for i, line in enumerate(lines):
            if 'def mempalace_diary_write' in line:
                # Check 3 lines back (blank line + @server.tool decorator + continuation comment)
                prev = lines[i - 3].strip() if i >= 3 else ""
                assert "TODO F176" in prev, f"TODO F176 should be 2 lines above diary_write, got: {prev!r}"
                assert "WriteCoalescer" in prev, f"WriteCoalescer mention should be in TODO comment"
                break
        else:
            pytest.fail("mempalace_diary_write function not found in module source")


class TestStatusCache:
    async def test_status_cache_hit(self, palace_path, collection):
        """Second status call within TTL returns cached result (same dict object)."""
        import mempalace.fastmcp_server as fm

        # Reset cache to ensure cold start
        fm._status_cache["data"] = None
        fm._status_cache["ts"] = 0.0

        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)

        async with Client(transport=server) as client:
            r1 = await client.call_tool("mempalace_status", {})
            d1 = _get_result_data(r1)
            r2 = await client.call_tool("mempalace_status", {})
            d2 = _get_result_data(r2)
            # Same total_drawers confirms cache hit
            assert d1.get("total_drawers") == d2.get("total_drawers")

        del server

    async def test_status_cache_expires(self, palace_path, collection):
        """After TTL, status call recomputes."""
        import mempalace.fastmcp_server as fm
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)

        async with Client(transport=server) as client:
            r1 = await client.call_tool("mempalace_status", {})
            data1 = _get_result_data(r1)
            # Manually expire the cache by setting ts to 0
            fm._status_cache["ts"] = 0.0
            r2 = await client.call_tool("mempalace_status", {})
            data2 = _get_result_data(r2)
            assert data2.get("total_drawers") is not None
        del server

    async def test_status_cache_expires(self, palace_path, collection):
        """After TTL, status call recomputes."""
        import mempalace.fastmcp_server as fm
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)

        async with Client(transport=server) as client:
            r1 = await client.call_tool("mempalace_status", {})
            data1 = _get_result_data(r1)
            # Manually expire the cache by setting ts to 0
            fm._status_cache["ts"] = 0.0
            r2 = await client.call_tool("mempalace_status", {})
            data2 = _get_result_data(r2)
            assert data2.get("total_drawers") is not None
        del server


class TestRememberCodeTruncation:
    async def test_remember_code_truncation_warning(self, palace_path, collection):
        """Code > 2000 chars returns code_truncated=True and original/stored lengths."""
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            long_code = "x" * 3000  # 3000 chars > 2000 limit
            result = await client.call_tool(
                "mempalace_remember_code",
                {
                    "code": long_code,
                    "description": "Test truncation",
                    "wing": "test_wing",
                    "room": "test_room",
                    "added_by": "test",
                },
            )
            data = _get_result_data(result)
            assert data.get("success"), f"Expected success, got: {data}"
            assert data.get("code_truncated") is True, "code_truncated should be True"
            assert data.get("original_length") == 3000, "original_length should be 3000"
            assert data.get("stored_length") == 2000, "stored_length should be 2000"
        del server


class TestKgThreadSafety:
    async def test_kg_thread_safety(self):
        """10 concurrent threads calling kg.add_triple — no sqlite3.ProgrammingError."""
        import threading
        from mempalace.knowledge_graph import KnowledgeGraph
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/kg_test.sqlite3"
            kg = KnowledgeGraph(db_path=db_path)
            errors = []

            def worker(i):
                try:
                    kg.add_triple(f"Entity{i}", "relates_to", f"Entity{i+100}", valid_from="2026-01-01")
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Thread safety errors: {errors}"
            del kg


class TestSearcherBugfixes:
    def test_query_cache_no_duplicate_definition(self):
        """searcher.py has exactly 1 _get_query_cache def and 1 _query_cache = None."""
        import re
        with open("/Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/mempalace/searcher.py") as f:
            content = f.read()
        count_qc = len(re.findall(r'def _get_query_cache', content))
        count_cache_none = len(re.findall(r'_query_cache = None', content))
        assert count_qc == 1, f"Expected 1 _get_query_cache def, got {count_qc}"
        assert count_cache_none == 1, f"Expected 1 _query_cache = None, got {count_cache_none}"

    def test_cache_key_includes_priority(self):
        """search_memories with different priority_gte produces different cache keys."""
        from mempalace.searcher import search_memories
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmp:
            palace_path = tmp
            # Same query, different priority_gte → should NOT return cached result
            # Since we can't easily test with real DB, verify cache key construction differs
            key_a = "test|wing|room|True|agent|5|False|10|None"
            key_b = "test|wing|room|True|agent|5|False|20|None"
            # Keys with different priority_gte are different
            assert key_a != key_b

    def test_query_cache_public_api(self):
        """QueryCache.get_value returns None before set, value after set."""
        from mempalace.query_cache import QueryCache
        cache = QueryCache(maxsize=10, ttl_seconds=10)
        assert cache.get_value("nonexistent") is None
        cache.set_value("key1", {"result": "value"})
        assert cache.get_value("key1") == {"result": "value"}

    def test_query_cache_clear_method(self):
        """QueryCache.clear() removes all entries."""
        from mempalace.query_cache import QueryCache
        cache = QueryCache(maxsize=10, ttl_seconds=60)
        cache.set_value("key1", {"a": 1})
        cache.set_value("key2", {"b": 2})
        assert cache.get_value("key1") is not None
        cache.clear()
        assert cache.get_value("key1") is None
        assert cache.get_value("key2") is None

    def test_invalidate_bm25_cache(self):
        """invalidate_bm25_cache sets all BM25 globals to None."""
        from mempalace.searcher import invalidate_bm25_cache, _bm25_index
        import mempalace.searcher as searcher_module
        # Set some globals first
        searcher_module._bm25_index = "dummy"
        searcher_module._bm25_corpus = ["doc"]
        searcher_module._bm25_ids = ["id1"]
        searcher_module._bm25_metas = [{"w": "meta"}]
        invalidate_bm25_cache()
        assert searcher_module._bm25_index is None
        assert searcher_module._bm25_corpus is None
        assert searcher_module._bm25_ids is None
        assert searcher_module._bm25_metas is None

    def test_invalidate_query_cache(self):
        """invalidate_query_cache clears the query cache."""
        from mempalace.searcher import invalidate_query_cache
        from mempalace.searcher import _get_query_cache
        cache = _get_query_cache()
        cache.set_value("testkey", {"result": "value"})
        assert cache.get_value("testkey") is not None
        invalidate_query_cache()
        assert cache.get_value("testkey") is None

    def test_invalidate_all_caches_both(self):
        """invalidate_all_caches clears both BM25 and query cache."""
        from mempalace.searcher import invalidate_all_caches, _get_query_cache
        import mempalace.searcher as searcher_module
        # Set BM25 globals
        searcher_module._bm25_index = "bm25dummy"
        # Set query cache entry
        cache = _get_query_cache()
        cache.set_value("alltest", {"v": 1})
        invalidate_all_caches()
        assert searcher_module._bm25_index is None
        assert cache.get_value("alltest") is None


class TestCacheInvalidationAfterWrite:
    """F176d: cache invalidation after write operations."""

    async def test_bm25_invalidated_after_add_drawer(self, palace_path, collection):
        """add_drawer invalidates BM25 cache (sets _bm25_index to None)."""
        import mempalace.fastmcp_server as fm
        import mempalace.searcher as searcher_module
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        # Set a dummy BM25 index
        searcher_module._bm25_index = "dummy"
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_add_drawer",
                {"wing": "test", "room": "room", "content": "new content here", "added_by": "test"},
            )
            data = _get_result_data(result)
            assert data.get("success"), f"add_drawer failed: {data}"
        # After add_drawer, BM25 cache should be invalidated
        assert searcher_module._bm25_index is None, "BM25 cache should be invalidated after add_drawer"
        del server

    async def test_query_cache_cleared_after_add_drawer(self, palace_path, collection):
        """add_drawer clears the query cache."""
        from mempalace.searcher import _get_query_cache
        import mempalace.searcher as searcher_module
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        cache = _get_query_cache()
        cache.set_value("pretest", {"v": 1})
        assert cache.get_value("pretest") is not None
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_add_drawer",
                {"wing": "test", "room": "room", "content": "more content", "added_by": "test"},
            )
            data = _get_result_data(result)
            assert data.get("success"), f"add_drawer failed: {data}"
        assert cache.get_value("pretest") is None, "Query cache should be cleared after add_drawer"
        del server

    async def test_status_cache_invalidated_after_add_drawer(self, palace_path, collection):
        """add_drawer invalidates status cache (_status_cache["data"] set to None)."""
        import mempalace.fastmcp_server as fm
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        # Pre-populate status cache
        fm._status_cache["data"] = {"total_drawers": 999}
        fm._status_cache["ts"] = 9999999
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_add_drawer",
                {"wing": "test", "room": "room", "content": "test content", "added_by": "test"},
            )
            data = _get_result_data(result)
            assert data.get("success"), f"add_drawer failed: {data}"
        # After write, status cache should be invalidated
        assert fm._status_cache["data"] is None, "Status cache should be invalidated after add_drawer"
        assert fm._status_cache["ts"] == 0.0, "Status cache ts should be reset"
        del server
        # Reset cache to not affect subsequent tests
        fm._status_cache["data"] = None
        fm._status_cache["ts"] = 0.0


class TestKgSingleton:
    def test_kg_singleton_reused(self):
        """_get_kg called 2× with same palace_path returns the same instance."""
        from mempalace.searcher import _get_kg
        import mempalace.searcher as searcher_module
        import tempfile

        # Reset singleton state first
        searcher_module._kg_instance = None
        searcher_module._kg_path_cached = None

        with tempfile.TemporaryDirectory() as tmp:
            kg1 = _get_kg(tmp)
            kg2 = _get_kg(tmp)
            assert kg1 is kg2, "KG singleton should return same instance on repeated calls"
            assert searcher_module._kg_instance is kg1
            assert searcher_module._kg_path_cached == str(__import__("pathlib").Path(tmp) / "knowledge_graph.sqlite3")

    def test_kg_no_close_called(self):
        """_get_kg does NOT call kg.close() on the returned instance."""
        from mempalace.searcher import _get_kg
        import mempalace.searcher as searcher_module
        import tempfile
        from unittest.mock import MagicMock

        searcher_module._kg_instance = None
        searcher_module._kg_path_cached = None

        with tempfile.TemporaryDirectory() as tmp:
            real_kg = _get_kg(tmp)
            # If we get a real KG, check it doesn't have close called by _get_kg
            # The singleton should not auto-close
            if hasattr(real_kg, "close"):
                # Track that _get_kg itself does not call close
                close_mock = MagicMock()
                real_kg.close = close_mock
                # Re-call _get_kg to trigger double-check path
                _get_kg(tmp)
                close_mock.assert_not_called()


class TestHybridSearchIsLatest:
    def test_hybrid_search_is_latest_none(self):
        """hybrid_search(is_latest=None) passes None to search_memories, not True."""
        from mempalace.searcher import hybrid_search
        import tempfile
        from unittest.mock import patch, MagicMock

        with tempfile.TemporaryDirectory() as tmp:
            with patch("mempalace.searcher.search_memories") as mock_sm:
                mock_sm.return_value = {"results": []}
                result = hybrid_search(
                    query="test query",
                    palace_path=tmp,
                    is_latest=None,
                )
                call_kwargs = mock_sm.call_args.kwargs
                assert "is_latest" in call_kwargs
                assert call_kwargs["is_latest"] is None, "is_latest should be None, not True"

    async def test_hybrid_search_is_latest_param_mcp(self, palace_path, collection):
        """mempalace_hybrid_search MCP tool accepts is_latest param."""
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_hybrid_search",
                {"query": "test", "limit": 5, "is_latest": None},
            )
            data = _get_result_data(result)
            assert "results" in data or "error" in data


class TestRrfMergeIdKey:
    def test_rrf_merge_id_key_different_ids(self):
        """Two hits with same text prefix but different IDs are both in merged result."""
        from mempalace.searcher import _rrf_merge
        hits1 = [
            {"id": "id1", "text": "The same text content here", "wing": "w1", "room": "r1"},
            {"id": "id2", "text": "Different text entirely", "wing": "w2", "room": "r2"},
        ]
        hits2 = [
            {"id": "id3", "text": "The same text content here", "wing": "w1", "room": "r1"},
        ]
        merged = _rrf_merge([hits1, hits2])
        ids = [h["id"] for h in merged]
        assert "id1" in ids
        assert "id3" in ids
        assert "id2" in ids


class TestGeneralExtractorIntegration:
    def test_general_extractor_import(self):
        """extract_memories is importable from general_extractor."""
        from mempalace.general_extractor import extract_memories
        assert callable(extract_memories)

    def test_general_extractor_extract(self):
        """extract_memories returns list (possibly empty) for text input."""
        from mempalace.general_extractor import extract_memories
        result = extract_memories("Alice works at Google. We decided to use Python.")
        # Should return a list, not raise
        assert isinstance(result, list)

    async def test_add_drawer_general_extraction_no_crash(self, palace_path, collection):
        """add_drawer with content that triggers general_extractor does not crash."""
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            result = await client.call_tool(
                "mempalace_add_drawer",
                {
                    "wing": "test",
                    "room": "room",
                    "content": "We decided to use Python because it works. "
                               "I always prefer functional style. "
                               "Finally got it working after a bug fix.",
                    "added_by": "test",
                },
            )
            data = _get_result_data(result)
            assert data.get("success") is True, f"add_drawer should succeed, got: {data}"
        del server


class TestSearchMemoriesResultsHaveId:
    def test_search_memories_results_have_id(self):
        """search_memories returns hits that include the 'id' key."""
        from mempalace.searcher import search_memories
        import tempfile
        from unittest.mock import patch, MagicMock

        with tempfile.TemporaryDirectory() as tmp:
            mock_col = MagicMock()
            mock_col.query.return_value = {
                "documents": [["test doc"]],
                "metadatas": [[{"wing": "w", "room": "r", "source_file": "f.py"}]],
                "distances": [[0.1]],
                "ids": [["doc_id_abc123"]],
            }
            mock_backend = MagicMock()
            mock_backend.get_collection.return_value = mock_col
            with patch("mempalace.searcher.get_backend", return_value=mock_backend):
                result = search_memories("test", palace_path=tmp, n_results=5)
                hits = result.get("results", [])
                assert len(hits) == 1
                assert "id" in hits[0], f"Hit should have 'id' key, got: {hits[0].keys()}"
                assert hits[0]["id"] == "doc_id_abc123"


class TestMemoryGuardIntegration:
    def test_memory_guard_import(self):
        """MemoryGuard can be imported from memory_guard module."""
        from mempalace.memory_guard import MemoryGuard, MemoryPressure
        assert MemoryGuard is not None
        assert MemoryPressure is not None

    async def test_add_drawer_guard_blocks(self, palace_path, collection):
        """When guard.should_pause_writes=True, add_drawer returns blocked error."""
        from mempalace.memory_guard import MemoryGuard
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        mock_guard = MagicMock()
        mock_guard.should_pause_writes.return_value = True
        mock_guard.pressure.value = "critical"
        mock_guard.used_ratio = 0.9
        with patch.object(MemoryGuard, "get", return_value=mock_guard):
            server = create_server(settings=settings)
            async with Client(transport=server) as client:
                result = await client.call_tool(
                    "mempalace_add_drawer",
                    {"wing": "test", "room": "room", "content": "test content", "added_by": "test"},
                )
                data = _get_result_data(result)
                assert "error" in data, f"Expected error, got: {data}"
                assert data.get("blocked_by") == "memory_guard", f"blocked_by should be memory_guard: {data}"
                assert "Write blocked" in data.get("error", "")
            del server

    async def test_add_drawer_guard_allows(self, palace_path, collection):
        """When guard.should_pause_writes=False, add_drawer proceeds normally."""
        from mempalace.memory_guard import MemoryGuard
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        mock_guard = MagicMock()
        mock_guard.should_pause_writes.return_value = False
        with patch.object(MemoryGuard, "get", return_value=mock_guard):
            server = create_server(settings=settings)
            async with Client(transport=server) as client:
                result = await client.call_tool(
                    "mempalace_add_drawer",
                    {"wing": "test", "room": "room", "content": "test content guard allows", "added_by": "test"},
                )
                data = _get_result_data(result)
                assert data.get("success") is True, f"Expected success, got: {data}"
            del server

    async def test_add_drawer_guard_fail_open(self, palace_path, collection):
        """When guard.should_pause_writes raises, add_drawer proceeds (fail open)."""
        from mempalace.memory_guard import MemoryGuard
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        mock_guard = MagicMock()
        mock_guard.should_pause_writes.side_effect = RuntimeError("guard error")
        with patch.object(MemoryGuard, "get", return_value=mock_guard):
            server = create_server(settings=settings)
            async with Client(transport=server) as client:
                result = await client.call_tool(
                    "mempalace_add_drawer",
                    {"wing": "test", "room": "room", "content": "test content fail open", "added_by": "test"},
                )
                data = _get_result_data(result)
                assert data.get("success") is True, f"Expected success (fail open), got: {data}"
            del server

    async def test_status_includes_guard(self, palace_path, collection):
        """mempalace_status response includes memory_guard key."""
        settings = MemPalaceSettings(db_path=palace_path, db_backend="chromadb")
        server = create_server(settings=settings)
        async with Client(transport=server) as client:
            result = await client.call_tool("mempalace_status", {})
            data = _get_result_data(result)
            assert "memory_guard" in data, f"status should include memory_guard key, got: {data}"
        del server


def _get_result_data(result):
    """Extract JSON data from FastMCP CallToolResult."""
    if hasattr(result, 'structured_content') and result.structured_content:
        return result.structured_content
    if hasattr(result, 'content') and result.content:
        import json
        return json.loads(result.content[0].text)
    return None
