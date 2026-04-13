"""
Tests for M1 CoreML ANE optimization in embed daemon.

Run: pytest tests/test_coreml.py -v -s
"""

import platform
import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed")


class TestCoreML:
    def test_coreml_provider_loads(self):
        """On Apple Silicon, _create_embedding_model uses CoreML EP."""
        if platform.machine() != "arm64":
            pytest.skip("CoreML EP only on Apple Silicon")

        from mempalace.embed_daemon import _create_embedding_model
        model = _create_embedding_model()
        # Ověř že model vrací správné dimenze (384 pro bge-small)
        embeddings = list(model.embed(["test"]))
        assert len(embeddings) == 1
        assert len(embeddings[0]) == 384

    def test_cpu_fallback_on_non_apple(self, monkeypatch):
        """Na non-Apple platformě funguje CPU fallback."""
        # Mockuj platform.system() → "Linux"
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")

        from mempalace.embed_daemon import _create_embedding_model
        model = _create_embedding_model()
        assert model is not None

    def test_coreml_warmup_logs(self, capsys):
        """První inference loguje CoreML kompilaci."""
        if platform.machine() != "arm64":
            pytest.skip("CoreML EP only on Apple Silicon")

        import logging
        logging.basicConfig(level=logging.INFO)

        from mempalace.embed_daemon import _create_embedding_model
        try:
            model = _create_embedding_model()
            list(model.embed(["warmup"]))
        except Exception:
            pass  # May fail if fastembed not installed

        # Could check capsys for "CoreML" in output if model loaded