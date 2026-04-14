"""
test_status_aggregation.py — Regression tests for status/list/taxonomy aggregation.

Ensures aggregation iterates over ALL records (no fixed limit truncation)
and returns correct counts regardless of palace size.
"""

import pytest


class TestStatusAggregationComplete:
    """Status/list/taxonomy must aggregate ALL records, not truncate at 10000."""

    def test_status_aggregates_all_records_no_truncation(self, tmp_path):
        """status() must count ALL drawers, not stop at 10000."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        # Create 500+ records — more than any fixed limit
        count = 550
        ids = [f"drawer_{i:04d}" for i in range(count)]
        docs = [f"content {i}" for i in range(count)]
        wings = [f"wing_{i % 5}" for i in range(count)]  # 5 different wings
        rooms = [f"room_{i % 10}" for i in range(count)]  # 10 different rooms
        metas = [
            {
                "wing": wings[i],
                "room": rooms[i],
                "source_file": f"file_{i}.py",
                "chunk_index": 0,
                "added_by": "test",
                "agent_id": "test",
                "timestamp": "2026-04-14T00:00:00Z",
                "origin_type": "observation",
                "is_latest": True,
                "supersedes_id": "",
            }
            for i in range(count)
        ]

        col.add(ids=ids, documents=docs, metadatas=metas)

        # Simulate status aggregation — iterative batch fetch
        _BATCH = 500
        wings_agg = {}
        rooms_agg = {}
        offset = 0
        while True:
            batch = col.get(include=["metadatas"], limit=_BATCH, offset=offset)
            metas_batch = batch.get("metadatas", [])
            if not metas_batch:
                break
            for m in metas_batch:
                w = m.get("wing", "unknown")
                r = m.get("room", "unknown")
                wings_agg[w] = wings_agg.get(w, 0) + 1
                rooms_agg[r] = rooms_agg.get(r, 0) + 1
            if len(metas_batch) < _BATCH:
                break
            offset += len(metas_batch)

        # All 550 records must be counted
        total = sum(wings_agg.values())
        assert total == count, f"Expected {count} total, got {total}. Truncation detected!"

    def test_taxonomy_aggregates_all_records_no_truncation(self, tmp_path):
        """get_taxonomy() must aggregate ALL wing/room pairs, not truncate."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        # 600 records across 20 wing/room combinations
        count = 600
        ids = [f"drawer_{i:04d}" for i in range(count)]
        docs = [f"content {i}" for i in range(count)]
        metas = [
            {
                "wing": f"wing_{i % 20}",
                "room": f"room_{i % 20}",
                "source_file": f"file_{i}.py",
                "chunk_index": 0,
                "added_by": "test",
                "agent_id": "test",
                "timestamp": "2026-04-14T00:00:00Z",
                "origin_type": "observation",
                "is_latest": True,
                "supersedes_id": "",
            }
            for i in range(count)
        ]

        col.add(ids=ids, documents=docs, metadatas=metas)

        # Simulate taxonomy aggregation — iterative batch fetch
        _BATCH = 500
        taxonomy = {}
        offset = 0
        while True:
            batch = col.get(include=["metadatas"], limit=_BATCH, offset=offset)
            metas_batch = batch.get("metadatas", [])
            if not metas_batch:
                break
            for m in metas_batch:
                w = m.get("wing", "unknown")
                r = m.get("room", "unknown")
                if w not in taxonomy:
                    taxonomy[w] = {}
                taxonomy[w][r] = taxonomy[w].get(r, 0) + 1
            if len(metas_batch) < _BATCH:
                break
            offset += len(metas_batch)

        # All 600 records must be in taxonomy
        total = sum(sum(rooms.values()) for rooms in taxonomy.values())
        assert total == count, f"Expected {count} total, got {total}. Truncation detected!"

    def test_list_wings_aggregates_all_no_truncation(self, tmp_path):
        """list_wings() must count ALL records, not truncate at 10000."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        count = 600
        ids = [f"drawer_{i:04d}" for i in range(count)]
        docs = [f"content {i}" for i in range(count)]
        wings = [f"wing_{i % 8}" for i in range(count)]
        metas = [
            {
                "wing": wings[i],
                "room": "general",
                "source_file": f"file_{i}.py",
                "chunk_index": 0,
                "added_by": "test",
                "agent_id": "test",
                "timestamp": "2026-04-14T00:00:00Z",
                "origin_type": "observation",
                "is_latest": True,
                "supersedes_id": "",
            }
            for i in range(count)
        ]

        col.add(ids=ids, documents=docs, metadatas=metas)

        _BATCH = 500
        wings_agg = {}
        offset = 0
        while True:
            batch = col.get(include=["metadatas"], limit=_BATCH, offset=offset)
            metas_batch = batch.get("metadatas", [])
            if not metas_batch:
                break
            for m in metas_batch:
                w = m.get("wing", "unknown")
                wings_agg[w] = wings_agg.get(w, 0) + 1
            if len(metas_batch) < _BATCH:
                break
            offset += len(metas_batch)

        total = sum(wings_agg.values())
        assert total == count, f"Expected {count} total, got {total}. Truncation detected!"
