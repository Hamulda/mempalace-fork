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


class TestExtractSymbolsLineNumbers:
    """Verify that line numbers are actual 1-based line numbers, not character offsets."""

    def test_python_line_numbers_are_correct(self):
        """match.start() gives char offset; we must count newlines to get line number."""
        code = '''"""Module docstring."""
import os

def foo():
    return 1

class Bar:
    def method(self):
        pass
'''
        result = extract_symbols(code, "test.py")
        symbols = {s["name"]: s["line"] for s in result["symbols"]}

        # def foo is on line 4 (1-indexed): line1=docstring, 2=import, 3=blank, 4=def foo
        assert symbols["foo"] == 4, f"foo should be on line 4, got {symbols['foo']}"
        # class Bar is on line 7
        assert symbols["Bar"] == 7, f"Bar should be on line 7, got {symbols['Bar']}"
        # method is on line 8
        assert symbols["method"] == 8, f"method should be on line 8, got {symbols['method']}"

    def test_python_line_numbers_with_blank_lines(self):
        """Blank lines must be counted correctly."""
        code = '''def a():
    pass


def b():
    pass
'''
        result = extract_symbols(code, "test.py")
        symbols = {s["name"]: s["line"] for s in result["symbols"]}
        assert symbols["a"] == 1
        assert symbols["b"] == 5, f"b should be on line 5 (after two blank lines), got {symbols['b']}"

    def test_js_line_numbers(self):
        code = '''function greet() {
    return "hi";
}

class Service {
    start() { }
}
'''
        result = extract_symbols(code, "test.js")
        symbols = {s["name"]: s["line"] for s in result["symbols"]}
        assert symbols["greet"] == 1
        assert symbols["Service"] == 5

    def test_go_line_numbers(self):
        code = '''package main

func main() {
}

func helper() {
}
'''
        result = extract_symbols(code, "test.go")
        symbols = {s["name"]: s["line"] for s in result["symbols"]}
        assert symbols["main"] == 3
        assert symbols["helper"] == 6


