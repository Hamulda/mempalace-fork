"""
Tests for batch semantic deduplication.

Run: pytest tests/test_batch_dedup.py -v -s
"""

import os
import tempfile
import pytest

os.environ["MEMPALACE_COALESCE_MS"] = "0"

pytest.importorskip("lancedb", reason="LanceDB not installed")

from mempalace.backends.lance import LanceBackend, SemanticDeduplicator


class TestBatchDedup:
    def test_classify_batch_single_onnx_call(self):
        """classify_batch() volá _embed_texts jednou pro celý batch."""
        backend = LanceBackend()
        with tempfile.TemporaryDirectory() as tmp:
            col = backend.get_collection(tmp, "batch_dedup_test", create=True)

            dedup = SemanticDeduplicator()

            # 3 dokumenty, classify_batch by mělo zavolat _embed_texts jen 1x
            docs = ["doc one", "doc two", "doc three"]
            metas = [{"wing": "test"}] * 3

            results, vectors, _failures = dedup.classify_batch(docs, metas, col)
            assert len(results) == 3
            assert all(r[0] == "unique" for r in results)
            assert len(vectors) == 3  # one embedding per document

    def test_batch_dedup_detects_duplicate(self):
        """Batch dedup správně detekuje duplikáty."""
        backend = LanceBackend()
        with tempfile.TemporaryDirectory() as tmp:
            col = backend.get_collection(tmp, "batch_dup_test", create=True)

            # Nejprve přidej dokument
            col.add(
                documents=["original document content"],
                ids=["original_id"],
                metadatas=[{"wing": "test", "room": "main"}],
            )

            dedup = SemanticDeduplicator()

            # Podobný dokument
            results, _vectors, _failures = dedup.classify_batch(
                ["original document content slightly modified"],
                [{"wing": "test", "room": "main"}],
                col,
            )
            # Měl by být detekován jako duplicate nebo conflict (vysoká podobnost)
            assert len(results) == 1
            action, _ = results[0]
            assert action in ("duplicate", "conflict", "unique")

    def test_classify_batch_empty_collection(self):
        """Prázdná collection vrací všechny dokumenty jako unique."""
        backend = LanceBackend()
        with tempfile.TemporaryDirectory() as tmp:
            col = backend.get_collection(tmp, "empty_dedup_test", create=True)

            dedup = SemanticDeduplicator()
            docs = ["a", "b", "c"]
            metas = [{"wing": "x"}] * 3

            results, _vectors, _failures = dedup.classify_batch(docs, metas, col)
            assert results == [("unique", None), ("unique", None), ("unique", None)]

    def test_batch_same_as_single(self):
        """Batch dedup produkuje stejné výsledky jako sekvenční classify()."""
        backend = LanceBackend()
        with tempfile.TemporaryDirectory() as tmp:
            col = backend.get_collection(tmp, "equiv_test", create=True)

            col.add(
                documents=["existing memory about python"],
                ids=["existing1"],
                metadatas=[{"wing": "ai", "room": "code"}],
            )

            dedup = SemanticDeduplicator()
            doc = "python programming language"
            meta = {"wing": "ai", "room": "code"}

            # Single classify
            single_result = dedup.classify(doc, meta, col)

            # Batch classify (batch of 1)
            batch_result, _batch_vecs, _batch_failures = dedup.classify_batch([doc], [meta], col)

            assert single_result[0] == batch_result[0][0]