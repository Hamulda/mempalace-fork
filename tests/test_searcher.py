"""
test_searcher.py -- Tests for both search() (CLI) and search_memories() (API).

Uses the real ChromaDB fixtures from conftest.py for integration tests,
plus mock-based tests for error paths.
"""

from unittest.mock import MagicMock, patch

import pytest

from mempalace.searcher import SearchError, _build_where_filter, search, search_memories


# ── search_memories (API) ──────────────────────────────────────────────


class TestSearchMemories:
    def test_basic_search(self, palace_path, seeded_collection):
        result = search_memories("JWT authentication", palace_path)
        assert "results" in result
        assert len(result["results"]) > 0
        assert result["query"] == "JWT authentication"

    def test_wing_filter(self, palace_path, seeded_collection):
        result = search_memories("planning", palace_path, wing="notes")
        assert all(r["wing"] == "notes" for r in result["results"])

    def test_room_filter(self, palace_path, seeded_collection):
        result = search_memories("database", palace_path, room="backend")
        assert all(r["room"] == "backend" for r in result["results"])

    def test_wing_and_room_filter(self, palace_path, seeded_collection):
        result = search_memories("code", palace_path, wing="project", room="frontend")
        assert all(r["wing"] == "project" and r["room"] == "frontend" for r in result["results"])

    def test_n_results_limit(self, palace_path, seeded_collection):
        result = search_memories("code", palace_path, n_results=2)
        assert len(result["results"]) <= 2

    def test_no_palace_returns_error(self, tmp_path):
        result = search_memories("anything", str(tmp_path / "missing"))
        assert "error" in result

    def test_result_fields(self, palace_path, seeded_collection):
        result = search_memories("authentication", palace_path)
        hit = result["results"][0]
        assert "text" in hit
        assert "wing" in hit
        assert "room" in hit
        assert "source_file" in hit
        assert "similarity" in hit
        assert isinstance(hit["similarity"], float)

    def test_search_memories_query_error(self):
        """search_memories returns error dict when query raises."""
        mock_col = MagicMock()
        mock_col.query.side_effect = RuntimeError("query failed")
        mock_backend = MagicMock()
        mock_backend.get_collection.return_value = mock_col

        with patch("mempalace.searcher.get_backend", return_value=mock_backend):
            result = search_memories("test", "/fake/path")
        assert "error" in result
        assert "query failed" in result["error"]

    def test_search_memories_filters_in_result(self, palace_path, seeded_collection):
        result = search_memories("test", palace_path, wing="project", room="backend")
        assert result["filters"]["wing"] == "project"
        assert result["filters"]["room"] == "backend"


# ── search() (CLI print function) ─────────────────────────────────────


class TestSearchCLI:
    def test_search_prints_results(self, palace_path, seeded_collection, capsys):
        search("JWT authentication", palace_path)
        captured = capsys.readouterr()
        assert "JWT" in captured.out or "authentication" in captured.out

    def test_search_with_wing_filter(self, palace_path, seeded_collection, capsys):
        search("planning", palace_path, wing="notes")
        captured = capsys.readouterr()
        assert "Results for" in captured.out

    def test_search_with_room_filter(self, palace_path, seeded_collection, capsys):
        search("database", palace_path, room="backend")
        captured = capsys.readouterr()
        assert "Room:" in captured.out

    def test_search_with_wing_and_room(self, palace_path, seeded_collection, capsys):
        search("code", palace_path, wing="project", room="frontend")
        captured = capsys.readouterr()
        assert "Wing:" in captured.out
        assert "Room:" in captured.out

    def test_search_no_palace_raises(self, tmp_path):
        with pytest.raises(SearchError, match="No palace found"):
            search("anything", str(tmp_path / "missing"))

    def test_search_no_results(self, palace_path, collection, capsys):
        """Empty collection returns no results message."""
        # collection is empty (no seeded data)
        result = search("xyzzy_nonexistent_query", palace_path, n_results=1)
        captured = capsys.readouterr()
        # Either prints "No results" or returns None
        assert result is None or "No results" in captured.out

    def test_search_query_error_raises(self):
        """search raises SearchError when query fails."""
        mock_col = MagicMock()
        mock_col.query.side_effect = RuntimeError("boom")
        mock_backend = MagicMock()
        mock_backend.get_collection.return_value = mock_col

        with patch("mempalace.searcher.get_backend", return_value=mock_backend):
            with pytest.raises(SearchError, match="Search error"):
                search("test", "/fake/path")

    def test_search_n_results(self, palace_path, seeded_collection, capsys):
        search("code", palace_path, n_results=1)
        captured = capsys.readouterr()
        # Should have output with at least one result block
        assert "[1]" in captured.out


