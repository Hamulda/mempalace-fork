"""
test_retrieval_gate.py — Tests for adaptive retrieval gate (query complexity + rerank gating).

Covers:
- _query_complexity() classification (path/code/simple/complex)
- _should_rerank() gate logic
- auto_search() routing: path → FTS5-only, code → code_search, simple/complex → hybrid_search
- backward compatibility: rerank=False always suppresses rerank_score
"""

import pytest


class TestQueryComplexity:
    """Unit tests for _query_complexity()."""

    def test_path_query_dot_ext(self):
        from mempalace.searcher import _query_complexity
        assert _query_complexity("src/utils/auth.py") == "path"
        assert _query_complexity("services/worker.ts") == "path"
        assert _query_complexity("lib/helpers.js") == "path"
        assert _query_complexity("components/Button.vue") == "path"

    def test_path_query_relative(self):
        from mempalace.searcher import _query_complexity
        assert _query_complexity("src/utils/auth") == "path"
        assert _query_complexity("lib/auth") == "path"
        assert _query_complexity("src/utils") == "path"
        assert _query_complexity("lib/helpers") == "path"
        # Bare directory names (no slash) are not path queries — classified as simple
        assert _query_complexity("components") == "simple"

    def test_code_query_def(self):
        from mempalace.searcher import _query_complexity
        assert _query_complexity("def authenticate_user") == "code"
        assert _query_complexity("class MemoryGuard") == "code"
        assert _query_complexity("function processQuery") == "code"

    def test_code_query_import(self):
        from mempalace.searcher import _query_complexity
        assert _query_complexity("from mempalace import search") == "code"
        assert _query_complexity("import json") == "code"
        assert _query_complexity("require('./cache')") == "code"

    def test_code_query_js(self):
        from mempalace.searcher import _query_complexity
        assert _query_complexity("const handler = () =>") == "code"
        assert _query_complexity("export function getData") == "code"
        assert _query_complexity("let result = process(x)") == "code"

    def test_code_query_dot_ext(self):
        from mempalace.searcher import _query_complexity
        assert _query_complexity("utils.py") == "code"
        assert _query_complexity("auth.ts") == "code"
        assert _query_complexity("server.go") == "code"
        assert _query_complexity("main.rs") == "code"

    def test_simple_query(self):
        from mempalace.searcher import _query_complexity
        assert _query_complexity("JWT auth") == "simple"
        assert _query_complexity("memory palace") == "simple"
        assert _query_complexity("what is this") == "simple"

    def test_complex_query(self):
        from mempalace.searcher import _query_complexity
        assert _query_complexity("how does the memory guard prevent writes under pressure") == "complex"
        assert _query_complexity("best practices for session coordination in multi-agent systems") == "complex"

    def test_public_api(self):
        from mempalace.searcher import query_complexity
        assert query_complexity("src/utils") == "path"
        assert query_complexity("def authenticate") == "code"
        assert query_complexity("memory palace") == "simple"


class TestShouldRerank:
    """Unit tests for _should_rerank()."""

    def test_no_rerank_path_query(self):
        from mempalace.searcher import _should_rerank
        assert _should_rerank("src/utils/auth.py", 5) is False
        assert _should_rerank("lib/worker.ts", 5) is False

    def test_no_rerank_code_query(self):
        from mempalace.searcher import _should_rerank
        assert _should_rerank("def authenticate_user", 5) is False
        assert _should_rerank("class MemoryGuard", 5) is False

    def test_no_rerank_simple_query(self):
        from mempalace.searcher import _should_rerank
        assert _should_rerank("JWT auth", 5) is False
        assert _should_rerank("memory palace", 5) is False

    def test_rerank_complex_small_n(self):
        from mempalace.searcher import _should_rerank
        # complex + n_results=5 → rerank (within budget)
        assert _should_rerank("how does session coordination work across agents", 5) is True
        assert _should_rerank("best approach for multi-agent memory sharing", 3) is True

    def test_no_rerank_complex_large_n(self):
        from mempalace.searcher import _should_rerank
        # complex + n_results > _RERANK_SHORTLIST_MAX (10) → no rerank
        assert _should_rerank("how does session coordination work across agents", 15) is False
        assert _should_rerank("best approach for multi-agent memory sharing", 100) is False

    def test_no_rerank_complex_n_1(self):
        from mempalace.searcher import _should_rerank
        assert _should_rerank("complex semantic query requiring reranking", 1) is False


