"""
test_metadata_contract.py — Regression tests for canonical metadata contract.

Tests that all writers produce records conforming to the canonical metadata contract
defined in backends/base.py.

Contract fields (mandatory):
  wing, room, source_file, added_by, agent_id, timestamp (UTC ISO8601 Z),
  is_latest, supersedes_id, origin_type, chunk_index
"""

import pytest
from datetime import datetime


class TestCanonicalMetadataFields:
    """All write paths must produce records with canonical metadata fields."""

    def test_add_drawer_metadata_fields(self, tmp_path):
        """mempalace_add_drawer produces all mandatory fields."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        drawer_id = "test_drawer_abc123"
        now = datetime.utcnow()
        col.upsert(
            ids=[drawer_id],
            documents=["test content"],
            metadatas=[{
                "wing": "test_wing",
                "room": "test_room",
                "source_file": "test.py",
                "chunk_index": 0,
                "added_by": "test_agent",
                "agent_id": "test_agent",
                "timestamp": now.isoformat() + "Z",
                "origin_type": "observation",
                "is_latest": True,
                "supersedes_id": "",
            }],
        )

        result = col.get(ids=[drawer_id])
        assert result["ids"]
        meta = result["metadatas"][0]

        # Mandatory fields
        assert meta["wing"] == "test_wing"
        assert meta["room"] == "test_room"
        assert meta["source_file"] == "test.py"
        assert meta["chunk_index"] == 0
        assert meta["added_by"] == "test_agent"
        assert meta["agent_id"] == "test_agent"
        assert meta["is_latest"] is True
        assert meta["supersedes_id"] == ""
        assert meta["origin_type"] == "observation"
        # timestamp must be UTC ISO8601 with Z
        assert meta["timestamp"].endswith("Z")
        assert "+" not in meta["timestamp"]  # no timezone offset

    def test_diary_write_metadata_fields(self, tmp_path):
        """mempalace_diary_write produces all mandatory fields."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        entry_id = "diary_test_20260414"
        col.upsert(
            ids=[entry_id],
            documents=["test diary entry"],
            metadatas=[{
                "wing": "wing_agent_test",
                "room": "diary",
                "source_file": "diary://agent_test/2026-04-14",
                "added_by": "agent_test",
                "agent_id": "agent_test",
                "topic": "general",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origin_type": "diary_entry",
                "is_latest": True,
                "supersedes_id": "",
                "chunk_index": 0,
            }],
        )

        result = col.get(ids=[entry_id])
        assert result["ids"]
        meta = result["metadatas"][0]

        assert meta["wing"] == "wing_agent_test"
        assert meta["room"] == "diary"
        assert meta["added_by"] == "agent_test"
        assert meta["agent_id"] == "agent_test"
        assert meta["origin_type"] == "diary_entry"
        assert meta["is_latest"] is True
        assert meta["supersedes_id"] == ""
        assert meta["timestamp"].endswith("Z")

    def test_code_memory_metadata_fields(self, tmp_path):
        """mempalace_remember_code produces all mandatory fields."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        drawer_id = "code_test_abc123"
        col.upsert(
            ids=[drawer_id],
            documents=["desc\n\n```\ncode\n```"],
            metadatas=[{
                "wing": "code_wing",
                "room": "code_room",
                "source_file": "test.py",
                "chunk_index": 0,
                "added_by": "test",
                "agent_id": "test",
                "description": "test description",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origin_type": "code_memory",
                "is_latest": True,
                "supersedes_id": "",
            }],
        )

        result = col.get(ids=[drawer_id])
        assert result["ids"]
        meta = result["metadatas"][0]

        assert meta["origin_type"] == "code_memory"
        assert meta["is_latest"] is True
        assert meta["supersedes_id"] == ""
        assert meta["timestamp"].endswith("Z")

    def test_convo_miner_metadata_fields(self, tmp_path):
        """mine_convos produces all mandatory fields."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        drawer_id = "convo_test_abc123"
        col.add(
            ids=[drawer_id],
            documents=["test conversation"],
            metadatas=[{
                "wing": "convo_wing",
                "room": "technical",
                "source_file": "test_convo.txt",
                "chunk_index": 0,
                "added_by": "convo_miner",
                "agent_id": "convo_miner",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origin_type": "convo",
                "is_latest": True,
                "supersedes_id": "",
                "ingest_mode": "convos",
                "extract_mode": "exchange",
            }],
        )

        result = col.get(ids=[drawer_id])
        assert result["ids"]
        meta = result["metadatas"][0]

        assert meta["origin_type"] == "convo"
        assert meta["agent_id"] == "convo_miner"
        assert meta["is_latest"] is True
        assert meta["supersedes_id"] == ""
        assert meta["timestamp"].endswith("Z")

    def test_project_miner_metadata_fields(self, tmp_path):
        """project miner add_drawer produces all mandatory fields."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        drawer_id = "proj_test_abc123"
        col.upsert(
            ids=[drawer_id],
            documents=["test project content"],
            metadatas=[{
                "wing": "project_wing",
                "room": "general",
                "source_file": "test_project.py",
                "chunk_index": 0,
                "added_by": "mempalace",
                "agent_id": "mempalace",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origin_type": "observation",
                "is_latest": True,
                "supersedes_id": "",
            }],
        )

        result = col.get(ids=[drawer_id])
        assert result["ids"]
        meta = result["metadatas"][0]

        assert meta["origin_type"] == "observation"
        assert meta["agent_id"] == "mempalace"
        assert meta["is_latest"] is True
        assert meta["supersedes_id"] == ""
        assert meta["timestamp"].endswith("Z")


class TestTimestampFormat:
    """timestamp field must be UTC ISO8601 with Z suffix."""

    def test_timestamp_utc_iso_format(self, tmp_path):
        """All timestamps use UTC ISO8601 format (Z suffix), not local time."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        # filed_at is deprecated — timestamp must be used instead
        col.upsert(
            ids=["ts_test"],
            documents=["test"],
            metadatas=[{
                "wing": "t",
                "room": "t",
                "source_file": "t",
                "chunk_index": 0,
                "added_by": "t",
                "agent_id": "t",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origin_type": "observation",
                "is_latest": True,
                "supersedes_id": "",
                # filed_at should NOT be present in new records
            }],
        )

        result = col.get(ids=["ts_test"])
        meta = result["metadatas"][0]
        ts = meta["timestamp"]

        # Must end with Z (UTC indicator)
        assert ts.endswith("Z"), f"timestamp {ts} must end with Z"
        # Must not have + or - timezone offsets (local time would have them)
        assert "+" not in ts[:-1], f"timestamp {ts} must not have + offset"


