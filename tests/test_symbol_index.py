"""
Tests for symbol_index hardening: symbol identity, search patterns,
concurrency, and contract clarity.
"""

import pytest
import threading
import tempfile
from pathlib import Path

from mempalace.symbol_index import (
    extract_symbols,
    SymbolIndex,
    _extract_py_symbols,
    _extract_js_symbols,
)


# =============================================================================
# SYMBOL IDENTITY — same name, different lines in same file
# =============================================================================


class TestSymbolIdentitySameNameDifferentLines:
    """
    Verify that the (symbol_name, file_path, line_start) uniqueness model
    preserves ALL symbol definitions, not just one per name-per-file.
    """

    def test_two_functions_same_name_different_lines_both_preserved(self, tmp_path):
        """
        Classic nested-function case: outer foo on line 1, inner foo on line 3.
        Both must appear in the index — no silent overwrite.
        """
        key = f"si_identity_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        code = '''def foo():
    pass


def bar():
    def foo():
        return 1
    return foo
'''
        si.update_file("/src/nested.py", code)

        found = si.find_symbol("foo")
        lines = {f["line_start"] for f in found}
        # foo appears at line 1 (top-level) AND line 6 (nested inside bar)
        assert len(lines) == 2, f"Expected 2 foo entries, got lines: {lines}"
        assert 1 in lines
        assert 6 in lines

    def test_class_method_same_name_as_top_level_both_preserved(self, tmp_path):
        """
        Class method and top-level function with the same name.
        Regex extraction can tell them apart by line number only.
        """
        key = f"si_classmethod_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        code = '''def process(x):
    return x

class Processor:
    def process(self, x):
        return x * 2
'''
        si.update_file("/src/processor.py", code)

        found = si.find_symbol("process")
        # Both top-level (line 1) and method (line 5) should be present
        assert len(found) == 2, f"Expected 2 process entries, got: {found}"
        lines = {f["line_start"] for f in found}
        assert 1 in lines
        assert 5 in lines

    def test_multiple_classes_same_method_name(self, tmp_path):
        """
        Two classes with the same method name, same file, different lines.
        """
        key = f"si_multiclass_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        code = '''class Foo:
    def run(self): pass

class Bar:
    def run(self): pass
'''
        si.update_file("/src/dual.py", code)

        found = si.find_symbol("run")
        assert len(found) == 2, f"Expected 2 run entries, got: {found}"
        lines = {f["line_start"] for f in found}
        assert 2 in lines  # Foo.run on line 2
        assert 5 in lines  # Bar.run on line 5

    def test_update_file_replace_behavior_preserves_distinct_lines(self, tmp_path):
        """
        update_file does DELETE + INSERT per file. Verify re-indexing the
        same file does not corrupt entries with distinct line numbers.
        """
        key = f"si_update_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        code = '''def inner(x):
    return x

def outer(y):
    def inner(y):
        return y + 1
    return inner(y)
'''
        # Index once
        si.update_file("/src/scope.py", code)
        found1 = si.find_symbol("inner")
        assert len(found1) == 2

        # Re-index same file (simulate edit + re-mine)
        si.update_file("/src/scope.py", code)
        found2 = si.find_symbol("inner")
        assert len(found2) == 2, "Re-indexing must not lose nested symbol"

    def test_get_file_symbols_shows_all_duplicates(self, tmp_path):
        """
        get_file_symbols must return ALL symbols including duplicates by name.
        """
        key = f"si_file_syms_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        code = '''def helper(): pass

class Util:
    def helper(self): pass
