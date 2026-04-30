"""
Hardening tests for MCP response contract — canonical fields always win.

Cases verified:
* raw hit with text=None but doc="abc" returns text="abc"
* raw hit with score=None but similarity=0.8 returns score=0.8
* raw project_path_applied does not override supplied project_path
* legacy keys remain present
* no_palace_response has contract version and structured error
"""
from __future__ import annotations

import pytest
import sys

from mempalace.server.response_contract import (
    TOOL_CONTRACT_VERSION,
    no_palace_response,
    normalize_hit,
    normalize_results,
    _compute_repo_rel,
)


# ── no_palace_response ────────────────────────────────────────────────────────


def test_no_palace_response_has_contract_version():
    resp = no_palace_response()
    assert "tool_contract_version" in resp
    assert resp["tool_contract_version"] == TOOL_CONTRACT_VERSION


def test_no_palace_response_has_structured_error():
    resp = no_palace_response()
    assert resp["ok"] is False
    assert "error" in resp
    assert "code" in resp["error"]
    assert "message" in resp["error"]
    assert resp["error"]["code"] == "no_palace"


def test_no_palace_response_tool_field_present():
    resp = no_palace_response()
    assert "tool" in resp


def test_no_palace_response_tool_defaults_to_unknown():
    resp = no_palace_response()
    assert resp["tool"] == "unknown"


def test_no_palace_response_tool_accepts_value():
    resp = no_palace_response(tool="search_code")
    assert resp["tool"] == "search_code"


def test_no_palace_response_has_hint():
    resp = no_palace_response()
    assert "hint" in resp["error"]


# ── normalize_hit: canonical wins ────────────────────────────────────────────


def test_text_none_but_doc_present_uses_doc():
    """text=None in raw hit, doc='abc' → canonical text='abc'."""
    hit = {"text": None, "doc": "abc", "id": "x"}
    result = normalize_hit(hit)
    assert result["text"] == "abc"
    assert result["doc"] == "abc"


def test_text_absent_but_doc_present_uses_doc():
    """text absent in raw hit, doc='abc' → canonical text='abc'."""
    hit = {"doc": "abc", "id": "x"}
    result = normalize_hit(hit)
    assert result["text"] == "abc"
    assert result["doc"] == "abc"


def test_score_none_but_similarity_present_uses_similarity():
    """score=None in raw hit, similarity=0.8 → canonical score=0.8."""
    hit = {"score": None, "similarity": 0.8, "id": "x"}
    result = normalize_hit(hit)
    assert result["score"] == 0.8


def test_score_absent_but_rrf_score_present_uses_rrf():
    """score absent in raw hit, rrf_score=0.5 → canonical score=0.5."""
    hit = {"rrf_score": 0.5, "id": "x"}
    result = normalize_hit(hit)
    assert result["score"] == 0.5


def test_score_none_similarity_none_rrf_present():
    """All None → score falls back to 0.0."""
    hit = {"score": None, "similarity": None, "rrf_score": None, "id": "x"}
    result = normalize_hit(hit)
    assert result["score"] == 0.0


def test_supplied_project_path_wins_over_raw_project_path_applied():
    """Supplied project_path='//proj' overrides raw project_path_applied='//other'."""
    hit = {"id": "x", "project_path_applied": "/other"}
    result = normalize_hit(hit, project_path="/proj")
    assert result["project_path_applied"] == "/proj"


def test_no_project_path_uses_raw_project_path_applied():
    """No project_path supplied → fall back to hit's project_path_applied."""
    hit = {"id": "x", "project_path_applied": "/fallback"}
    result = normalize_hit(hit)
    assert result["project_path_applied"] == "/fallback"


# ── normalize_hit: legacy keys preserved ──────────────────────────────────────


def test_legacy_drawer_id_preserved():
    hit = {"drawer_id": "drawer-1", "id": None}
    result = normalize_hit(hit)
    assert result["id"] == "drawer-1"
    assert "drawer_id" in result  # raw key kept


def test_legacy_file_path_preserved():
    hit = {"file_path": "/proj/foo.py"}
    result = normalize_hit(hit)
    assert result["source_file"] == "/proj/foo.py"
    assert "file_path" in result  # raw key kept


def test_legacy_fqn_preserved():
    hit = {"fqn": "module.func", "symbol_fqn": None}
    result = normalize_hit(hit)
    assert result["symbol_fqn"] == "module.func"
    assert "fqn" in result  # raw key kept


def test_legacy_kind_preserved():
    hit = {"kind": "method", "chunk_kind": None}
    result = normalize_hit(hit)
    assert result["chunk_kind"] == "method"
    assert "kind" in result  # raw key kept


def test_legacy_lineno_preserved():
    hit = {"lineno": 42, "line_start": None}
    result = normalize_hit(hit)
    assert result["line_start"] == 42
    assert "lineno" in result  # raw key kept


def test_legacy_content_preserved():
    hit = {"content": "some content", "text": None, "doc": None}
    result = normalize_hit(hit)
    assert result["text"] == "some content"
    assert "content" in result  # raw key kept


def test_custom_arbitrary_keys_preserved():
    hit = {"text": "x", "custom_field": "kept", "another": 123}
    result = normalize_hit(hit)
    assert result["custom_field"] == "kept"
    assert result["another"] == 123


# ── normalize_hit: canonical id resolution ───────────────────────────────────


def test_id_from_drawer_id_when_id_absent():
    hit = {"drawer_id": "drawer-2"}
    result = normalize_hit(hit)
    assert result["id"] == "drawer-2"


def test_id_from_id_takes_precedence():
    hit = {"id": "explicit-id", "drawer_id": "drawer-3"}
    result = normalize_hit(hit)
    assert result["id"] == "explicit-id"


# ── normalize_results: integration ───────────────────────────────────────────


def test_normalize_results_preserves_all_hits():
    hits = [
        {"id": "a", "text": None, "doc": "doc-a", "score": None, "similarity": 0.9},
        {"id": "b", "text": None, "doc": "doc-b", "score": 0.5, "similarity": 0.1},
    ]
    results = normalize_results(hits)
    assert len(results) == 2
    assert results[0]["text"] == "doc-a"
    assert results[0]["score"] == 0.9
    assert results[1]["text"] == "doc-b"
    assert results[1]["score"] == 0.5


def test_normalize_results_empty():
    assert normalize_results([]) == []


# ── _compute_repo_rel: no duplicate return ──────────────────────────────────


def test_compute_repo_rel_returns_correctly():
    assert _compute_repo_rel("/proj/src/main.py", "/proj") == "src/main.py"
    assert _compute_repo_rel("/proj/README.md", "/proj") == "README.md"
    assert _compute_repo_rel("/proj", "/proj") == ""
    # Only one return statement — no duplicate