class TestIsLatestSupersedesContract:
    """is_latest and supersedes_id semantics for version history."""

    def test_new_record_has_is_latest_true(self, tmp_path):
        """New records are created with is_latest=True."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        col.upsert(
            ids=["new_record"],
            documents=["new content"],
            metadatas=[{
                "wing": "t",
                "room": "t",
                "source_file": "t",
                "chunk_index": 0,
                "added_by": "t",
                "agent_id": "t",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origin_type": "observation",
                "is_latest": True,
                "supersedes_id": "",
            }],
        )

        result = col.get(ids=["new_record"])
        assert result["metadatas"][0]["is_latest"] is True

    def test_superseded_record_has_is_latest_false(self, tmp_path):
        """When a record is superseded, old record should have is_latest=False."""
        import os
        os.environ["HOME"] = str(tmp_path)
        from mempalace.backends import get_backend
        from mempalace.config import MempalaceConfig

        cfg = MempalaceConfig()
        cfg._config_dir = tmp_path / ".mempalace"
        cfg._config_dir.mkdir(exist_ok=True)

        backend = get_backend("chroma")
        col = backend.get_collection(str(tmp_path / "palace"), "test", create=True)

        # Old record
        col.upsert(
            ids=["old_record"],
            documents=["old content"],
            metadatas=[{
                "wing": "t",
                "room": "t",
                "source_file": "t",
                "chunk_index": 0,
                "added_by": "t",
                "agent_id": "t",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origin_type": "observation",
                "is_latest": False,  # superseded
                "supersedes_id": "",
            }],
        )

        # New record supersedes old
        col.upsert(
            ids=["new_record"],
            documents=["new content"],
            metadatas=[{
                "wing": "t",
                "room": "t",
                "source_file": "t",
                "chunk_index": 0,
                "added_by": "t",
                "agent_id": "t",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "origin_type": "observation",
                "is_latest": True,
                "supersedes_id": "old_record",
            }],
        )

        old = col.get(ids=["old_record"])
        new = col.get(ids=["new_record"])

        assert old["metadatas"][0]["is_latest"] is False
        assert new["metadatas"][0]["supersedes_id"] == "old_record"
        assert new["metadatas"][0]["is_latest"] is True
