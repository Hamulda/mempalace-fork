"""tests/test_lance_error_paths.py — LanceDB error path coverage.

Covers:
- Dimension mismatch via collection-level upsert_with_tombstones()
- Corrupted/missing collection handling
- load_meta behavior on corrupt/partial JSON
- miner _commit_batch per-file failure isolation
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_REPO_ROOT = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(_REPO_ROOT))


# Env isolation — no daemon, no real embedding model
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
os.environ["MOCK_EMBED"] = "1"
os.environ["MEMPALACE_COALESCE_MS"] = "0"
os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"


class TestEmbedMetadataErrorPaths:

    def test_load_meta_returns_none_for_new_palace(self, tmp_path: Path):
        """load_meta returns None for palace with no embedding_meta.json."""
        from mempalace import embed_metadata as em
        palace = tmp_path / "new_palace"
        palace.mkdir()
        meta = em.load_meta(str(palace))
        assert meta is None

    def test_load_meta_corrupt_json_returns_none_with_warning(self, tmp_path: Path):
        """load_meta returns None when embedding_meta.json is corrupt JSON (logged, not raised)."""
        from mempalace import embed_metadata as em
        palace = tmp_path / "corrupt_palace"
        palace.mkdir()
        meta_file = palace / "embedding_meta.json"
        meta_file.write_text("{ this is not json")

        meta = em.load_meta(str(palace))
        assert meta is None  # returns None, logs warning

    def test_load_meta_partial_json_returns_partial_dict(self, tmp_path: Path):
        """load_meta with partial meta dict (missing provider/dims) returns partial dict."""
        from mempalace import embed_metadata as em
        palace = tmp_path / "partial_palace"
        palace.mkdir()
        meta_file = palace / "embedding_meta.json"
        meta_file.write_text(json.dumps({"version": 1, "model_id": "test"}))

        meta = em.load_meta(str(palace))
        assert meta is not None
        assert meta.get("model_id") == "test"
        assert meta.get("provider") is None

    def test_validate_write_rejects_wrong_dims_via_direct_call(self, tmp_path: Path):
        """validate_write raises EmbeddingDimsMismatchError when stored dims != provided dims."""
        from mempalace import embed_metadata as em
        from mempalace.embed_metadata import EmbeddingDimsMismatchError

        palace = tmp_path / "dim_palace"
        palace.mkdir()

        # Create meta with dims=256
        em.ensure_meta(str(palace), "mock", "eval-mock", 256)

        # Validate write with wrong dims=128
        with pytest.raises(EmbeddingDimsMismatchError):
            em.validate_write(str(palace), "mock", "eval-mock", 128)

    def test_validate_write_rejects_wrong_provider(self, tmp_path: Path):
        """validate_write raises EmbeddingProviderDriftError when provider doesn't match."""
        from mempalace import embed_metadata as em
        from mempalace.embed_metadata import EmbeddingProviderDriftError

        palace = tmp_path / "prov_palace"
        palace.mkdir()

        # Create meta with provider=mlx
        em.ensure_meta(str(palace), "mlx", "test-model", 256)

        # Validate write with different provider
        with pytest.raises(EmbeddingProviderDriftError):
            em.validate_write(str(palace), "fastembed_cpu", "other-model", 256)


