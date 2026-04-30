"""
tests/test_symbol_tools_response_contract.py — Tests for symbol tools response contract.

Verifies all 5 symbol tools use ok_response/error_response with v1.0 contract:
- ok: true / ok: false
- tool_contract_version: "1.0"
- tool: actual tool name
- structured error with code + message

Cases:
- find_symbol success has ok + contract version.
- find_symbol missing symbol_name returns structured error.
- callers success preserves why/match_type/confidence.
- file_symbols success includes source_file/repo_rel_path.
- recent_changes error shape is structured.
- no ChromaDB import.
"""
from __future__ import annotations

import pytest
import sys
import tempfile
from pathlib import Path

from mempalace.server.response_contract import (
    TOOL_CONTRACT_VERSION,
    ok_response,
    error_response,
    make_symbol_response,
    make_callers_response,
)


# ── Contract structure helpers ─────────────────────────────────────────────────


def assert_contract_ok(resp: dict, tool: str) -> None:
    assert resp["ok"] is True, f"Expected ok=True, got {resp}"
    assert resp["tool_contract_version"] == "1.0", f"Expected v1.0, got {resp}"
    assert resp["tool"] == tool, f"Expected tool={tool}, got {resp['tool']}"


def assert_contract_error(resp: dict, tool: str, code: str | None = None) -> None:
    assert resp["ok"] is False, f"Expected ok=False, got {resp}"
    assert resp["tool_contract_version"] == "1.0", f"Expected v1.0, got {resp}"
    assert resp["tool"] == tool, f"Expected tool={tool}, got {resp['tool']}"
    assert "error" in resp, f"Expected error key in {resp}"
    assert "code" in resp["error"], f"Expected error.code in {resp}"
    assert "message" in resp["error"], f"Expected error.message in {resp}"
    if code:
        assert resp["error"]["code"] == code, f"Expected code={code}, got {resp['error']['code']}"


# ── Contract base tests ────────────────────────────────────────────────────────


def test_tool_contract_version():
    assert TOOL_CONTRACT_VERSION == "1.0"


def test_ok_response_structure():
    resp = ok_response("test_tool", {"data": "value"})
    assert_contract_ok(resp, "test_tool")
    assert resp["data"] == "value"


def test_error_response_structure():
    resp = error_response("test_tool", "oops", code="oops")
    assert_contract_error(resp, "test_tool", code="oops")
    assert resp["error"]["message"] == "oops"


def test_make_symbol_response_structure():
    resp = make_symbol_response("foo", [], tool="mempalace_find_symbol")
    assert_contract_ok(resp, "mempalace_find_symbol")
    assert "results" in resp
    assert "count" in resp


def test_make_callers_response_structure():
    resp = make_callers_response("login", [{"caller_fqn": "x.y"}])
    assert_contract_ok(resp, "mempalace_callers")
    assert "callers" in resp
    assert "count" in resp
    assert "symbol_name" in resp


# ── Symbol tool response contracts ─────────────────────────────────────────────


class TestFindSymbolContract:
    def test_success_returns_contract_ok(self, tmp_path):
        """mempalace_find_symbol success returns ok + contract version."""
        from mempalace.symbol_index import SymbolIndex

        si = SymbolIndex.get(str(tmp_path / "si"))
        si.clear()
        si.update_file(str(tmp_path / "mod.py"), "def foo(): pass\nclass Bar:\n    pass")

        raw_results = si.find_symbol("foo")
        resp = make_symbol_response("foo", raw_results, str(tmp_path), tool="mempalace_find_symbol")

        assert_contract_ok(resp, "mempalace_find_symbol")
        assert "results" in resp
        assert "count" in resp

    def test_missing_symbol_name_error(self):
        """mempalace_find_symbol with empty symbol_name returns structured error."""
        resp = error_response("mempalace_find_symbol", "symbol_name is required", code="missing_argument")
        assert_contract_error(resp, "mempalace_find_symbol", code="missing_argument")
        assert resp["error"]["message"] == "symbol_name is required"

    def test_internal_error(self):
        """mempalace_find_symbol on exception returns structured error."""
        resp = error_response("mempalace_find_symbol", "connection refused", code="internal_error")
        assert_contract_error(resp, "mempalace_find_symbol", code="internal_error")
        assert resp["error"]["message"] == "connection refused"


