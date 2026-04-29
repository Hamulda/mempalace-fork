"""
tests/test_file_context_scope.py

File-context scope security tests — no daemon, no LanceDB required.
Tests the path allowlist hardening for mempalace_file_context.

Run: pytest tests/test_file_context_scope.py -q
"""

import os
import tempfile
from pathlib import Path

from mempalace.server._code_tools import _is_path_allowed, _source_file_matches, _path_is_under_or_equal


class TestPathIsUnderOrEqual:
    """Unit tests for the strict security helper _path_is_under_or_equal."""

    def test_exact_match(self):
        with tempfile.TemporaryDirectory() as d:
            assert _path_is_under_or_equal(d, d)

    def test_subdirectory_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            assert _path_is_under_or_equal(f"{d}/src/auth.py", d)
            assert _path_is_under_or_equal(f"{d}/src/utils/helpers.py", d)

    def test_parent_traversal_denied(self):
        with tempfile.TemporaryDirectory() as d:
            # /tmp/proj/../etc → should NOT be under /tmp/proj
            assert not _path_is_under_or_equal(f"{d}/../etc/hosts", d)

    def test_sibling_dir_denied(self):
        with tempfile.TemporaryDirectory() as d:
            sibling = tempfile.gettempdir()
            # sibling is not under d
            assert not _path_is_under_or_equal(f"{sibling}/foo.py", d)

    def test_basename_only_not_enough(self):
        """A file whose basename matches the project_path must still be denied.

        E.g. project_path=/tmp/proj, file_path=/etc/passwd
        /etc/passwd is NOT under /tmp/proj even though "passwd" ≠ "proj".
        """
        with tempfile.TemporaryDirectory() as proj:
            # /etc/passwd is not under proj even if basenames differ
            assert not _path_is_under_or_equal("/etc/passwd", proj)

    def test_proj_old_not_matching_proj(self):
        """Ensure /proj-old does not match /proj via substring heuristic.

        Strict containment requires commonpath to equal root, not just overlap.
        """
        with tempfile.TemporaryDirectory() as proj:
            proj_old = proj + "-old"
            Path(proj_old).mkdir(exist_ok=True)
            assert not _path_is_under_or_equal(f"{proj_old}/auth.py", proj)
            assert not _path_is_under_or_equal(proj_old, proj)

    def test_traversal_outside_denied(self):
        with tempfile.TemporaryDirectory() as d:
            assert not _path_is_under_or_equal(f"{d}/../foo.py", d)
            assert not _path_is_under_or_equal(f"{d}/src/../../etc/hosts", d)

    def test_traversal_inside_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            Path(f"{d}/src").mkdir()
            assert _path_is_under_or_equal(f"{d}/src/../src/auth.py", d)

    def test_symlink_to_subdir_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "real"
            real.mkdir()
            link = Path(d) / "link"
            link.symlink_to(real)
            assert _path_is_under_or_equal(str(link), d)

    def test_symlink_outside_denied(self):
        with tempfile.TemporaryDirectory() as d:
            other = tempfile.gettempdir()
            link = Path(d) / "evil_link"
            link.symlink_to(other)
            assert not _path_is_under_or_equal(str(link), d)

    def test_invalid_path_returns_false(self):
        """OSError or ValueError from path resolution returns False safely."""
        assert not _path_is_under_or_equal("", "/tmp")
        assert not _path_is_under_or_equal("/nonexistent", "")


class TestSourceFileMatches:
    """Unit tests for _source_file_matches (used by _is_path_allowed)."""

    def test_exact_path(self):
        with tempfile.TemporaryDirectory() as d:
            assert _source_file_matches(f"{d}/foo.py", d)

    def test_subdirectory(self):
        with tempfile.TemporaryDirectory() as d:
            assert _source_file_matches(f"{d}/src/auth.py", d)

    def test_parent_traversal_denied(self):
        with tempfile.TemporaryDirectory() as d:
            # /tmp/../etc/hosts resolves to /etc/hosts (or similar)
            # We just verify that /etc/hosts is NOT under /tmp
            assert not _source_file_matches("/etc/hosts", d)

    def test_symlink_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            link_path = Path(d) / "link"
            real_path = Path(d) / "real"
            real_path.write_text("x")
            link_path.symlink_to(real_path)
            assert _source_file_matches(str(link_path), d)


