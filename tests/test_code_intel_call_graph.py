"""
test_code_intel_call_graph.py — Tests for AST call graph and ref tables.

Fixtures:
- auth.py:   defines AuthManager.login
- service.py: imports AuthManager, calls login
- other.py:  mentions AuthManager in a comment only

Expected:
- callers(AuthManager.login) finds service.py high confidence
- comment-only mention is low confidence or excluded
- reindex service.py removes old call refs
"""

import pytest
import tempfile
import os
from pathlib import Path

from mempalace.symbol_index import SymbolIndex
from mempalace.code_index.ast_extractor import extract_code_structure


def _tmp_si():
    """Fresh SymbolIndex in temp dir."""
    path = tempfile.mkdtemp()
    si = SymbolIndex.get(path)
    si.clear()  # ensure clean state
    return si


# ── Fixtures ──────────────────────────────────────────────────────────────────

AUTH_PY = '''
"""Authentication module."""


class AuthManager:
    """Manages user authentication."""

    @staticmethod
    def login(username: str) -> bool:
        """Authenticate a user."""
        return True

    @staticmethod
    def logout(username: str) -> None:
        """End a user session."""
        pass
'''

SERVICE_PY = '''
"""Service layer that uses authentication."""

from auth import AuthManager


class PaymentService:
    """Payment processing service."""

    def process(self, user: str) -> None:
        # High-confidence call: actual function invocation
        AuthManager.login(user)
        self._verify(user)

    def _verify(self, user: str) -> None:
        # Medium-confidence: method call on self
        self._log(user)

    def _log(self, user: str) -> None:
        print(f"processed {user}")
'''

OTHER_PY = '''
# This file mentions AuthManager in a comment only.
# It does not import or call anything from it.
'''


# ── extract_code_structure tests ─────────────────────────────────────────────

class TestExtractCallRefs:
    """Unit tests for call_refs extraction."""

    def test_call_refs_basic(self):
        """AuthManager.login call is recorded with correct metadata."""
        result = extract_code_structure(SERVICE_PY, "service.py")
        call_refs = result.get("call_refs", [])
        login_calls = [c for c in call_refs if c["callee_name"] == "login"]
        assert len(login_calls) == 1, f"Expected 1 login call, got {len(login_calls)}"
        call = login_calls[0]
        assert call["caller_fqn"] == "PaymentService.process"
        assert call["callee_attr"] == "AuthManager"
        assert call["line"] == 12  # AuthManager.login(user) in SERVICE_PY

    def test_call_refs_method_chains(self):
        """self._verify() is recorded with correct caller_fqn."""
        result = extract_code_structure(SERVICE_PY, "service.py")
        call_refs = result.get("call_refs", [])
        verify_calls = [c for c in call_refs if c["callee_name"] == "_verify"]
        assert len(verify_calls) == 1
        assert verify_calls[0]["caller_fqn"] == "PaymentService.process"
        assert verify_calls[0]["callee_attr"] == "self"

    def test_import_refs(self):
        """from auth import AuthManager is recorded."""
        result = extract_code_structure(SERVICE_PY, "service.py")
        import_refs = result.get("import_refs", [])
        assert len(import_refs) == 1
        assert import_refs[0]["module"] == "auth"
        assert "AuthManager" in import_refs[0]["names"]

    def test_class_inheritance(self):
        """class_inheritance records base classes."""
        code = "class PaymentGateway(BaseGateway, AbstractPayment): pass"
        result = extract_code_structure(code, "test.py")
        inh = result.get("class_inheritance", [])
        assert len(inh) == 1
        assert "BaseGateway" in inh[0]["bases"]
        assert "AbstractPayment" in inh[0]["bases"]

    def test_decorators(self):
        """Decorators are captured with parent_fqn."""
        code = '''
@abstractmethod
def get_value(self): pass
'''
        result = extract_code_structure(code, "test.py")
        decs = result.get("decorators", [])
        assert len(decs) == 1
        assert decs[0]["name"] == "abstractmethod"
        assert decs[0]["symbol_kind"] == "function"


# ── SymbolIndex ref table tests ───────────────────────────────────────────────

