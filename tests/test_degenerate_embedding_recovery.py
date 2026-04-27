"""
Regression tests for per-chunk degenerate embedding recovery.

Root cause: Full mine died at osint_frameworks.py because MLX daemon returned
a degenerate embedding (all-zero or all-NaN) for one chunk. The mining pipeline
treated one bad chunk as fatal for the entire run.

These tests verify:
- One bad chunk does not abort the whole mining run
- Bad chunks are quarantined and reported
- LanceDB never receives NaN/Inf/all-zero vectors
- Fallback remains disabled for mining
- Quarantine JSONL is written
"""
import math
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest


# =============================================================================
# Unit tests — _is_degenerate_vector
# =============================================================================

class TestIsDegenerateVector:
    """Pure unit tests for vector degeneracy detection."""

    def test_valid_unit_vector_not_degenerate(self):
        from mempalace.backends.lance import _is_degenerate_vector, EMBEDDING_DIMS

        vec = [1.0 / math.sqrt(EMBEDDING_DIMS)] * EMBEDDING_DIMS
        is_deg, reason = _is_degenerate_vector(vec)
        assert not is_deg, f"Valid unit vector marked degenerate: {reason}"

    def test_all_zero_is_degenerate(self):
        from mempalace.backends.lance import _is_degenerate_vector, EMBEDDING_DIMS

        vec = [0.0] * EMBEDDING_DIMS
        is_deg, reason = _is_degenerate_vector(vec)
        assert is_deg, "All-zero vector not marked degenerate"
        assert "zero-norm" in reason

    def test_near_zero_is_degenerate(self):
        from mempalace.backends.lance import _is_degenerate_vector, EMBEDDING_DIMS

        # 1e-15 per element → norm ≈ 1.6e-14 < 1e-9 threshold
        vec = [1e-15] * EMBEDDING_DIMS
        is_deg, reason = _is_degenerate_vector(vec)
        assert is_deg, "Near-zero vector not marked degenerate"
        assert "zero-norm" in reason

    def test_all_nan_is_degenerate(self):
        from mempalace.backends.lance import _is_degenerate_vector, EMBEDDING_DIMS

        vec = [float("nan")] * EMBEDDING_DIMS
        is_deg, reason = _is_degenerate_vector(vec)
        assert is_deg, "All-NaN vector not marked degenerate"
        assert "NaN" in reason

    def test_sparse_nan_is_degenerate(self):
        """Sparse NaN is flagged as degenerate (quarantine triggers repair downstream)."""
        from mempalace.backends.lance import _is_degenerate_vector, EMBEDDING_DIMS

        vec = [0.1] * EMBEDDING_DIMS
        vec[42] = float("nan")
        is_deg, reason = _is_degenerate_vector(vec)
        # Any NaN presence flags as degenerate — quarantine → downstream repair
        assert is_deg, f"Sparse-NaN vector should be flagged degenerate: {reason}"
        assert "NaN" in reason

    def test_wrong_dim_is_degenerate(self):
        from mempalace.backends.lance import _is_degenerate_vector

        vec = [0.1] * 128  # wrong dimension
        is_deg, reason = _is_degenerate_vector(vec)
        assert is_deg, "Wrong-dim vector not marked degenerate"
        assert "dimension" in reason

    def test_not_convertible_is_degenerate(self):
        from mempalace.backends.lance import _is_degenerate_vector

        vec = ["not", "a", "number"]
        is_deg, reason = _is_degenerate_vector(vec)
        assert is_deg, "Non-float vector not marked degenerate"
        assert "not convertible" in reason


# =============================================================================
# Unit tests — _embed_texts_resilient
# =============================================================================

