"""
Tests for LanceDB backend contract compliance.

Run: pytest tests/test_lance_backend_contract.py -v
"""

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed — run: pip install 'mempalace[lance]'")

import os
from mempalace.backends.lance import LanceBackend, _where_to_sql, _apply_where_filter


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
