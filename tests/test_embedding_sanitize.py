"""
Regression tests for NaN/Inf embedding sanitation.

Root cause: MLX embed daemon returned NaN vector for 2 chunks in osint_frameworks.py.
LanceDB rejected add because vectors contained NaN. These tests verify the fix:
- NaN/Inf are detected and repaired (zero-fill + renormalize) when sparse
- Degenerate/all-invalid embeddings raise RuntimeError
- LanceDB never receives NaN/Inf after sanitation
"""
import math
import pytest


# =============================================================================
# Unit tests — _sanitize_embedding_vector and _sanitize_embedding_batch
# =============================================================================

class TestSanitizeEmbeddingVector:
    """Pure unit tests for the sanitation helpers."""

    def test_sanitize_replaces_nan_and_returns_finite_256(self):
        """Sparse NaN in otherwise valid vector → zero-fill + renormalize."""
        from mempalace.backends.lance import _sanitize_embedding_vector, EMBEDDING_DIMS

        # Vector with one NaN at position 42, rest valid random values
        vec = [0.1] * EMBEDDING_DIMS
        vec[42] = float("nan")

        result = _sanitize_embedding_vector(vec)

        # Must be finite everywhere
        assert all(math.isfinite(v) for v in result), "Result contains NaN/Inf"
        # Length preserved
        assert len(result) == EMBEDDING_DIMS
        # Was renormalized (norm == 1.0 for unit vector)
        norm = math.sqrt(sum(v * v for v in result))
        assert abs(norm - 1.0) < 1e-6, f"Vector not renormalized, norm={norm}"

    def test_sanitize_replaces_inf_and_returns_finite_256(self):
        """Sparse Inf in otherwise valid vector → zero-fill + renormalize."""
        from mempalace.backends.lance import _sanitize_embedding_vector, EMBEDDING_DIMS

        vec = [0.05] * EMBEDDING_DIMS
        vec[7] = float("inf")
        vec[200] = float("-inf")

        result = _sanitize_embedding_vector(vec)

        assert all(math.isfinite(v) for v in result), "Result contains Inf"
        assert len(result) == EMBEDDING_DIMS
        norm = math.sqrt(sum(v * v for v in result))
        assert abs(norm - 1.0) < 1e-6, f"Vector not renormalized, norm={norm}"

    def test_sanitize_wrong_dim_raises(self):
        """Wrong dimension raises RuntimeError with clear message."""
        from mempalace.backends.lance import _sanitize_embedding_vector

        vec = [0.1] * 128  # should be 256

        with pytest.raises(RuntimeError) as exc_info:
            _sanitize_embedding_vector(vec)
        assert "128 != expected 256" in str(exc_info.value)

    def test_sanitize_all_nan_raises(self):
        """All values NaN → RuntimeError (cannot renormalize)."""
        from mempalace.backends.lance import _sanitize_embedding_vector, EMBEDDING_DIMS

        vec = [float("nan")] * EMBEDDING_DIMS

        with pytest.raises(RuntimeError) as exc_info:
            _sanitize_embedding_vector(vec)
        assert "degenerate" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()

    def test_sanitize_none_raises(self):
        """None input raises RuntimeError with context."""
        from mempalace.backends.lance import _sanitize_embedding_vector

        with pytest.raises(RuntimeError) as exc_info:
            _sanitize_embedding_vector(None)
        assert "None" in str(exc_info.value)

    def test_sanitize_valid_vector_unchanged(self):
        """Already-finite vector passes through unchanged."""
        from mempalace.backends.lance import _sanitize_embedding_vector, EMBEDDING_DIMS

        # Unit vector (norm=1) — no change expected
        vec = [1.0] + [0.0] * (EMBEDDING_DIMS - 1)

        result = _sanitize_embedding_vector(vec)

        assert result == vec
        assert all(math.isfinite(v) for v in result)

    def test_sanitize_zero_vector_raises(self):
        """All-zero vector → RuntimeError (zero norm)."""
        from mempalace.backends.lance import _sanitize_embedding_vector, EMBEDDING_DIMS

        vec = [0.0] * EMBEDDING_DIMS

        with pytest.raises(RuntimeError) as exc_info:
            _sanitize_embedding_vector(vec)
        assert "degenerate" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()

    def test_sanitize_batch_applies_per_vector(self):
        """_sanitize_embedding_batch applies sanitation to each element."""
        from mempalace.backends.lance import _sanitize_embedding_batch, EMBEDDING_DIMS

        vectors = [
            [0.1] * EMBEDDING_DIMS,
            [0.2] * EMBEDDING_DIMS,
            [float("nan")] * EMBEDDING_DIMS,  # all nan → raises
        ]

        # Should fail on 3rd vector (all nan)
        with pytest.raises(RuntimeError):
            _sanitize_embedding_batch(vectors)

    def test_sanitize_batch_replaces_sparse_nan(self):
        """Batch with one sparse-nan vector gets repaired, others pass."""
        from mempalace.backends.lance import _sanitize_embedding_batch, EMBEDDING_DIMS

        vec0 = [0.1] * EMBEDDING_DIMS
        vec0[5] = float("nan")
        vec1 = [0.3] * EMBEDDING_DIMS  # valid

        results = _sanitize_embedding_batch([vec0, vec1])

        assert len(results) == 2
        assert all(math.isfinite(v) for v in results[0])
        assert all(math.isfinite(v) for v in results[1])


