"""
test_hledac_eval_script_smoke.py -- Smoke test for eval_hledac_code_rag.py

Does NOT require real Hledac path unless env var MEMPALACE_RUN_REAL_HLEDAC_EVAL=1 is set.

When env var is set, runs the real eval against the Hledac project.
Otherwise, tests the script's CLI parsing and internal helpers in isolation.
"""

import os
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Path setup                                                                  #
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# --------------------------------------------------------------------------- #
# Env isolation for mempalace imports                                         #
# --------------------------------------------------------------------------- #

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
# Helper tests (always run, no real Hledac needed)                            #
# --------------------------------------------------------------------------- #

class TestExpectedFileMap:
    """Verify expected file map covers all query IDs."""

    def test_all_queries_have_expected_file(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "eval_hledac", _REPO_ROOT / "scripts" / "eval_hledac_code_rag.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for q in mod.QUERIES:
            qid = q["id"]
            assert qid in mod.EXPECTED_FILE_MAP, f"No expected file for query: {qid}"


class TestMetricHelpers:
    """Test metric helper functions with synthetic data."""

    def test_top1_file_hit_no_results(self):
        from scripts.eval_hledac_code_rag import _top1_file_hit
        assert _top1_file_hit({}, "core/__main__.py") == 0.0

    def test_top1_file_hit_miss(self):
        from scripts.eval_hledac_code_rag import _top1_file_hit
        result = {"results": [{"source_file": "/foo/bar/other.py"}]}
        assert _top1_file_hit(result, "core/__main__.py") == 0.0

    def test_top1_file_hit_hit(self):
        from scripts.eval_hledac_code_rag import _top1_file_hit
        result = {"results": [{"source_file": "/some/path/core/__main__.py"}]}
        assert _top1_file_hit(result, "core/__main__.py") == 1.0

    def test_top5_file_hit_miss(self):
        from scripts.eval_hledac_code_rag import _top5_file_hit
        result = {"results": [
            {"source_file": "/a.py"},
            {"source_file": "/b.py"},
            {"source_file": "/c.py"},
        ]}
        assert _top5_file_hit(result, "core/__main__.py") == 0.0

    def test_top5_file_hit_hit_position_3(self):
        from scripts.eval_hledac_code_rag import _top5_file_hit
        result = {"results": [
            {"source_file": "/a.py"},
            {"source_file": "/b.py"},
            {"source_file": "/some/core/__main__.py"},
        ]}
        assert _top5_file_hit(result, "core/__main__.py") == 1.0

    def test_has_line_range_true(self):
        from scripts.eval_hledac_code_rag import _has_line_range
        result = {"results": [{"line_range": (10, 20)}]}
        assert _has_line_range(result) == 1.0

    def test_has_line_range_false(self):
        from scripts.eval_hledac_code_rag import _has_line_range
        result = {"results": [{"source_file": "/a.py"}]}
        assert _has_line_range(result) == 0.0

    def test_has_symbol_name_hit(self):
        from scripts.eval_hledac_code_rag import _has_symbol_name
        result = {"results": [{"text": "def run_sprint(): pass"}]}
        assert _has_symbol_name(result, "run_sprint") == 1.0

    def test_has_symbol_name_miss(self):
        from scripts.eval_hledac_code_rag import _has_symbol_name
        result = {"results": [{"text": "def other(): pass"}]}
        assert _has_symbol_name(result, "run_sprint") == 0.0

    def test_is_in_project_path_true(self):
        from scripts.eval_hledac_code_rag import _is_in_project_path
        result = {"results": [{"source_file": "/Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal/core/__main__.py"}]}
        assert _is_in_project_path(result, "/Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal") is True

    def test_is_in_project_path_false(self):
        from scripts.eval_hledac_code_rag import _is_in_project_path
        result = {"results": [{"source_file": "/tmp/other_palace/a.py"}]}
        assert _is_in_project_path(result, "/Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal") is False

    def test_is_in_project_path_empty(self):
        from scripts.eval_hledac_code_rag import _is_in_project_path
        assert _is_in_project_path({}, "/Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal") is False


class TestCLIParsing:
    """Test that CLI argument parsing works."""

    def test_script_imports_cleanly(self):
        import scripts.eval_hledac_code_rag  # noqa: F401
        assert hasattr(scripts.eval_hledac_code_rag, "QUERIES")
        assert len(scripts.eval_hledac_code_rag.QUERIES) == 18

    def test_query_ids_match_expected_map(self):
        import scripts.eval_hledac_code_rag as mod
        query_ids = {q["id"] for q in mod.QUERIES}
        expected_ids = set(mod.EXPECTED_FILE_MAP.keys())
        assert query_ids == expected_ids, f"Query IDs mismatch: extra={query_ids - expected_ids}, missing={expected_ids - query_ids}"


# --------------------------------------------------------------------------- #
# Real eval tests (only when env var is set)                                  #
# --------------------------------------------------------------------------- #

REAL_HLEDAC_PATH = "/Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal"
REAL_PALACE_PATH = "/tmp/mempalace_hledac_eval_smoke"


class TestRealEval:
    """Run real eval against Hledac. Only active when MEMPALACE_RUN_REAL_HLEDAC_EVAL=1."""

    @pytest.mark.skipif(
        os.environ.get("MEMPALACE_RUN_REAL_HLEDAC_EVAL") != "1",
        reason="Set MEMPALACE_RUN_REAL_HLEDAC_EVAL=1 to run real eval against Hledac",
    )
    def test_real_eval_limit3(self):
        import asyncio
        import scripts.eval_hledac_code_rag as eval_mod

        rc = asyncio.run(eval_mod._eval_project(
            project_path=REAL_HLEDAC_PATH,
            palace_path=REAL_PALACE_PATH,
            do_mine=True,
            max_queries=3,
            report_json=None,
        ))
        assert rc in (0, 1), f"Eval exited with unexpected code {rc} (0=pass, 1=fail)"

    @pytest.mark.skipif(
        os.environ.get("MEMPALACE_RUN_REAL_HLEDAC_EVAL") != "1",
        reason="Set MEMPALACE_RUN_REAL_HLEDAC_EVAL=1 to run real eval against Hledac",
    )
    def test_real_eval_full_run(self):
        import asyncio
        import scripts.eval_hledac_code_rag as eval_mod

        report_path = _REPO_ROOT / "probe_eval" / "hledac_code_rag_eval.json"

        rc = asyncio.run(eval_mod._eval_project(
            project_path=REAL_HLEDAC_PATH,
            palace_path=REAL_PALACE_PATH,
            do_mine=True,
            max_queries=None,
            report_json=str(report_path),
        ))
        assert rc in (0, 1)
        # Verify report was written
        assert report_path.exists(), "Report JSON was not written"