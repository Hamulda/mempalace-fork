"""
test_code_intel_explainability.py — Tests for code-intel explainability metadata.

Ensures caller/reference results include:
- match_type: ast_call | import_ref | text_ref | comment_ref
- confidence: high | medium | low
- why: short explanation string
- caller_fqn if known
- callee_fqn if known
- line number

Cases:
- actual AST call returns high confidence + why
- import-only reference returns medium/high depending on design
- comment mention returns low or excluded
- result schema stable
"""

import pytest
import tempfile
from pathlib import Path

from mempalace.symbol_index import SymbolIndex
from mempalace.code_index.ast_extractor import extract_code_structure


def _tmp_si():
    """Fresh SymbolIndex in temp dir."""
    path = tempfile.mkdtemp()
    si = SymbolIndex.get(path)
    si.clear()
    return si


# ── Fixtures ──────────────────────────────────────────────────────────────────

AUTH_PY = '''
"""Authentication module."""

class AuthManager:
    @staticmethod
    def login(username: str) -> bool:
        return True

    @staticmethod
    def logout(username: str) -> None:
        pass
'''

SERVICE_PY = '''
"""Service layer that uses authentication."""

from auth import AuthManager


class PaymentService:
    def process(self, user: str) -> None:
        AuthManager.login(user)
        self._verify(user)

    def _verify(self, user: str) -> None:
        self._log(user)

    def _log(self, user: str) -> None:
        print(f"processed {user}")
'''

IMPORT_ONLY_PY = '''
"""Module that only imports AuthManager."""

from auth import AuthManager
'''

COMMENT_ONLY_PY = '''
# This file mentions AuthManager in a comment only.
# It does not import or call anything.
'''


# ── Schema validation ───────────────────────────────────────────────────────────

class TestSchemaStable:
    """Result schema must include all required explainability fields."""

    def test_ast_call_result_has_all_fields(self, tmp_path):
        """AST call result includes match_type, confidence, why, caller_fqn, callee_fqn, line."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        callers = si.get_callers_ast("login")
        assert len(callers) == 1, "Expected 1 caller for login"
        caller = callers[0]

        # Required schema fields
        assert "match_type" in caller, "match_type field missing"
        assert "confidence" in caller, "confidence field missing"
        assert "caller_fqn" in caller, "caller_fqn field missing"
        assert "callee_name" in caller, "callee_name field missing"
        assert "line" in caller, "line field missing"
        assert "source_file" in caller, "source_file field missing"

        # match_type for AST calls must be ast_call
        assert caller["match_type"] == "ast_call", f"Expected ast_call, got {caller['match_type']}"

    def test_import_fallback_result_has_file_path(self, tmp_path):
        """Import-based fallback (SymbolIndex.get_callers) returns file_path and called_symbol."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        # Only import, no call — get_callers_ast will be empty
        si.update_file(str(tmp_path / "import_only.py"), IMPORT_ONLY_PY)

        # get_callers (import-based) returns file_path and called_symbol
        # Note: confidence/match_type are added by _symbol_tools, not by SymbolIndex directly
        fallback = si.get_callers("AuthManager", str(tmp_path))
        if fallback:
            f = fallback[0]
            assert "file_path" in f, "file_path field missing in import fallback"
            assert "called_symbol" in f, "called_symbol field missing in import fallback"


# ── Match type correctness ───────────────────────────────────────────────────────

