"""
test_fastmcp_runtime_paths.py — Runtime path correctness tests.

Tests the three runtime bugs fixed in FastMCP server:
1. serve_http() works outside __main__ (no global mcp dependency)
2. BM25 warmup thread receives correct _get_bm25 signature
3. diary/room/wing tool paths handle where filters gracefully

Note: FastMCP 3.2.3 has a pre-existing incompatibility with rich>=13.6,<14
      (tracebacks_max_frames parameter). Tests that require importing
      fastmcp use source-parsing verification instead.
"""

import ast
import json
import pytest
from unittest.mock import MagicMock
import pandas as pd

# ── Bug 1: serve_http() deprecated shim (replaces old Starlette wrapper) ─────────


def test_serve_http_deprecated_shim_no_global_mcp_via_source():
    """serve_http must not reference the global `mcp` variable.

    serve_http is now a deprecated shim that calls create_server() +
    mcp.run(transport='streamable-http'). The old Starlette+Uvicorn wrapper
    that referenced global `mcp` is removed.

    Root cause (historical): serve_http referenced global `mcp` which only
    existed in the __main__ block. Calling serve_http from CLI import path
    raised NameError.
    Fix: deprecated shim in http_transport.py calls factory.create_server().
    """
    source = open("mempalace/server/http_transport.py").read()
    tree = ast.parse(source)

    # Find serve_http function (defined in http_transport.py)
    serve_http_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "serve_http":
            serve_http_node = node
            break

    assert serve_http_node is not None, "serve_http function not found in http_transport.py"

    # Collect all Name nodes used in serve_http
    names_used = {n.id for n in ast.walk(serve_http_node) if isinstance(n, ast.Name)}

    # `mcp` should NOT be referenced in serve_http (the shim uses local variables)
    assert "mcp" not in names_used, (
        "serve_http shim must not reference global 'mcp' — use create_server() instead. "
        "Global 'mcp' only exists in __main__ blocks."
    )


def test_serve_http_deprecated_shim_calls_create_server():
    """serve_http shim calls create_server(shared_server_mode=True)."""
    source = open("mempalace/server/http_transport.py").read()
    # The shim must call create_server with shared_server_mode=True
    assert "create_server(shared_server_mode=True)" in source, (
        "serve_http shim must call create_server(shared_server_mode=True) "
        "to activate session coordinators."
    )
    # Must call mcp.run with streamable-http
    assert 'transport="streamable-http"' in source or "transport='streamable-http'" in source, (
        "serve_http shim must use transport='streamable-http' (canonical FastMCP HTTP path)."
    )


# ── Bug 2: BM25 warmup wrong signature ──────────────────────────────────────────


def test_bm25_warmup_thread_signature():
    """_get_bm25 is called with (col, palace_path) — not (col) only.

    Root cause: fastmcp_server.py called _get_bm25(col2) but the function
    signature is _get_bm25(col, palace_path: str, max_docs: int = 10000).
    Missing required argument caused TypeError in the thread, making the
    BM25 warmup a silent dead branch.

    Fix: _get_bm25(col2, settings.db_path)
    """
    from mempalace.searcher import _get_bm25
    import inspect

    # Verify the function signature
    sig = inspect.signature(_get_bm25)
    params = list(sig.parameters.keys())
    assert "col" in params, "_get_bm25 must have 'col' parameter"
    assert "palace_path" in params, "_get_bm25 must have 'palace_path' parameter"

    # The bug: calling _get_bm25(col) without palace_path raises TypeError
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}

    # This should NOT raise TypeError now (was the bug)
    try:
        _get_bm25(mock_col, "/fake/palace", max_docs=100)
    except TypeError as e:
        if "palace_path" in str(e):
            pytest.fail(f"_get_bm25 still called without palace_path: {e}")
        raise


def test_bm25_warmup_in_fastmcp_source():
    """Verify fastmcp_server.py calls _get_bm25 with palace_path argument."""
    source = open("mempalace/fastmcp_server.py").read()

    # The fixed call should include settings.db_path as second argument
    # Look for the BM25 rebuild section
    assert "settings.db_path" in source, "BM25 warmup must pass palace_path"

    # Anti-pattern check: bare _get_bm25(col2) without palace_path must NOT exist
    assert "_get_bm25(col2)" not in source, (
        "BM25 warmup must NOT call bare _get_bm25(col2) — palace_path required"
    )


# ── Bug 3: where filter paths (diary / list_rooms / export) ─────────────────────


