"""
Tests for MCP response contract: unified schema for all MemPalace tools.

Runs against:
- mempalace_search_code → normalized results + legacy chunks
- mempalace_project_context → both results/chunks or structured error
- mempalace_file_context → structured error on denial
- mempalace_auto_search → includes project_path_applied

No ChromaDB import.
"""
from __future__ import annotations

import pytest
import sys

# Ensure no ChromaDB in sys.modules (task rule)
def test_no_chromadb_import():
    """Verify no chromadb in sys.modules."""
    assert "chromadb" not in sys.modules, "ChromaDB must not be imported"


# ── Test response_contract helpers directly ───────────────────────────────────


from mempalace.server.response_contract import (
    TOOL_CONTRACT_VERSION,
    ok_response,
    error_response,
    no_palace_response,
    normalize_hit,
    normalize_results,
    make_search_response,
    make_symbol_response,
    make_file_context_response,
    make_project_context_response,
    file_context_error,
    _compute_repo_rel,
)


def test_tool_contract_version():
    assert TOOL_CONTRACT_VERSION == "1.0"


def test_ok_response_structure():
    resp = ok_response("test_tool", {"data": "value"}, meta={"src": "test"})
    assert resp["ok"] is True
    assert resp["tool_contract_version"] == "1.0"
    assert resp["tool"] == "test_tool"
    assert resp["data"] == "value"
    assert resp["_meta"]["src"] == "test"


def test_error_response_structure():
    resp = error_response("test_tool", "something went wrong", code="oops", meta={"x": 1})
    assert resp["ok"] is False
    assert resp["tool_contract_version"] == "1.0"
    assert resp["tool"] == "test_tool"
    assert resp["error"]["code"] == "oops"
    assert resp["error"]["message"] == "something went wrong"
    assert resp["_meta"]["x"] == 1


def test_no_palace_response_structure():
    resp = no_palace_response()
    assert resp["ok"] is False
    assert resp["tool_contract_version"] == "1.0"
    assert resp["error"]["code"] == "no_palace"
    assert "hint" in resp["error"]


def test_normalize_hit_with_all_fields():
    hit = {
        "id": "abc123",
        "text": "def foo(): pass",
        "source_file": "/proj/src/main.py",
        "language": "python",
        "line_start": 10,
        "line_end": 12,
        "symbol_name": "foo",
        "symbol_fqn": "mymodule.foo",
        "chunk_kind": "function",
        "score": 0.95,
        "retrieval_path": "vector",
    }
    normalized = normalize_hit(hit, project_path="/proj")
    assert normalized["id"] == "abc123"
    assert normalized["text"] == "def foo(): pass"
    assert normalized["doc"] == "def foo(): pass"  # legacy alias
    assert normalized["source_file"] == "/proj/src/main.py"
    assert normalized["repo_rel_path"] == "src/main.py"
    assert normalized["language"] == "python"
    assert normalized["line_start"] == 10
    assert normalized["line_end"] == 12
    assert normalized["symbol_name"] == "foo"
    assert normalized["symbol_fqn"] == "mymodule.foo"
    assert normalized["chunk_kind"] == "function"
    assert normalized["score"] == 0.95
    assert normalized["retrieval_path"] == "vector"
    assert normalized["project_path_applied"] == "/proj"


def test_normalize_hit_legacy_doc_fallback():
    hit = {"doc": "legacy content", "source_file": "/proj/foo.py"}
    normalized = normalize_hit(hit)
    assert normalized["text"] == "legacy content"
    assert normalized["doc"] == "legacy content"


def test_normalize_hit_no_text_no_doc():
    hit = {"id": "xyz"}
    normalized = normalize_hit(hit)
    assert normalized["text"] == ""
    assert normalized["doc"] == ""


def test_normalize_hit_preserves_original_keys():
    hit = {"text": "code", "custom_field": "kept", "unknown_key": 42}
    normalized = normalize_hit(hit)
    assert normalized["custom_field"] == "kept"
    assert normalized["unknown_key"] == 42


def test_normalize_results_empty():
    assert normalize_results([], "/proj") == []


def test_normalize_results_multiple():
    hits = [
        {"text": "a", "source_file": "/proj/a.py", "line_start": 1},
        {"text": "b", "source_file": "/proj/b.py", "line_start": 5},
    ]
    results = normalize_results(hits, "/proj", project_path_applied=True)
    assert len(results) == 2
    assert results[0]["repo_rel_path"] == "a.py"
    assert results[1]["repo_rel_path"] == "b.py"
    assert all(r["project_path_applied"] == "/proj" for r in results)


