"""
Phase 3 Retrieval Planner Tests.

Verifies:
1. classify_query routing is correct across the six intent categories.
2. build_planner_filters produces valid ChromaDB-style where dicts.
3. $starts_with operator works in _apply_where_filter for project isolation.
4. Path-first and symbol-first search functions exist and route correctly.
"""
from __future__ import annotations

import pandas as pd
import pytest


class TestClassifyQuery:
    """Unit tests for classify_query (retrieval_planner module)."""

    @pytest.mark.parametrize("query,expected", [
        # Path queries — slash or extension signals
        ("src/auth/login.py", "path"),
        ("*.py", "path"),
        ("foo/bar/baz.rs", "path"),
        ("utils/", "path"),
        ("src/utils", "path"),
        ("api/v1/users.go", "path"),
        ("foo.bar/baz.py", "path"),
        # Symbol queries — def/class keyword, bare identifier, or dot-qualified
        ("authenticate", "symbol"),
        ("MyClass", "symbol"),
        ("def authenticate", "symbol"),
        ("async def fetch", "symbol"),
        ("UserService", "symbol"),
        ("my_function", "symbol"),
        ("foo.bar", "symbol"),
        ("ClassName.method", "symbol"),
        ("utils", "symbol"),
        # Code exact — import statements
        ("import os", "code_exact"),
        ("from typing import Optional", "code_exact"),
        # Code semantic — multi-word with code vocabulary
        ("how does the login flow work", "code_semantic"),
        ("where is the authentication handled", "code_semantic"),
        # Memory — prose signals
        ("what is authentication", "memory"),
        ("tell me about the login", "memory"),
        ("describe the system", "memory"),
        # Mixed — ambiguous
        ("foo bar baz qux", "mixed"),
        ("", "mixed"),
    ])
    def test_classify_query(self, query, expected):
        from mempalace.retrieval_planner import classify_query
        result = classify_query(query)
        assert result == expected, f"{query!r} → {result}, expected {expected}"


class TestBuildPlannerFilters:
    """Unit tests for build_planner_filters."""

    def test_is_latest_default_true(self):
        """Default call (no args) returns is_latest filter."""
        from mempalace.retrieval_planner import build_planner_filters
        f = build_planner_filters()
        assert f == {"is_latest": {"$eq": True}}

    def test_wing_filter_only(self):
        from mempalace.retrieval_planner import build_planner_filters
        f = build_planner_filters(wing="repo", is_latest=None)
        assert f == {"wing": {"$eq": "repo"}}

    def test_project_path_filter(self):
        from mempalace.retrieval_planner import build_planner_filters
        f = build_planner_filters(project_path="/Users/me/project")
        assert "$and" in f
        prefix = next(c["source_file"]["$starts_with"] for c in f["$and"] if "source_file" in c)
        assert prefix == "/Users/me/project/"

    def test_all_filters(self):
        from mempalace.retrieval_planner import build_planner_filters
        f = build_planner_filters(project_path="/proj", language="Python", wing="repo", is_latest=True)
        assert "$and" in f
        assert len(f["$and"]) == 4


class TestProjectIsolation:
    """Tests for cross-project isolation via $starts_with."""

    def test_different_prefixes(self):
        """project A and project B must produce different source_file prefixes."""
        from mempalace.retrieval_planner import build_planner_filters
        fA = build_planner_filters(project_path="/projA", wing="repo", is_latest=True)
        fB = build_planner_filters(project_path="/projB", wing="repo", is_latest=True)
        pA = next(c["source_file"]["$starts_with"] for c in fA["$and"] if "source_file" in c)
        pB = next(c["source_file"]["$starts_with"] for c in fB["$and"] if "source_file" in c)
        assert pA == "/projA/"
        assert pB == "/projB/"
        assert pA != pB

    def test_starts_with_db_filter(self):
        """$starts_with in _apply_where_filter returns only matching rows."""
        from mempalace.backends.lance import _apply_where_filter
        df = pd.DataFrame([
            {"id": "1", "_meta": {"source_file": "/projA/auth.py", "wing": "repo", "is_latest": True}},
            {"id": "2", "_meta": {"source_file": "/projB/auth.py", "wing": "repo", "is_latest": True}},
            {"id": "3", "_meta": {"source_file": "/projA/core.py", "wing": "repo", "is_latest": True}},
            {"id": "4", "_meta": {"source_file": "/projB/core.py", "wing": "repo", "is_latest": True}},
        ])
        result = _apply_where_filter(df, {"source_file": {"$starts_with": "/projA/"}})
        assert len(result) == 2
        assert set(result["id"]) == {"1", "3"}


class TestRetrievalPaths:
    """Verify path-first and symbol-first search functions exist and are callable."""

    def test_path_first_search_exists(self):
        from mempalace.searcher import _path_first_search
        assert callable(_path_first_search)

    def test_symbol_first_search_exists(self):
        from mempalace.searcher import _symbol_first_search
        assert callable(_symbol_first_search)

    def test_symbol_query_routes_to_symbol_path(self):
        from mempalace.retrieval_planner import classify_query
        intent = classify_query("MyClass")
        assert intent == "symbol"

    def test_path_query_routes_to_path_first(self):
        from mempalace.retrieval_planner import classify_query
        intent = classify_query("src/auth/login.py")
        assert intent == "path"
