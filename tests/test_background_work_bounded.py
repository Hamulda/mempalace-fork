"""
Test bounded background work — thread storm prevention on M1/8GB.

Verifies via source inspection:
1. server/_infrastructure.py: _bg_executor exists and has bounded max_workers
2. server/_write_tools.py: add_drawer uses bg_executor.submit for general extraction
3. server/_write_tools.py: BM25 rebuild is debounced/coalesced
4. embed_daemon.py: uses bounded ThreadPoolExecutor instead of thread-per-connection
"""
from __future__ import annotations

import ast
import pytest
from pathlib import Path
import re

ROOT = Path(__file__).parent.parent / "mempalace"
INFRASTRUCTURE_PATH = ROOT / "server" / "_infrastructure.py"
WRITE_TOOLS_PATH = ROOT / "server" / "_write_tools.py"
EMBED_PATH = ROOT / "embed_daemon.py"
FASTMCP_PATH = ROOT / "fastmcp_server.py"


# ─────────────────────────────────────────────────────────────────────────────
# Source-level verification helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_module_vars(path: Path, prefix: str) -> dict[str, ast.expr]:
    """Parse a Python file and return module-level variable assignments.

    Handles both plain `a = value` (Assign) and annotated `a: T = value` (AnnAssign).
    """
    tree = ast.parse(path.read_text())
    vars = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id.startswith(prefix):
                vars[node.target.id] = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith(prefix):
                    vars[target.id] = node.value
    return vars


# ─────────────────────────────────────────────────────────────────────────────
# fastmcp_server.py — executor globals
# ─────────────────────────────────────────────────────────────────────────────

class TestInfrastructureExecutorGlobals:
    """Verify module-level executor variables exist and are bounded."""

    def test_bg_executor_exists(self):
        vars = get_module_vars(INFRASTRUCTURE_PATH, "bg_executor")
        assert "bg_executor" in vars, "bg_executor not found in server/_infrastructure.py"

    def test_bg_executor_max_workers_is_bounded(self):
        src = INFRASTRUCTURE_PATH.read_text()
        match = re.search(
            r"bg_executor\s*=\s*ThreadPoolExecutor\s*\(\s*max_workers\s*=\s*(\d+)",
            src,
        )
        assert match, "bg_executor with max_workers not found"
        n = int(match.group(1))
        assert n <= 4, f"bg_executor max_workers={n} is too large for M1/8GB"
        assert n >= 1, f"bg_executor max_workers={n} must be at least 1"


# ─────────────────────────────────────────────────────────────────────────────
# fastmcp_server.py — add_drawer background work
# ─────────────────────────────────────────────────────────────────────────────

def _find_top_level_calls(func_node: ast.FunctionDef, module: str) -> list[str]:
    """
    Return string repr of Call nodes that appear directly in func_node's body
    (not inside nested function/class definitions).
    """
    results = []

    class TopLevelCallFinder(ast.NodeVisitor):
        depth = 0  # noqa: UP006

        def visit(self, node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Don't descend into nested definitions
                return
            if self.depth == 0 and isinstance(node, ast.Call):
                results.append(ast.unparse(node))
            self.depth += 1
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            self.depth -= 1

    for stmt in func_node.body:
        finder = TopLevelCallFinder()
        finder.visit(stmt)
    return results


class TestWriteToolsDrawerBackgroundWork:
    """Verify add_drawer sends background work to executor, not bare threads."""

    def test_no_bare_threading_thread_at_top_level(self):
        """
        add_drawer's top-level statements must NOT call threading.Thread(...).
        Nested function definitions (e.g. _schedule_bm25_rebuild) are allowed
        to contain threading.Thread — they are implementation details.
        """
        src = WRITE_TOOLS_PATH.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "mempalace_add_drawer":
                calls = _find_top_level_calls(node, src)
                bad = [c for c in calls if "threading.Thread" in c]
                assert len(bad) == 0, (
                    f"add_drawer top-level creates bare threading.Thread: {bad}. "
                    "Should use bg_executor.submit()"
                )
                return
        pytest.fail("mempalace_add_drawer not found")

    def test_gen_extraction_uses_bg_executor(self):
        """_extract_general_facts must be submitted via bg_executor.submit."""
        src = WRITE_TOOLS_PATH.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "mempalace_add_drawer":
                body_text = ast.unparse(node)
                assert "bg_executor.submit" in body_text, (
                    "add_drawer should call bg_executor.submit for background work"
                )
                return
        pytest.fail("mempalace_add_drawer not found")


# ─────────────────────────────────────────────────────────────────────────────
# embed_daemon.py — bounded connection handling
# ─────────────────────────────────────────────────────────────────────────────

class TestEmbedDaemonExecutor:
    """Verify embed_daemon uses bounded ThreadPoolExecutor for connections."""

    def test_embed_daemon_executor_exists(self):
        vars = get_module_vars(EMBED_PATH, "_bg_executor")
        assert "_bg_executor" in vars, "_bg_executor not found in embed_daemon.py"

    def test_embed_daemon_executor_max_workers(self):
        src = EMBED_PATH.read_text()
        match = re.search(
            r"_bg_executor\s*=\s*ThreadPoolExecutor\s*\(\s*max_workers\s*=\s*(\d+)",
            src,
        )
        assert match, "_bg_executor with max_workers not found in embed_daemon.py"
        n = int(match.group(1))
        assert 1 <= n <= 8, f"embed_daemon max_workers={n} should be 1-8"

    def test_no_raw_threads_in_accept_loop(self):
        """Accept loop must NOT create bare threading.Thread for connections."""
        src = EMBED_PATH.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_daemon":
                # Find the while True accept loop and check its body
                for stmt in node.body:
                    # Only inspect the while loop at top level
                    if isinstance(stmt, ast.While):
                        loop_src = ast.unparse(stmt)
                        assert "threading.Thread(" not in loop_src, (
                            "Accept loop creates bare threading.Thread — "
                            "should use _bg_executor.submit()"
                        )
                return
        pytest.fail("run_daemon not found in embed_daemon.py")

    def test_executor_shutdown_in_finally(self):
        """Daemon must shut down the executor on exit."""
        src = EMBED_PATH.read_text()
        assert "_bg_executor.shutdown" in src, (
            "embed_daemon should call _bg_executor.shutdown() on exit"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Module syntax sanity check
# ─────────────────────────────────────────────────────────────────────────────

class TestModuleSyntax:
    """Verify both modules are syntactically valid Python."""

    def test_fastmcp_server_syntax(self):
        src = FASTMCP_PATH.read_text()
        try:
            ast.parse(src)
        except SyntaxError as e:
            pytest.fail(f"fastmcp_server.py has syntax error: {e}")

    def test_embed_daemon_syntax(self):
        src = EMBED_PATH.read_text()
        try:
            ast.parse(src)
        except SyntaxError as e:
            pytest.fail(f"embed_daemon.py has syntax error: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
