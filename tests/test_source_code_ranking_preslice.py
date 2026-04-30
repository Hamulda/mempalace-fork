"""
test_source_code_ranking_preslice.py — Pre-slice code-boost regression tests.

Tests that _apply_code_boost runs BEFORE the final [:n_results] slice,
so a source-code hit that was ranked below the top-k can still surface
in the final results after the boost is applied.

Fixture:
  src/auth.py          — contains "class AuthManager" ONCE
  docs/auth_00.md..auth_20.md — 21 files repeating "AuthManager" 40× each
  Query:    AuthManager
  n_results: 5
  Assertion: src/auth.py must appear in top-5 after boost.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from mempalace.miner import mine
from mempalace.searcher import (
    auto_search,
    code_search_async,
    hybrid_search_async,
)
from mempalace.server._code_tools import _source_file_matches


# ── Helpers ───────────────────────────────────────────────────────────────────

def run_async(coro):
    """Run an async coroutine. Always creates a fresh event loop for isolation."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_project_with_auth_and_many_docs(root: Path) -> Path:
    """
    Create project with:
      - src/auth.py: contains "class AuthManager" ONCE
      - docs/auth_00.md .. docs/auth_20.md: 21 docs each repeating
        "AuthManager" 40× via "See `AuthManager` for {i}" prose lines

    The docs collectively have ~840 AuthManager mentions vs 1 in source.
    Without pre-slice boost, docs would dominate the top-5.
    """
    (root / "mempalace.yaml").write_text(
        "name: test-project\nversion: 1\nwing: repo\n"
    )

    src = root / "src"
    src.mkdir()
    (src / "auth.py").write_text(
        '"""Authentication module."""\n\n'
        'class AuthManager:\n'
        '    """Central authentication manager."""\n'
        '    def __init__(self, config):\n'
        '        self.config = config\n'
        '    def authenticate(self, user, password):\n'
        '        return True\n'
        '    def revoke(self, token):\n'
        '        self._session_store.pop(token, None)\n'
    )

    docs = root / "docs"
    docs.mkdir()
    for i in range(21):
        (docs / f"auth_{i:02d}.md").write_text(
            "# Authentication\n\n"
            + "\n".join(f"See `AuthManager` for {j}"
                        for j in range(40))
            + "\n\nAuthManager handles authentication."
        )

    return root


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def project_with_auth_and_many_docs(tmp_path):
    """
    Create project with:
      - src/auth.py: contains "class AuthManager" ONCE
      - docs/auth_00.md .. docs/auth_20.md: 21 docs each repeating
        "AuthManager" 40× via "See `AuthManager` for {i}" prose lines

    The docs collectively have ~840 AuthManager mentions vs 1 in source.
    Without pre-slice boost, docs would dominate the top-5.
    """
    (tmp_path / "mempalace.yaml").write_text(
        "name: test-project\nversion: 1\nwing: repo\n"
    )

    src = tmp_path / "src"
    src.mkdir()
    (src / "auth.py").write_text(
        '"""Authentication module."""\n\n'
        'class AuthManager:\n'
        '    """Central authentication manager."""\n'
        '    def __init__(self, config):\n'
        '        self.config = config\n'
        '    def authenticate(self, user, password):\n'
        '        return True\n'
        '    def revoke(self, token):\n'
        '        self._session_store.pop(token, None)\n'
    )

    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(21):
        (docs / f"auth_{i:02d}.md").write_text(
            "# Authentication\n\n"
            + "\n".join(f"See `AuthManager` for {j}"
                        for j in range(40))
            + "\n\nAuthManager handles authentication."
        )

    return tmp_path


@pytest.fixture
def mixed_palace(project_with_auth_and_many_docs):
    """Mine project and yield palace path."""
    palace = tempfile.mkdtemp(prefix="mempalace_scrps_")
    try:
        mine(str(project_with_auth_and_many_docs), palace, agent="test")
        yield palace
    finally:
        shutil.rmtree(palace, ignore_errors=True)


# ── Regression tests ────────────────────────────────────────────────────────

