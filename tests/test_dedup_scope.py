"""
tests/test_dedup_scope.py

Dedup scope policy tests for _dedup_scope_matches().
Verifies that code/repo chunks are only deduplicated within the same source_file
(and optionally same chunk_index), and never across projects or files.
"""

import pytest

pytest.importorskip("lancedb", reason="LanceDB required for mempalace.backends.lance")

from mempalace.backends.lance import _dedup_scope_matches


class TestDedupScopeMatches:
    """Policy: same source_file + same chunk_index → duplicate. Everything else varies."""

    def test_same_source_file_same_chunk_index(self):
        """Identical source_file and chunk_index → same dedup scope (True)."""
        new_meta = {"source_file": "src/auth.py", "chunk_index": 0}
        old_meta = {"source_file": "src/auth.py", "chunk_index": 0}
        assert _dedup_scope_matches(new_meta, old_meta) is True

    def test_same_source_file_different_chunk_index(self):
        """Same source_file but different chunk_index → different scope (False)."""
        new_meta = {"source_file": "src/auth.py", "chunk_index": 1}
        old_meta = {"source_file": "src/auth.py", "chunk_index": 0}
        assert _dedup_scope_matches(new_meta, old_meta) is False

    def test_different_source_file(self):
        """Different source_file → different scope (False), even without chunk_index."""
        new_meta = {"source_file": "src/auth.py", "chunk_index": 0}
        old_meta = {"source_file": "src/utils.py", "chunk_index": 0}
        assert _dedup_scope_matches(new_meta, old_meta) is False

    def test_one_has_source_file_other_not(self):
        """One has source_file, other doesn't → not same scope (False)."""
        new_meta = {"source_file": "src/auth.py", "chunk_index": 0}
        old_meta = {"chunk_index": 0}
        assert _dedup_scope_matches(new_meta, old_meta) is False

        new_meta = {"chunk_index": 0}
        old_meta = {"source_file": "src/auth.py", "chunk_index": 0}
        assert _dedup_scope_matches(new_meta, old_meta) is False

    def test_neither_has_source_file(self):
        """Neither has source_file → legacy dedup allowed (True)."""
        new_meta = {"content": "some memory"}
        old_meta = {"content": "some memory"}
        assert _dedup_scope_matches(new_meta, old_meta) is True

    def test_different_source_file_no_chunk_index(self):
        """Different source_file without chunk_index → False."""
        new_meta = {"source_file": "projA/src/auth.py"}
        old_meta = {"source_file": "projB/src/auth.py"}
        assert _dedup_scope_matches(new_meta, old_meta) is False