def test_make_search_response_includes_chunks():
    hits = [{"text": "fn", "source_file": "/proj/f.py", "line_start": 1}]
    resp = make_search_response("search_code", hits, "find fn", project_path="/proj")
    assert "results" in resp
    assert "chunks" in resp  # legacy alias
    assert resp["results"] == resp["chunks"]
    assert resp["ok"] is True
    assert resp["tool_contract_version"] == "1.0"
    assert resp["query"] == "find fn"
    assert resp["count"] == 1


def test_make_search_response_excludes_chunks_when_flagged():
    hits = [{"text": "fn", "source_file": "/proj/f.py", "line_start": 1}]
    resp = make_search_response("search_code", hits, "find fn", include_chunks=False)
    assert "chunks" not in resp
    assert "results" in resp


def test_make_search_response_filters_and_sources():
    hits = [{"text": "x", "source_file": "/proj/x.py", "line_start": 1}]
    resp = make_search_response(
        "search_code", hits, "x",
        filters={"language": "python"},
        sources={"vector": 5},
    )
    assert resp["filters"] == {"language": "python"}
    assert resp["sources"] == {"vector": 5}


def test_make_project_context_response():
    hits = [{"text": "code", "source_file": "/proj/src/a.py", "line_start": 1}]
    resp = make_project_context_response("/proj", hits, "query", "python", "mixed")
    assert resp["ok"] is True
    assert "results" in resp
    assert "chunks" in resp  # legacy
    assert resp["project_path"] == "/proj"
    assert resp["query"] == "query"
    assert resp["language"] == "python"
    assert resp["intent"] == "mixed"


def test_make_file_context_response():
    resp = make_file_context_response(
        "/proj/src/main.py", 500, 10, 50,
        [{"line_num": 10, "text": "line10"}, {"line_num": 11, "text": "line11"}],
        True, False, "/proj",
    )
    assert resp["ok"] is True
    assert resp["file_path"] == "/proj/src/main.py"
    assert resp["total_lines"] == 500
    assert resp["range_start"] == 10
    assert resp["range_end"] == 50
    assert len(resp["lines"]) == 2
    assert resp["has_more_before"] is True
    assert resp["has_more_after"] is False


def test_file_context_error():
    resp = file_context_error("path is outside allowed roots", code="access_denied")
    assert resp["ok"] is False
    assert resp["tool"] == "file_context"
    assert resp["error"]["code"] == "access_denied"
    assert "outside allowed roots" in resp["error"]["message"]


def test_file_context_error_default_code():
    resp = file_context_error("not found")
    assert resp["error"]["code"] == "file_context_error"


def test_compute_repo_rel():
    # Subpath → relative
    assert _compute_repo_rel("/proj/src/main.py", "/proj") == "src/main.py"
    # File at root
    assert _compute_repo_rel("/proj/README.md", "/proj") == "README.md"
    # Exact match (project root dir vs project root dir): empty relative path
    assert _compute_repo_rel("/proj", "/proj") == ""


def test_compute_repo_rel_file_to_file():
    # Single-file project: sf == pp, so "" is correct (no relative subpath).
    # The source file IS the project root; there's nothing more relative to report.
    assert _compute_repo_rel("/proj/setup.py", "/proj/setup.py") == ""


# ── Test MCP tool function signatures via import ───────────────────────────────


def test_response_contract_module_exists():
    from mempalace.server.response_contract import (
        ok_response, error_response, no_palace_response,
        normalize_hit, normalize_results, make_search_response,
        make_file_context_response, make_project_context_response,
        file_context_error, TOOL_CONTRACT_VERSION,
    )
    # Sanity: all are callable
    assert callable(ok_response)
    assert callable(error_response)
    assert callable(normalize_hit)


# ── Test _code_tools uses contract ────────────────────────────────────────────


def test_code_tools_imports_response_contract():
    """Verify _code_tools imports response_contract helpers."""
    from mempalace.server import _code_tools as ct
    # These should be imported at module level
    from mempalace.server.response_contract import (
        make_search_response,
        make_file_context_response,
        make_project_context_response,
        file_context_error,
        normalize_hit,
        no_palace_response,
    )
    # Confirm the module has access to them (via import)
    # We check the imports exist in the file
    import mempalace.server._code_tools as code_tools_module
    src = code_tools_module.__file__
    with open(src) as f:
        content = f.read()
    assert "from .response_contract import" in content