# =============================================================================
# Integration tests — mocked _embed_via_socket and _do_add path
# =============================================================================

class TestEmbedSocketNanIsSanitized:
    """Socket path: NaN returned by daemon is sanitized before reaching LanceDB."""

    @pytest.fixture
    def tmp_palace(self, tmp_path):
        import os
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_BACKEND"] = "lance"
        palace_dir = tmp_path / "nan_test_palace"
        palace_dir.mkdir()
        return str(palace_dir)

    @pytest.fixture
    def lance_col(self, tmp_palace):
        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(tmp_palace, "nan_test", create=True)
        return col

    def test_embed_socket_nan_sanitized_before_lance_add(self, lance_col, tmp_path):
        """
        Simulate daemon returning NaN vector.
        Verify add() succeeds (sanitation repaired it) rather than
        LanceDB raising due to NaN in vector field.
        """
        import os
        from mempalace.backends.lance import _embed_texts, EMBEDDING_DIMS

        # Patch _embed_via_socket to return a NaN vector (like broken MLX daemon)
        old_embed_socket = None
        try:
            from mempalace.backends import lance as lance_module
            old_embed_socket = lance_module._embed_via_socket

            def fake_nan_socket(texts):
                # Return batch with one NaN vector
                nan_vec = [float("nan") if i == 0 else 0.01 for i in range(EMBEDDING_DIMS)]
                return [nan_vec] * len(texts)

            lance_module._embed_via_socket = fake_nan_socket

            # Calling _embed_texts directly bypasses cache (use unique texts)
            import uuid
            unique_text = f"nan_embed_test_{uuid.uuid4().hex[:8]}"
            embeddings = _embed_texts([unique_text])

            # Should be finite after sanitation
            assert len(embeddings) == 1
            assert all(math.isfinite(v) for v in embeddings[0])
            assert len(embeddings[0]) == EMBEDDING_DIMS

        finally:
            if old_embed_socket is not None:
                lance_module._embed_via_socket = old_embed_socket

    def test_lance_add_succeeds_with_sparse_nan_in_embed_path(self, lance_col):
        """
        Full integration: patched embed produces NaN, add() succeeds via sanitation.
        No LanceDB error about NaN vectors.
        """
        from mempalace.backends import lance as lance_module

        old_embed_socket = lance_module._embed_via_socket
        try:
            from mempalace.backends.lance import EMBEDDING_DIMS

            def fake_nan_socket(texts):
                # One NaN per text
                return [
                    [float("nan") if i == 10 else 0.02 for i in range(EMBEDDING_DIMS)]
                    for _ in texts
                ]

            lance_module._embed_via_socket = fake_nan_socket

            # This would crash with LanceDB NaN error before the fix
            # Now it should succeed
            import uuid
            doc_id = f"nan_int_test_{uuid.uuid4().hex[:8]}"
            lance_col.add(
                documents=["test nan repair in add path"],
                ids=[doc_id],
                metadatas=[{"test": "nan_sanitize"}],
            )

            # Verify it was actually written
            count = lance_col.count()
            assert count >= 1

        finally:
            lance_module._embed_via_socket = old_embed_socket


class TestAllNanEmbedFailsClosed:
    """All-NaN embedding must raise RuntimeError, not silently pass."""

    def test_all_nan_embed_raises_not_silent_pass(self):
        """Degenerate all-NaN vector must fail closed, not become zero-vector."""
        from mempalace.backends.lance import _sanitize_embedding_vector, EMBEDDING_DIMS

        vec = [float("nan")] * EMBEDDING_DIMS

        with pytest.raises(RuntimeError) as exc_info:
            _sanitize_embedding_vector(vec)

        # Must not be a silent pass (zero vector written to LanceDB)
        assert "degenerate" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()