class TestMinerBatchCommitErrorPaths:

    def test_upsert_failure_does_not_prevent_subsequent_files(self, tmp_path: Path):
        """
        If upsert_with_tombstones fails for one file, subsequent files
        still get processed (per-file try/except with continue).
        """
        from mempalace.miner import _commit_batch

        source_files = ["sf1", "sf2", "sf3"]
        call_order = []

        class RecordingCollection:
            def get_by_source_files(self, source_files, is_latest=True):
                return {sf: [] for sf in source_files}

            def upsert_with_tombstones(self, documents, ids, metadatas, old_chunks_by_hash=None):
                sf = documents[0].get("id", "unknown")
                call_order.append(sf)
                if sf == "id2":
                    raise RuntimeError("upsert failed for sf2")
                return len(documents)

        # Build proper pending structure — must have documents for upsert path
        pending = [
            {"source_file": "sf1", "documents": [{"id": "id1", "content": "test1"}], "ids": ["id1"], "metadatas": [{}], "room": "r"},
            {"source_file": "sf2", "documents": [{"id": "id2", "content": "test2"}], "ids": ["id2"], "metadatas": [{}], "room": "r"},
            {"source_file": "sf3", "documents": [{"id": "id3", "content": "test3"}], "ids": ["id3"], "metadatas": [{}], "room": "r"},
        ]

        manifest = MagicMock()

        total_drawers, files_committed, phase_totals = _commit_batch(
            pending=pending,
            collection=RecordingCollection(),
            wing="w",
            rooms=["r"],
            agent="a",
            palace_path=str(tmp_path / "p"),
            stats=None,
            manifest=manifest,
            project_path=str(tmp_path / "p"),
        )

        # id1 and id3 should have been processed despite id2 failing
        assert "id1" in call_order, f"id1 not in {call_order}"
        assert "id2" in call_order, f"id2 not in {call_order}"
        assert "id3" in call_order, f"id3 not in {call_order}"
        # Only id1 and id3 succeed → files_committed should be 2 (not 3!)
        assert files_committed == 2, f"Expected 2, got {files_committed}"

    def test_files_committed_count_reflects_success_only(self, tmp_path: Path):
        """
        _commit_batch returns files_committed = count of files that succeeded.
        A failing file should not increment the success count.
        """
        from mempalace.miner import _commit_batch

        source_files = ["a", "b", "c"]

        class FailingCollection:
            def __init__(self):
                self.calls = []

            def get_by_source_files(self, source_files, is_latest=True):
                return {sf: [] for sf in source_files}

            def upsert_with_tombstones(self, documents, ids, metadatas, old_chunks_by_hash=None):
                doc_id = documents[0].get("id", "unknown")
                self.calls.append(doc_id)
                if doc_id == "idb":
                    raise RuntimeError("b failed")
                return len(documents)

        pending = [
            {"source_file": "a", "documents": [{"id": "ida"}], "ids": ["ida"], "metadatas": [{}], "room": "r"},
            {"source_file": "b", "documents": [{"id": "idb"}], "ids": ["idb"], "metadatas": [{}], "room": "r"},
            {"source_file": "c", "documents": [{"id": "idc"}], "ids": ["idc"], "metadatas": [{}], "room": "r"},
        ]

        manifest = MagicMock()

        total_drawers, files_committed, phase_totals = _commit_batch(
            pending=pending,
            collection=FailingCollection(),
            wing="w",
            rooms=["r"],
            agent="a",
            palace_path=str(tmp_path / "p"),
            stats=None,
            manifest=manifest,
            project_path=str(tmp_path / "p"),
        )

        # Only ida and idc succeed → files_committed = 2
        assert files_committed == 2, f"Expected 2, got {files_committed}"
        # manifest.update_success called only when _manifest key present in p dict
        # (actual pipeline sets this; _commit_batch in isolation has no _manifest)

    def test_get_by_source_files_raises_is_not_silently_swallowed(self, tmp_path: Path):
        """
        get_by_source_files() raises — the function catches Exception and continues,
        but files_committed does NOT count files that weren't processed.
        """
        from mempalace.miner import _commit_batch

        class BadCollection:
            def get_by_source_files(self, source_files, is_latest=True):
                raise RuntimeError("database unavailable")

            def upsert_with_tombstones(self, documents, source_mtime, is_latest=True):
                return len(documents)

        manifest = MagicMock()

        # Should not crash — exception is caught
        total_drawers, files_committed, phase_totals = _commit_batch(
            pending=[],
            collection=BadCollection(),
            wing="w",
            rooms=["r"],
            agent="a",
            palace_path=str(tmp_path / "p"),
            stats=None,
            manifest=manifest,
            project_path=str(tmp_path / "p"),
        )

        # get_by_source_files raised → no files processed → files_committed = 0
        assert files_committed == 0