"""
Deterministic embedding tests — no daemon spawning, no importlib.reload.
Uses unittest.mock to stub embedding paths.

Run: pytest tests/test_embed_deterministic.py -v
"""

import tempfile
import os
import time
import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed")

# Deterministic mock embeddings — always returns the same vectors
_DETERMINISTIC_DIM = 256


def _mock_embed_texts(texts):
    """Return deterministic fake embeddings for any input."""
    import hashlib
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:_DETERMINISTIC_DIM]) + [0.0] * (_DETERMINISTIC_DIM - len(h))
        result.append(vec)
    return result


class TestEmbedDeterministic:
    """Tests that use mocked embeddings — fast and deterministic."""

    def test_lance_collection_add_with_mock(self):
        """LanceCollection.add() works when embedding is mocked."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend

        palace = tempfile.mkdtemp(prefix="mempalace_embed_det_")

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "test_mock", create=True)

            col.add(
                documents=["hello world", "foo bar baz"],
                ids=["doc1", "doc2"],
                metadatas=[{"wing": "repo", "room": "src"}, {"wing": "repo", "room": "src"}],
            )

            assert col.count() == 2

    def test_lance_collection_upsert_with_mock(self):
        """LanceCollection.upsert() works when embedding is mocked."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend

        palace = tempfile.mkdtemp(prefix="mempalace_embed_det_")

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "test_upsert", create=True)

            col.upsert(
                documents=["hello world"],
                ids=["doc1"],
                metadatas=[{"wing": "repo", "room": "src"}],
            )

            assert col.count() == 1

            # Upsert same ID with different content — replaces
            col.upsert(
                documents=["hello world updated"],
                ids=["doc1"],
                metadatas=[{"wing": "repo", "room": "src"}],
            )

            assert col.count() == 1

    def test_lance_collection_query_with_mock(self):
        """LanceCollection.query() works when embedding is mocked."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend

        palace = tempfile.mkdtemp(prefix="mempalace_embed_det_")

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "test_query", create=True)

            col.add(
                documents=["def hello(): pass", "class Foo: pass"],
                ids=["fn1", "cls1"],
                metadatas=[{"wing": "repo", "room": "src"}, {"wing": "repo", "room": "src"}],
            )

            results = col.query(query_texts=["function definition"], n_results=2)
            assert len(results["ids"][0]) <= 2

    def test_lance_collection_delete_with_mock(self):
        """LanceCollection.delete() removes records."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend

        palace = tempfile.mkdtemp(prefix="mempalace_embed_det_")

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "test_delete", create=True)

            col.add(
                documents=["to be deleted"],
                ids=["del1"],
                metadatas=[{"wing": "repo", "room": "src"}],
            )

            assert col.count() == 1

            col.delete(ids=["del1"])
            assert col.count() == 0

    def test_semantic_deduplicator_classify_batch_mock(self):
        """SemanticDeduplicator.classify_batch returns correct classifications."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend, SemanticDeduplicator

        palace = tempfile.mkdtemp(prefix="mempalace_embed_det_")

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "test_dedup", create=True)

            dedup = SemanticDeduplicator()

            # Empty collection — all unique
            results, vecs, _failures = dedup.classify_batch(
                documents=["hello", "world"],
                metadatas=[{}, {}],
                collection=col,
            )
            assert results == [("unique", None), ("unique", None)]
            assert len(vecs) == 2

    def test_upsert_then_query_flow_with_mock(self):
        """Full upsert → query → delete flow with mocked embeddings."""
        import unittest.mock as mock
        from mempalace.backends.lance import LanceBackend

        palace = tempfile.mkdtemp(prefix="mempalace_embed_det_")

        with mock.patch("mempalace.backends.lance._embed_texts", side_effect=_mock_embed_texts):
            backend = LanceBackend()
            col = backend.get_collection(palace, "test_flow", create=True)

            # Add some content
            col.add(
                documents=[
                    "import os\nimport sys",
                    "def main(): pass",
                    "class Config: pass",
                ],
                ids=["mod1", "fn1", "cls1"],
                metadatas=[
                    {"wing": "repo", "room": "src"},
                    {"wing": "repo", "room": "src"},
                    {"wing": "repo", "room": "src"},
                ],
            )

            assert col.count() == 3

            # Query
            results = col.query(query_texts=["configuration class"], n_results=3)
            assert len(results["ids"][0]) >= 1

            # Delete one
            col.delete(ids=["fn1"])
            assert col.count() == 2

            # Upsert update
            col.upsert(
                documents=["import pathlib"],
                ids=["mod1"],
                metadatas=[{"wing": "repo", "room": "src"}],
            )
            assert col.count() == 2


class TestEmbedFallbackMock:
    """Tests for fallback behavior when daemon is unavailable.

    Note: These tests are marked @pytest.mark.slow because they verify
    real daemon/unavailable behavior which requires actual embedding machinery.
    With the conftest mock in place, daemon-based tests cannot run.
    """

    @pytest.mark.slow
    def test_embed_fallback_when_daemon_unavailable(self):
        """When daemon socket is unavailable, fallback is used. Integration test."""
        import unittest.mock as mock

        # Restore real _embed_texts for this test (undo conftest mock)
        import mempalace.backends.lance as lance_mod
        original_embed_texts = lance_mod._embed_texts
        # Replace with a version that calls the real fallback path
        import mempalace.backends.lance as LM

        called_fallback = []

        def mock_fallback(texts):
            called_fallback.append(texts)
            return _mock_embed_texts(texts)

        with mock.patch.object(LM, "_embed_texts_fallback", side_effect=mock_fallback):
            with mock.patch.object(LM, "_daemon_is_running", return_value=False):
                with mock.patch.object(LM, "_start_daemon_if_needed", return_value=False):
                    result = LM._embed_texts(["test input"])

        assert called_fallback == [["test input"]]
        assert len(result) == 1
        assert len(result[0]) == _DETERMINISTIC_DIM


class TestEmbedCircuitBreaker:
    """Tests for embed circuit breaker behavior."""

    def test_should_try_socket_false_when_circuit_open(self):
        """Circuit breaker should block socket attempts when open."""
        from mempalace.circuit_breaker import _embed_circuit, _State

        # Save and restore state
        orig_state = _embed_circuit._state
        orig_failures = _embed_circuit._failures

        # Force circuit open
        _embed_circuit._state = _State.OPEN
        _embed_circuit._opened_at = time.monotonic()

        try:
            assert _embed_circuit.should_try_socket() is False
        finally:
            _embed_circuit._state = orig_state
            _embed_circuit._failures = orig_failures

    def test_should_try_socket_true_when_closed(self):
        """Circuit breaker allows socket attempts when closed."""
        from mempalace.circuit_breaker import _embed_circuit, _State

        orig_state = _embed_circuit._state

        _embed_circuit._state = _State.CLOSED

        try:
            assert _embed_circuit.should_try_socket() is True
        finally:
            _embed_circuit._state = orig_state
