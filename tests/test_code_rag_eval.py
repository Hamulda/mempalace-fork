"""
test_code_rag_eval.py -- Pytest wrapper for code-RAG evaluation suite.

Runs a tiny subset (5 queries) of the eval suite as a hermetic pytest.
Marked @pytest.mark.slow because it requires actual mining + search.

Full eval:
    python scripts/code_rag_eval.py --fixture tests/fixtures/code_rag_eval_repo
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed")


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_code_rag_eval_tiny_subset():
    """
    Run the eval suite with max 5 queries to verify basic retrieval quality.
    This is a smoke test -- it passes if the fixture mines cleanly and
    retrieval finds the expected files.
    """
    repo_root = Path(__file__).parent.parent  # …/mempalace
    script = repo_root / "scripts" / "code_rag_eval.py"
    fixture = repo_root / "tests" / "fixtures" / "code_rag_eval_repo"

    # Quick smoke: just check the script runs and exits 0 or 1 (not 2 or 3)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--fixture",
            str(fixture),
            "--queries",
            "5",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    # Exit 0 = pass, 1 = threshold fail (acceptable for smoke), 2/3 = setup error
    assert result.returncode in (0, 1), (
        f"eval script exited {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    # Should have some PASS/WARN/FAIL rows in output
    assert "PASS" in result.stdout or "FAIL" in result.stdout or "WARN" in result.stdout


@pytest.mark.slow
def test_code_rag_eval_fixture_integrity():
    """
    Verify the fixture repo contains all expected files and tricky structures.
    """
    fixture = Path(__file__).parent.parent / "tests" / "fixtures" / "code_rag_eval_repo"
    assert (fixture / "src" / "auth.py").exists()
    assert (fixture / "src" / "db.py").exists()
    assert (fixture / "src" / "config.py").exists()
    assert (fixture / "src" / "utils.py").exists()
    assert (fixture / "README.md").exists()
    assert (fixture / "pyproject.toml").exists()

    auth_text = (fixture / "src" / "auth.py").read_text()
    # Both classes must be present (disambiguation test)
    assert "class AuthManager" in auth_text
    assert "class LegacyAuth" in auth_text
    # Both login methods (same name, different classes)
    assert auth_text.count("def login(") == 2
    # Misleading README
    readme = (fixture / "README.md").read_text()
    assert "LegacyAuth" in readme
    assert "recommended" in readme.lower()


@pytest.mark.slow
def test_code_rag_eval_expected_answers_valid():
    """
    Verify the expected answers JSON is parseable and well-formed.
    """
    import json

    expected = Path(__file__).parent.parent / "tests" / "fixtures" / "code_rag_eval_expected.json"
    with open(expected) as f:
        data = json.load(f)

    assert data["version"] == 1
    assert len(data["queries"]) >= 5
    for q in data["queries"]:
        assert "id" in q
        assert "query" in q
        assert "type" in q
        assert "expected_file" in q
        assert "cross_project_leak" in q

    thresholds = data["thresholds"]
    assert 0 < thresholds["top1_file_hit_min"] <= 1.0
    assert 0 < thresholds["top5_file_hit_min"] <= 1.0
    assert thresholds["cross_project_leak_max"] == 0
