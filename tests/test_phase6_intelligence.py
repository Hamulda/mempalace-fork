"""
Tests for Phase 6 repository intelligence: symbol_index and recent_changes.
"""

import pytest
import tempfile
import os
from pathlib import Path

from mempalace.symbol_index import extract_symbols, SymbolIndex
from mempalace.recent_changes import get_recent_changes, get_hot_spots, build_change_summary


class TestExtractSymbols:
    def test_python_function_definitions(self):
        code = '''
def foo(a, b):
    return a + b

class MyClass:
    def method(self):
        pass
'''
        result = extract_symbols(code, "test.py")
        names = [s["name"] for s in result["symbols"]]
        types = {s["name"]: s["type"] for s in result["symbols"]}
        assert "foo" in names
        assert "MyClass" in names
        assert types.get("foo") == "function"
        assert types.get("MyClass") == "class"

    def test_python_imports(self):
        code = '''
import os
import sys
from pathlib import Path
'''
        result = extract_symbols(code, "test.py")
        assert "os" in result["imports"]
        assert "sys" in result["imports"]
        assert "pathlib" in result["imports"]

    def test_python_file_signature(self):
        code = '''"""Module docstring here."""
import os
'''
        result = extract_symbols(code, "test.py")
        assert "Module docstring here" in result["file_signature"]

    def test_js_function_and_class(self):
        code = '''
export function greet(name) {
    return "Hello " + name;
}
export class Service {
    start() { }
}
'''
        result = extract_symbols(code, "test.js")
        names = [s["name"] for s in result["symbols"]]
        assert "greet" in names
        assert "Service" in names


class TestSymbolIndex:
    def test_update_and_find(self, tmp_path):
        si = SymbolIndex.get(str(tmp_path))
        code = 'def foo(): pass\nclass Bar: pass'
        si.update_file("/src/test.py", code)
        found = si.find_symbol("foo")
        assert len(found) == 1
        assert found[0]["symbol_name"] == "foo"
        assert found[0]["symbol_type"] == "function"

    def test_search_symbols(self, tmp_path):
        si = SymbolIndex.get(str(tmp_path))
        si.update_file("/src/util.py", "def helper_a(): pass\ndef helper_b(): pass")
        si.update_file("/src/utils.py", "class HelperClass: pass")
        results = si.search_symbols("helper")
        names = [r["symbol_name"] for r in results]
        assert "helper_a" in names
        assert "helper_b" in names

    def test_get_file_symbols(self, tmp_path):
        si = SymbolIndex.get(str(tmp_path))
        si.update_file("/src/math.py", "def add(a, b): return a + b\ndef sub(a, b): return a - b")
        result = si.get_file_symbols("/src/math.py")
        sym_names = [s["name"] for s in result["symbols"]]
        assert "add" in sym_names
        assert "sub" in sym_names

    def test_stats(self, tmp_path):
        si = SymbolIndex.get(str(tmp_path))
        si.update_file("/src/a.py", "def a(): pass")
        si.update_file("/src/b.py", "def b(): pass\ndef c(): pass")
        stats = si.stats()
        assert stats["total_symbols"] == 3
        assert stats["total_files"] == 2

    def test_build_index(self, tmp_path):
        si = SymbolIndex.get(str(tmp_path))
        files = [str(tmp_path / "f1.py"), str(tmp_path / "f2.py")]
        Path(files[0]).write_text("def f1(): pass")
        Path(files[1]).write_text("class F2: pass")
        si.build_index(str(tmp_path), files)
        stats = si.stats()
        assert stats["total_symbols"] == 2
        assert stats["total_files"] == 2


class TestRecentChanges:
    def test_get_recent_changes_returns_list(self, tmp_path):
        # Init a git repo to test
        Path(tmp_path / ".git").mkdir()
        changes = get_recent_changes(str(tmp_path), n=5)
        assert isinstance(changes, list)

    def test_get_hot_spots_returns_list(self, tmp_path):
        spots = get_hot_spots(str(tmp_path), n=5)
        assert isinstance(spots, list)

    def test_build_change_summary(self, tmp_path):
        summary = build_change_summary(str(tmp_path), n=5)
        assert "recent_files" in summary
        assert "hot_spots" in summary
        assert "total_commits_30d" in summary
        assert "languages_with_changes" in summary

    def test_recent_changes_works_on_real_repo(self):
        """Integration test against this actual repo."""
        repo = "/Users/vojtechhamada/.claude/plugins/marketplaces/mempalace"
        changes = get_recent_changes(repo, n=5)
        assert len(changes) > 0
        assert changes[0]["file_path"]
        assert changes[0]["commit_hash"]