class TestHotSpotsParser:
    """Verify get_hot_spots correctly parses git log --name-only output."""

    def test_hot_spots_counts_files_correctly(self, monkeypatch, tmp_path):
        """
        git log --name-only outputs: hash\\n\\tfile1.py\\n\\tfile2.py\\n\\n
        Each file in a commit should be counted exactly once.
        """
        # Use valid 40-char hex hashes
        git_output = (
            "ec0b4f0b5c90ed0fa911a2972ccc452641b31563\n"
            "\tsrc/main.py\n"
            "\tsrc/util.py\n"
            "\n"
            "54563f95fefa691baa82a522156322c21f7d6df3\n"
            "\tsrc/main.py\n"
            "\tsrc/other.py\n"
        )

        def mock_run_git(project_path, args):
            return git_output

        import mempalace.recent_changes as rc
        monkeypatch.setattr(rc, "_run_git", mock_run_git)

        spots = get_hot_spots(str(tmp_path), n=10)

        # src/main.py appears in 2 commits
        main_spots = [s for s in spots if "src/main.py" in s["file_path"]]
        assert len(main_spots) == 1
        assert main_spots[0]["change_count"] == 2, (
            f"src/main.py should have change_count=2, got {main_spots[0]['change_count']}"
        )

        # src/util.py and src/other.py each appear once
        util_spots = [s for s in spots if "src/util.py" in s["file_path"]]
        assert len(util_spots) == 1
        assert util_spots[0]["change_count"] == 1

        other_spots = [s for s in spots if "src/other.py" in s["file_path"]]
        assert len(other_spots) == 1
        assert other_spots[0]["change_count"] == 1

    def test_hot_spots_only_counts_indented_lines(self, monkeypatch, tmp_path):
        """Only indented lines following a hash should be counted as filenames."""
        # Real git --name-only format: hash on its own line, filenames on indented lines
        git_output = (
            "abc123def456789012345678901234567890abcd\n"
            "\tsrc/main.py\n"
            "\tsrc/util.py\n"
        )

        def mock_run_git(project_path, args):
            return git_output

        import mempalace.recent_changes as rc
        monkeypatch.setattr(rc, "_run_git", mock_run_git)

        spots = get_hot_spots(str(tmp_path), n=10)
        counts = {s["file_path"]: s["change_count"] for s in spots}

        # Both files are indented (real git --name-only format), so both counted
        assert counts.get("src/main.py", 0) == 1
        assert counts.get("src/util.py", 0) == 1

    def test_hot_spots_resets_on_blank_line(self, monkeypatch, tmp_path):
        """Blank lines reset the commit block state."""
        git_output = (
            "ec0b4f0b5c90ed0fa911a2972ccc452641b31563\n"
            "\tsrc/file1.py\n"
            "\n"
            "54563f95fefa691baa82a522156322c21f7d6df3\n"
            "\tsrc/file2.py\n"
        )

        def mock_run_git(project_path, args):
            return git_output

        import mempalace.recent_changes as rc
        monkeypatch.setattr(rc, "_run_git", mock_run_git)

        spots = get_hot_spots(str(tmp_path), n=10)
        counts = {s["file_path"]: s["change_count"] for s in spots}

        assert counts.get("src/file1.py", 0) == 1
        assert counts.get("src/file2.py", 0) == 1

    def test_hot_spots_hash_not_indentation(self, monkeypatch, tmp_path):
        """
        Parser must use hash detection (not indentation) as the state-machine trigger.
        A non-indented line that is NOT a hash should NOT be treated as a filename.
        Only lines following a hash commit line are filenames.
        """
        # Non-hash, non-indented line between commits (e.g., unexpected format/wrapper)
        # must NOT be treated as a filename — only indented lines AFTER a hash count.
        git_output = (
            "4ba10998a6ebceb06ecf1a94bb2970c91ff19a67\n"
            "\tsrc/main.py\n"
            "some unexpected wrapper text\n"      # not indented, not a hash → should be ignored
            "\tsrc/util.py\n"
            "\n"
            "b9469a95e64ad83017429739bd95b527100cdfec\n"
            "\tsrc/main.py\n"
        )

        def mock_run_git(project_path, args):
            return git_output

        import mempalace.recent_changes as rc
        monkeypatch.setattr(rc, "_run_git", mock_run_git)

        spots = get_hot_spots(str(tmp_path), n=10)
        counts = {s["file_path"]: s["change_count"] for s in spots}

        # Both src files are correctly counted despite unexpected non-indented text
        assert counts.get("src/main.py", 0) == 2
        assert counts.get("src/util.py", 0) == 1
        # "some unexpected wrapper text" must NOT appear as a file
        unexpected = [s for s in spots if "unexpected" in s["file_path"]]
        assert len(unexpected) == 0, "Non-indented non-hash lines must not be treated as filenames"

    def test_hot_spots_only_counted_after_hash(self, monkeypatch, tmp_path):
        """
        Files listed before any hash line must NOT be counted.
        Verifies state machine enters commit block ONLY after seeing a hash.
        """
        git_output = (
            "\tsrc/before.py\n"    # before any hash → should NOT be counted
            "4ba10998a6ebceb06ecf1a94bb2970c91ff19a67\n"
            "\tsrc/main.py\n"
        )

        def mock_run_git(project_path, args):
            return git_output

        import mempalace.recent_changes as rc
        monkeypatch.setattr(rc, "_run_git", mock_run_git)

        spots = get_hot_spots(str(tmp_path), n=10)
        counts = {s["file_path"]: s["change_count"] for s in spots}

        assert counts.get("src/before.py", 0) == 0, "Files before first hash must not be counted"
        assert counts.get("src/main.py", 0) == 1


