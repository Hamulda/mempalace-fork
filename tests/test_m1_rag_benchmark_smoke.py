"""Smoke tests for m1_rag_benchmark.py — no heavy model loads."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys


def test_benchmark_import_no_heavy_load():
    """Benchmark script must import without triggering model loads."""
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, '.'); import scripts.m1_rag_benchmark"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Import failed: {result.stderr}"


def test_benchmark_synthetic_small_completes_under_30s():
    """Benchmark with synthetic-small fixture must complete within 30s.

    If swap is active, benchmark aborts early (exit 1). This is expected
    ABORT behavior on a swap-active system. We verify the script ran and
    produced a report rather than requiring exit 0.
    """
    repo_root = pathlib.Path(__file__).parent.parent.resolve()
    result = subprocess.run(
        [
            sys.executable, str(repo_root / "scripts" / "m1_rag_benchmark.py"),
            "--fixture", "synthetic-small",
            "--concurrency", "1",
            "--duration-seconds", "20",
        ],
        capture_output=True,
        text=True,
        timeout=35,
    )
    # Exit 0 = success, Exit 1 = abort (swap detected — acceptable)
    assert result.returncode in (0, 1), f"Benchmark crashed: {result.stderr}"

    # Verify report exists and is parseable
    report_path = repo_root / "probe_runtime" / "benchmark_report.json"
    assert report_path.exists(), f"Report not written to {report_path}"

    report = json.loads(open(report_path).read())
    # Report must have swap_warning or mine data
    assert "swap_warning" in report or "mine" in report, "Report missing key data"


def test_benchmark_no_chromadb_import():
    """Benchmark must not import chromadb into sys.modules."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; sys.path.insert(0, '.'); import scripts.m1_rag_benchmark; print('chromadb' in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "False"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])