# ── Integration: simulate search_code response shape ──────────────────────────


def test_search_code_response_shape_from_normalize():
    """
    Simulate what mempalace_search_code returns when there are hits.

    The tool wraps code_search_async results through make_search_response,
    so we verify the contract holds for a typical hit list.
    """
    hits = [
        {
            "id": "drawer_001",
            "text": "def authenticate(user, passwd): ...",
            "source_file": "/myproject/auth.py",
            "language": "python",
            "line_start": 42,
            "line_end": 45,
            "symbol_name": "authenticate",
            "symbol_fqn": "auth.authenticate",
            "chunk_kind": "function",
            "score": 0.97,
            "retrieval_path": "vector",
            "project_path_applied": "/myproject",
        },
        {
            "id": "drawer_002",
            "text": "# TODO: add auth",
            "source_file": "/myproject/README.md",
            "language": "python",
            "line_start": 1,
            "line_end": 1,
            "symbol_name": "",
            "symbol_fqn": "",
            "chunk_kind": "comment",
            "score": 0.72,
            "retrieval_path": "fts5",
            "project_path_applied": "/myproject",
        },
    ]

    resp = make_search_response(
        "search_code", hits, "authenticate function",
        project_path="/myproject",
        project_path_applied=True,
        filters={"language": "python"},
        sources={"vector": 2},
    )

    assert resp["ok"] is True
    assert resp["tool"] == "search_code"
    assert resp["tool_contract_version"] == "1.0"
    assert "results" in resp
    assert "chunks" in resp  # legacy

    # Check first result fully normalized
    r0 = resp["results"][0]
    assert r0["id"] == "drawer_001"
    assert r0["text"] == hits[0]["text"]
    assert r0["doc"] == hits[0]["text"]  # legacy
    assert r0["source_file"] == "/myproject/auth.py"
    assert r0["repo_rel_path"] == "auth.py"
    assert r0["language"] == "python"
    assert r0["line_start"] == 42
    assert r0["line_end"] == 45
    assert r0["symbol_name"] == "authenticate"
    assert r0["symbol_fqn"] == "auth.authenticate"
    assert r0["chunk_kind"] == "function"
    assert r0["score"] == 0.97
    assert r0["retrieval_path"] == "vector"
    assert r0["project_path_applied"] == "/myproject"

    # Second result
    r1 = resp["results"][1]
    assert r1["id"] == "drawer_002"
    assert r1["score"] == 0.72
    assert r1["retrieval_path"] == "fts5"

    assert resp["count"] == 2
    assert resp["filters"] == {"language": "python"}
    assert resp["sources"] == {"vector": 2}


def test_project_context_deterministic_response_shape():
    """
    Simulate mempalace_project_context with no query (deterministic mode).
    Should return both results and chunks.
    """
    hits = [
        {"doc": "import os", "source_file": "/proj/main.py", "line_start": 1, "line_end": 1,
         "language": "python", "symbol_name": "", "chunk_kind": "import"},
        {"doc": "def main(): pass", "source_file": "/proj/main.py", "line_start": 3, "line_end": 4,
         "language": "python", "symbol_name": "main", "chunk_kind": "function"},
    ]
    resp = make_project_context_response(
        "/proj", hits, None, "python", "deterministic", project_path_applied=True,
    )
    assert resp["ok"] is True
    assert "results" in resp
    assert "chunks" in resp
    assert resp["project_path"] == "/proj"
    assert resp["query"] is None
    assert resp["intent"] == "deterministic"
    assert resp["count"] == 2


def test_file_context_error_response_shape():
    """
    Simulate mempalace_file_context denied response.
    """
    resp = file_context_error("path is outside allowed roots", code="access_denied")
    assert resp["ok"] is False
    assert resp["tool"] == "file_context"
    assert resp["error"]["code"] == "access_denied"
    assert "outside allowed roots" in resp["error"]["message"]
    assert resp["tool_contract_version"] == "1.0"


def test_auto_search_response_includes_project_path_applied():
    """
    Simulate mempalace_auto_search response — should include project_path_applied.
    """
    hits = [{"text": "cache logic", "source_file": "/proj/cache.py", "line_start": 10}]
    resp = make_search_response(
        "auto_search", hits, "cache invalidation",
        project_path="/proj",
        project_path_applied=True,
        filters={"complexity": "code"},
        sources={"code": 1},
    )
    assert resp["ok"] is True
    assert "project_path_applied" in resp["results"][0]
    assert resp["results"][0]["project_path_applied"] == "/proj"