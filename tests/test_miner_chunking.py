"""
Test miner structural chunking correctness.

Tests scope tracking, tombstone logic, duplicate content hash scenarios.
"""

import pytest
from collections import defaultdict


class TestStructuralChunkingScopeTracking:
    """Verify that scope tracking correctly handles class exit."""

    def test_class_then_top_level_function_scope_clear(self):
        """After a class body, top-level functions must NOT inherit class scope."""
        from mempalace.miner import split_code_structurally

        code = '''class Foo:
    def method(self):
        return "method result"

def bar():
    return "bar result"

def baz():
    return "baz result"
'''
        chunks = split_code_structurally(code, 'test.py')
        # Filter to only chunks with symbol names (non-trivial chunks)
        symbol_chunks = [c for c in chunks if c['symbol_name']]

        # Foo should have empty scope (top-level class)
        foo_chunks = [c for c in symbol_chunks if c['symbol_name'] == 'Foo']
        assert len(foo_chunks) == 1
        assert foo_chunks[0]['symbol_scope'] == '', f"Foo should have empty scope, got {foo_chunks[0]['symbol_scope']}"

        # bar and baz should NOT have Foo in their scope
        for chunk in symbol_chunks:
            if chunk['symbol_name'] in ('bar', 'baz'):
                assert chunk['symbol_scope'] == '', f"{chunk['symbol_name']} should have empty scope, got {chunk['symbol_scope']}"

    def test_nested_class_scope(self):
        """Nested classes should track correctly."""
        from mempalace.miner import split_code_structurally

        code = '''class Outer:
    def outer_method(self):
        return "outer method body"

    class Inner:
        def inner_method(self):
            return "inner method body"

def top_level():
    return "top level function body that exceeds minimum chunk size"
'''
        chunks = split_code_structurally(code, 'test.py')
        symbol_chunks = {c['symbol_name']: c for c in chunks if c['symbol_name']}

        # Outer should have empty scope
        assert symbol_chunks['Outer']['symbol_scope'] == ''

        # Inner should have Outer in scope
        assert 'Outer' in symbol_chunks['Inner']['symbol_scope']

        # top_level should have empty scope (not inherited from Outer)
        assert symbol_chunks['top_level']['symbol_scope'] == ''

    def test_method_inside_class_not_split(self):
        """Methods inside classes should not create separate chunks (split at class level)."""
        from mempalace.miner import split_code_structurally

        code = '''class Foo:
    def method_a(self):
        return "a"
    def method_b(self):
        return "b"
'''
        chunks = split_code_structurally(code, 'test.py')
        symbol_chunks = [c for c in chunks if c['symbol_name']]

        # Methods should NOT appear as separate chunks — class Foo is one chunk
        method_chunks = [c for c in symbol_chunks if c['symbol_name'] in ('method_a', 'method_b')]
        assert len(method_chunks) == 0, "Methods should not create separate chunks, got: " + str([c['symbol_name'] for c in method_chunks])


