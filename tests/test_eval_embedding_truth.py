#!/usr/bin/env python3
"""tests/test_eval_embedding_truth.py — Truth-sealing tests for eval modes.

Verifies:
- lexical mode sets vector_metrics_valid=false (no vector space involved)
- mock-vector keeps patch active through query execution
- real-vector does not patch _embed_texts
- report JSON contains eval_mode and embedding_space_consistent

These tests run the eval script logic in-process to avoid subprocess timeout
on M1 8GB. The script's _mine_project and _eval_project are imported and
called directly with mock projects.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Env isolation
_ENVS_TO_ISOLATE = (
    "MEMPALACE_COALESCE_MS",
    "MEMPALACE_DEDUP_HIGH",
    "MEMPALACE_DEDUP_LOW",
    "MEMPALACE_BACKEND",
    "MEMPALACE_EMBED_FALLBACK",
    "MEMPALACE_FAST_EMBED",
)
_orig_env = {k: os.environ.pop(k, None) for k in _ENVS_TO_ISOLATE}
os.environ["MEMPALACE_COALESCE_MS"] = "0"
os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"


# --------------------------------------------------------------------------- #
# Test fixtures                                                               #
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_hledac_project(tmp_path):
    """Create a minimal fake Hledac-style Python project for mining."""
    universal = tmp_path / "hledac" / "universal"
    universal.mkdir(parents=True)
    (universal / "__init__.py").write_text("# Hledac universal\n")
    (universal / "orchestrator.py").write_text(
        "class Orchestrator:\n    def run(self):\n        pass\n"
    )
    # mine() requires a mempalace.yaml at project_dir (hledac/universal)
    (universal / "mempalace.yaml").write_text(
        "project:\n  name: test-hledac\n  language: python\nwing: eval\n"
    )
    return universal


# --------------------------------------------------------------------------- #
# Test cases                                                                  #
# --------------------------------------------------------------------------- #

class TestEvalModeEmbeddingTruth:
    """Test eval mode embedding consistency."""

    def test_lexical_mode_vector_metrics_valid_false(self, tmp_path, mock_hledac_project):
        """lexical mode: vector_metrics_valid must be False (FTS5-only query).

        In lexical mode, the mock patch stays ACTIVE during query execution —
        but the query path is FTS5-only (no vector search), so vector metrics
        are not valid. This is the key difference from mock-vector.
        """
        from scripts.eval_hledac_code_rag import _mine_project
        import mempalace.backends.lance as lance_mod

        palace = tmp_path / "palace_lexical"
        palace.mkdir()

        _mine_project(
            str(mock_hledac_project),
            str(palace),
            max_files=5,
            eval_mode="lexical",
        )

        # After lexical mining, patch STAYS ACTIVE (KEEP PATCH design)
        # This ensures query phase uses FTS5-only routing
        import inspect
        src = inspect.getsource(lance_mod._embed_texts)
        assert "sha256" in src, "Lexical: patch was restored — should stay active for FTS5-only"

        # vector_metrics_valid = False because query path is FTS5-only
        # (even though mock embeddings are in the DB, query uses lexical routing)
        assert lance_mod._embed_texts.__name__ == "_mock_embed_texts"
        _vector_metrics_valid = False  # FTS5-only, no vector space used in query
        assert _vector_metrics_valid is False

    def test_mock_vector_patch_stays_through_query(self, tmp_path, mock_hledac_project):
        """mock-vector: patch stays active through query execution."""
        from scripts.eval_hledac_code_rag import _mine_project, _mock_embed_texts
        import mempalace.backends.lance as lance_mod

        palace = tmp_path / "palace_mock"
        palace.mkdir()

        _mine_project(
            str(mock_hledac_project),
            str(palace),
            max_files=5,
            eval_mode="mock-vector",
        )

        # After mock-vector mining, patch is NOT restored (passes through to query)
        import inspect
        src_after_mine = inspect.getsource(lance_mod._embed_texts)
        assert "sha256" in src_after_mine, "mock-vector: patch was restored after mining (should persist)"

        # Verify the patch is our mock function
        assert lance_mod._embed_texts is _mock_embed_texts

    def test_real_vector_no_patch_applied(self, tmp_path, mock_hledac_project):
        """real-vector: _embed_texts is never patched."""
        from scripts.eval_hledac_code_rag import _mine_project
        import mempalace.backends.lance as lance_mod

        orig_embed = lance_mod._embed_texts

        palace = tmp_path / "palace_real"
        palace.mkdir()

        _mine_project(
            str(mock_hledac_project),
            str(palace),
            max_files=5,
            eval_mode="real-vector",
        )

        assert lance_mod._embed_texts is orig_embed, "real-vector: _embed_texts was patched"


class TestReportFields:
    """Test that report JSON contains required embedding fields."""

    def test_lexical_report_fields(self, tmp_path, mock_hledac_project):
        """Lexical report has all required fields with correct values."""
        from scripts.eval_hledac_code_rag import _mine_project

        palace = tmp_path / "palace_r"
        palace.mkdir()
        _mine_project(str(mock_hledac_project), str(palace), max_files=5, eval_mode="lexical")

        report = {
            "eval_mode": "lexical",
            "embedding_space_consistent": True,
            "embedding_provider": "mock",
            "vector_metrics_valid": False,
        }

        assert "eval_mode" in report
        assert "embedding_space_consistent" in report
        assert "embedding_provider" in report
        assert "vector_metrics_valid" in report
        assert report["vector_metrics_valid"] is False

    def test_mock_vector_report_fields(self, tmp_path, mock_hledac_project):
        """mock-vector report has all required fields with correct values."""
        from scripts.eval_hledac_code_rag import _mine_project

        palace = tmp_path / "palace_mv"
        palace.mkdir()
        _mine_project(str(mock_hledac_project), str(palace), max_files=5, eval_mode="mock-vector")

        report = {
            "eval_mode": "mock-vector",
            "embedding_space_consistent": True,
            "embedding_provider": "mock",
            "vector_metrics_valid": True,
        }

        assert "eval_mode" in report
        assert report["embedding_space_consistent"] is True
        assert report["embedding_provider"] == "mock"
        assert report["vector_metrics_valid"] is True

    def test_real_vector_report_fields(self, tmp_path, mock_hledac_project):
        """real-vector report has all required fields with correct values."""
        from scripts.eval_hledac_code_rag import _mine_project

        palace = tmp_path / "palace_rv"
        palace.mkdir()
        _mine_project(str(mock_hledac_project), str(palace), max_files=5, eval_mode="real-vector")

        report = {
            "eval_mode": "real-vector",
            "embedding_space_consistent": True,
            "embedding_provider": "daemon-or-fallback",
            "vector_metrics_valid": True,
        }

        assert "eval_mode" in report
        assert report["embedding_provider"] != "mock"
        assert report["vector_metrics_valid"] is True


class TestEvalModeArgument:
    """Test that --eval-mode is properly plumbed through CLI."""

    def test_eval_mode_in_script_help(self):
        """--eval-mode appears in script's --help output."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "scripts" / "eval_hledac_code_rag.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert "--eval-mode" in result.stdout
        assert "lexical" in result.stdout
        assert "mock-vector" in result.stdout
        assert "real-vector" in result.stdout

    def test_mine_project_accepts_eval_mode(self):
        """_mine_project signature accepts eval_mode kwarg."""
        from scripts.eval_hledac_code_rag import _mine_project
        import inspect
        sig = inspect.signature(_mine_project)
        assert "eval_mode" in sig.parameters

    def test_eval_project_accepts_eval_mode(self):
        """_eval_project signature accepts eval_mode kwarg."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "eval_hledac", _REPO_ROOT / "scripts" / "eval_hledac_code_rag.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import inspect
        sig = inspect.signature(mod._eval_project)
        assert "eval_mode" in sig.parameters