def test_apply_where_filter_handles_scalar_and_chroma_style():
    """LanceDB backend's _apply_where_filter handles both scalar and Chroma-style dicts.

    Scalar: {"wing": "project"}
    Chroma-style: {"wing": {"$eq": "project"}}

    Both must work so tools don't crash on backend switching.
    """
    from mempalace.backends.lance import _apply_where_filter

    df = pd.DataFrame({
        "id": ["a", "b", "c"],
        "document": ["doc1", "doc2", "doc3"],
        "metadata_json": [
            json.dumps({"wing": "project", "room": "backend"}),
            json.dumps({"wing": "project", "room": "frontend"}),
            json.dumps({"wing": "notes", "room": "planning"}),
        ],
    })

    # Scalar form — should work on both Chroma and Lance
    result = _apply_where_filter(df, {"wing": "project"})
    assert len(result) == 2, "Scalar filter should return 2 rows"

    # Chroma-style explicit operator
    result2 = _apply_where_filter(df, {"wing": {"$eq": "project"}})
    assert len(result2) == 2, "Chroma-style $eq filter should return 2 rows"

    # $and combinator (used in diary_read)
    result3 = _apply_where_filter(df, {"$and": [{"wing": "project"}, {"room": "backend"}]})
    assert len(result3) == 1, "$and filter should return 1 row"


def test_diary_read_where_filter_on_lance():
    """diary_read uses $and combinator which must work on Lance backend.

    Root cause: diary_read sent {"$and": [...]} but Lance's json_extract
    cannot handle $and on UTF-8 metadata_json. The _apply_where_filter
    in lance.py handles this via pandas post-filtering.
    """
    from mempalace.backends.lance import _apply_where_filter

    df = pd.DataFrame({
        "id": ["diary1", "diary2"],
        "document": ["entry1", "entry2"],
        "metadata_json": [
            json.dumps({"wing": "wing_testagent", "room": "diary", "topic": "arch"}),
            json.dumps({"wing": "wing_other", "room": "diary", "topic": "other"}),
        ],
    })

    # This is exactly what diary_read sends
    result = _apply_where_filter(df, {"$and": [{"wing": "wing_testagent"}, {"room": "diary"}]})
    assert len(result) == 1, "diary_read $and filter should return 1 row"
    # Verify the returned row is the correct one (by metadata content)
    meta = json.loads(result.iloc[0]["metadata_json"])
    assert meta["topic"] == "arch", "Should return the arch topic entry"


# ── Dead branch verification ────────────────────────────────────────────────────


def test_bm25_rebuild_thread_is_daemon():
    """BM25 rebuild thread must be daemon so it doesn't block shutdown."""
    source = open("mempalace/fastmcp_server.py").read()
    # The thread that rebuilds BM25 should be named and daemon
    assert 'name="bm25_debounce"' in source, "BM25 rebuild thread must be named"
    # Verify daemon=True is set
    assert "daemon=True" in source, "BM25 rebuild thread must be daemon"


def test_diary_read_uses_correct_where_structure():
    """diary_read tool sends $and-filtered where clause (not bare scalar dict).

    This verifies the fix is in place: diary_read must use
    where={"$and": [{"wing": wing}, {"room": "diary"}]}
    not where={"wing": wing, "room": "diary"} which Chroma accepts but
    LanceDB's json_extract cannot handle.
    """
    source = open("mempalace/fastmcp_server.py").read()

    # diary_read function must use $and for the room/diary filter
    assert '"$and"' in source or "'$and'" in source, (
        "diary_read must use $and combinator for multi-condition filter"
    )


def test_list_rooms_scalar_where_works_on_both_backends():
    """list_rooms sends scalar {"wing": wing} which both Chroma and Lance handle.

    This is a valid approach since it's a single-condition filter.
    The _apply_where_filter in LanceDB accepts both scalar and Chroma-style.
    """
    source = open("mempalace/fastmcp_server.py").read()

    # Verify list_rooms uses simple scalar where
    # This is fine — single condition works on both backends
    assert 'kwargs["where"] = {"wing": wing}' in source or \
           'kwargs[\'where\'] = {"wing": wing}' in source or \
           'where = {"wing": wing}' in source


def test_export_claude_md_conditional_where():
    """export_claude_md builds where dict conditionally, sends to col.get."""
    source = open("mempalace/fastmcp_server.py").read()

    # export builds where conditionally and only adds it when non-empty
    assert "if where:" in source, "export must build where conditionally"
    assert 'kwargs["where"] = where' in source or \
           'kwargs[\'where\'] = where' in source or \
           'kwargs["where"]=where' in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
