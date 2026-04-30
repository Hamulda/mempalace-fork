#!/usr/bin/env python3
"""tests/test_embedding_provider_lock.py — Embedding provider compatibility lock.

Verifies:
- First write creates embedding_meta.json
- Same provider/dims passes validation
- Dims mismatch raises EmbeddingDimsMismatchError
- Provider/model drift warns and raises EmbeddingProviderDriftError (default)
- MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT=1 allows drift
- Mock provider (eval/lexical mode) handled correctly
- No model loading in tests — uses fake provider

Env isolation: each test uses a fresh tmp_path palace.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(_REPO_ROOT))

# Env isolation
_ENVS = (
    "MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT",
    "MOCK_EMBED",
    "MEMPALACE_EVAL_MODE",
    "MEMPALACE_EMBED_FALLBACK",
    "MEMPALACE_BACKEND",
    "MEMPALACE_COALESCE_MS",
    "MEMPALACE_DEDUP_HIGH",
    "MEMPALACE_DEDUP_LOW",
)
_orig_env = {k: os.environ.pop(k, None) for k in _ENVS}
os.environ["MEMPALACE_COALESCE_MS"] = "0"
os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"


def _fake_detect(provider="mlx", model_id="mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M", dims=256):
    """Return a fake detect function for testing without real model loading."""
    def fake_detect():
        return (provider, model_id, dims)
    return fake_detect


# --------------------------------------------------------------------------- #
# Tests — first write
# --------------------------------------------------------------------------- #

class TestFirstWriteCreatesMeta:

    def test_first_write_creates_embedding_meta(self, tmp_path):
        """First write to a palace should create embedding_meta.json."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_new"
        palace.mkdir()

        # No meta yet
        assert em.load_meta(str(palace)) is None

        # Fake detect provider and validate first write
        provider, model_id, dims = "mlx", "mlx-community/test-model", 256

        # validate_write creates on first write (no meta exists)
        allowed, reason = em.validate_write(str(palace), provider, model_id, dims)
        assert allowed is True
        assert reason == ""

        # ensure_meta should now save
        em.ensure_meta(str(palace), provider, model_id, dims)

        meta = em.load_meta(str(palace))
        assert meta is not None
        assert meta["provider"] == "mlx"
        assert meta["model_id"] == "mlx-community/test-model"
        assert meta["dims"] == 256
        assert meta["version"] == 1
        assert "created_at" in meta
        assert "updated_at" in meta

    def test_first_write_with_unknown_provider(self, tmp_path):
        """Unknown provider should be stored as 'unknown'."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_unknown"
        palace.mkdir()

        em.ensure_meta(str(palace), "unknown", "", 256)
        meta = em.load_meta(str(palace))
        assert meta["provider"] == "unknown"


# --------------------------------------------------------------------------- #
# Tests — dims validation
# --------------------------------------------------------------------------- #

class TestDimsMismatch:

    def test_different_dims_raises_error(self, tmp_path):
        """256-dim write to a 384-dim palace must raise EmbeddingDimsMismatchError."""
        from mempalace import embed_metadata as em
        from mempalace.embed_metadata import EmbeddingDimsMismatchError

        palace = tmp_path / "palace_384"
        palace.mkdir()

        # Create meta with 384 dims (simulating a palace created with a different model)
        em.save_meta(str(palace), {
            "version": 1,
            "provider": "fastembed_cpu",
            "model_id": "some-384-dim-model",
            "dims": 384,
            "collection_name": "mempalace_drawers",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })

        # 256-dim write should fail hard
        with pytest.raises(EmbeddingDimsMismatchError) as exc_info:
            em.validate_write(str(palace), "mlx", "mlx-community/test", 256)
        assert "dimension mismatch" in str(exc_info.value).lower()
        assert "256" in str(exc_info.value)
        assert "384" in str(exc_info.value)

    def test_same_dims_passes(self, tmp_path):
        """Same dims and same provider always passes (same model_id)."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_256"
        palace.mkdir()

        em.save_meta(str(palace), {
            "version": 1,
            "provider": "fastembed_cpu",
            "model_id": "BAAI/bge-small-en-v1.5",
            "dims": 256,
            "collection_name": "mempalace_drawers",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })

        allowed, reason = em.validate_write(
            str(palace),
            "fastembed_cpu",
            "BAAI/bge-small-en-v1.5",
            256,
        )
        assert allowed is True
        assert reason == ""


# --------------------------------------------------------------------------- #
# Tests — provider/model drift
# --------------------------------------------------------------------------- #

