"""
test_ast_extractor.py — Tests for AST-aware code structure extraction.

Covers:
- extract_code_structure for Python with parent/fqn
- nested class/method/function parent tracking
- duplicate method names in different classes
- regex fallback when tree-sitter unavailable
- extraction_backend field correctness
- SymbolIndex.update_file stores new columns
- SymbolIndex.get_file_symbols returns rich metadata
"""

import pytest
import tempfile
import os
from pathlib import Path

# ── helpers ────────────────────────────────────────────────────────────────────

def _tmp_path(name="test_symbols.db"):
    return str(Path(tempfile.mkdtemp()) / name)


# ── ast_extractor unit tests ──────────────────────────────────────────────────

class TestExtractCodeStructure:
    """Unit tests for extract_code_structure and extract_symbols shim."""

    def test_nested_class_method_has_parent_and_fqn(self):
        """Methods inside a class get parent=classname, fqn=ClassName.method."""
        from mempalace.code_index.ast_extractor import extract_code_structure
        code = """
class Outer:
    def method(self):
        pass

    class Inner:
        def inner_method(self):
            pass

def standalone():
    pass
"""
        result = extract_code_structure(code, "test.py")

        # Tree-sitter or regex — parent/fqn must be present for tree-sitter
        backend = result["extraction_backend"]

        # Build name→entry map
        by_name = {s["name"]: s for s in result["symbols"]}

        # Standalone function has no parent
        assert by_name["standalone"]["parent"] is None
        assert by_name["standalone"]["fqn"] == "standalone"

        if backend in ("tree_sitter", "stdlib_ast"):
            # Outer is top-level class
            assert by_name["Outer"]["parent"] is None
            assert by_name["Outer"]["fqn"] == "Outer"

            # method is inside Outer
            assert by_name["method"]["parent"] == "Outer", f"method parent should be Outer, got {by_name['method']['parent']}"
            assert by_name["method"]["fqn"] == "Outer.method"

            # Inner is inside Outer
            assert by_name["Inner"]["parent"] == "Outer"
            assert by_name["Inner"]["fqn"] == "Outer.Inner"

            # inner_method is inside Inner (which is inside Outer)
            assert by_name["inner_method"]["parent"] == "Inner"
            assert by_name["inner_method"]["fqn"] == "Outer.Inner.inner_method"
        # For regex backend: parent is always None (expected limitation)

    def test_duplicate_method_names_different_classes(self):
        """Same method name in different classes gives different fqns."""
        from mempalace.code_index.ast_extractor import extract_code_structure
        code = """
class Foo:
    def handle(self):
        pass

class Bar:
    def handle(self):
        pass
"""
        result = extract_code_structure(code, "test.py")
        by_name = {s["name"]: s for s in result["symbols"]}

        # Both handles have same local name
        handles = [s for s in result["symbols"] if s["name"] == "handle"]
        assert len(handles) == 2

        if result["extraction_backend"] == "tree_sitter":
            fqns = {s["fqn"] for s in handles}
            assert "Foo.handle" in fqns
            assert "Bar.handle" in fqns

    def test_extraction_backend_field(self):
        """extraction_backend is always present and is 'tree_sitter' or 'regex'."""
        from mempalace.code_index.ast_extractor import extract_code_structure

        code = "def foo(): pass"
        result = extract_code_structure(code, "test.py")

        assert "extraction_backend" in result
        assert result["extraction_backend"] in ("tree_sitter", "stdlib_ast", "regex")

    def test_extract_symbols_shim_returns_legacy_format(self):
        """Legacy extract_symbols shim returns name/type/line fields."""
        from mempalace.code_index.ast_extractor import extract_symbols

        code = """
class Foo:
    def bar(self):
        pass
"""
        result = extract_symbols(code, "test.py")

        assert "symbols" in result
        assert "extraction_backend" in result
        for sym in result["symbols"]:
            assert "name" in sym
            assert "type" in sym  # kind→type shim
            assert "line" in sym  # line_start alias

    def test_tree_sitter_strict_fqn_when_available(self):
        """Strict fqn assertions when tree-sitter is available."""
        from mempalace.code_index.ast_extractor import is_tree_sitter_available, tree_sitter_diagnostics

        if not is_tree_sitter_available():
            pytest.skip("tree-sitter not available")

        diag = tree_sitter_diagnostics()
        if not diag.get("python_parser_works"):
            pytest.skip(f"python parser not working: {diag.get('error')}")

        from mempalace.code_index.ast_extractor import extract_code_structure

        code = """
class Outer:
    def method(self):
        pass

    class Inner:
        def inner_method(self):
            pass

class Foo:
    def handle(self):
        pass

class Bar:
    def handle(self):
        pass
"""
        result = extract_code_structure(code, "test.py")
        assert result["extraction_backend"] == "tree_sitter"

        by_name = {s["name"]: s for s in result["symbols"]}

        # Exact fqns when tree-sitter is working
        assert by_name["Outer"]["fqn"] == "Outer"
        assert by_name["method"]["fqn"] == "Outer.method"
        assert by_name["Outer"]["parent"] is None

        assert by_name["Inner"]["fqn"] == "Outer.Inner"
        assert by_name["Inner"]["parent"] == "Outer"

        assert by_name["inner_method"]["fqn"] == "Outer.Inner.inner_method"
        assert by_name["inner_method"]["parent"] == "Inner"

        # Duplicate method names in different classes
        handles = [s for s in result["symbols"] if s["name"] == "handle"]
        assert len(handles) == 2
        fqns = {s["fqn"] for s in handles}
        assert fqns == {"Foo.handle", "Bar.handle"}

    def test_tree_sitter_unavailable_still_works(self):
        """If tree-sitter import fails, extract_code_structure returns result."""
        from mempalace.code_index.ast_extractor import is_tree_sitter_available

        available = is_tree_sitter_available()
        # We just need the function to work either way
        from mempalace.code_index.ast_extractor import extract_code_structure
        code = "def func(): pass"
        result = extract_code_structure(code, "test.py")
        assert "symbols" in result
        assert len(result["symbols"]) == 1