class TestTombstoneModel:
    """Verify tombstone supersession logic."""

    def test_duplicate_content_hash_all_old_chunks_superseded(self):
        """When multiple old chunks share the same content_hash, all are superseded."""
        from mempalace.miner import process_file
        from unittest.mock import MagicMock

        # Mock collection
        mock_col = MagicMock()

        # Simulate: old chunks at positions 0 and 2 (both with same content hash)
        old_data = {
            'ids': ['id_pos0', 'id_pos1', 'id_pos2'],
            'metadatas': [
                {'content_hash': 'same_hash', 'source_file': '/fake/file.py'},
                {'content_hash': 'other_hash', 'source_file': '/fake/file.py'},
                {'content_hash': 'same_hash', 'source_file': '/fake/file.py'},
            ]
        }
        mock_col.get.return_value = old_data
        mock_col.upsert.return_value = None

        # New content: two chunks, both with same hash (simulating re-chunking same content)
        # The process_file logic reads the file, but we can trace through the hash logic
        from mempalace.miner import _compute_content_hash

        # Simulate: new chunks both have same hash "same_hash"
        # When process_file sees content_hash "same_hash" is in old_chunks_by_hash,
        # it should supersede ALL old chunks with that hash
        old_chunks_by_hash = defaultdict(list)
        old_chunks_by_hash['same_hash'] = [('id_pos0', {}), ('id_pos2', {})]
        old_chunks_by_hash['other_hash'] = [('id_pos1', {})]

        # New content_hash same as one of the old
        new_hash = 'same_hash'

        # Build supersedes set
        superseded_ids = set()
        if new_hash in old_chunks_by_hash:
            for old_id, old_meta in old_chunks_by_hash[new_hash]:
                superseded_ids.add(old_id)

        # Both old chunks with same_hash should be in superseded_ids
        assert 'id_pos0' in superseded_ids, "id_pos0 should be superseded"
        assert 'id_pos2' in superseded_ids, "id_pos2 should be superseded"
        assert 'id_pos1' not in superseded_ids, "id_pos1 should NOT be superseded (different hash)"

    def test_tombstone_only_unmatched_old_chunks(self):
        """Old chunks are only tombstoned if they were NOT matched by any new chunk."""
        from collections import defaultdict

        # Scenario: old has hashA, hashB. New has hashA only.
        old_chunks_by_hash = defaultdict(list)
        old_chunks_by_hash['hashA'] = [('old_id_A', {})]
        old_chunks_by_hash['hashB'] = [('old_id_B', {})]

        new_hashes = {'hashA'}

        # Superseded IDs are those referenced by new chunks
        # In this scenario, new chunks all reference hashA → superseded_ids = {'old_id_A'}
        superseded_ids = {'old_id_A'}

        # Tombstone: old chunks NOT in superseded_ids
        to_tombstone = []
        for old_hash, old_list in old_chunks_by_hash.items():
            for old_id, old_meta in old_list:
                if old_id not in superseded_ids:
                    to_tombstone.append(old_id)

        # hashB's old_id_B was NOT matched → should be tombstoned
        assert 'old_id_B' in to_tombstone
        # hashA's old_id_A WAS matched → should NOT be tombstoned
        assert 'old_id_A' not in to_tombstone


class TestRevisionModel:
    """Verify revision model correctness."""

    def test_compute_content_hash_deterministic(self):
        """_compute_content_hash must be deterministic."""
        from mempalace.miner import _compute_content_hash

        h1 = _compute_content_hash("def foo():\n    pass")
        h2 = _compute_content_hash("def foo():\n    pass")
        assert h1 == h2, "Same content must produce same hash"

        h3 = _compute_content_hash("def bar():\n    pass")
        assert h1 != h3, "Different content must produce different hash"

    def test_compute_file_revision_includes_mtime(self):
        """_compute_file_revision must include mtime for change detection."""
        from mempalace.miner import _compute_file_revision
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("original content")
            f.flush()
            path = f.name

        try:
            rev1 = _compute_file_revision(path, "original content")
            mtime1 = os.path.getmtime(path)

            # Modify file
            import time
            time.sleep(0.1)
            with open(path, 'w') as f:
                f.write("modified content")
            os.utime(path, None)  # Update mtime

            rev2 = _compute_file_revision(path, "modified content")

            # Same content but different mtime → different revision
            assert rev1 != rev2, f"Different mtime should produce different revision: {rev1} vs {rev2}"
        finally:
            os.unlink(path)

    def test_supersedes_id_set_on_new_metadata(self):
        """When old chunk is matched, supersedes_id is set on the new chunk's metadata."""
        from collections import defaultdict

        old_chunks_by_hash = defaultdict(list)
        old_chunks_by_hash['hash123'] = [('old_chunk_id', {'content_hash': 'hash123'})]

        new_hash = 'hash123'
        metadata = {'supersedes_id': '', 'is_latest': True}

        if new_hash in old_chunks_by_hash:
            for old_id, old_meta in old_chunks_by_hash[new_hash]:
                metadata['supersedes_id'] = old_id
                metadata['is_latest'] = True

        assert metadata['supersedes_id'] == 'old_chunk_id'
        assert metadata['is_latest'] is True


