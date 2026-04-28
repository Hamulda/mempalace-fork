"""
tests/test_truth_invariants.py

Truth-invariant checks after post-refactor cleanup.
These are static checks — no network, no daemon required.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


class TestRetrievalPlannerCanonical:
    """Verify retrieval_planner.classify_query is the canonical classifier."""

    def test_retrieval_planner_classify_query_exists(self):
        """retrieval_planner.classify_query must be importable."""
        from mempalace import retrieval_planner

        assert hasattr(retrieval_planner, "classify_query"), (
            "retrieval_planner.classify_query not found — canonical classifier missing"
        )

    def test_retrieval_planner_classify_query_returns_literal(self):
        """Canonical classify_query must return a typed literal, not bare str."""
        from mempalace.retrieval_planner import classify_query

        result = classify_query("def foo():")
        assert isinstance(result, str), "classify_query must return str"
        # Validate it returns one of the known categories
        valid = {"path", "symbol", "code_exact", "code_semantic", "memory", "mixed"}
        assert result in valid, f"Unexpected category {result!r}"

    def test_searcher_classify_query_drift_check(self):
        """
        searcher.py has its own classify_query at line 787.
        Flag if it diverges from retrieval_planner.classify_query behavior.
        Both must agree on at least the 6 category labels.
        """
        searcher_path = REPO_ROOT / "mempalace" / "searcher.py"
        content = searcher_path.read_text()

        # Extract searcher's classify_query body
        lines = content.split("\n")
        start = next((i for i, l in enumerate(lines) if "def classify_query" in l), None)
        assert start is not None, "searcher.classify_query not found"

        # Read until next top-level def
        end = start + 1
        while end < len(lines) and not lines[end].startswith(("def ", "class ", "async def ")):
            end += 1
        body = "\n".join(lines[start:end])

        # Ensure searcher mentions all 6 categories (drift detection)
        categories = ["path", "symbol", "code_exact", "code_semantic", "memory", "mixed"]
        found = [cat for cat in categories if cat in body]
        missing = set(categories) - set(found)
        assert not missing, (
            f"searcher.classify_query missing categories: {missing}. "
            "Drift from canonical retrieval_planner.classify_query."
        )

    def test_classify_query_behavior_agreement(self):
        """
        searcher.classify_query and retrieval_planner.classify_query must agree
        on a representative set of queries (behavior invariant, not just text).
        """
        from mempalace.retrieval_planner import classify_query as rp_classify
        from mempalace.searcher import classify_query as sr_classify

        test_queries = [
            "src/auth.py",
            "AuthManager",
            "class UserAuth",
            "how does login verify credentials",
            "memory of past sessions",
            "general knowledge question",
        ]
        for q in test_queries:
            rp_result = rp_classify(q)
            sr_result = sr_classify(q)
            assert rp_result == sr_result, (
                f"Query {q!r}: retrieval_planner={rp_result!r}, searcher={sr_result!r} — must agree"
            )


class TestPluginConfig:
    """Verify .claude-plugin/.mcp.json points to correct MCP endpoint."""

    def test_mcp_json_url_is_localhost_8765(self):
        """MCP URL must be http://127.0.0.1:8765/mcp."""
        mcp_json_path = REPO_ROOT / ".claude-plugin" / ".mcp.json"
        assert mcp_json_path.exists(), ".claude-plugin/.mcp.json not found"

        data = json.loads(mcp_json_path.read_text())
        mempalace_cfg = data.get("mempalace", {})
        assert mempalace_cfg.get("transport") == "http"
        assert mempalace_cfg.get("url") == "http://127.0.0.1:8765/mcp", (
            f"MCP URL mismatch: {mempalace_cfg.get('url')!r}"
        )


class TestPyprojectConsistency:
    """Verify pyproject.toml python target is not contradictory."""

    def test_requires_python_not_empty(self):
        """requires-python must be set."""
        pyproject = REPO_ROOT / "pyproject.toml"
        content = pyproject.read_text()
        m = re.search(r'^requires-python\s*=\s*["\'](.+?)["\']', content, re.MULTILINE)
        assert m, "requires-python not found in pyproject.toml"
        value = m.group(1)
        assert value, "requires-python is empty"

    def test_ruff_target_version_not_wrong(self):
        """
        ruff target-version must not imply a Python version
        that contradicts requires-python floor.
        """
        pyproject = REPO_ROOT / "pyproject.toml"
        content = pyproject.read_text()

        # Extract requires-python floor
        m = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', content, re.MULTILINE)
        assert m, "requires-python floor not parseable"
        floor_min = int(m.group(1)), int(m.group(2))

        # Extract ruff target-version
        m2 = re.search(r'target-version\s*=\s*"py(\d+)"', content, re.MULTILINE)
        assert m2, "ruff target-version not found"
        target = int(m2.group(1)), 0

        assert target >= floor_min, (
            f"ruff target-version py{target[0]} is below requires-python floor "
            f">={floor_min[0]}.{floor_min[1]}"
        )


class TestDoctorCommand:
    """Verify doctor command does not contain invalid stale-session cleanup."""

    def test_doctor_no_manual_stale_prune_instruction(self):
        """
        doctor.md must not contain manual stale-session prune instructions.
        Sessions auto-expire via TTL (6h) — manual prune is unnecessary.
        """
        doctor_md = REPO_ROOT / ".claude-plugin" / "commands" / "doctor.md"
        assert doctor_md.exists(), "commands/doctor.md not found"

        content = doctor_md.read_text()
        # Flag if a find/-mtime/+6h/-delete prune instruction appears
        assert not re.search(
            r"find.*\.session.*-mtime\s*\+\d+.*delete", content, re.IGNORECASE
        ), "doctor.md contains manual stale-session prune instruction"