class TestSymbolIndexThreadSafety:
    """Verify SymbolIndex operations are serialized with per-instance lock."""

    def test_concurrent_updates_are_safe(self, tmp_path):
        """Multiple threads updating the index should not corrupt the DB."""
        import threading
        # Use a unique path key to avoid cached instances from other tests
        key = f"si_concurrent_{id(tmp_path)}"
        # Ensure no cached instance for this key
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        errors = []
        barrier = threading.Barrier(4)

        def worker(worker_id):
            try:
                barrier.wait()
                for i in range(20):
                    # Each file must be unique to avoid INSERT OR REPLACE overwriting
                    si.update_file(
                        f"/src/worker{worker_id}_{i}.py",
                        f"def func_{worker_id}_{i}(): pass\nclass Class_{worker_id}_{i}: pass"
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Thread errors: {errors}"
        stats = si.stats()
        # 4 workers × 20 files × 2 symbols (1 func + 1 class) = 160
        assert stats["total_symbols"] == 4 * 20 * 2, f"Expected 160 symbols, got {stats}"

    def test_concurrent_reads_and_writes(self, tmp_path):
        """Concurrent reads while writing should not crash or return corrupt data."""
        import threading
        key = f"si_readwrite_{id(tmp_path)}"
        SymbolIndex._instances.pop(key, None)
        si = SymbolIndex.get(key)

        # Pre-populate
        for i in range(10):
            si.update_file(f"/src/f{i}.py", f"def fn{i}(): pass\nclass C{i}: pass")

        barrier = threading.Barrier(3)
        read_results = []
        write_errors = []

        def reader():
            barrier.wait()
            for _ in range(30):
                try:
                    r = si.find_symbol("fn5")
                    read_results.append(r)
                except Exception as e:
                    read_results.append(e)

        def writer():
            barrier.wait()
            for i in range(10, 20):
                try:
                    si.update_file(f"/src/f{i}.py", f"def fn{i}(): pass")
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
            t.join(timeout=15)

        assert not write_errors, f"Write errors: {write_errors}"
        assert len(read_results) == 60
        # All successful reads should be lists (not exceptions)
        for r in read_results:
            assert isinstance(r, list), f"Read returned non-list: {type(r)}"


class TestRecentChangesParser:
    """Verify get_recent_changes correctly parses null-byte-delimited git log."""

    def test_recent_changes_null_byte_delimiter(self, monkeypatch, tmp_path):
        """Null-byte delimiter should not break on pipe chars in commit messages."""
        # Format: %H%x00%ad%x00%s = hash + null + date + null + subject
        git_log_output = (
            "abc123def456789012345678901234567890abcd\x00"
            "2026-04-01T10:00:00+00:00\x00"
            "feat: add | pipe support\x00"
            "def456abc789012345678901234567890abcdef\x00"
            "2026-04-02T11:00:00+00:00\x00"
            "fix: core | critical\x00"
        )

        def mock_run_git(project_path, args):
            return git_log_output

        monkeypatch.setattr("mempalace.recent_changes._run_git", mock_run_git)

        # Need to mock diff-tree too
        diff_tree_calls = {}
        def mock_diff_tree(project_path, args):
            commit_hash = args[-1]
            if commit_hash == "abc123def456789012345678901234567890abcd":
                return "src/main.py"
            elif commit_hash == "def456abc789012345678901234567890abcdef":
                return "src/util.py"
            return ""

        import mempalace.recent_changes as rc
        original_run_git = rc._run_git
        call_count = [0]
        def patched_run_git(project_path, args):
            call_count[0] += 1
            if "diff-tree" in args:
                return mock_diff_tree(project_path, args)
            return original_run_git(project_path, args)
        monkeypatch.setattr(rc, "_run_git", patched_run_git)

        changes = get_recent_changes(str(tmp_path), n=5)

        # Should have 2 changes
        assert len(changes) == 2, f"Expected 2 changes, got {len(changes)}: {changes}"
        # Commit messages should include pipe chars (not split)
        msgs = {c["commit_hash"]: c["commit_message"] for c in changes}
        assert "feat: add | pipe support" in msgs["abc123d"], f"Got: {msgs}"
        assert "fix: core | critical" in msgs["def456a"], f"Got: {msgs}"

    def test_recent_changes_date_parsing_with_timezone(self, monkeypatch, tmp_path):
        """Date parsing should handle iso-strict format with timezone."""
        git_log_output = (
            "abc123def456789012345678901234567890abcd\x00"
            "2026-04-01T10:00:00+00:00\x00"
            "test commit\x00"
        )

        def mock_diff_tree(project_path, args):
            return "src/main.py"

        import mempalace.recent_changes as rc
        original_run_git = rc._run_git

        def patched_run_git(project_path, args):
            args_str = str(args)
            if "diff-tree" in args_str:
                return mock_diff_tree(project_path, args)
            if "log" in args_str:
                return git_log_output
            return original_run_git(project_path, args)

        monkeypatch.setattr(rc, "_run_git", patched_run_git)

        # Should not raise on timezone-aware date
        changes = get_recent_changes(str(tmp_path), n=5)
        assert len(changes) >= 1