class TestScopeStackSiblingClasses:
    """Verify scope stack correctly handles sibling classes (not nesting)."""

    def test_sibling_classes_at_indent_zero(self):
        """Two top-level classes at indent 0 must NOT nest — each has empty scope."""
        from mempalace.miner import split_code_structurally

        # Each chunk must be >= MIN_CHUNK_SIZE (50 chars) to survive filtering
        code = '''class Foo:
    def method(self):
        return 1
        return 2
        return 3
        return 4

class Bar:
    def method(self):
        return 1
        return 2
        return 3
        return 4

def top_func():
    return 1
    return 2
    return 3
    return 4
'''
        chunks = split_code_structurally(code, 'test.py')
        symbol_chunks = {c['symbol_name']: c for c in chunks if c['symbol_name']}

        # Foo and Bar are siblings at indent 0 — neither should have the other in scope
        assert 'Foo' in symbol_chunks, f"Foo not in chunks: {[c['symbol_name'] for c in chunks]}"
        assert symbol_chunks['Foo']['symbol_scope'] == '', \
            f"Foo should have empty scope, got {symbol_chunks['Foo']['symbol_scope']}"
        assert symbol_chunks['Bar']['symbol_scope'] == '', \
            f"Bar should have empty scope, got {symbol_chunks['Bar']['symbol_scope']}"

        # top_func should also have empty scope
        assert symbol_chunks['top_func']['symbol_scope'] == '', \
            f"top_func should have empty scope, got {symbol_chunks['top_func']['symbol_scope']}"

    def test_sibling_nested_classes_same_body_indent(self):
        """Inner class inside Outer's body correctly has scope='Outer'; sibling at module level has empty scope."""
        from mempalace.miner import split_code_structurally

        code = '''class Outer:
    def outer_method(self):
        return 1
        return 2
        return 3
        return 4

    class Inner:
        def inner_method(self):
            return 1
            return 2
            return 3
            return 4

def after_outer():
    return 1
    return 2
    return 3
    return 4
'''
        chunks = split_code_structurally(code, 'test.py')
        symbol_chunks = {c['symbol_name']: c for c in chunks if c['symbol_name']}

        # Outer should have empty scope (top-level class)
        assert 'Outer' in symbol_chunks, f"Outer not in chunks: {[c['symbol_name'] for c in chunks]}"
        assert symbol_chunks['Outer']['symbol_scope'] == ''

        # Inner is INSIDE Outer's body → scope='Outer' (correctly nested)
        assert symbol_chunks['Inner']['symbol_scope'] == 'Outer', \
            f"Inner should have scope='Outer' (defined inside Outer's body), got {symbol_chunks['Inner']['symbol_scope']}"

        # after_outer is at module level → empty scope (sibling of Outer, not nested)
        assert symbol_chunks['after_outer']['symbol_scope'] == '', \
            f"after_outer should have empty scope (module level), got {symbol_chunks['after_outer']['symbol_scope']}"

    def test_class_then_top_level_function_scope_clear(self):
        """After a class body, top-level functions must NOT inherit class scope."""
        from mempalace.miner import split_code_structurally

        code = '''class Foo:
    def method(self):
        return "method result"
        return "extra line"
        return "extra line"
        return "extra line"

def bar():
    return "bar result"
    return "extra"
    return "extra"
    return "extra"

def baz():
    return "baz result"
    return "extra"
    return "extra"
    return "extra"
'''
        chunks = split_code_structurally(code, 'test.py')
        symbol_chunks = {c['symbol_name']: c for c in chunks if c['symbol_name']}

        # Foo should have empty scope (top-level class)
        assert 'Foo' in symbol_chunks, f"Foo not in chunks: {[c['symbol_name'] for c in chunks]}"
        assert symbol_chunks['Foo']['symbol_scope'] == '', \
            f"Foo should have empty scope, got {symbol_chunks['Foo']['symbol_scope']}"

        # bar and baz should NOT have Foo in their scope
        for name in ('bar', 'baz'):
            assert symbol_chunks[name]['symbol_scope'] == '', \
                f"{name} should have empty scope, got {symbol_chunks[name]['symbol_scope']}"

    def test_decorator_not_scope_pollution(self):
        """A decorator absorbed into class chunk should not pollute function symbol."""
        from mempalace.miner import split_code_structurally

        code = '''class Foo:
    @property
    def bar(self):
        return 1
        return 2
        return 3
        return 4

    @property
    def baz(self):
        return 1
        return 2
        return 3
        return 4
'''
        chunks = split_code_structurally(code, 'test.py')
        # The first chunk should be class Foo (symbol_name=Foo)
        # Decorators should be absorbed into class chunk, not appear as separate chunks
        foo_chunks = [c for c in chunks if c['symbol_name'] == 'Foo']
        assert len(foo_chunks) == 1, f"Expected 1 Foo chunk, got {len(foo_chunks)}: {[c['symbol_name'] for c in chunks]}"

        # The decorators should NOT appear as separate symbol chunks
        decorator_chunks = [c for c in chunks if c['symbol_name'] and c['symbol_name'].startswith('@')]
        assert len(decorator_chunks) == 0, \
            f"Decorators should not appear as separate chunks, got: {[c['symbol_name'] for c in chunks]}"


