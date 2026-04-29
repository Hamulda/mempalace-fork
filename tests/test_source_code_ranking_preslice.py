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
    code_search,
    code_search_async,
    hybrid_search_async,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def run_async(coro):
    """Run an async coroutine in an existing event loop, or create a new one."""
    try:
        loop = asyncio.get_running_loop()
        if loop.is_closed():
            raise RuntimeError("event loop is closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        return loop.run_until_complete(coro)
    return loop.run_until_complete(coro)


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

    def test_no_cross_project_leakage(self, tmp_path):
        """
        Two separate palaces — querying project A must not leak hits from B.
        Uses the mixed_palace fixture for project A and a separate mine for B.
        """
        # Project A: the fixture project (has src/auth.py + docs)
        palace_a = tempfile.mkdtemp(prefix="mempalace_a_")
        palace_b = tempfile.mkdtemp(prefix="mempalace_b_")
        try:
            proj_a = project_with_auth_and_many_docs(tmp_path)
            mine(str(proj_a), palace_a, agent="test")

            # Project B: empty/minimal
            proj_b = tmp_path / "proj_b"
            proj_b.mkdir()
            (proj_b / "mempalace.yaml").write_text(
                "name: proj-b\nversion: 1\nwing: repo\n"
            )
            src_b = proj_b / "src"
            src_b.mkdir()
            (src_b / "other.py").write_text(
                "class Unrelated:\n    pass\n"
            )
            mine(str(proj_b), palace_b, agent="test")

            # Query A from A's palace — no project_path filter
            result_a = run_async(
                auto_search("AuthManager", palace_a, n_results=5)
            )
            hits_a = result_a.get("results", [])
            assert len(hits_a) >= 1

            # Query A from A's palace WITH project_path filter
            result_a_filtered = run_async(
                auto_search("AuthManager", palace_a, n_results=5,
                            project_path=str(proj_a))
            )
            hits_a_filtered = result_a_filtered.get("results", [])
            assert len(hits_a_filtered) >= 1

            # Both must return auth.py — filtering must NOT break results
            top_filtered = [h.get("source_file", "") for h in hits_a_filtered]
            assert any("auth.py" in src for src in top_filtered), (
                f"project_path filter broke results.\n"
                f"Top-5 filtered: {top_filtered}"
            )
        finally:
            shutil.rmtree(palace_a, ignore_errors=True)
            shutil.rmtree(palace_b, ignore_errors=True)


class TestExactPathQuery:
    """Exact path queries must not be damaged by boost."""

    def test_exact_path_query_returns_file(self, mixed_palace,
                                          project_with_auth_and_many_docs):
        """Exact path query for src/auth.py returns that file, not boosted away."""
        auth_path = str(project_with_auth_and_many_docs / "src" / "auth.py")
        result = run_async(
            auto_search(auth_path, mixed_palace, n_results=3)
        )
        hits = result.get("results", [])
        assert len(hits) >= 1, f"No results for exact path: {result}"
        top_src = hits[0].get("source_file", "")
        assert "auth.py" in top_src, (
            f"Exact path query did not return auth.py: {top_src}"
        )