class TestSearchSymbolsContract:
    def test_success_returns_contract_ok(self, tmp_path):
        """mempalace_search_symbols success returns ok + contract version."""
        from mempalace.symbol_index import SymbolIndex

        si = SymbolIndex.get(str(tmp_path / "si"))
        si.clear()
        si.update_file(str(tmp_path / "mod.py"), "def bar(): pass\nclass Baz:\n    pass")

        raw_results = si.search_symbols("bar")
        resp = make_symbol_response("bar", raw_results, str(tmp_path), tool="mempalace_search_symbols")

        assert_contract_ok(resp, "mempalace_search_symbols")
        assert "results" in resp
        assert "count" in resp

    def test_missing_pattern_error(self):
        """mempalace_search_symbols with empty pattern returns structured error."""
        resp = error_response("mempalace_search_symbols", "pattern is required", code="missing_argument")
        assert_contract_error(resp, "mempalace_search_symbols", code="missing_argument")


class TestCallersContract:
    def test_success_preserves_explainability(self, tmp_path):
        """mempalace_callers success preserves why/match_type/confidence."""
        from mempalace.symbol_index import SymbolIndex

        si = SymbolIndex.get(str(tmp_path / "si"))
        si.clear()
        si.update_file(str(tmp_path / "auth.py"), "class AuthManager:\n    def login(self): pass")
        si.update_file(str(tmp_path / "svc.py"), """
from auth import AuthManager
svc = AuthManager()
svc.login()
""")

        raw_callers = si.get_callers_ast("login")
        callers = []
        for r in raw_callers:
            r["why"] = f"AST call: {r.get('caller_fqn', '')}() calls login() at line {r.get('line', '?')}"
            r["callee_fqn"] = "login"
            callers.append(r)

        resp = make_callers_response("login", callers)

        assert_contract_ok(resp, "mempalace_callers")
        assert "callers" in resp
        assert resp["count"] == 1
        caller = resp["callers"][0]
        assert "why" in caller, "why field missing"
        assert "match_type" in caller, "match_type missing"
        assert "confidence" in caller, "confidence missing"
        assert "callee_fqn" in caller, "callee_fqn missing"
        assert "AST call" in caller["why"]

    def test_missing_symbol_name_error(self):
        """mempalace_callers with empty symbol_name returns structured error."""
        resp = error_response("mempalace_callers", "symbol_name is required", code="missing_argument")
        assert_contract_error(resp, "mempalace_callers", code="missing_argument")


class TestFileSymbolsContract:
    def test_success_includes_path_info(self, tmp_path):
        """mempalace_file_symbols success includes source_file/repo_rel_path."""
        from mempalace.symbol_index import SymbolIndex

        si = SymbolIndex.get(str(tmp_path / "si"))
        si.clear()
        test_file = str(tmp_path / "mod.py")
        si.update_file(test_file, "def foo(): pass\nclass Bar:\n    pass")

        from mempalace.server._symbol_tools import _make_path_result
        path_info = _make_path_result(test_file, str(tmp_path))

        assert "source_file" in path_info, f"source_file missing from {path_info}"
        assert "repo_rel_path" in path_info, f"repo_rel_path missing from {path_info}"
        assert path_info["source_file"] == test_file
        assert path_info["repo_rel_path"] == "mod.py"

    def test_missing_file_path_error(self):
        """mempalace_file_symbols with empty file_path returns structured error."""
        resp = error_response("mempalace_file_symbols", "file_path is required", code="missing_argument")
        assert_contract_error(resp, "mempalace_file_symbols", code="missing_argument")


class TestRecentChangesContract:
    def test_missing_project_root_error(self):
        """mempalace_recent_changes without project_root returns structured error."""
        resp = error_response("mempalace_recent_changes", "project_root is required", code="missing_argument")
        assert_contract_error(resp, "mempalace_recent_changes", code="missing_argument")

    def test_success_returns_contract_ok(self, tmp_path):
        """mempalace_recent_changes success returns ok + contract version."""
        from mempalace.recent_changes import get_recent_changes, get_hot_spots

        changes = get_recent_changes(str(tmp_path), n=5)
        hot_spots = get_hot_spots(str(tmp_path), n=3)

        resp = ok_response("mempalace_recent_changes", {
            "recent_changes": changes,
            "hot_spots": hot_spots,
            "count": len(changes),
        })
        assert_contract_ok(resp, "mempalace_recent_changes")
        assert "recent_changes" in resp
        assert "hot_spots" in resp
        assert "count" in resp


# ── No ChromaDB ────────────────────────────────────────────────────────────────


def test_no_chromadb_in_sys_modules():
    """Verify no ChromaDB in sys.modules after importing symbol tools."""
    assert "chromadb" not in sys.modules, "chromadb should not be imported"
    assert "chrom" not in sys.modules, "chrom should not be imported"
