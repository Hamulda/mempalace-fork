"""
tests/test_dedup_scope.py

Dedup scope policy tests for _dedup_scope_matches().
Verifies that code/repo chunks are only deduplicated within the same source_file
(and optionally same chunk_index), and never across projects or files.
"""

import os
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


class TestClassifyBatchScope:
    """SemanticDeduplicator.classify_batch() must not mark cross-project chunks as duplicate."""

    def _make_fake_collection(self, candidates: list[dict]):
        """Return a mock LanceCollection that returns given candidates for any query."""
        class FakeLanceCollection:
            def count(self):
                return len(candidates)

            def query_by_vector(self, vector, n_results=5):
                return {
                    "ids": [[c["id"]] for c in candidates],
                    "distances": [[c.get("dist", 0.0)] for c in candidates],
                    "metadatas": [[c.get("meta", {})] for c in candidates],
                    "documents": [[c.get("doc", "")] for c in candidates],
                }

            def get(self, where=None, include=None, ids=None):
                return {"ids": [], "documents": [], "metadatas": [], "distances": []}

        return FakeLanceCollection()

    def test_cross_project_high_similarity_is_unique(self):
        """Different source_file + high similarity → unique (not duplicate).

        This is the key invariant: /projA/src/auth.py and /projB/src/auth.py
        are semantically similar but MUST NOT be deduplicated against each other.
        """
        from mempalace.backends.lance import SemanticDeduplicator

        fake_col = self._make_fake_collection([
            {
                "id": "projA_auth_0",
                "dist": 0.05,  # ~0.95 similarity — above high_threshold
                "meta": {"source_file": "/projA/src/auth.py", "chunk_index": 0,
                         "wing": "repo", "room": "general"},
            }
        ])

        sd = SemanticDeduplicator(high_threshold=0.92, low_threshold=0.82)
        docs = ["projB login implementation"]
        metas = [{"source_file": "/projB/src/auth.py", "chunk_index": 0,
                  "wing": "repo", "room": "general"}]

        classifications, _, _ = sd.classify_batch(docs, metas, fake_col, n_candidates=5)

        # Must be "unique", NOT "duplicate" — cross-project scope must prevent dedup
        action, _ = classifications[0]
        assert action == "unique", (
            f"Expected 'unique' for cross-project chunk (got {action!r}). "
            f"_dedup_scope_matches should return False for different source_file."
        )

    def test_same_file_high_similarity_is_duplicate(self):
        """Same source_file + same chunk_index + high similarity → duplicate."""
        from mempalace.backends.lance import SemanticDeduplicator

        fake_col = self._make_fake_collection([
            {
                "id": "projA_auth_0",
                "dist": 0.05,  # ~0.95 similarity — above high_threshold
                "meta": {"source_file": "/projA/src/auth.py", "chunk_index": 0,
                         "wing": "repo", "room": "general"},
            }
        ])

        os.environ["MEMPALACE_DEDUP_HIGH"] = "0.92"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.82"
        sd = SemanticDeduplicator(high_threshold=0.92, low_threshold=0.82)
        docs = ["projA login implementation"]
        metas = [{"source_file": "/projA/src/auth.py", "chunk_index": 0,
                  "wing": "repo", "room": "general"}]

        classifications, _, _ = sd.classify_batch(docs, metas, fake_col, n_candidates=5)

        action, existing_id = classifications[0]
        assert action == "duplicate", f"Expected 'duplicate' for same-file chunk (got {action!r})"
        assert existing_id == "projA_auth_0"

    def test_cross_project_low_similarity_is_unique(self):
        """Different source_file + low similarity → unique (not conflict)."""
        from mempalace.backends.lance import SemanticDeduplicator

        fake_col = self._make_fake_collection([
            {
                "id": "projA_auth_0",
                "dist": 0.20,  # ~0.80 similarity — between low and high threshold
                "meta": {"source_file": "/projA/src/auth.py", "chunk_index": 0,
                         "wing": "repo", "room": "general"},
            }
        ])

        sd = SemanticDeduplicator(high_threshold=0.92, low_threshold=0.82)
        docs = ["projB different implementation"]
        metas = [{"source_file": "/projB/src/auth.py", "chunk_index": 0,
                  "wing": "repo", "room": "general"}]

        classifications, _, _ = sd.classify_batch(docs, metas, fake_col, n_candidates=5)

        action, _ = classifications[0]
        assert action == "unique", f"Expected 'unique' for cross-project low-similarity (got {action!r})"