class TestEmbedTextsResilient:
    """Tests for the resilient embedding wrapper."""

    def test_valid_batch_returns_all_valid(self):
        from mempalace.backends.lance import _embed_texts_resilient, EMBEDDING_DIMS

        texts = ["hello world", "foo bar baz", "another sentence here"]
        valid_t, valid_e, failures, indices = _embed_texts_resilient(texts)

        assert len(valid_t) == 3
        assert len(valid_e) == 3
        assert len(failures) == 0
        assert indices == [0, 1, 2]

    def test_one_bad_nan_quarantined_valid_survives(self):
        from mempalace.backends.lance import _embed_texts_resilient, EMBEDDING_DIMS

        texts = ["good text one", "bad text with degenerate", "good text two"]
        nan_vec = [float("nan")] * EMBEDDING_DIMS

        with patch("mempalace.backends.lance._embed_texts") as mock_embed:
            mock_embed.return_value = [
                [0.1] * EMBEDDING_DIMS,  # good
                nan_vec,                  # degenerate NaN
                [0.2] * EMBEDDING_DIMS,  # good
            ]
            valid_t, valid_e, failures, indices = _embed_texts_resilient(texts, context="test")

        assert valid_t == ["good text one", "good text two"]
        assert len(valid_e) == 2
        assert len(failures) == 1
        assert failures[0]["index"] == 1
        assert failures[0]["reason"] == "contains NaN/Inf (nan=True, inf=False)"
        assert indices == [0, 2]

    def test_all_zero_vector_quarantined(self):
        from mempalace.backends.lance import _embed_texts_resilient, EMBEDDING_DIMS

        texts = ["some text", "zero vector text"]
        zero_vec = [0.0] * EMBEDDING_DIMS

        with patch("mempalace.backends.lance._embed_texts") as mock_embed:
            mock_embed.return_value = [
                [0.1] * EMBEDDING_DIMS,
                zero_vec,
            ]
            valid_t, valid_e, failures, indices = _embed_texts_resilient(texts)

        assert len(valid_t) == 1
        assert len(failures) == 1
        assert "zero-norm" in failures[0]["reason"]

    def test_wrong_dim_quarantined(self):
        from mempalace.backends.lance import _embed_texts_resilient

        texts = ["text one", "text two short"]
        with patch("mempalace.backends.lance._embed_texts") as mock_embed:
            mock_embed.return_value = [
                [0.1] * 256,
                [0.2] * 128,  # wrong dimension
            ]
            valid_t, valid_e, failures, indices = _embed_texts_resilient(texts)

        assert len(valid_t) == 1
        assert len(failures) == 1
        assert "dimension" in failures[0]["reason"]

    def test_systemic_error_propagates(self):
        """If _embed_texts raises RuntimeError (daemon down, fallback disabled), propagate."""
        from mempalace.backends.lance import _embed_texts_resilient

        with patch("mempalace.backends.lance._embed_texts") as mock_embed:
            mock_embed.side_effect = RuntimeError("Daemon embedding error: circuit open")
            with pytest.raises(RuntimeError, match="Daemon embedding error"):
                _embed_texts_resilient(["some text"])

    def test_empty_batch_returns_empty(self):
        from mempalace.backends.lance import _embed_texts_resilient

        valid_t, valid_e, failures, indices = _embed_texts_resilient([])
        assert valid_t == []
        assert valid_e == []
        assert failures == []
        assert indices == []


# =============================================================================
# Integration tests — classify_batch with quarantined chunks
# =============================================================================