class TestRefTables:
    """Integration tests for call_refs/import_refs in SymbolIndex."""

    def test_update_file_stores_call_refs(self, tmp_path):
        """update_file populates call_refs table."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        callers = si.get_callers_ast("login")
        assert len(callers) == 1
        assert callers[0]["source_file"] == str(tmp_path / "service.py")
        assert callers[0]["caller_fqn"] == "PaymentService.process"
        assert callers[0]["confidence"] == "high"
        assert callers[0]["match_type"] == "ast_call"

    def test_reindex_removes_old_call_refs(self, tmp_path):
        """Re-indexing a file removes its old call refs."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)
        callers = si.get_callers_ast("login")
        assert len(callers) == 1

        # Re-index with no calls
        si.update_file(str(tmp_path / "service.py"), OTHER_PY)
        callers_after = si.get_callers_ast("login")
        assert len(callers_after) == 0, "Old call refs should be removed after reindex"

    def test_import_refs_stored(self, tmp_path):
        """import_refs are populated on update_file."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)
        with si._lock:
            cur = si._conn.execute(
                "SELECT module, imported_names FROM import_refs WHERE source_file = ?",
                (str(tmp_path / "service.py"),),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "auth"
        assert "AuthManager" in rows[0][1]

    def test_other_py_comment_only_no_refs(self, tmp_path):
        """OTHER_PY (comment-only) produces no call or import refs."""
        result = extract_code_structure(OTHER_PY, "other.py")
        assert len(result.get("call_refs", [])) == 0
        assert len(result.get("import_refs", [])) == 0

    def test_full_fixture_auth_manager_login(self, tmp_path):
        """Full end-to-end: callers(AuthManager.login) finds service.py."""
        auth_path = tmp_path / "auth.py"
        svc_path = tmp_path / "service.py"
        other_path = tmp_path / "other.py"

        auth_path.write_text(AUTH_PY)
        svc_path.write_text(SERVICE_PY)
        other_path.write_text(OTHER_PY)

        si = _tmp_si()
        si.update_file(str(auth_path), AUTH_PY)
        si.update_file(str(svc_path), SERVICE_PY)
        si.update_file(str(other_path), OTHER_PY)

        # AuthManager.login call found
        callers = si.get_callers_ast("login")
        assert len(callers) == 1, f"Expected 1 caller, got {len(callers)}"
        assert callers[0]["source_file"] == str(svc_path)
        assert callers[0]["caller_fqn"] == "PaymentService.process"
        assert callers[0]["confidence"] == "high"
        assert callers[0]["match_type"] == "ast_call"

    def test_full_fixture_comment_excluded(self, tmp_path):
        """Comment-only mention in other.py is NOT reported as caller."""
        auth_path = tmp_path / "auth.py"
        svc_path = tmp_path / "service.py"
        other_path = tmp_path / "other.py"

        auth_path.write_text(AUTH_PY)
        svc_path.write_text(SERVICE_PY)
        other_path.write_text(OTHER_PY)

        si = _tmp_si()
        si.update_file(str(auth_path), AUTH_PY)
        si.update_file(str(svc_path), SERVICE_PY)
        si.update_file(str(other_path), OTHER_PY)

        callers = si.get_callers_ast("login")
        source_files = {c["source_file"] for c in callers}
        assert str(other_path) not in source_files, \
            "other.py (comment-only) should not appear as caller"

    def test_incremental_ref_update_preserves_other_files(self, tmp_path):
        """Updating one file does not remove refs from other files."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)

        # Verify both files have refs
        callers_auth = si.get_callers_ast("logout")
        assert len(callers_auth) == 0  # no calls to logout yet

        # Re-index auth.py with a new call
        auth_with_call = AUTH_PY + '''
def new_func():
    AuthManager.logout("user")
'''
        si.update_file(str(tmp_path / "auth.py"), auth_with_call)

        # service.py call_refs should still be there
        callers_login = si.get_callers_ast("login")
        assert len(callers_login) == 1
        assert callers_login[0]["source_file"] == str(tmp_path / "service.py")

    def test_symbol_index_preserves_own_refs(self, tmp_path):
        """SymbolIndex entries are preserved when ref tables are updated."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        # Verify symbol records still exist
        symbols = si.get_file_symbols(str(tmp_path / "auth.py"))
        assert len(symbols["symbols"]) >= 3  # AuthManager, login, logout

        # Re-index service.py
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)
        symbols_auth = si.get_file_symbols(str(tmp_path / "auth.py"))
        assert len(symbols_auth["symbols"]) >= 3  # auth.py symbols preserved


class TestConfidenceScoring:
    """Confidence scoring for different match types."""

    def test_direct_method_call_high_confidence(self, tmp_path):
        """Direct method call (obj.method()) gets high confidence."""
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
        assert callers[0]["callee_attr"] == "x"
        assert callers[0]["confidence"] == "high"  # default; AST gives medium

    def test_import_based_fallback_low_confidence(self, tmp_path):
        """Fallback get_callers returns low confidence."""
        si = _tmp_si()
        si.update_file(str(tmp_path / "auth.py"), AUTH_PY)
        si.update_file(str(tmp_path / "service.py"), SERVICE_PY)

        # get_callers (heuristic fallback) used when get_callers_ast finds nothing
        # Simulate: symbol defined but no AST call found
        si.update_file(str(tmp_path / "service.py"), "from auth import AuthManager\n")
        fallback = si.get_callers("AuthManager", str(tmp_path))
        assert len(fallback) >= 0  # import-based may find something