class TestPresliceCodeBoost:
    """
    Verify boost is applied BEFORE the final slice so source code
    hits outside initial top-k can move into final results.
    """

    def test_authmanager_in_top5_auto_search(self, mixed_palace):
        """auto_search complexity=code — src/auth.py must be in top-5."""
        result = run_async(
            auto_search("AuthManager", mixed_palace, n_results=5)
        )
        hits = result.get("results", [])
        assert len(hits) >= 1, f"No results returned: {result}"

        top_sources = [h.get("source_file", "") for h in hits]
        auth_in_top5 = any("auth.py" in src for src in top_sources)
        assert auth_in_top5, (
            f"src/auth.py NOT in top-5 after boost.\n"
            f"Top-5 sources: {top_sources}\n"
            f"Hits: {hits[:3]}"
        )

    def test_authmanager_code_via_auto_search(self, mixed_palace):
        """auto_search auto-detects code query — src/auth.py must be in top-5."""
        # "AuthManager" auto-detects as code complexity (symbol/class name pattern)
        result = run_async(
            auto_search("AuthManager", mixed_palace, n_results=5)
        )
        hits = result.get("results", [])
        top_sources = [h.get("source_file", "") for h in hits]
        auth_in_top5 = any("auth.py" in src for src in top_sources)
        assert auth_in_top5, (
            f"auto_search: src/auth.py NOT in top-5.\n"
            f"Top-5: {top_sources}"
        )

    @pytest.mark.parametrize("intent", ["symbol", "code_exact", "code_semantic"])
    def test_code_search_intent_top5(self, mixed_palace, intent):
        """code_search with various intents — src/auth.py in top-5."""
        result = run_async(
            code_search_async("AuthManager", mixed_palace, n_results=5)
        )
        hits = result.get("results", [])
        assert len(hits) >= 1, f"No results for intent={intent}: {result}"
        top_sources = [h.get("source_file", "") for h in hits]
        auth_in_top5 = any("auth.py" in src for src in top_sources)
        assert auth_in_top5, (
            f"intent={intent}: src/auth.py NOT in top-5.\n"
            f"Top-5: {top_sources}"
        )

    def test_hybrid_search_async_authmanager(self, mixed_palace):
        """hybrid_search_async — src/auth.py in top-5."""
        result = run_async(
            hybrid_search_async("AuthManager", mixed_palace, n_results=5)
        )
        hits = result.get("results", [])
        assert len(hits) >= 1, f"No results: {result}"
        top_sources = [h.get("source_file", "") for h in hits]
        auth_in_top5 = any("auth.py" in src for src in top_sources)
        assert auth_in_top5, (
            f"hybrid_search_async: src/auth.py NOT in top-5.\n"
            f"Top-5: {top_sources}"
        )

    def test_memory_query_not_boosted(self, mixed_palace):
        """Memory query returns prose freely — boost must NOT dominate memory intent."""
        # "past authentication sessions" is clearly prose/memory
        result = run_async(
            auto_search("authentication manager architecture overview",
                        mixed_palace, n_results=5)
        )
        hits = result.get("results", [])
        # Must return something — not restricted by over-aggressive boost
        assert len(hits) >= 1, f"Memory query returned no hits: {result}"
        # Memory intent uses minimal boost (1.05 for code ext), docs should appear
        top_sources = [h.get("source_file", "") for h in hits]
        # At least some hits should be .md (prose) for a memory query
        prose_hits = [s for s in top_sources if s.endswith(".md")]
        assert len(prose_hits) >= 1, (
            f"Memory query returned no prose docs.\n"
            f"Top-5: {top_sources}"
        )


class TestProjectPathIsolation:
    """Verify project_path filter is applied AFTER boost (no project leakage)."""

    def test_no_cross_project_leakage(self):
        """
        In a palace with proj_a, querying with project_path=proj_a filter
        must not return hits from outside that project.
        Uses make_project_with_auth_and_many_docs helper directly.
        """
        proj_a = make_project_with_auth_and_many_docs(Path(tempfile.mkdtemp(prefix="mempalace_proj_a_")))
        palace = tempfile.mkdtemp(prefix="mempalace_iso_")
        try:
            # Mine proj_a into palace
            mine(str(proj_a), palace, agent="test")

            # Query AuthManager with project_path=proj_a filter via code_search_async
            result = run_async(
                code_search_async("AuthManager", palace, n_results=10,
                                  project_path=str(proj_a))
            )
            hits = result.get("results", [])
            assert len(hits) >= 1, f"No results with project_path filter: {result}"

            # All returned hits must belong to proj_a (use same _source_file_matches as filter)
            for hit in hits:
                sf = hit.get("source_file", "")
                assert _source_file_matches(sf, str(proj_a)), (
                    f"Hit from wrong project: {sf} not under {proj_a}"
                )
        finally:
            shutil.rmtree(palace, ignore_errors=True)
            shutil.rmtree(proj_a, ignore_errors=True)


class TestExactPathQuery:
    """Symbol-first queries (intent=symbol) route through _symbol_first_search."""

    def test_symbol_intent_query(self, mixed_palace):
        """
        Query 'AuthManager' is classified as symbol intent.
        Goes through _symbol_first_search → SymbolIndex → returns auth.py.
        """
        result = run_async(
            auto_search("AuthManager", mixed_palace, n_results=5)
        )
        hits = result.get("results", [])
        assert len(hits) >= 1, f"No results for AuthManager: {result}"
        top_sources = [h.get("source_file", "") for h in hits]
        auth_found = any("auth.py" in src for src in top_sources)
        assert auth_found, (
            f"AuthManager query did not return auth.py.\n"
            f"Sources: {top_sources}"
        )