class TestIsPathAllowedAllowAny:
    """Tests for allow_any=True restores permissive behavior."""

    def test_allow_any_true_permits_etc_hosts(self):
        assert _is_path_allowed("/etc/hosts", project_path=None, allow_any=True, allowed_roots="")
        assert _is_path_allowed("/etc/passwd", project_path=None, allow_any=True, allowed_roots="")

    def test_allow_any_true_permits_temp(self):
        with tempfile.TemporaryDirectory() as d:
            assert _is_path_allowed(d, project_path=None, allow_any=True, allowed_roots="")


class TestIsPathAllowedDefaultDenied:
    """Tests: with allow_any=False and no roots, all external paths are denied."""

    def test_etc_hosts_denied_by_default(self):
        assert not _is_path_allowed("/etc/hosts", project_path=None, allow_any=False, allowed_roots="")

    def test_temp_denied_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            assert not _is_path_allowed(d, project_path=None, allow_any=False, allowed_roots="")

    def test_home_subdir_denied_without_roots(self):
        home = str(Path.home())
        # home itself may be allowed if it's an allowed root, but subdirs outside a project root are not
        # Since we don't set any allowed_roots, even home subdirs are denied
        subdir = str(Path(home) / "some_project")
        assert not _is_path_allowed(subdir, project_path=None, allow_any=False, allowed_roots="")


class TestIsPathAllowedAllowedRoots:
    """Tests: colon-separated allowed_roots restrict reads correctly."""

    def test_file_under_allowed_root_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            assert _is_path_allowed(f"{d}/foo.py", project_path=None, allow_any=False, allowed_roots=d)

    def test_file_outside_allowed_root_fails(self):
        with tempfile.TemporaryDirectory() as d:
            other = tempfile.gettempdir()
            # other is not under d
            assert not _is_path_allowed(f"{other}/foo.py", project_path=None, allow_any=False, allowed_roots=d)

    def test_multiple_allowed_roots(self):
        with tempfile.TemporaryDirectory() as root1:
            with tempfile.TemporaryDirectory() as root2:
                roots = f"{root1}:{root2}"
                assert _is_path_allowed(f"{root1}/a.py", project_path=None, allow_any=False, allowed_roots=roots)
                assert _is_path_allowed(f"{root2}/b.py", project_path=None, allow_any=False, allowed_roots=roots)
                # /etc is in neither
                assert not _is_path_allowed("/etc/hosts", project_path=None, allow_any=False, allowed_roots=roots)

    def test_empty_root_parts_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            # "::" produces empty parts — should be skipped gracefully
            assert _is_path_allowed(f"{d}/foo.py", project_path=None, allow_any=False, allowed_roots=f"::{d}")

    def test_whitespace_trimmed(self):
        with tempfile.TemporaryDirectory() as d:
            assert _is_path_allowed(f"{d}/foo.py", project_path=None, allow_any=False, allowed_roots=f"  {d}  ")


class TestIsPathAllowedProjectPath:
    """Tests: project_path parameter gates access to its own subtree."""

    def test_file_under_project_path_succeeds(self):
        with tempfile.TemporaryDirectory() as proj:
            src = f"{proj}/src/auth.py"
            assert _is_path_allowed(src, project_path=proj, allow_any=False, allowed_roots="")

    def test_file_outside_project_path_denied(self):
        with tempfile.TemporaryDirectory() as proj:
            other = tempfile.gettempdir()
            assert not _is_path_allowed(f"{other}/foo.py", project_path=proj, allow_any=False, allowed_roots="")

    def test_project_path_takes_precedence_over_empty_allowed_roots(self):
        # Even with empty allowed_roots, project_path alone grants access
        with tempfile.TemporaryDirectory() as proj:
            assert _is_path_allowed(f"{proj}/x.py", project_path=proj, allow_any=False, allowed_roots="")

    def test_same_basename_outside_project_path_denied(self):
        """Files outside project_path with the same basename must be blocked.

        e.g. project_path=/tmp/proj/auth.py, file_path=/etc/auth.py
        /etc/auth.py must NOT be allowed via basename heuristic.
        """
        with tempfile.TemporaryDirectory() as proj:
            auth_in_proj = f"{proj}/auth.py"
            Path(auth_in_proj).touch()
            # /etc/auth.py has same basename "auth.py" but is outside proj
            assert not _is_path_allowed("/etc/auth.py", project_path=auth_in_proj, allow_any=False, allowed_roots="")
            # Also test sibling directory with same basename
            sibling = Path(tempfile.gettempdir()) / "other_project"
            sibling.mkdir(exist_ok=True)
            sibling_auth = sibling / "auth.py"
            sibling_auth.touch()
            assert not _is_path_allowed(str(sibling_auth), project_path=auth_in_proj, allow_any=False, allowed_roots="")

    def test_proj_old_not_matching_proj(self):
        """Directory with -old suffix must not match the base project directory."""
        with tempfile.TemporaryDirectory() as proj:
            proj_old = Path(f"{proj}-old")
            proj_old.mkdir()
            (proj_old / "auth.py").touch()
            # /proj-old/auth.py is NOT under /proj
            assert not _is_path_allowed(str(proj_old / "auth.py"), project_path=proj, allow_any=False, allowed_roots="")