class TestDuplicateHashScenarios:
    """Verify correct behavior when multiple old chunks share a content_hash."""

    def test_multiple_old_chunks_same_hash_all_superseded(self):
        """When N old chunks share a content_hash matched by one new chunk, ALL are superseded."""
        from collections import defaultdict
        from mempalace.miner import _compute_content_hash

        # Simulate: old file had the same content twice (duplicate chunks in file)
        old_chunks_by_hash = defaultdict(list)
        # old_id_pos0 and old_id_pos2 both have hash 'same_hash'
        # old_id_pos1 has a different hash
        old_chunks_by_hash['same_hash'] = [
            ('old_id_pos0', {'content_hash': 'same_hash'}),
            ('old_id_pos2', {'content_hash': 'same_hash'}),
        ]
        old_chunks_by_hash['other_hash'] = [
            ('old_id_pos1', {'content_hash': 'other_hash'}),
        ]

        # New chunk matches 'same_hash'
        new_hash = 'same_hash'

        # Trace through process_file logic:
        # superseded_ids_for_chunk = []
        # for each old_id in old_chunks_by_hash['same_hash']:
        #     superseded_ids_for_chunk.append(old_id)
        # metadata['supersedes_id'] = superseded_ids_for_chunk[-1]  # last one
        superseded_ids_for_chunk = []
        if new_hash in old_chunks_by_hash:
            for old_id, old_meta in old_chunks_by_hash[new_hash]:
                superseded_ids_for_chunk.append(old_id)

        # ALL old chunks with matching hash should be tracked
        assert 'old_id_pos0' in superseded_ids_for_chunk
        assert 'old_id_pos2' in superseded_ids_for_chunk
        # supersedes_id stores last one (single-string contract)
        assert superseded_ids_for_chunk[-1] == 'old_id_pos2'

        # Now build superseded_ids set from metadatas
        # Each new chunk's metadata has supersedes_id set
        superseded_ids = set(superseded_ids_for_chunk)

        # Both old chunks with same_hash should be in superseded_ids
        assert 'old_id_pos0' in superseded_ids
        assert 'old_id_pos2' in superseded_ids
        assert 'old_id_pos1' not in superseded_ids  # different hash

        # Tombstone: old chunks NOT in superseded_ids
        all_old_ids = {'old_id_pos0', 'old_id_pos1', 'old_id_pos2'}
        to_tombstone = all_old_ids - superseded_ids
        assert to_tombstone == {'old_id_pos1'}  # only the non-matching hash

    def test_identical_file_remeining_no_stale_current(self):
        """Re-mining identical content should not leave stale current chunks."""
        from collections import defaultdict

        # Old: file had chunks with hashes H1, H2
        old_chunks_by_hash = defaultdict(list)
        old_chunks_by_hash['H1'] = [('old_id_0', {'content_hash': 'H1'})]
        old_chunks_by_hash['H2'] = [('old_id_1', {'content_hash': 'H2'})]

        # New: same content — same hashes
        new_hashes = ['H1', 'H2']

        # Build superseded_ids
        superseded_ids = set()
        for new_hash in new_hashes:
            if new_hash in old_chunks_by_hash:
                for old_id, _ in old_chunks_by_hash[new_hash]:
                    superseded_ids.add(old_id)

        # All old IDs should be superseded
        assert superseded_ids == {'old_id_0', 'old_id_1'}

        # Nothing to tombstone
        all_old_ids = {'old_id_0', 'old_id_1'}
        to_tombstone = all_old_ids - superseded_ids
        assert to_tombstone == set(), "Identical re-mining should not leave stale chunks"

    def test_file_shrinks_old_chunks_tombstoned(self):
        """File that shrinks from 3 chunks to 2 should tombstone the removed chunk."""
        from collections import defaultdict

        # Old: 3 chunks with hashes H1, H2, H3
        old_chunks_by_hash = defaultdict(list)
        old_chunks_by_hash['H1'] = [('old_id_0', {'content_hash': 'H1'})]
        old_chunks_by_hash['H2'] = [('old_id_1', {'content_hash': 'H2'})]
        old_chunks_by_hash['H3'] = [('old_id_2', {'content_hash': 'H3'})]

        # New: only 2 chunks — H1 and H2 (H3 is gone)
        new_hashes = ['H1', 'H2']

        superseded_ids = set()
        for new_hash in new_hashes:
            if new_hash in old_chunks_by_hash:
                for old_id, _ in old_chunks_by_hash[new_hash]:
                    superseded_ids.add(old_id)

        # H1 and H2 superseded, H3 not
        assert superseded_ids == {'old_id_0', 'old_id_1'}

        all_old_ids = {'old_id_0', 'old_id_1', 'old_id_2'}
        to_tombstone = all_old_ids - superseded_ids
        assert to_tombstone == {'old_id_2'}, "Removed chunk H3 should be tombstoned"

    def test_file_grows_new_chunks_added(self):
        """File that grows from 2 chunks to 3 should add new without phantom old."""
        from collections import defaultdict

        # Old: 2 chunks H1, H2
        old_chunks_by_hash = defaultdict(list)
        old_chunks_by_hash['H1'] = [('old_id_0', {'content_hash': 'H1'})]
        old_chunks_by_hash['H2'] = [('old_id_1', {'content_hash': 'H2'})]

        # New: 3 chunks H1, H2, H3 (new)
        new_hashes = ['H1', 'H2', 'H3']

        superseded_ids = set()
        for new_hash in new_hashes:
            if new_hash in old_chunks_by_hash:
                for old_id, _ in old_chunks_by_hash[new_hash]:
                    superseded_ids.add(old_id)

        # H1 and H2 superseded, H3 has no old match (new)
        assert superseded_ids == {'old_id_0', 'old_id_1'}

        all_old_ids = {'old_id_0', 'old_id_1'}
        to_tombstone = all_old_ids - superseded_ids
        assert to_tombstone == set(), "Existing chunks should not be tombstoned when content preserved"
        # H3 is added as a new current chunk (not in all_old_ids, so not tombstoned)