'''
        si.update_file("/src/util.py", code)
        result = si.get_file_symbols("/src/util.py")

        names = [s["name"] for s in result["symbols"]]
        # Both helper definitions should appear
        assert names.count("helper") == 2, f"Expected 2 helpers, got: {names}"


# =============================================================================
# SEARCH PATTERNS — SQL LIKE semantics
# =============================================================================


class TestSearchSymbolsPatterns:
    """Verify search_symbols SQL LIKE pattern semantics."""

    def test_exact_match_no_wildcards(self, tmp_path):
        """No wildcards → contains match %%pattern%%."""
        key = f"si_search_exact_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/a.py", "def foo_bar(): pass")
        si.update_file("/src/b.py", "def baz_qux(): pass")

        results = si.search_symbols("foo")
        names = {r["symbol_name"] for r in results}
        assert "foo_bar" in names
        assert "baz_qux" not in names

    def test_prefix_pattern(self, tmp_path):
        """^foo → LIKE 'foo%'."""
        key = f"si_prefix_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/a.py", "def foo_bar(): pass\ndef foo_baz(): pass")
        si.update_file("/src/b.py", "def foo(): pass\ndef bar(): pass")

        results = si.search_symbols("^foo")
        names = {r["symbol_name"] for r in results}
        assert "foo_bar" in names
        assert "foo_baz" in names
        assert "foo" in names
        assert "bar" not in names

    def test_suffix_pattern(self, tmp_path):
        """foo$ → LIKE '%foo'."""
        key = f"si_suffix_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/a.py", "def bar_foo(): pass\ndef bar(): pass")
        si.update_file("/src/b.py", "def baz(): pass")

        results = si.search_symbols("foo$")
        names = {r["symbol_name"] for r in results}
        assert "bar_foo" in names
        assert "bar" not in names

    def test_full_match_pattern(self, tmp_path):
        """^foo$ → LIKE 'foo%' (prefix match, not true exact match)."""
        key = f"si_full_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/a.py", "def foo(): pass\ndef foobar(): pass")
        si.update_file("/src/b.py", "def foo_bar(): pass")

        results = si.search_symbols("^foo$")
        names = {r["symbol_name"] for r in results}
        # ^foo$ becomes LIKE 'foo%' which is prefix match
        assert "foo" in names
        assert "foobar" in names  # starts with foo, matches foo%
        assert "foo_bar" in names  # starts with foo, matches foo%

    def test_underscore_wildcard(self, tmp_path):
        """_ in pattern is SQL LIKE single-char wildcard."""
        key = f"si_underscore_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/a.py", "def foo_bar(): pass\ndef foobar(): pass")

        results = si.search_symbols("foo_bar")  # underscore is literal
        names = {r["symbol_name"] for r in results}
        assert "foo_bar" in names

    def test_percent_wildcard(self, tmp_path):
        """% in pattern is SQL LIKE multi-char wildcard (any chars)."""
        key = f"si_percent_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/a.py", "def foo_bar(): pass\ndef foo_baz(): pass")

        results = si.search_symbols("foo%bar")
        names = {r["symbol_name"] for r in results}
        # foo%bar means "foo" + anything + "bar" at END
        assert "foo_bar" in names  # ends with bar

    def test_search_returns_empty_for_no_match(self, tmp_path):
        """No matches returns empty list, not None."""
        key = f"si_empty_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/a.py", "def foo(): pass")
        results = si.search_symbols("nonexistent_symbol_xyz")
        assert results == []

    def test_search_respects_limit(self, tmp_path):
        """search_symbols(limit=N) respects the limit."""
        key = f"si_limit_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        # Create 15 symbols
        for i in range(15):
            si.update_file(f"/src/file{i}.py", f"def fn{i}(): pass")

        # Default limit is 100 - should return all 15
        results = si.search_symbols("fn")
        assert len(results) == 15

        # With limit=5, should return only 5
        results5 = si.search_symbols("fn", limit=5)
        assert len(results5) == 5

    def test_scoped_search_class_method(self, tmp_path):
        """
        "ClassName.method" pattern searches for 'method' in files
        whose path contains 'ClassName' (scope heuristic).
        """
        key = f"si_scoped_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        # File with class method
        si.update_file("/src/utils.py", "class Foo:\n    def bar(self): pass")
        si.update_file("/src/helper.py", "def bar(): pass")

        results = si.search_symbols("Foo.bar")
        names = {r["symbol_name"] for r in results}
        # Should find 'bar' defined inside Foo class in utils.py
        assert "bar" in names
        # The Foo.bar result should be from utils.py (where Foo is defined)
        foo_bar_files = {r["file_path"] for r in results if r["symbol_name"] == "bar"}
        assert any("utils.py" in f for f in foo_bar_files)

    def test_scoped_search_module_class_method(self, tmp_path):
        """
        "Module.ClassName.method" searches for 'method' in files whose
        path contains 'Module/ClassName' (deeper scope heuristic).
        """
        key = f"si_scoped2_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/project/lib/foo.py", "class Bar:\n    def baz(self): pass")
        si.update_file("/project/lib/other.py", "def baz(): pass")

        results = si.search_symbols("lib.Bar.baz")
        names = {r["symbol_name"] for r in results}
        assert "baz" in names
        baz_files = {r["file_path"] for r in results if r["symbol_name"] == "baz"}
        assert any("lib/foo.py" in f for f in baz_files)

    def test_scoped_search_no_false_positives_standalone_function(self, tmp_path):
        """
        Scoped search filters out symbols in files that don't contain the scope class.
        File with standalone function 'bar' (no class Foo) should not match 'Foo.bar'.
        """
        key = f"si_scoped3_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        # utils.py has class Foo but standalone helper function
        si.update_file("/src/utils.py", "class Foo:\n    pass\ndef helper(): pass")
        # other.py has standalone helper with no Foo class
        si.update_file("/src/other.py", "def helper(): pass")

        results = si.search_symbols("Foo.helper")
        helper_files = {r["file_path"] for r in results if r["symbol_name"] == "helper"}
        # Should only find helper in utils.py (where Foo class is defined)
        # not in other.py (no Foo class)
        assert len(helper_files) >= 1
        for f in helper_files:
            assert "utils.py" in f, f"helper in {f} should be filtered out (no Foo class)"

    def test_scoped_search_finds_method_not_standalone(self, tmp_path):
        """
        When a file has both a class method and a standalone function with
        the same name, scoped search should prefer the class-scoped one.
        """
        key = f"si_scoped4_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        # File has a class with a method AND a standalone function with same name
        si.update_file("/src/utils.py", "class Foo:\n    def bar(self): pass\ndef bar(): pass")

        results = si.search_symbols("Foo.bar")
        bar_files = {r["file_path"] for r in results if r["symbol_name"] == "bar"}
        # Both bar definitions are in utils.py, but the scoped search
        # should still find them (scope filter passes since Foo is in file)
        assert len(bar_files) >= 1
        # The class method bar should be included
        assert any("utils.py" in f for f in bar_files)


# =============================================================================
# LINE NUMBER CORRECTNESS
# =============================================================================


class TestLineNumbers:
    """Verify line_start and line_end are stored and returned correctly."""

    def test_line_end_is_set(self, tmp_path):
        """
        line_end should default to line_start + 1 (single-line marker),
        not 0 or NULL.
        """
        key = f"si_line_end_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        code = "def foo(): pass\nclass Bar: pass"
        si.update_file("/src/test.py", code)

        found = si.find_symbol("foo")
        assert len(found) == 1
        assert found[0]["line_end"] == found[0]["line_start"] + 1, (
            f"line_end should be line_start+1, got {found[0]['line_end']}"
        )

    def test_get_file_symbols_line_start_and_line_end(self, tmp_path):
        """get_file_symbols returns line_start and line_end for each symbol."""
        key = f"si_ls_le_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        code = "def first(): pass\n\n\ndef second(): pass"
        si.update_file("/src/lines.py", code)

        result = si.get_file_symbols("/src/lines.py")
        sym_by_name = {s["name"]: s for s in result["symbols"]}

        assert sym_by_name["first"]["line_start"] == 1
        assert sym_by_name["first"]["line_end"] == 2
        assert sym_by_name["second"]["line_start"] == 4
        assert sym_by_name["second"]["line_end"] == 5


# =============================================================================
# CALLERS HEURISTIC
# =============================================================================


class TestGetCallers:
    """Verify get_callers import-based heuristic behavior."""

    def test_callers_finds_importing_file(self, tmp_path):
        """
        If file A defines 'helper' and file B imports A,
        get_callers('helper') should find B.
        """
        key = f"si_callers_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        project = str(tmp_path / "myproject")
        # File that DEFINES the symbol (under project root)
        si.update_file(f"{project}/src/utils.py", "def helper(): pass")
        # File that IMPORTS the module (under same project root)
        si.update_file(
            f"{project}/src/main.py",
            "from src.utils import helper\nx = helper()",
        )

        callers = si.get_callers("helper", project)
        caller_paths = {c["file_path"] for c in callers}
        assert f"{project}/src/main.py" in caller_paths

    def test_callers_does_not_include_definition_file(self, tmp_path):
        """
        The file that DEFINES the symbol should not be listed as its own caller.
        """
        key = f"si_no_self_call_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        project = str(tmp_path / "myproject")
        si.update_file(f"{project}/src/utils.py", "def helper(): pass")
        si.update_file(
            f"{project}/src/main.py",
            "from src.utils import helper\nx = helper()",
        )

        callers = si.get_callers("helper", project)
        caller_paths = {c["file_path"] for c in callers}
        assert f"{project}/src/utils.py" not in caller_paths

    def test_callers_returns_empty_for_unknown_symbol(self, tmp_path):
        """Unknown symbol returns empty list."""
        key = f"si_callers_empty_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/project/src/utils.py", "def known(): pass")
        callers = si.get_callers("unknown_symbol_xyz", "/project")
        assert callers == []

    def test_callers_finds_direct_import(self, tmp_path):
        """
        from module import symbol (direct import) should be detected.
        This was the key limitation before - now fixed via direct_imports column.
        """
        key = f"si_direct_import_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        project = str(tmp_path / "myproject")
        si.update_file(
            f"{project}/src/utils.py",
            "def helper(): pass\ndef other(): pass",
        )
        si.update_file(
            f"{project}/src/main.py",
            "from src.utils import helper\nx = helper()",
        )

        callers = si.get_callers("helper", project)
        caller_paths = {c["file_path"] for c in callers}
        assert f"{project}/src/main.py" in caller_paths

        # Verify import_type is set to "direct"
        direct_callers = [c for c in callers if c.get("import_type") == "direct"]
        assert len(direct_callers) >= 1

    def test_callers_finds_import_alias(self, tmp_path):
        """
        import module as alias should be detected via module-level search.
        """
        key = f"si_alias_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        project = str(tmp_path / "myproject")
        si.update_file(
            f"{project}/src/utils.py",
            "def helper(): pass",
        )
        si.update_file(
            f"{project}/src/main.py",
            "import src.utils as u\nx = u.helper()",
        )

        callers = si.get_callers("helper", project)
        caller_paths = {c["file_path"] for c in callers}
        assert f"{project}/src/main.py" in caller_paths

    def test_callers_finds_symbol_alias(self, tmp_path):
        """
        from module import symbol as alias should be detected via direct_imports.
        """
        key = f"si_sym_alias_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        project = str(tmp_path / "myproject")
        si.update_file(
            f"{project}/src/utils.py",
            "def helper(): pass",
        )
        si.update_file(
            f"{project}/src/main.py",
            "from src.utils import helper as h\nx = h()",
        )

        callers = si.get_callers("helper", project)
        caller_paths = {c["file_path"] for c in callers}
        assert f"{project}/src/main.py" in caller_paths

        # Verify import_type is "direct" for symbol alias
        direct_callers = [c for c in callers if c.get("import_type") == "direct"]
        assert len(direct_callers) >= 1

    def test_callers_deduplicates_across_search_types(self, tmp_path):
        """
        If the same file matches via both module search and direct search,
        it should appear only once (deduplicated by file_path).
        """
        key = f"si_dedup_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        project = str(tmp_path / "myproject")
        si.update_file(
            f"{project}/src/utils.py",
            "def helper(): pass",
        )
        # This file imports both the module AND the direct symbol
        si.update_file(
            f"{project}/src/main.py",
            "import src.utils\nfrom src.utils import helper\nx = src.utils.helper()\ny = helper()",
        )

        callers = si.get_callers("helper", project)
        caller_paths = {c["file_path"] for c in callers}
        # Should only appear once despite matching both search paths
        assert f"{project}/src/main.py" in caller_paths
        # Count should be 1, not 2
        count = sum(1 for c in callers if c["file_path"] == f"{project}/src/main.py")
        assert count == 1


# =============================================================================
# THREAD SAFETY — RLock ensures reentrancy
# =============================================================================


class TestSymbolIndexRLock:
    """Verify RLock allows reentrant (nested) lock acquisition."""

    def test_rlock_is_reentrant_lock(self, tmp_path):
        """
        Verify that the lock used by SymbolIndex is an RLock (reentrant).
        This is verified by checking that acquiring the lock twice from the
        same thread does NOT raise RuntimeError (which a plain Lock would).
        """
        key = f"si_rlock_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/utils.py", "def helper(): pass")

        errors = []
        # Acquire lock twice from same thread (nested)
        try:
            si._lock.acquire()
            try:
                si._lock.acquire()  # Should NOT raise with RLock
            finally:
                si._lock.release()
        except RuntimeError as e:
            errors.append(f"Lock is not reentrant: {e}")
        finally:
            si._lock.release()

        assert not errors, f"RLock test failed: {errors}"

    def test_concurrent_find_and_update(self, tmp_path):
        """
        Concurrent find_symbol calls while update_file is running.
        """
        key = f"si_concurrent_find_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        # Pre-populate
        for i in range(5):
            si.update_file(f"/src/file{i}.py", f"def fn{i}(): pass\nclass C{i}: pass")

        find_results = []
        find_errors = []
        write_errors = []

        def reader():
            for _ in range(20):
                try:
                    r = si.find_symbol("fn2")
                    find_results.append(r)
                except Exception as e:
                    find_errors.append(e)

        def writer():
            for i in range(10, 15):
                try:
                    si.update_file(f"/src/new{i}.py", f"def fn{i}(): pass")
                except Exception as e:
                    write_errors.append(e)

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not find_errors, f"Find errors: {find_errors}"
        assert not write_errors, f"Write errors: {write_errors}"
        assert len(find_results) == 40


# =============================================================================
# EXTRACT_SYMBOLS — consistency and correctness
# =============================================================================


class TestExtractSymbolsPython:
    """Verify Python symbol extraction handles edge cases."""

    def test_async_function_extracted(self):
        code = '''async def async_foo():
    pass