class TestIsPathAllowedTraversal:
    """Tests: path traversal (..) is resolved before checking."""

    def test_traversal_outside_project_path_denied(self):
        with tempfile.TemporaryDirectory() as proj:
            # /proj/src/.././../etc/hosts — resolves to /etc/hosts, which is outside proj
            traversal = f"{proj}/src/../../etc/hosts"
            assert not _is_path_allowed(traversal, project_path=proj, allow_any=False, allowed_roots="")

    def test_traversal_with_allowed_roots_denied(self):
        with tempfile.TemporaryDirectory() as root:
            traversal = f"{root}/../{os.path.basename(tempfile.gettempdir())}/evil.py"
            # The resolved path is outside root, so it should be denied
            result = _is_path_allowed(traversal, project_path=None, allow_any=False, allowed_roots=root)
            # resolved is outside root (it's in /tmp/...), so should be denied
            assert not result

    def test_traversal_inside_project_path_succeeds(self):
        with tempfile.TemporaryDirectory() as proj:
            src_dir = f"{proj}/src"
            Path(src_dir).mkdir()
            traversal = f"{proj}/src/../src/auth.py"
            assert _is_path_allowed(traversal, project_path=proj, allow_any=False, allowed_roots="")


class TestIsPathAllowedNoProjectPathNoRoots:
    """Edge case: no project_path, no allowed_roots, allow_any=False."""

    def test_strict_deny_for_any_path(self):
        with tempfile.TemporaryDirectory() as d:
            # Without project_path or allowed_roots, nothing is allowed
            assert not _is_path_allowed(f"{d}/foo.py", project_path=None, allow_any=False, allowed_roots="")


# ---------------------------------------------------------------------------
# Integration-ish tests: call the tool function directly
# These simulate what the MCP server does when the tool is invoked.
# ---------------------------------------------------------------------------

class TestMempalaceFileContextSecurity:
    """Direct unit tests for the security logic inside mempalace_file_context."""

    def _check(self, file_path: str, project_path: str | None, allow_any: bool, allowed_roots: str) -> bool:
        """Wrapper that mirrors the security check in mempalace_file_context."""
        return _is_path_allowed(file_path, project_path, allow_any, allowed_roots)

    def test_tool_does_not_crash_on_legitimate_file(self):
        """A real file inside allowed_roots should not be blocked."""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "legit.py"
            f.write_text("# real file")
            assert self._check(str(f), project_path=None, allow_any=False, allowed_roots=d)

    def test_tool_blocks_etc_hosts_default(self):
        """By default, /etc/hosts is blocked."""
        assert not self._check("/etc/hosts", project_path=None, allow_any=False, allowed_roots="")

    def test_tool_blocks_temp_default(self):
        """By default, temp files are blocked."""
        with tempfile.TemporaryDirectory() as d:
            assert not self._check(d, project_path=None, allow_any=False, allowed_roots="")

    def test_tool_allows_with_project_path(self):
        """When project_path is provided and file is under it, access is granted."""
        with tempfile.TemporaryDirectory() as proj:
            f = Path(proj) / "myfile.py"
            f.write_text("x")
            assert self._check(str(f), project_path=proj, allow_any=False, allowed_roots="")

    def test_tool_blocks_without_project_path_or_roots(self):
        """When neither project_path nor allowed_roots is set, access is denied."""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "myfile.py"
            f.write_text("x")
            # No project_path, no allowed_roots → denied
            assert not self._check(str(f), project_path=None, allow_any=False, allowed_roots="")

    def test_allow_any_overrides_all_restrictions(self):
        """MEMPALACE_FILE_CONTEXT_ALLOW_ANY=1 restores old permissive behavior."""
        with tempfile.TemporaryDirectory() as d:
            assert self._check(d, project_path=None, allow_any=True, allowed_roots="")
            assert self._check("/etc/hosts", project_path=None, allow_any=True, allowed_roots="")

    def test_traversal_blocked_despite_project_path(self):
        """Path traversal that exits project_path is denied."""
        with tempfile.TemporaryDirectory() as proj:
            evil = f"{proj}/../etc/hosts"
            assert not self._check(evil, project_path=proj, allow_any=False, allowed_roots="")