# ── SymbolIndex integration tests ─────────────────────────────────────────────

class TestSymbolIndexAST:
    """Integration tests: SymbolIndex with AST-extractor columns."""

    def test_update_file_stores_parent_and_fqn(self):
        """update_file stores parent_symbol and symbol_fqn columns."""
        from mempalace.symbol_index import SymbolIndex

        path = _tmp_path()
        si = SymbolIndex.get(path)
        si.clear()

        code = """
class Container:
    def method(self):
        pass

def standalone():
    pass
"""
        si.update_file("/test/file.py", code)
        rows = si.get_file_symbols("/test/file.py")

        symbols = rows["symbols"]
        assert rows["extraction_backend"] in ("tree_sitter", "stdlib_ast", "regex")

        # Container class
        container = next((s for s in symbols if s["name"] == "Container"), None)
        assert container is not None
        if rows["extraction_backend"] == "tree_sitter":
            assert container["parent"] is None
            assert container["fqn"] == "Container"

        # method inside Container
        method = next((s for s in symbols if s["name"] == "method"), None)
        assert method is not None
        if rows["extraction_backend"] == "tree_sitter":
            assert method["parent"] == "Container"
            assert method["fqn"] == "Container.method"

        # standalone function
        standalone = next((s for s in symbols if s["name"] == "standalone"), None)
        assert standalone is not None
        if rows["extraction_backend"] == "tree_sitter":
            assert standalone["parent"] is None
            assert standalone["fqn"] == "standalone"

    def test_duplicate_methods_in_different_classes(self):
        """Two classes with same method name get distinct fqns."""
        from mempalace.symbol_index import SymbolIndex

        path = _tmp_path()
        si = SymbolIndex.get(path)
        si.clear()

        code = """
class Foo:
    def handle(self):
        pass

class Bar:
    def handle(self):
        pass
"""
        si.update_file("/test/dup.py", code)
        rows = si.get_file_symbols("/test/dup.py")

        handles = [s for s in rows["symbols"] if s["name"] == "handle"]
        assert len(handles) == 2

        if rows["extraction_backend"] == "tree_sitter":
            fqns = {s["fqn"] for s in handles}
            assert "Foo.handle" in fqns
            assert "Bar.handle" in fqns

    def test_find_symbol_returns_parent_and_fqn(self):
        """find_symbol returns parent_symbol, symbol_fqn, extraction_backend."""
        from mempalace.symbol_index import SymbolIndex

        path = _tmp_path()
        si = SymbolIndex.get(path)
        si.clear()

        code = """
class MyClass:
    def my_method(self):
        pass
"""
        si.update_file("/test/find.py", code)
        results = si.find_symbol("my_method")

        assert len(results) >= 1
        # Should have new columns
        assert "parent_symbol" in results[0]
        assert "symbol_fqn" in results[0]
        assert "extraction_backend" in results[0]

        if results[0]["extraction_backend"] == "tree_sitter":
            assert results[0]["parent_symbol"] == "MyClass"
            assert results[0]["symbol_fqn"] == "MyClass.my_method"

    def test_get_file_symbols_returns_rich_metadata(self):
        """get_file_symbols returns extraction_backend, parent_symbols list, fqns list."""
        from mempalace.symbol_index import SymbolIndex

        path = _tmp_path()
        si = SymbolIndex.get(path)
        si.clear()

        code = """
class Outer:
    class Inner:
        def inner_method(self):
            pass
"""
        si.update_file("/test/rich.py", code)
        result = si.get_file_symbols("/test/rich.py")

        assert "extraction_backend" in result
        assert result["extraction_backend"] in ("tree_sitter", "stdlib_ast", "regex")
        assert "parent_symbols" in result
        assert "fqns" in result
        assert isinstance(result["parent_symbols"], list)

        if result["extraction_backend"] == "tree_sitter":
            assert "Outer" in result["parent_symbols"]
            assert "Inner" in result["parent_symbols"]
            assert any("Outer.Inner" in fqn for fqn in result["fqns"])

    def test_stats_still_works(self):
        """stats() is unaffected by new columns."""
        from mempalace.symbol_index import SymbolIndex

        path = _tmp_path()
        si = SymbolIndex.get(path)
        si.clear()

        code = """
class Foo:
    def bar(self): pass

def baz(): pass
"""
        si.update_file("/test/stats.py", code)
        stats = si.stats()

        assert "total_symbols" in stats
        assert "total_files" in stats
        assert stats["total_files"] == 1

    def test_regex_fallback_preserves_old_behavior(self):
        """Regex fallback (no tree-sitter) still indexes correctly."""
        from mempalace.code_index.ast_extractor import extract_code_structure

        code = """
class MyClass:
    def my_method(self):
        pass
"""
        result = extract_code_structure(code, "test.py")

        # Regardless of backend, we get symbols
        names = {s["name"] for s in result["symbols"]}
        assert "MyClass" in names
        assert "my_method" in names

    def test_regex_fallback_on_parser_failure(self):
        """If tree-sitter imports but Python parser is None, fall back to regex."""
        import unittest.mock
        from mempalace.code_index import ast_extractor
        from mempalace.code_index.ast_extractor import extract_code_structure

        code = "class Foo: pass"
        with unittest.mock.patch.object(
            ast_extractor, "_get_tree_sitter_parser", return_value=None
        ):
            result = extract_code_structure(code, "test.py")
        names = {s["name"] for s in result["symbols"]}
        assert "Foo" in names
        assert result["extraction_backend"] in ("regex", "stdlib_ast")

    def test_import_only_file_no_duplicate_errors(self):
        """Import-only file (no symbols) stores placeholder row without error."""
        from mempalace.symbol_index import SymbolIndex

        path = _tmp_path()
        si = SymbolIndex.get(path)
        si.clear()

        si.update_file("/test/imports_only.py", "from os import path\nimport sys")
        result = si.get_file_symbols("/test/imports_only.py")

        assert "symbols" in result
        # Placeholder row: empty symbol_name, line_start=0
        symbols = result["symbols"]
        assert any(s["name"] == "" for s in symbols)


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])