#!/usr/bin/env python3
"""
test_mining_budgets.py — Budget enforcement for mempalace mine.

Cases:
- max_files via limit param stops after N files;
- max_chunks budget stops gracefully;
- max_seconds budget stops gracefully;
- partial run leaves palace non-empty if some chunks were written;
- budget abort does not corrupt FTS5 or SymbolIndex.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _mine_via_subprocess(project_dir: str, palace_path: str, *, limit: int = 0,
                          max_files: int = 0, max_chunks: int = 0,
                          max_seconds: float = 0.0,
                          embed_fallback: bool = True) -> dict:
    """Run mine() in a fresh subprocess so env vars and module state are clean."""
    env = dict(os.environ)
    if embed_fallback:
        env["MEMPALACE_EMBED_FALLBACK"] = "1"
    if max_files > 0:
        env["MEMPALACE_MINE_MAX_FILES"] = str(max_files)
    if max_chunks > 0:
        env["MEMPALACE_MINE_MAX_CHUNKS"] = str(max_chunks)
    if max_seconds > 0:
        env["MEMPALACE_MINE_MAX_SECONDS"] = str(max_seconds)

    code = f"""
import sys
sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))})
from mempalace.miner import mine
result = mine({repr(project_dir)}, {repr(palace_path)}, limit={limit})
print("RESULT:", result)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60,
        env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    if result.returncode != 0:
        raise RuntimeError(f"mine subprocess failed: {result.stderr[:500]}")
    # Parse "RESULT: {...}" from stdout
    for line in result.stdout.splitlines():
        if line.startswith("RESULT:"):
            import ast
            return ast.literal_eval(line[len("RESULT:"):].strip())
    raise RuntimeError(f"no RESULT line in mine output: {result.stdout[:500]}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def tiny_project(tmp_path):
    """Create a tiny project with 5 Python files for budget testing."""
    src = tmp_path / "src"
    src.mkdir()
    # Each file has 1 function + 1 class = 2 structural chunks per file
    for i in range(5):
        (src / f"module_{i}.py").write_text(
            f'"""Module {i}."""\n\n'
            f'def func_{i}():\n    """Function {i}."""\n    return {i}\n\n'
            f'class Class_{i}:\n    """Class {i}."""\n    pass\n'
        )
    (tmp_path / "mempalace.yaml").write_text(
        "wing: test\nrooms:\n  - name: general\n    description: General\n"
    )
    return tmp_path


@pytest.fixture
def fresh_palace(tmp_path):
    p = tmp_path / "palace"
    p.mkdir()
    return p


# --------------------------------------------------------------------------- #
# Tests: budget report structure
# --------------------------------------------------------------------------- #

class TestBudgetReport:
    """Budget report has all required fields."""

    def test_report_fields_complete(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace))
        for field in ("completed", "abort_reason", "files_seen", "files_processed",
                      "chunks_written", "elapsed_s", "swap_mb"):
            assert field in r, f"missing field: {field}"

    def test_swap_mb_is_float_or_none(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace))
        assert r["swap_mb"] is None or isinstance(r["swap_mb"], float)


# --------------------------------------------------------------------------- #
# Tests: limit param (CLI-level max files)
# --------------------------------------------------------------------------- #

class TestLimitParam:
    """limit param to mine() controls max files processed."""

    def test_limit_stops_at_3(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace), limit=3)
        assert r["completed"] is True
        assert r["files_processed"] == 3
        assert r["files_seen"] == 5

    def test_limit_zero_unlimited(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace), limit=0)
        assert r["completed"] is True
        assert r["files_processed"] == 5


# --------------------------------------------------------------------------- #
# Tests: MEMPALACE_MINE_MAX_FILES env
# --------------------------------------------------------------------------- #

class TestMaxFilesEnv:
    """MEMPALACE_MINE_MAX_FILES env truncates scanned file list."""

    def test_max_files_env(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace), max_files=3)
        assert r["completed"] is True
        assert r["files_processed"] == 3
        assert r["files_seen"] == 5

    def test_max_files_zero_unlimited(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace), max_files=0)
        assert r["completed"] is True
        assert r["files_processed"] == 5


# --------------------------------------------------------------------------- #
# Tests: MEMPALACE_MINE_MAX_CHUNKS env
# --------------------------------------------------------------------------- #

class TestMaxChunksEnv:
    """MEMPALACE_MINE_MAX_CHUNKS env stops mining when chunk budget is hit."""

    def test_max_chunks_aborts(self, tiny_project, fresh_palace):
        # 2 files × ~2 chunks each = 4 chunks; budget=3 should trigger abort
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace),
                                  limit=2, max_chunks=3)
        assert r["completed"] is False
        assert r["abort_reason"] == "max_chunks"
        assert r["chunks_written"] <= 3

    def test_max_chunks_within_limit(self, tiny_project, fresh_palace):
        # 1 file × ~2 chunks; budget=3 should NOT trigger
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace),
                                  limit=1, max_chunks=3)
        assert r["completed"] is True
        assert r["chunks_written"] >= 2


# --------------------------------------------------------------------------- #
# Tests: MEMPALACE_MINE_MAX_SECONDS env
# --------------------------------------------------------------------------- #

class TestMaxSecondsEnv:
    """MEMPALACE_MINE_MAX_SECONDS env stops mining after wall-clock timeout."""

    def test_max_seconds_aborts(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace),
                                  limit=2, max_seconds=0.01)
        assert r["completed"] is False
        assert r["abort_reason"] == "max_seconds"
        assert r["elapsed_s"] < 5  # Should exit promptly

    def test_max_seconds_zero_unlimited(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace),
                                  max_seconds=0.0)
        assert r["completed"] is True
        assert r["elapsed_s"] >= 0


# --------------------------------------------------------------------------- #
# Tests: partial mining / no corruption
# --------------------------------------------------------------------------- #

class TestBudgetPartialMining:
    """Partial run leaves palace non-empty and intact."""

    def test_partial_leaves_data(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace),
                                  limit=2, max_chunks=3)
        assert r["completed"] is False
        # Palace should have some data
        palace_sqlite = fresh_palace / "mempalace.lance"
        assert palace_sqlite.exists()

    def test_symbol_index_readable_after_abort(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace),
                                  limit=2, max_chunks=3)
        assert r["completed"] is False
        si_path = fresh_palace / "symbol_index.sqlite3"
        if si_path.exists():
            import sqlite3
            conn = sqlite3.connect(str(si_path))
            cur = conn.execute("PRAGMA integrity_check")
            assert cur.fetchone()[0] == "ok"
            conn.close()

    def test_lance_readable_after_abort(self, tiny_project, fresh_palace):
        r = _mine_via_subprocess(str(tiny_project), str(fresh_palace),
                                  max_seconds=0.001, limit=1)
        assert r["completed"] is False
        from mempalace.config import MempalaceConfig
        from mempalace.backends import get_backend
        cfg = MempalaceConfig()
        backend = get_backend(cfg.backend)
        col = backend.get_collection(str(fresh_palace), cfg.collection_name, create=False)
        # Should not raise
        result = col.get(limit=100)
        assert "ids" in result
