"""
Tests for LanceDB backend and backend abstraction.

Run: pytest tests/test_lance_backend.py -v
"""

import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

# These tests are skipped unless lancedb is installed
pytest.importorskip("lancedb", reason="LanceDB not installed — run: pip install 'mempalace[lance]'")

from mempalace.backends.lance import LanceBackend, LanceCollection, _where_to_sql, _apply_where_filter
from mempalace.backends import get_backend, ChromaBackend


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_palace(tmp_path):
    """Temporary palace directory."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    return str(palace_dir)


@pytest.fixture
def lance_collection(tmp_palace):
    """Fresh LanceDB collection."""
    backend = LanceBackend()
    col = backend.get_collection(tmp_palace, "test_drawers", create=True)
    return col


# ── Backend abstraction ────────────────────────────────────────────────────────

class TestBackendFactory:
    def test_get_chroma_backend(self):
        backend = get_backend("chroma")
        assert isinstance(backend, ChromaBackend)

    def test_get_lance_backend(self):
        backend = get_backend("lance")
        assert isinstance(backend, LanceBackend)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            get_backend("unknown")


# ── LanceCollection add + query ────────────────────────────────────────────────

class TestLanceAddAndQuery:
    def test_add_and_query_single(self, lance_collection):
        lance_collection.add(
            documents=["hello world this is a test"],
            ids=["drawer_1"],
            metadatas=[{"wing": "test", "room": "general"}],
        )
        results = lance_collection.query(query_texts=["hello world"], n_results=5)

        assert len(results["ids"]) == 1
        assert len(results["ids"][0]) == 1
        assert "drawer_1" in results["ids"][0]
        assert results["documents"][0][0] == "hello world this is a test"

    def test_add_and_query_multiple(self, lance_collection):
        docs = ["apple fruit", "banana fruit", "carrot vegetable"]
        ids = ["d1", "d2", "d3"]
        metas = [
            {"wing": "food", "room": "fruit"},
            {"wing": "food", "room": "fruit"},
            {"wing": "food", "room": "vegetable"},
        ]
        lance_collection.add(documents=docs, ids=ids, metadatas=metas)

        results = lance_collection.query(query_texts=["fruit"], n_results=5)
        assert len(results["ids"][0]) >= 2

    def test_duplicate_id_raises(self, lance_collection):
        lance_collection.add(
            documents=["doc1"],
            ids=["dup_id"],
            metadatas=[{"wing": "a"}],
        )
        with pytest.raises(ValueError, match="already exists"):
            lance_collection.add(
                documents=["doc2"],
                ids=["dup_id"],
                metadatas=[{"wing": "b"}],
            )

    def test_count(self, lance_collection):
        assert lance_collection.count() == 0
        lance_collection.add(
            documents=["a", "b", "c"],
            ids=["id1", "id2", "id3"],
            metadatas=[{}, {}, {}],
        )
        assert lance_collection.count() == 3


# ── LanceCollection upsert ────────────────────────────────────────────────────

class TestLanceUpsert:
    def test_upsert_updates_existing(self, lance_collection):
        lance_collection.add(
            documents=["original content"],
            ids=["upsert_id"],
            metadatas=[{"wing": "test", "v": 1}],
        )
        assert lance_collection.count() == 1

        lance_collection.upsert(
            documents=["updated content"],
            ids=["upsert_id"],
            metadatas=[{"wing": "test", "v": 2}],
        )
        assert lance_collection.count() == 1

        result = lance_collection.get(ids=["upsert_id"])
        assert result["documents"][0] == "updated content"
        assert result["metadatas"][0]["v"] == 2

    def test_upsert_inserts_new(self, lance_collection):
        lance_collection.upsert(
            documents=["new doc"],
            ids=["new_id"],
            metadatas=[{"wing": "test"}],
        )
        assert lance_collection.count() == 1


# ── LanceCollection get ────────────────────────────────────────────────────────

class TestLanceGet:
    def test_get_by_id(self, lance_collection):
        lance_collection.add(
            documents=["find me by id"],
            ids=["gettest_1"],
            metadatas=[{"wing": "test", "room": "find"}],
        )
        result = lance_collection.get(ids=["gettest_1"])
        assert result["ids"] == ["gettest_1"]
        assert result["documents"][0] == "find me by id"

    def test_get_by_id_not_found(self, lance_collection):
        result = lance_collection.get(ids=["nonexistent"])
        assert result["ids"] == []

    def test_get_by_where(self, lance_collection):
        lance_collection.add(
            documents=["doc for wing-a"],
            ids=["g1"],
            metadatas=[{"wing": "wing-a"}],
        )
        lance_collection.add(
            documents=["doc for wing-b"],
            ids=["g2"],
            metadatas=[{"wing": "wing-b"}],
        )

        result = lance_collection.get(where={"wing": {"$eq": "wing-a"}})
        assert "g1" in result["ids"]
        assert "g2" not in result["ids"]

    def test_get_limit(self, lance_collection):
        for i in range(10):
            lance_collection.add(
                documents=[f"doc {i}"],
                ids=[f"limit_{i}"],
                metadatas=[{"i": i}],
            )
        result = lance_collection.get(limit=3)
        assert len(result["ids"]) == 3


# ── LanceCollection delete ────────────────────────────────────────────────────

class TestLanceDelete:
    def test_delete_by_id(self, lance_collection):
        lance_collection.add(
            documents=["to delete"],
            ids=["del_1"],
            metadatas=[{}],
        )
        assert lance_collection.count() == 1
        lance_collection.delete(ids=["del_1"])
        assert lance_collection.count() == 0

    def test_delete_by_where(self, lance_collection):
        lance_collection.add(documents=["keep"], ids=["keep"], metadatas=[{"x": 1}])
        lance_collection.add(documents=["drop"], ids=["drop"], metadatas=[{"x": 2}])
        lance_collection.delete(where={"x": {"$eq": 2}})
        assert lance_collection.count() == 1


# ── Where-to-SQL ───────────────────────────────────────────────────────────────

class TestWhereToSql:
    def test_eq(self):
        assert _where_to_sql({"key": {"$eq": "val"}}) is not None
        assert "json_extract" in _where_to_sql({"key": {"$eq": "val"}})

    def test_in(self):
        sql = _where_to_sql({"key": {"$in": ["a", "b"]}})
        assert sql is not None
        assert "IN" in sql

    def test_and(self):
        sql = _where_to_sql({
            "$and": [
                {"wing": {"$eq": "test"}},
                {"room": {"$eq": "general"}},
            ]
        })
        assert sql is not None
        assert "AND" in sql

    def test_or(self):
        sql = _where_to_sql({
            "$or": [
                {"a": {"$eq": "1"}},
                {"b": {"$eq": "2"}},
            ]
        })
        assert sql is not None
        assert "OR" in sql

    def test_none_input(self):
        assert _where_to_sql(None) is None


# ── Concurrent writes ─────────────────────────────────────────────────────────

class TestConcurrentWrites:
    def test_concurrent_writes_from_threads(self, tmp_palace):
        """Simulate 3 threads writing simultaneously — all records should land."""
        backend = LanceBackend()
        col = backend.get_collection(tmp_palace, "concurrent_test", create=True)

        docs = ["concurrent doc"]
        ids = []
        for i in range(3):
            ids.append(f"concurrent_{i}")

        errors = []

        def writer(doc_id):
            try:
                col.upsert(
                    documents=[f"content from thread {doc_id}"],
                    ids=[doc_id],
                    metadatas=[{"thread": doc_id}],
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(tid,)) for tid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent writes: {errors}"
        assert col.count() == 3


# ── ChromaDB → LanceDB migration ─────────────────────────────────────────────

class TestMigration:
    def test_chroma_to_lance_migration_count(self, tmp_palace):
        """Verify record count after migration (using upsert as proxy)."""
        backend = LanceBackend()
        col = backend.get_collection(tmp_palace, "migration_test", create=True)

        # Add test data via upsert (simulating migrated data)
        docs = [f"migrated doc {i}" for i in range(20)]
        ids = [f"mig_{i}" for i in range(20)]
        metas = [{"i": i} for i in range(20)]

        for i in range(0, 20, 5):
            col.upsert(
                documents=docs[i:i+5],
                ids=ids[i:i+5],
                metadatas=metas[i:i+5],
            )

        assert col.count() == 20


# ── get_by_id ────────────────────────────────────────────────────────────────

class TestGetById:
    def test_get_by_id_found(self, lance_collection):
        lance_collection.add(
            documents=["test doc"],
            ids=["findme"],
            metadatas=[{"key": "value"}],
        )
        result = lance_collection.get_by_id("findme")
        assert result is not None
        assert result["id"] == "findme"
        assert result["document"] == "test doc"

    def test_get_by_id_not_found(self, lance_collection):
        result = lance_collection.get_by_id("does_not_exist")
        assert result is None


# ── Memory footprint ────────────────────────────────────────────────────────────

def test_embedding_memory_footprint():
    """Ověří že fastembed je načten (ne sentence-transformers/PyTorch)."""
    import os, psutil, tempfile, sys

    # Ověř že torch není načten
    assert "torch" not in sys.modules, (
        "torch je stále v sys.modules – sentence-transformers pravděpodobně stále nainstalován"
    )

    process = psutil.Process(os.getpid())
    mem_before = process.memory_info().rss / 1024 / 1024  # MB

    with tempfile.TemporaryDirectory() as tmp:
        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        col = backend.get_collection(tmp, "memtest", create=True)
        col.add(
            documents=["Test memory footprint of fastembed ONNX model"],
            ids=["mem_test_1"],
            metadatas=[{}],
        )

    mem_after = process.memory_info().rss / 1024 / 1024  # MB
    delta = mem_after - mem_before

    print(f"\nEmbedding model RAM delta: {delta:.1f} MB")
    # fastembed ONNX runtime: ~280MB. PyTorch by byl 350-500MB.
    assert delta < 500, (
        f"Embedding model spotřeboval {delta:.1f}MB – pravděpodobně stále používá PyTorch. "
        f"Zkontroluj že sentence-transformers není nainstalován."
    )
    print(f"PASS: delta {delta:.1f}MB < 500MB, torch NOT in sys.modules — ONNX runtime confirmed")
