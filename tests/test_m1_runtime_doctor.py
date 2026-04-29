"""Tests for m1_runtime_doctor.py — smoke tests only, no heavy model loads."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys


def test_doctor_script_imports_without_crash():
    """Script must import without triggering heavy model loads."""
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.m1_runtime_doctor"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Import failed: {result.stderr}"


def test_doctor_json_output_has_required_keys():
    """Doctor --json output must contain all required diagnostic keys.

    Note: Doctor exits 1 if swap is detected (ABORT condition), but still
    outputs valid JSON with all required keys. We only require that the
    JSON is parseable and contains all keys.
    """
    repo_root = pathlib.Path(__file__).parent.parent.resolve()
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "m1_runtime_doctor.py"), "--json"],
        capture_output=True,
        text=True,
    )
    # Doctor may exit 1 if swap is active (ABORT), but JSON is still valid
    report = json.loads(result.stdout)
    required_keys = [
        "python_version",
        "python_executable",
        "platform_system",
        "proc_rss_mb",
        "available_mem_mb",
        "swap_detected",
        "lancedb_version",
        "pyarrow_version",
        "fastmcp_available",
        "fastembed_available",
        "mlx_available",
        "sentence_transformers_available",
        "chromadb_in_modules",
        "default_backend",
        "palace_path",
        "lance_collection_count",
        "fts5_count",
        "symbol_index_stats",
    ]
    for key in required_keys:
        assert key in report, f"Missing key: {key}"


def test_chromadb_not_in_modules_after_import():
    """After doctor runs, chromadb must NOT be in sys.modules."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import scripts.m1_runtime_doctor; import sys; print('chromadb' in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # Should print False
    assert result.stdout.strip() == "False", "chromadb should not be imported"


if __name__ == "__main__":
    import os
    import pytest
    pytest.main([__file__, "-v"])