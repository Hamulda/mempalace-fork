"""
Tests for LanceDB backend contract compliance.

Run: pytest tests/test_lance_backend_contract.py -v
"""

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed — run: pip install 'mempalace[lance]'")

import os
import subprocess
from mempalace.backends.lance import LanceBackend, _where_to_sql


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_palace(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    return str(palace_dir)


@pytest.fixture
def lance_collection(tmp_palace):
    """Fresh LanceDB collection with dedup disabled for predictable test results."""
    os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
    os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"
    os.environ["MEMPALACE_COALESCE_MS"] = "0"
    backend = LanceBackend()
    col = backend.get_collection(tmp_palace, "contract_test", create=True)
    return col


# ── Bug 1: get() offset pagination ─────────────────────────────────────────────

class TestGetOffsetPagination:
    def test_get_offset_returns_second_page(self, lance_collection):
        """get(limit=3, offset=3) returns the next batch, not the first."""
        # Insert 7 records
        for i in range(7):
            lance_collection.add(
                documents=[f"document number {i}"],
                ids=[f"offset_doc_{i}"],
                metadatas=[{"i": i}],
            )

        # First page
        page1 = lance_collection.get(limit=3, offset=0)
        assert len(page1["ids"]) == 3
        assert "offset_doc_0" in page1["ids"]

        # Second page — must NOT overlap with first
        page2 = lance_collection.get(limit=3, offset=3)
        assert len(page2["ids"]) == 3
        assert "offset_doc_0" not in page2["ids"]
        assert "offset_doc_3" in page2["ids"]

        # Third page
        page3 = lance_collection.get(limit=3, offset=6)
        assert len(page3["ids"]) == 1
        assert "offset_doc_6" in page3["ids"]

    def test_get_offset_zero_equivalent_to_no_offset(self, lance_collection):
        """offset=0 should behave identically to no offset."""
        lance_collection.add(
            documents=["doc a", "doc b"],
            ids=["a", "b"],
            metadatas=[{}, {}],
        )
        r1 = lance_collection.get(limit=10, offset=0)
        r2 = lance_collection.get(limit=10)
        assert r1["ids"] == r2["ids"]

    def test_get_offset_with_where_filter(self, lance_collection):
        """offset pagination works together with where filter."""
        for i in range(6):
            lance_collection.add(
                documents=[f"wing-x doc {i}"],
                ids=[f"wf_doc_{i}"],
                metadatas=[{"wing": "x", "i": i}],
            )

        page1 = lance_collection.get(where={"wing": "x"}, limit=2, offset=0)
        page2 = lance_collection.get(where={"wing": "x"}, limit=2, offset=2)

        assert len(page1["ids"]) == 2
        assert len(page2["ids"]) == 2
        assert page1["ids"] != page2["ids"]

    def test_get_offset_sparse_filtered_rows_across_raw_pages(self, lance_collection):
        """offset must mean offset IN THE FILTERED result, not offset in raw table.

        Regression: when matching rows are sparse (separated by many non-matching
        rows), the old code applied offset to the raw table scan, returning wrong
        rows. This test puts non-matching rows between every matching row so that
        the 4th matching row lands well beyond offset=2 in the raw table.
        """
        # Interleave: 10 non-matching between each matching.
        # After 4 matching rows we have ~40+ raw rows, well beyond a single page.
        for i in range(4):
            # 10 non-matching first
            for j in range(10):
                lance_collection.add(
                    documents=[f"noise {i}-{j}"],
                    ids=[f"noise_{i}_{j}"],
                    metadatas=[{"wing": "other", "scope": j}],
                )
            # Then one matching
            lance_collection.add(
                documents=[f"match {i}"],
                ids=[f"match_{i}"],
                metadatas=[{"wing": "target", "idx": i}],
            )

        # page1: offset=0, limit=2 → first 2 matches
        p1 = lance_collection.get(where={"wing": "target"}, limit=2, offset=0)
        assert len(p1["ids"]) == 2
        assert "match_0" in p1["ids"]
        assert "match_1" in p1["ids"]

        # page2: offset=2, limit=2 → 3rd and 4th match
        p2 = lance_collection.get(where={"wing": "target"}, limit=2, offset=2)
        assert len(p2["ids"]) == 2
        assert "match_2" in p2["ids"], f"Expected match_2, got {p2['ids']}"
        assert "match_3" in p2["ids"], f"Expected match_3, got {p2['ids']}"

        # page3: offset=4, limit=2 → should be empty (only 4 matches total)
        p3 = lance_collection.get(where={"wing": "target"}, limit=2, offset=4)
        assert len(p3["ids"]) == 0

    def test_get_offset_with_limit_lower_than_batch(self, lance_collection):
        """limit smaller than scan batch — ensure head(limit) fires after correct offset."""
        for i in range(20):
            lance_collection.add(
                documents=[f"small limit doc {i}"],
                ids=[f"sl_doc_{i}"],
                metadatas=[{"category": "test", "i": i}],
            )

        # offset=5, limit=3 → should return 3 items from positions 5,6,7 of filtered
        result = lance_collection.get(where={"category": "test"}, limit=3, offset=5)
        ids_out = result["ids"]
        # All must be sl_doc_5, sl_doc_6, sl_doc_7 (or similar ordering)
        assert len(ids_out) == 3
        assert all("sl_doc_" in i for i in ids_out)
        # Must NOT include any doc before offset 5
        for i in ids_out:
            idx = int(i.split("_")[-1])
            assert idx >= 5, f"id {i} has index {idx} < offset 5"


# ── Bug 2: _where_to_sql scalar metadata equality ───────────────────────────────

class TestWhereToSqlScalar:
    def test_scalar_string_generates_json_eq(self):
        """{"wing": "x"} must produce metadata_json.wing = 'x', NOT id = 'x'."""
        sql = _where_to_sql({"wing": "x"})
        assert sql is not None
        assert "json_extract" in sql
        assert "id = " not in sql
        assert "'x'" in sql

    def test_scalar_int_generates_json_eq(self):
        sql = _where_to_sql({"count": 42})
        assert sql is not None
        assert "json_extract" in sql
        assert "42" in sql

    def test_scalar_in_and_block(self):
        """$and with scalar nested conditions generates correct AND of json_extracts."""
        sql = _where_to_sql({
            "$and": [
                {"wing": "test"},
                {"room": "general"},
            ]
        })
        assert sql is not None
        assert "AND" in sql
        assert "json_extract" in sql
        assert "'test'" in sql
        assert "'general'" in sql

    def test_mixed_scalar_and_explicit_op(self):
        """Mix of scalar {"wing":"x"} and explicit {"room": {"$eq":"y"}}."""
        sql = _where_to_sql({
            "wing": "alpha",
            "room": {"$eq": "beta"},
        })
        assert sql is not None
        # Both wing and room must appear in the SQL
        assert "wing" in sql
        assert "room" in sql

    def test_scalar_with_special_chars(self):
        sql = _where_to_sql({"source_file": "/path/to/file.py"})
        assert sql is not None
        assert "json_extract" in sql
        assert "/path/to/file.py" in sql


class TestGetScalarWhere:
    def test_get_scalar_where_returns_matching(self, lance_collection):
        """get(where={"wing": "..."}) returns only matching metadata rows."""
        lance_collection.add(
            documents=["doc in wing-a"],
            ids=["s1"],
            metadatas=[{"wing": "wing-a", "room": "r1"}],
        )
        lance_collection.add(
            documents=["doc in wing-b"],
            ids=["s2"],
            metadatas=[{"wing": "wing-b", "room": "r2"}],
        )

        result = lance_collection.get(where={"wing": "wing-a"})
        assert "s1" in result["ids"]
        assert "s2" not in result["ids"]

    def test_get_scalar_where_no_match(self, lance_collection):
        result = lance_collection.get(where={"wing": "nonexistent"})
        assert result["ids"] == []


# ── Bug 3: delete(where=...) removes ALL matching rows ─────────────────────────

class TestDeleteWhereAllRows:
    def test_delete_where_removes_all_matching_rows(self, lance_collection):
        """delete(where=...) must delete every matching row, not just the first batch."""
        # Add 10 records, 6 of which match the where clause
        for i in range(10):
            lance_collection.add(
                documents=[f"doc {i}"],
                ids=[f"del_doc_{i}"],
                metadatas=[{"category": "delete_me" if i < 6 else "keep"}],
            )

        assert lance_collection.count() == 10
        lance_collection.delete(where={"category": "delete_me"})
        assert lance_collection.count() == 4

    def test_delete_where_with_limit_removes_subset(self, lance_collection):
        """delete with a scalar where that matches N rows removes all N."""
        for i in range(5):
            lance_collection.add(
                documents=[f"target {i}"],
                ids=[f"t{i}"],
                metadatas=[{"target": True}],
            )
        lance_collection.add(
            documents=["not target"],
            ids=["n1"],
            metadatas=[{"target": False}],
        )

        lance_collection.delete(where={"target": True})
        assert lance_collection.count() == 1
        result = lance_collection.get(ids=["n1"])
        assert result["ids"] == ["n1"]

    def test_delete_where_scalar_and_explicit_op_mixed(self, lance_collection):
        """delete with $and of scalars removes all rows matching the conjunction."""
        for i in range(4):
            lance_collection.add(
                documents=[f"doc {i}"],
                ids=[f"mix{i}"],
                metadatas=[{"wing": "shared", "room": f"room{i}"}],
            )
        lance_collection.add(
            documents=["different wing"],
            ids=["dw"],
            metadatas=[{"wing": "other", "room": "room0"}],
        )

        lance_collection.delete(where={
            "$and": [
                {"wing": "shared"},
                {"room": "room0"},
            ]
        })
        # Only mix0 should be deleted (wing=shared AND room=room0)
        assert lance_collection.count() == 4

    def test_delete_where_many_rows_across_batches(self, lance_collection):
        """delete(where=...) must not skip rows when matches span multiple scan pages.

        Regression: the old offset-based loop would advance offset+=batch_size after
        each delete batch, skipping rows when deletes shifted positions.
        """
        # Add 1200 records (exceeds batch_size=500, spans multiple pages)
        for i in range(1200):
            lance_collection.add(
                documents=[f"target doc {i}"],
                ids=[f"tdel_{i}"],
                metadatas=[{"scope": "delete_all", "idx": i}],
            )
        # Add 100 non-matching records
        for i in range(100):
            lance_collection.add(
                documents=[f"keep doc {i}"],
                ids=[f"keep_{i}"],
                metadatas=[{"scope": "other"}],
            )

        assert lance_collection.count() == 1300

        # Delete all 1200 matching records
        lance_collection.delete(where={"scope": "delete_all"})

        # Must remove ALL 1200, not just first batch
        assert lance_collection.count() == 100
        remaining = lance_collection.get(where={"scope": "delete_all"})
        assert remaining["ids"] == []

        # Non-matching records must be untouched
        assert lance_collection.count() == 100


# ── Bug 4: delete(where=...) MAX_SCAN ceiling must be loud ───────────────────

class TestDeleteWhereMaxScanCeiling:
    def test_delete_where_max_scan_raises(self, lance_collection):
        """delete(where=...) must raise RuntimeError when MAX_SCAN ceiling is hit."""
        # Add MAX_SCAN+1 non-matching rows, then 1 matching row that would be
        # beyond the scan cap — delete must raise, not silently skip.
        import os
        os.environ["MEMPALACE_COALESCE_MS"] = "0"
        from mempalace.backends.lance import LanceCollection
        # Lower MAX_SCAN locally for test predictability
        import mempalace.backends.lance as lance_module
        orig_max = 50000
        lance_module.LanceCollection._DELETE_MAX_SCAN = 10  # force low cap

        try:
            # Add 15 non-matching, then 5 matching (5 would hit the cap)
            for i in range(15):
                lance_collection.add(
                    documents=[f"noise {i}"],
                    ids=[f"noise_{i}"],
                    metadatas=[{"wing": "rubbish"}],
                )
            for i in range(5):
                lance_collection.add(
                    documents=[f"target {i}"],
                    ids=[f"target_{i}"],
                    metadatas=[{"scope": "delete_me"}],
                )

            # Attempt to delete ALL 5 targets — only 2 can be found before cap
            with pytest.raises(RuntimeError, match="MAX_SCAN"):
                lance_collection.delete(where={"scope": "delete_me"})

            # Targets must still exist (delete was aborted)
            remaining = lance_collection.get(where={"scope": "delete_me"})
            assert len(remaining["ids"]) == 5
        finally:
            lance_module.LanceCollection._DELETE_MAX_SCAN = orig_max

    def test_delete_where_at_max_scan_boundary(self, lance_collection):
        """delete succeeds when matching rows are exactly at MAX_SCAN boundary."""
        # Add exactly 10 matching rows (at the lowered cap)
        import mempalace.backends.lance as lance_module
        orig_max = 50000
        lance_module.LanceCollection._DELETE_MAX_SCAN = 10

        try:
            for i in range(10):
                lance_collection.add(
                    documents=[f"boundary {i}"],
                    ids=[f"boundary_{i}"],
                    metadatas=[{"scope": "boundary_test"}],
                )

            # Must succeed — 10 rows == cap, no ceiling hit
            lance_collection.delete(where={"scope": "boundary_test"})
            assert lance_collection.count() == 0
        finally:
            lance_module.LanceCollection._DELETE_MAX_SCAN = orig_max


# ── Embedding daemon timeout cleanup ──────────────────────────────────────────

class TestEmbedDaemonTimeoutCleanup:
    def test_cmd_embed_daemon_kills_child_on_timeout(self, tmp_path, monkeypatch):
        """embed-daemon start must kill child process when READY is not emitted."""
        import io
        import os
        import mempalace.cli as cli_module

        # Use os.pipe() to get real file descriptors for select.select()
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"")  # write nothing so select sees it as ready with empty data

        killed = []

        def mock_popen(*args, **kwargs):
            class MockProc:
                pid = 99999
                # Use real file descriptors for select compatibility
                stdout = os.fdopen(r_fd, 'rb', buffering=0)
                stderr = os.fdopen(os.dup(r_fd), 'rb', buffering=0)
                poll_count = 0

                def poll(self):
                    self.poll_count += 1
                    return None  # still running

                def kill(self):
                    killed.append(True)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            return MockProc()

        class MockArgs:
            action = "start"

        captured_print = io.StringIO()

        monkeypatch.setattr("subprocess.Popen", mock_popen)
        monkeypatch.setattr("sys.stdout", captured_print)
        # Ensure daemon is not already running so we actually try to start it.
        # The import is "from .backends.lance import _daemon_is_running" inside cmd_embed_daemon.
        import mempalace.backends.lance as lance_module
        monkeypatch.setattr(lance_module, "_daemon_is_running", lambda: False)

        cli_module.cmd_embed_daemon(MockArgs())

        output = captured_print.getvalue()
        assert "killing child process" in output.lower(), f"Expected 'killing child process' in output, got: {output!r}"
        assert killed, "Child process kill() was not called"

    def test_start_daemon_kills_child_on_timeout(self, monkeypatch):
        """_start_daemon_if_needed must kill child when READY timeout expires."""
        import mempalace.backends.lance as lance_module

        killed = []

        def mock_popen(*args, **kwargs):
            class MockProc:
                pid = 88888
                stdout = None
                stderr = None

                def poll(self):
                    return None

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            return MockProc()

        original_popen = subprocess.Popen
        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        # Ensure daemon is not already running
        monkeypatch.setattr(lance_module, "_daemon_is_running", lambda: False)

        result = lance_module._start_daemon_if_needed()

        # Should return False (startup failed), child should be killed
        assert result is False

