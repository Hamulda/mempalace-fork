"""
Tests for MLX embeddings in embed daemon.

Run: pytest tests/test_mlx_embeddings.py -v -s
"""

import platform
import pytest

try:
    import mlx  # noqa: F401
    import mlx_embeddings  # noqa: F401
    HAS_MLX = True
except ImportError:
    HAS_MLX = False


class TestMLXEmbeddings:
    @pytest.mark.skipif(not HAS_MLX, reason="mlx_embeddings not installed")
    def test_mlx_model_or_fallback_loads(self):
        """_create_embedding_model() uspěje na libovolné platformě."""
        from mempalace.embed_daemon import _create_embedding_model
        model = _create_embedding_model()
        embeddings = list(model.embed(["test"]))
        assert len(embeddings) == 1
        assert len(embeddings[0]) == 256, f"Expected 256 dims (ModernBERT Matryoshka), got {len(embeddings[0])}"

    @pytest.mark.skipif(
        not HAS_MLX or platform.machine() != "arm64",
        reason="MLX requires Apple Silicon + mlx_embeddings installed"
    )
    def test_mlx_preferred_on_apple_silicon(self):
        """Na Apple Silicon je preferován MLX backend."""
        from mempalace.embed_daemon import _create_mlx_model
        model = _create_mlx_model()
        result = list(model.embed(["MLX test"]))
        assert len(result) == 1
        assert len(result[0]) == 256, f"Expected 256 dims, got {len(result[0])}"

    @pytest.mark.skipif(not HAS_MLX, reason="mlx_embeddings not installed")
    def test_mlx_wrapper_batch(self):
        """MLX wrapper correctly batch-encodes multiple texts."""
        from mempalace.embed_daemon import _create_mlx_model
        model = _create_mlx_model()
        result = list(model.embed(["text1", "text2", "text3"]))
        assert len(result) == 3
        assert all(len(r) == 256 for r in result)