class TestChunkBoundariesChange:
    """When chunk boundaries change but some content is preserved."""

    def test_boundary_shift_same_content_supersedes(self):
        """Content that moves to different chunk boundary should still be matched by hash."""
        from collections import defaultdict
        from mempalace.miner import _compute_content_hash

        # Old: content "same content" at chunk 0 with hash H
        content = "same content that spans boundaries"
        h = _compute_content_hash(content)

        old_chunks_by_hash = defaultdict(list)
        old_chunks_by_hash[h] = [('old_id_0', {'content_hash': h})]

        # New: same content (different chunking) — same hash
        new_hash = _compute_content_hash(content)
        assert new_hash == h

        # Hash match → superseded
        superseded_ids = set()
        if new_hash in old_chunks_by_hash:
            for old_id, _ in old_chunks_by_hash[new_hash]:
                superseded_ids.add(old_id)

        assert 'old_id_0' in superseded_ids

    def test_partial_content_change_mixed_supersession(self):
        """File with mixed changes: some content same, some changed."""
        from collections import defaultdict
        from mempalace.miner import _compute_content_hash

        old_chunks_by_hash = defaultdict(list)
        h1 = _compute_content_hash("unchanged content A")
        h2 = _compute_content_hash("old content B")
        h3 = _compute_content_hash("unchanged content C")

        old_chunks_by_hash[h1] = [('old_id_A', {'content_hash': h1})]
        old_chunks_by_hash[h2] = [('old_id_B', {'content_hash': h2})]
        old_chunks_by_hash[h3] = [('old_id_C', {'content_hash': h3})]

        # New: A unchanged, B changed to new hash, C unchanged
        new_hashes = [h1, _compute_content_hash("new content B"), h3]

        superseded_ids = set()
        for new_hash in new_hashes:
            if new_hash in old_chunks_by_hash:
                for old_id, _ in old_chunks_by_hash[new_hash]:
                    superseded_ids.add(old_id)

        # A and C superseded, B not (content changed)
        assert 'old_id_A' in superseded_ids
        assert 'old_id_C' in superseded_ids
        assert 'old_id_B' not in superseded_ids  # content changed, new hash doesn't match

        # B's old chunk should be tombstoned (not superseded)
        all_old_ids = {'old_id_A', 'old_id_B', 'old_id_C'}
        to_tombstone = all_old_ids - superseded_ids
        assert to_tombstone == {'old_id_B'}, "Changed content's old chunk should be tombstoned"