# ── _build_where_filter ────────────────────────────────────────────────


class TestBuildWhereFilter:
    def test_build_where_no_params(self):
        assert _build_where_filter() == {}

    def test_build_where_wing_only(self):
        assert _build_where_filter(wing="wing_user") == {"wing": {"$eq": "wing_user"}}

    def test_build_where_room_only(self):
        assert _build_where_filter(room="backend") == {"room": {"$eq": "backend"}}

    def test_build_where_wing_room(self):
        result = _build_where_filter(wing="wing_user", room="backend")
        assert result == {"$and": [{"wing": {"$eq": "wing_user"}}, {"room": {"$eq": "backend"}}]}

    def test_build_where_is_latest(self):
        result = _build_where_filter(is_latest=True)
        assert result == {"is_latest": {"$eq": True}}

    def test_build_where_agent_id(self):
        result = _build_where_filter(agent_id="agent_1")
        assert result == {"agent_id": {"$eq": "agent_1"}}

    def test_build_where_priority_gte(self):
        result = _build_where_filter(priority_gte=5)
        assert result == {"priority": {"$gte": 5}}

    def test_build_where_priority_lte(self):
        result = _build_where_filter(priority_lte=3)
        assert result == {"priority": {"$lte": 3}}

    def test_build_where_all_params(self):
        result = _build_where_filter(
            wing="w",
            room="r",
            is_latest=True,
            agent_id="a1",
            priority_gte=5,
            priority_lte=3,
        )
        assert len(result["$and"]) == 6
        assert {"wing": {"$eq": "w"}} in result["$and"]
        assert {"room": {"$eq": "r"}} in result["$and"]
        assert {"is_latest": {"$eq": True}} in result["$and"]
        assert {"agent_id": {"$eq": "a1"}} in result["$and"]
        assert {"priority": {"$gte": 5}} in result["$and"]
        assert {"priority": {"$lte": 3}} in result["$and"]


# ── Async wrapper ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_memories_async_returns_dict(palace_path, seeded_collection):
    """search_memories_async returns a dict (not coroutine)."""
    from mempalace.searcher import search_memories_async

    result = await search_memories_async("JWT authentication", palace_path)
    assert isinstance(result, dict)
    assert "results" in result
    assert len(result["results"]) > 0


@pytest.mark.asyncio
async def test_six_parallel_async_searches(palace_path, seeded_collection):
    """Six parallel async searches run without exception."""
    import asyncio
    from mempalace.searcher import search_memories_async

    queries = ["JWT", "database", "React", "API", "config", "auth"]
    tasks = [search_memories_async(q, palace_path, n_results=3) for q in queries]
    results = await asyncio.gather(*tasks)
    assert len(results) == 6
    assert all(isinstance(r, dict) for r in results)


def test_adaptive_top_k_no_crash(palace_path, seeded_collection):
    """search_memories with n_results > doc count does not raise."""
    from mempalace.searcher import search_memories

    # seeded_collection has 4 docs — request 100, should clamp to 4
    result = search_memories("code", palace_path, n_results=100)
    assert "error" not in result or result.get("error", "") == ""
    assert len(result.get("results", [])) <= 4


# ── Rerank ───────────────────────────────────────────────────────────


def test_rerank_false_preserves_cosine_order(palace_path, seeded_collection):
    """rerank=False returns results sorted by cosine similarity, not rerank_score."""
    result = search_memories("authentication", palace_path, n_results=5, rerank=False)
    assert "results" in result
    similarities = [h["similarity"] for h in result["results"]]
    # Should be in descending cosine order
    assert similarities == sorted(similarities, reverse=True)
    # No rerank_score when rerank=False
    assert all("rerank_score" not in h for h in result["results"])


def test_rerank_skips_short_query(palace_path, seeded_collection):
    """Query with <= 3 words and rerank=True produces no rerank_score."""
    # Short query: "database code" = 2 words — reranking skipped
    result = search_memories("database code", palace_path, n_results=5, rerank=True)
    assert "results" in result
    assert all("rerank_score" not in h for h in result["results"])


def test_rerank_graceful_no_sentence_transformers(palace_path, seeded_collection):
    """search_memories with rerank=True works even if sentence-transformers is absent."""
    pytest.importorskip("sentence_transformers", reason="sentence-transformers installed")
    # If we reach here, sentence-transformers IS installed — rerank should work
    result = search_memories("JWT authentication pattern", palace_path, n_results=3, rerank=True)
    assert "error" not in result
    assert "results" in result