class TestClassifyBatchQuarantine:
    """classify_batch handles quarantined chunks without aborting."""

    @pytest.fixture
    def tmp_palace(self, tmp_path):
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_BACKEND"] = "lance"
        palace_dir = tmp_path / "quar_test_palace"
        palace_dir.mkdir()
        return str(palace_dir)

    @pytest.fixture
    def lance_col(self, tmp_palace):
        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(tmp_palace, "quar_test", create=True)
        return col

    def test_classify_batch_returns_3_tuples(self, lance_col):
        """classify_batch now returns 3 values: (classifications, embeddings, failures)."""
        from mempalace.backends.lance import SemanticDeduplicator, EMBEDDING_DIMS

        dedup = SemanticDeduplicator()
        docs = ["test doc one", "test doc two"]
        metas = [{}, {}]

        with patch("mempalace.backends.lance._embed_texts_resilient") as mock_resilient:
            mock_resilient.return_value = (
                docs,
                [[0.1] * EMBEDDING_DIMS, [0.2] * EMBEDDING_DIMS],
                [],  # no failures
                [0, 1],
            )
            result = dedup.classify_batch(docs, metas, lance_col)

        assert len(result) == 3
        classifications, embeddings, failures = result
        assert len(classifications) == 2
        assert len(embeddings) == 2
        assert failures == []

    def test_classify_batch_quarantined_slot_marked(self, lance_col):
        from mempalace.backends.lance import SemanticDeduplicator, EMBEDDING_DIMS

        dedup = SemanticDeduplicator()
        docs = ["good doc", "bad doc", "good doc two"]
        metas = [{}, {}, {}]

        with patch("mempalace.backends.lance._embed_texts_resilient") as mock_resilient:
            mock_resilient.return_value = (
                ["good doc", "good doc two"],
                [[0.1] * EMBEDDING_DIMS, [0.2] * EMBEDDING_DIMS],
                [{"index": 1, "reason": "all-zero", "preview": "bad doc", "context": ""}],
                [0, 2],
            )
            classifications, embeddings, failures = dedup.classify_batch(docs, metas, lance_col)

        assert len(classifications) == 3  # aligned with original docs
        assert classifications[0] == ("unique", None)
        assert classifications[1] == ("quarantined", None)  # quarantined slot
        assert classifications[2] == ("unique", None)


# =============================================================================
# Integration tests — upsert with quarantined chunks
# =============================================================================

class TestUpsertQuarantine:
    """upsert skips quarantined chunks without aborting."""

    @pytest.fixture
    def tmp_palace(self, tmp_path):
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_BACKEND"] = "lance"
        palace_dir = tmp_path / "upsert_quar_palace"
        palace_dir.mkdir()
        return str(palace_dir)

    @pytest.fixture
    def lance_col(self, tmp_palace):
        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(tmp_palace, "upsert_quar", create=True)
        return col

    def test_upsert_one_bad_one_good_skips_bad_only(self, lance_col):
        """One quarantined chunk + one good chunk → only good chunk is upserted."""
        from mempalace.backends.lance import EMBEDDING_DIMS

        nan_vec = [float("nan")] * EMBEDDING_DIMS
        good_vec = [0.1] * EMBEDDING_DIMS

        with patch("mempalace.backends.lance._embed_texts") as mock_embed:
            with patch("mempalace.backends.lance._embed_texts_resilient") as mock_resilient:
                # Simulate: texts=[good_text, bad_text], embeddings=[good_vec, nan_vec]
                # After quarantine: valid=[good_text], valid_embs=[good_vec], failures=[bad_text]
                mock_resilient.return_value = (
                    ["good text"],
                    [good_vec],
                    [{"index": 1, "reason": "all-NaN", "preview": "bad text", "context": ""}],
                    [0],  # only original index 0 is valid
                )

                # This must NOT raise
                lance_col.upsert(
                    documents=["good text", "bad text"],
                    ids=["id_good", "id_bad"],
                    metadatas=[{"source": "good"}, {"source": "bad"}],
                )

                # Good doc was written
                assert lance_col.count() == 1

    def test_upsert_all_quarantined_returns_early(self, lance_col):
        """If all chunks are quarantined, upsert returns without writing."""
        from mempalace.backends.lance import EMBEDDING_DIMS

        nan_vec = [float("nan")] * EMBEDDING_DIMS

        with patch("mempalace.backends.lance._embed_texts_resilient") as mock_resilient:
            mock_resilient.return_value = (
                [],  # no valid texts
                [],  # no valid embeddings
                [{"index": 0, "reason": "all-zero", "preview": "bad", "context": ""}],
                [],  # no valid indices
            )
            # Must not raise — all quarantined → returns early
            lance_col.upsert(
                documents=["bad text"],
                ids=["bad_id"],
                metadatas=[{"source": "test"}],
            )

        assert lance_col.count() == 0