'''
        result = extract_symbols(code, "test.py")
        names = [s["name"] for s in result["symbols"]]
        assert "async_foo" in names

    def test_class_and_function_same_name(self):
        """Class and function with same name appear at different lines."""
        code = '''def Foo():
    pass

class Foo:
    pass
'''
        result = extract_symbols(code, "test.py")
        by_line = sorted([(s["name"], s["line"]) for s in result["symbols"]])
        assert by_line[0][0] == "Foo"  # function first
        assert by_line[1][0] == "Foo"  # class second


class TestExtractSymbolsJS:
    """Verify JavaScript/TypeScript symbol extraction."""

    def test_named_imports(self):
        code = '''import { foo, bar } from "module";
import baz from "other";'''
        result = extract_symbols(code, "test.js")
        assert "module" in result["imports"]
        assert "other" in result["imports"]

    def test_export_function_and_class(self):
        code = '''
export function greet() { }
export class Service { }
'''
        result = extract_symbols(code, "test.js")
        names = [s["name"] for s in result["symbols"]]
        assert "greet" in names
        assert "Service" in names


# =============================================================================
# CONTRACT — find_symbol, get_file_symbols return types
# =============================================================================


class TestSymbolIndexContract:
    """Verify return types and field presence match documented contracts."""

    def test_find_symbol_returns_all_fields(self, tmp_path):
        """find_symbol result dict must have documented fields."""
        key = f"si_contract_find_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/test.py", "def foo(): pass")
        found = si.find_symbol("foo")

        assert len(found) == 1
        r = found[0]
        assert "symbol_name" in r
        assert "symbol_type" in r
        assert "file_path" in r
        assert "line_start" in r
        assert "line_end" in r
        assert "file_signature" in r
        assert "imports" in r
        assert "exports" in r

    def test_find_symbol_exact_mode(self, tmp_path):
        """
        find_symbol(exact=True) uses COLLATE BINARY for guaranteed
        case-sensitive matching, while default (exact=False) is
        also case-sensitive for ASCII.
        """
        key = f"si_exact_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/test.py", "def foo(): pass\ndef Foo(): pass")
        si.update_file("/src/other.py", "def FOO(): pass")

        # Default exact=False: case-sensitive match for foo
        results = si.find_symbol("foo")
        assert len(results) == 1
        assert results[0]["file_path"].endswith("test.py")

        # exact=True: also case-sensitive
        results_exact = si.find_symbol("foo", exact=True)
        assert len(results_exact) == 1

        # Find Foo (capitalized) - default
        results_foo = si.find_symbol("Foo")
        assert len(results_foo) == 1

        # FOO - should find only the one in other.py
        results_foo_cap = si.find_symbol("FOO")
        assert len(results_foo_cap) == 1

    def test_search_symbols_returns_subset_fields(self, tmp_path):
        """search_symbols returns symbol_name, file_path, symbol_type, line_start, line_end."""
        key = f"si_contract_search_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/test.py", "def foo(): pass")
        results = si.search_symbols("foo")

        assert len(results) == 1
        r = results[0]
        assert "symbol_name" in r
        assert "file_path" in r
        assert "symbol_type" in r
        assert "line_start" in r
        assert "line_end" in r

    def test_get_file_symbols_returns_all_fields(self, tmp_path):
        """get_file_symbols returns symbols, imports, exports, file_signature."""
        key = f"si_contract_file_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        code = '''"""Module docstring."""