class TestAutoSearchRouting:
    """Integration tests for auto_search() async routing decisions."""

    @pytest.mark.asyncio
    async def test_auto_search_includes_complexity_filter(self, palace_path, seeded_collection):
        from mempalace.searcher import auto_search
        result = await auto_search("how does the memory guard work under pressure", palace_path, n_results=5)
        assert "filters" in result
        assert result["filters"].get("complexity") == "complex"

    @pytest.mark.asyncio
    async def test_auto_search_path_query(self, palace_path, seeded_collection):
        from mempalace.searcher import auto_search
        result = await auto_search("src/utils", palace_path, n_results=5)
        assert result["filters"].get("complexity") == "path"

    @pytest.mark.asyncio
    async def test_auto_search_code_query(self, palace_path, seeded_collection):
        from mempalace.searcher import auto_search
        result = await auto_search("def authenticate", palace_path, n_results=5)
        assert result["filters"].get("complexity") == "code"

    @pytest.mark.asyncio
    async def test_auto_search_simple_query(self, palace_path, seeded_collection):
        from mempalace.searcher import auto_search
        result = await auto_search("JWT token", palace_path, n_results=5)
        assert result["filters"].get("complexity") == "simple"


class TestRerankBackwardCompat:
    """Verify rerank=False always suppresses rerank_score (backward compat)."""

    def test_rerank_false_no_score(self, palace_path, seeded_collection):
        from mempalace.searcher import search_memories
        result = search_memories("JWT authentication pattern", palace_path, n_results=5, rerank=False)
        assert all("rerank_score" not in h for h in result["results"])

    def test_rerank_true_complex_long_query(self, palace_path, seeded_collection):
        """rerank=True on complex query with >3 words still adds rerank_score."""
        from mempalace.searcher import search_memories
        result = search_memories(
            "what is the memory guard policy under high RAM pressure",
            palace_path,
            n_results=5,
            rerank=True,
        )
        # Result depends on whether sentence-transformers is installed
        # Key invariant: either all have rerank_score or none do (not mixed)
        scores = [h for h in result["results"] if "rerank_score" in h]
        non_scores = [h for h in result["results"] if "rerank_score" not in h]
        # Should be all-or-nothing
        assert len(scores) == 0 or len(non_scores) == 0

    def test_rerank_shortlist_ceiling(self, palace_path, seeded_collection):
        """Complex query with n_results > shortlist ceiling skips rerank."""
        from mempalace.searcher import search_memories, _RERANK_SHORTLIST_MAX
        result = search_memories(
            "best approach for multi-agent coordination across sessions with shared state",
            palace_path,
            n_results=_RERANK_SHORTLIST_MAX + 5,
            rerank=True,
        )
        assert all("rerank_score" not in h for h in result["results"])


class TestIsPathQuery:
    """Tests for is_path_query() public API."""

    def test_is_path_query(self):
        from mempalace.searcher import is_path_query
        assert is_path_query("src/utils/auth.py") is True
        assert is_path_query("lib/helpers") is True
        assert is_path_query("def authenticate") is False
        assert is_path_query("memory palace") is False


class TestQueryComplexityExports:
    """Verify all public symbols are importable."""

    def test_public_symbols_importable(self):
        from mempalace.searcher import (
            query_complexity,
            is_code_query,
            is_path_query,
        )
        assert callable(query_complexity)
        assert callable(is_code_query)
        assert callable(is_path_query)