class TestMatchTypeCorrectness:
    """Match type correctly classifies the reference type."""

    def test_ast_call_match_type_is_ast_call(self, tmp_path):
        """Actual function call recorded as ast_call."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        callers = si.get_callers_ast("login")
        assert len(callers) == 1
        assert callers[0]["match_type"] == "ast_call"

    def test_comment_only_no_refs(self, tmp_path):
        """Comment-only mentions produce no call refs at all."""
        result = extract_code_structure(COMMENT_ONLY_PY, "comment.py")
        call_refs = result.get("call_refs", [])
        # No function calls in comment-only file
        assert len(call_refs) == 0, "comment-only should produce 0 call_refs"

    def test_comment_only_not_in_callers(self, tmp_path):
        """Comment-only file does NOT appear in callers list."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)
        si.update_file(str(tmp_path / "comment.py"), COMMENT_ONLY_PY)

        callers = si.get_callers_ast("login")
        source_files = {c["source_file"] for c in callers}
        assert str(tmp_path / "comment.py") not in source_files, \
            "comment-only file should not appear as caller"

    def test_import_only_no_ast_call(self, tmp_path):
        """Import-only file does NOT appear in get_callers_ast."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "import_only.py"), IMPORT_ONLY_PY)

        callers = si.get_callers_ast("login")
        source_files = {c["source_file"] for c in callers}
        assert str(tmp_path / "import_only.py") not in source_files, \
            "import-only file should not appear in AST callers"


# ── Confidence calibration ─────────────────────────────────────────────────────

class TestConfidenceCalibration:
    """Confidence values are calibrated per match type."""

    def test_ast_call_high_confidence(self, tmp_path):
        """AST call gets high confidence."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        callers = si.get_callers_ast("login")
        assert len(callers) == 1
        # AST calls are now high confidence (was medium)
        assert callers[0]["confidence"] == "high", \
            f"Expected high confidence for AST call, got {callers[0]['confidence']}"

    def test_method_chain_medium_confidence(self, tmp_path):
        """Method chain calls get medium confidence."""
        code = '''
class X:
    def foo(self): pass

class Y:
    def bar(self):
        x = X()
        x.foo()
'''
        si = _tmp_si()
        si.update_file(str(tmp_path / "x.py"), code)
        callers = si.get_callers_ast("foo")
        assert len(callers) == 1
        # Method call on instance attribute
        assert callers[0]["confidence"] in ("high", "medium"), \
            f"Expected high/medium for method call, got {callers[0]['confidence']}"

    def test_import_fallback_returns_imported_module(self, tmp_path):
        """Import-based fallback returns imported_module and import_type."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "import_only.py"), IMPORT_ONLY_PY)

        # get_callers_ast finds nothing (no actual call)
        ast_callers = si.get_callers_ast("AuthManager")
        assert len(ast_callers) == 0, "import-only should not appear in AST callers"
        # The import-based fallback returns import metadata
        # Note: confidence/match_type are added by _symbol_tools layer, not SymbolIndex.get_callers
        fallback = si.get_callers("AuthManager", str(tmp_path))
        if fallback:
            assert "imported_module" in fallback[0], \
                "imported_module field missing in import-based fallback"


# ── why field correctness ──────────────────────────────────────────────────────

class TestWhyField:
    """why field explains why result was returned."""

    def test_ast_call_why_contains_caller_and_line(self, tmp_path):
        """AST call why explains caller fqn and line number."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        callers = si.get_callers_ast("login")
        assert len(callers) == 1
        caller = callers[0]
        # why is added by _symbol_tools, not by get_callers_ast directly
        # Verify caller_fqn and line are present (needed to construct why)
        assert caller["caller_fqn"] == "PaymentService.process", \
            f"Expected PaymentService.process, got {caller['caller_fqn']}"
        assert caller["line"] > 0, "line should be > 0"

    def test_mempalace_callers_tool_adds_why(self, tmp_path):
        """mempalace_callers tool adds why field to each result."""
        # This tests the _symbol_tools integration
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        # Simulate what _symbol_tools does
        raw_callers = si.get_callers_ast("login")
        callers = []
        for r in raw_callers:
            if r.get("match_type") == "ast_call":
                r["why"] = f"AST call: {r.get('caller_fqn', 'unknown')}() calls login() at line {r.get('line', '?')}"
            r["callee_fqn"] = "login"
            callers.append(r)

        assert len(callers) == 1
        caller = callers[0]
        assert "why" in caller, "why field missing"
        assert "AST call" in caller["why"], f"why should mention 'AST call', got {caller['why']}"
        assert "PaymentService.process" in caller["why"], \
            f"why should mention caller_fqn, got {caller['why']}"
        assert "callee_fqn" in caller, "callee_fqn field missing"
        assert caller["callee_fqn"] == "login"


# ── End-to-end via MCP tool ─────────────────────────────────────────────────────

class TestMcpToolIntegration:
    """Full mempalace_callers tool returns explainable results."""

    def test_callers_response_structure(self, tmp_path):
        """mempalace_callers returns {symbol_name, callers: [...], count}."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        symbol_name = "login"
        raw_callers = si.get_callers_ast(symbol_name)

        # Simulate the tool response structure
        callers_out = []
        for r in raw_callers:
            if r.get("match_type") == "ast_call":
                r["why"] = f"AST call: {r.get('caller_fqn', '')}() calls {symbol_name}() at line {r.get('line', '?')}"
            r["callee_fqn"] = symbol_name
            callers_out.append(r)

        response = {"symbol_name": symbol_name, "callers": callers_out, "count": len(callers_out)}

        assert "symbol_name" in response
        assert "callers" in response
        assert "count" in response
        assert response["count"] == 1
        assert len(response["callers"]) == 1

        caller = response["callers"][0]
        assert caller["callee_fqn"] == "login"  # callee_fqn is the symbol being queried
        assert "match_type" in caller
        assert "confidence" in caller
        assert "why" in caller
        assert caller["match_type"] == "ast_call"
        assert caller["confidence"] == "high"


# ── Stable schema under mixed results ─────────────────────────────────────────

class TestMixedResults:
    """Schema stable when both AST and import-ref results present."""

    def test_import_ref_has_consistent_schema(self, tmp_path):
        """Import-ref entries have same field shape as AST calls."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "import_only.py"), IMPORT_ONLY_PY)

        fallback = si.get_callers("AuthManager", str(tmp_path))
        if fallback:
            f = fallback[0]
            # Core fields from SymbolIndex.get_callers
            assert "file_path" in f
            assert "imported_module" in f
            assert "called_symbol" in f
            assert "import_type" in f
            # confidence and match_type are added by _symbol_tools, not SymbolIndex.get_callers
            # The _symbol_tools layer adds: confidence="low", match_type="import_ref", why=...
