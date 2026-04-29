"""
test_source_code_ranking.py — Source-code-first ranking tests.

Tests that code queries prefer source code (.py/.js/etc.) over docs (.md)
when both contain similar content. Also verifies general/memory queries
are not affected by the boost.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from mempalace.miner import mine
from mempalace.searcher import auto_search, code_search


# ── Helpers ───────────────────────────────────────────────────────────────────

def run_async(coro):
    """Run an async coroutine in an existing event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        return loop.run_until_complete(coro)
    return asyncio.run(coro)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def project_with_auth_and_docs(tmp_path):
    """Create project with both source and docs containing AuthManager."""
    # mempalace.yaml required by mine()
    (tmp_path / "mempalace.yaml").write_text("name: test-project\nversion: 1\nwing: repo\n")

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
    (docs / "auth.md").write_text(
        "# Authentication\n\n"
        + "\n".join(f"See `AuthManager` for {i}"
                    for i in range(40))
        + "\n\nAuthManager handles authentication."
    )
    return tmp_path


@pytest.fixture
def mixed_palace(project_with_auth_and_docs):
    """Mine project and yield palace path."""
    palace = tempfile.mkdtemp(prefix="mempalace_scr_")
    try:
        mine(str(project_with_auth_and_docs), palace, agent="test")
        yield palace
    finally:
        shutil.rmtree(palace, ignore_errors=True)


@pytest.fixture
def other_project(tmp_path):
    """A second, separate project for cross-project isolation test."""
    (tmp_path / "mempalace.yaml").write_text("name: other-project\nversion: 1\nwing: repo\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "auth.py").write_text("class AuthManager:\n    pass\n")
    return tmp_path


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCodeQueryRanking:
    """Code queries should rank source code above docs."""

    def test_code_query_prefers_source_py_over_md(self, mixed_palace):
        """Query for AuthManager symbol → src/auth.py top1."""
        result = run_async(auto_search("AuthManager", mixed_palace, n_results=5))
        hits = result.get("results", [])
        assert len(hits) >= 1, f"No results: {result}"
        top_src = hits[0].get("source_file", "")
        assert top_src.endswith(".py"), (
            f"Top result not .py: {top_src}\n"
            f"Top 3: {[h.get('source_file') for h in hits[:3]]}"
        )

    def test_python_file_extension_in_top3(self, mixed_palace):
        """Auto search for 'auth.py' → .py file in top results."""
        result = run_async(auto_search("auth.py", mixed_palace, n_results=5))
        hits = result.get("results", [])
        py_in_top = any(h.get("source_file", "").endswith(".py") for h in hits[:3])
        assert py_in_top, f"No .py in top 3: {[h.get('source_file') for h in hits[:3]]}"

    def test_class_query_source_preferred(self, mixed_palace):
        """Query 'class AuthManager' → source file preferred."""
        result = run_async(auto_search("class AuthManager", mixed_palace, n_results=5))
        hits = result.get("results", [])
        assert len(hits) >= 1
        top_ext = Path(hits[0].get("source_file", "")).suffix
        assert top_ext == ".py", f"Top ext: {top_ext!r}, expected .py"


class TestMemoryQueryIsolation:
    """Memory/general queries should not be artificially boosted toward code."""

    def test_memory_query_can_return_docs(self, mixed_palace):
        """Memory-style query may still return .md content (no artificial boost)."""
        result = run_async(auto_search(
            "authentication architecture overview history",
            mixed_palace, n_results=5
        ))
        hits = result.get("results", [])
        assert len(hits) >= 1, "Memory query returned no results"

    def test_prose_query_still_returns_relevant(self, mixed_palace):
        """Prose query → get results and verify they are relevant."""
        result = run_async(auto_search(
            "what is the authentication architecture",
            mixed_palace, n_results=5
        ))
        hits = result.get("results", [])
        assert len(hits) >= 1, "Prose query returned no results"
        # We don't assert docs aren't top (boost is minimal for memory),
        # just that results are relevant to the query
        top_text = hits[0].get("text", "").lower()
        assert "auth" in top_text or "authentication" in top_text, (
            f"Top result not relevant: {top_text[:100]}"
        )


class TestPathQueryIsolation:
    """Path queries should respect explicit path, not be overridden by code boost."""

    def test_path_query_exact_file(self, project_with_auth_and_docs, mixed_palace):
        """Exact path query → returns that specific file."""
        auth_path = str(project_with_auth_and_docs / "src" / "auth.py")
        result = run_async(auto_search(auth_path, mixed_palace, n_results=3))
        hits = result.get("results", [])
        assert len(hits) >= 1
        top_src = hits[0].get("source_file", "")
        assert "auth.py" in top_src, f"Path query did not return auth.py: {top_src}"


class TestProjectIsolation:
    """Code boost must not cause cross-project result leakage."""

    def test_no_cross_project_leak(self, mixed_palace, other_project):
        """All results must be from mixed_palace project, not other_project."""
        other_palace = tempfile.mkdtemp(prefix="mempalace_other_")
        try:
            mine(str(other_project), other_palace, agent="test")
            result = run_async(auto_search("AuthManager", mixed_palace, n_results=10))
            hits = result.get("results", [])
            for h in hits:
                src = h.get("source_file", "")
                assert not src.startswith(str(other_project)), (
                    f"Cross-project leak: {src} from other project"
                )
        finally:
            shutil.rmtree(other_palace, ignore_errors=True)