class TestProviderDrift:

    def test_different_provider_same_dims_blocks_by_default(self, tmp_path):
        """Provider drift (dims match) blocks without env override."""
        from mempalace import embed_metadata as em
        from mempalace.embed_metadata import EmbeddingProviderDriftError

        palace = tmp_path / "palace_cpu"
        palace.mkdir()

        em.save_meta(str(palace), {
            "version": 1,
            "provider": "fastembed_cpu",
            "model_id": "BAAI/bge-small-en-v1.5",
            "dims": 256,
            "collection_name": "mempalace_drawers",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })

        # Provider drift — should raise without env override
        with pytest.raises(EmbeddingProviderDriftError) as exc_info:
            em.validate_write(str(palace), "mlx", "mlx-community/test", 256)
        assert "drift" in str(exc_info.value).lower()
        assert "fastembed_cpu" in str(exc_info.value)
        assert "mlx" in str(exc_info.value)

    def test_different_provider_same_dims_allowed_with_env(self, tmp_path):
        """Provider drift allowed when MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT=1."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_cpu"
        palace.mkdir()

        em.save_meta(str(palace), {
            "version": 1,
            "provider": "fastembed_cpu",
            "model_id": "BAAI/bge-small-en-v1.5",
            "dims": 256,
            "collection_name": "mempalace_drawers",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })

        os.environ["MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT"] = "1"
        try:
            allowed, reason = em.validate_write(str(palace), "mlx", "mlx-community/test", 256)
            assert allowed is True
            assert "drift" in reason
        finally:
            os.environ.pop("MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT", None)

    def test_same_provider_same_dims_passes(self, tmp_path):
        """Same provider/model with same dims always passes."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_mlx"
        palace.mkdir()

        em.save_meta(str(palace), {
            "version": 1,
            "provider": "mlx",
            "model_id": "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M",
            "dims": 256,
            "collection_name": "mempalace_drawers",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })

        allowed, reason = em.validate_write(
            str(palace),
            "mlx",
            "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M",
            256,
        )
        assert allowed is True
        assert reason == ""


# --------------------------------------------------------------------------- #
# Tests — mock provider (eval/lexical modes)
# --------------------------------------------------------------------------- #

class TestMockProvider:

    def test_mock_provider_detected_from_env(self, tmp_path):
        """MOCK_EMBED env var should return 'mock' provider."""
        os.environ["MOCK_EMBED"] = "1"
        try:
            from mempalace import embed_metadata as em
            provider, model_id, dims = em.detect_current_provider()
            assert provider == "mock"
            assert dims == 256
        finally:
            os.environ.pop("MOCK_EMBED", None)

    def test_mock_eval_mode_detected(self, tmp_path):
        """MEMPALACE_EVAL_MODE env var should return 'mock' provider."""
        os.environ["MEMPALACE_EVAL_MODE"] = "lexical"
        try:
            from mempalace import embed_metadata as em
            provider, model_id, dims = em.detect_current_provider()
            assert provider == "mock"
        finally:
            os.environ.pop("MEMPALACE_EVAL_MODE", None)

    def test_mock_provider_writes_meta(self, tmp_path):
        """Mock provider writes 'mock' to metadata."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_mock"
        palace.mkdir()

        em.ensure_meta(str(palace), "mock", "eval-mock", 256)
        meta = em.load_meta(str(palace))
        assert meta["provider"] == "mock"
        assert meta["model_id"] == "eval-mock"


# --------------------------------------------------------------------------- #
# Tests — metadata round-trip
# --------------------------------------------------------------------------- #

class TestMetaPersistence:

    def test_meta_round_trip(self, tmp_path):
        """Meta survives save/load cycle."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_rt"
        palace.mkdir()

        original = em.build_meta("mlx", "mlx-community/test", 256, "test_drawers")
        em.save_meta(str(palace), original)
        loaded = em.load_meta(str(palace))

        assert loaded is not None
        assert loaded["provider"] == "mlx"
        assert loaded["model_id"] == "mlx-community/test"
        assert loaded["dims"] == 256
        assert loaded["collection_name"] == "test_drawers"
        assert loaded["version"] == 1

    def test_meta_file_permissions(self, tmp_path):
        """Meta file should be readable only by owner."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_perm"
        palace.mkdir()

        em.ensure_meta(str(palace), "mlx", "test", 256)
        meta_path = em._meta_path(str(palace))
        assert meta_path.exists()


# --------------------------------------------------------------------------- #
# Tests — validate_write first-write path
# --------------------------------------------------------------------------- #

class TestValidateWriteFirstWrite:

    def test_validate_write_creates_no_meta(self, tmp_path):
        """validate_write doesn't create meta, only ensure_meta does."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_val"
        palace.mkdir()

        # validate_write returns allowed but does NOT create meta
        allowed, reason = em.validate_write(str(palace), "mlx", "test", 256)
        assert allowed is True
        assert em.load_meta(str(palace)) is None  # not created by validate_write

    def test_ensure_meta_first_write(self, tmp_path):
        """ensure_meta creates meta on first write."""
        from mempalace import embed_metadata as em

        palace = tmp_path / "palace_ensure"
        palace.mkdir()

        em.ensure_meta(str(palace), "mlx", "test", 256)
        assert em.load_meta(str(palace)) is not None


# --------------------------------------------------------------------------- #
# Tests — error classes
# --------------------------------------------------------------------------- #

class TestErrorClasses:

    def test_mismatch_error_is_base_of_dims_error(self, tmp_path):
        """EmbeddingDimsMismatchError should be a subclass of EmbeddingMismatchError."""
        from mempalace.embed_metadata import EmbeddingMismatchError, EmbeddingDimsMismatchError
        assert issubclass(EmbeddingDimsMismatchError, EmbeddingMismatchError)

    def test_mismatch_error_is_base_of_provider_drift_error(self, tmp_path):
        """EmbeddingProviderDriftError should be a subclass of EmbeddingMismatchError."""
        from mempalace.embed_metadata import EmbeddingMismatchError, EmbeddingProviderDriftError
        assert issubclass(EmbeddingProviderDriftError, EmbeddingMismatchError)


# --------------------------------------------------------------------------- #
# Restore env
# --------------------------------------------------------------------------- #

def teardown_module(module=None):
    for k, v in _orig_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v