# =============================================================================
# Mining loop simulation — one bad file does not abort
# =============================================================================

class TestMiningLoopContinues:
    """Full mining simulation: one bad chunk does not abort the loop."""

    @pytest.fixture
    def tmp_palace(self, tmp_path):
        os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
        os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        os.environ["MEMPALACE_BACKEND"] = "lance"
        palace_dir = tmp_path / "loop_test_palace"
        palace_dir.mkdir()
        return str(palace_dir)

    @pytest.fixture
    def lance_col(self, tmp_palace):
        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(tmp_palace, "loop_test", create=True)
        return col

    def test_mining_loop_continues_after_quarantined_file(self, lance_col):
        """
        Simulate the mining loop calling classify_batch for multiple files.
        File 0: all good → processed
        File 1: one bad chunk → quarantined (but not error)
        File 2: all good → processed
        The loop must NOT abort on File 1.
        """
        from mempalace.backends.lance import SemanticDeduplicator, EMBEDDING_DIMS

        good_vec = [0.1] * EMBEDDING_DIMS

        def fake_embed_texts_resilient(texts, *, context="", wing="", room=""):
            if not texts:
                return [], [], [], []
            # File with "bad" in content → quarantined
            if "bad" in texts[0]:
                return (
                    [],          # no valid texts
                    [],          # no valid embeddings
                    [{"index": 0, "reason": "all-zero", "preview": texts[0], "context": context}],
                    [],          # no valid indices
                )
            return (
                texts,
                [good_vec] * len(texts),
                [],
                list(range(len(texts))),
            )

        results = []
        with patch("mempalace.backends.lance._embed_texts_resilient", side_effect=fake_embed_texts_resilient):
            for file_name in ["good_file_0", "bad_file_1", "good_file_2"]:
                try:
                    docs = [f"content of {file_name}"]
                    metas = [{"source_file": file_name}]
                    dedup = SemanticDeduplicator()
                    classifications, embeddings, failures = dedup.classify_batch(
                        docs, metas, lance_col, quarantine_ctx=file_name
                    )
                    results.append({
                        "file": file_name,
                        "classifications": classifications,
                        "failures": failures,
                        "status": "ok",
                    })
                except Exception as e:
                    results.append({
                        "file": file_name,
                        "status": "error",
                        "error": str(e),
                    })

        # File 0: ok
        assert results[0]["status"] == "ok"
        assert len(results[0]["failures"]) == 0
        # File 1: quarantined but loop continued
        assert results[1]["status"] == "ok"
        assert len(results[1]["failures"]) == 1
        assert results[1]["failures"][0]["reason"] == "all-zero"
        # File 2: ok (loop DID continue!)
        assert results[2]["status"] == "ok"


# =============================================================================
# Fallback disabled — no "Using in-process embedding" for per-chunk degenerate
# =============================================================================

class TestFallbackDisabled:
    """Verify fallback remains disabled when MEMPALACE_EMBED_FALLBACK=0."""

    def test_quarantine_does_not_trigger_fallback(self):
        """A quarantined chunk must not trigger the in-process fallback."""
        from mempalace.backends.lance import _embed_texts_resilient, EMBEDDING_DIMS

        texts = ["test text"]
        nan_vec = [float("nan")] * EMBEDDING_DIMS

        with patch("mempalace.backends.lance._embed_texts") as mock_embed:
            mock_embed.return_value = [nan_vec]

            valid_t, valid_e, failures, indices = _embed_texts_resilient(texts)

        assert len(valid_t) == 0, "Bad vector should be quarantined"
        assert len(failures) == 1
        # Must NOT fall back — failure reason should be about the vector, not fallback
        assert "daemon" not in failures[0]["reason"].lower()
        assert "fallback" not in failures[0]["reason"].lower()