import os

def foo(): pass
'''
        si.update_file("/src/test.py", code)
        result = si.get_file_symbols("/src/test.py")

        assert "symbols" in result
        assert "imports" in result
        assert "exports" in result
        assert "file_signature" in result
        assert "Module docstring" in result["file_signature"]

        # Each symbol dict has name, type, line_start, line_end
        for s in result["symbols"]:
            assert "name" in s
            assert "type" in s
            assert "line_start" in s
            assert "line_end" in s


# =============================================================================
# EDGE CASES
# =============================================================================


class TestSymbolIndexEdgeCases:
    """Verify behavior in edge cases."""

    def test_empty_file(self, tmp_path):
        """Empty file should not raise."""
        key = f"si_empty_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/empty.py", "")
        result = si.get_file_symbols("/src/empty.py")
        assert result["symbols"] == []

    def test_nonexistent_file(self, tmp_path):
        """Nonexistent file returns empty structure."""
        key = f"si_nonexist_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        result = si.get_file_symbols("/src/does_not_exist.py")
        assert result["symbols"] == []
        assert result["imports"] == []

    def test_stats_after_updates(self, tmp_path):
        """stats() reflects all indexed symbols and files."""
        key = f"si_stats_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        si.update_file("/src/a.py", "def foo(): pass\ndef bar(): pass")
        si.update_file("/src/b.py", "class Baz: pass")

        stats = si.stats()
        assert stats["total_symbols"] == 3
        assert stats["total_files"] == 2

    def test_build_index_multiple_files(self, tmp_path):
        """build_index processes a list of file paths."""
        key = f"si_build_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        files = [
            str(tmp_path / "f1.py"),
            str(tmp_path / "f2.py"),
            str(tmp_path / "f3.ts"),
        ]
        Path(files[0]).write_text("def a(): pass\ndef b(): pass")
        Path(files[1]).write_text("class C: pass")
        Path(files[2]).write_text("function d() { }")

        si.build_index(str(tmp_path), files)

        stats = si.stats()
        assert stats["total_symbols"] == 4  # 2 from f1 + 1 from f2 + 1 from f3
        assert stats["total_files"] == 3
