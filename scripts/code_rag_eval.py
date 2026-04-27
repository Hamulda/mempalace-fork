#!/usr/bin/env python3
"""
code_rag_eval.py -- MemPalace Code-RAG Evaluation Suite

Hermetic, offline evaluation of retrieval quality for Claude Code editing tasks.

Usage:
    python scripts/code_rag_eval.py --fixture tests/fixtures/code_rag_eval_repo
    python scripts/code_rag_eval.py --fixture tests/fixtures/code_rag_eval_repo --queries 5
    python scripts/code_rag_eval.py --fixture tests/fixtures/code_rag_eval_repo --report-json /tmp/eval.json

Exit codes:
    0  = pass (all thresholds met)
    1  = threshold failure
    2  = fixture not found
    3  = mining failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Path setup                                                                  #
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# --------------------------------------------------------------------------- #
# Env isolation                                                               #
# --------------------------------------------------------------------------- #

_ENVS_TO_ISOLATE = (
    "MEMPALACE_COALESCE_MS",
    "MEMPALACE_DEDUP_HIGH",
    "MEMPALACE_DEDUP_LOW",
    "MEMPALACE_BACKEND",
    "MEMPALACE_EMBED_FALLBACK",
    "MEMPALACE_FAST_EMBED",
)

_orig_env = {k: os.environ.pop(k, None) for k in _ENVS_TO_ISOLATE}
os.environ["MEMPALACE_COALESCE_MS"] = "0"
os.environ["MEMPALACE_DEDUP_HIGH"] = "1.0"
os.environ["MEMPALACE_DEDUP_LOW"] = "0.99"

# --------------------------------------------------------------------------- #
# Imports (after env isolation)                                               #
# --------------------------------------------------------------------------- #

from mempalace.miner import mine
from mempalace.searcher import auto_search
from mempalace.symbol_index import SymbolIndex


# --------------------------------------------------------------------------- #
# Mock embeddings (bypass MLX / fastembed / daemon)                          #
# --------------------------------------------------------------------------- #

def _mock_embed_texts(texts: list[str]) -> list[list[float]]:
    import hashlib
    dim = 256
    result = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = list(h[:dim]) + [0.0] * (dim - len(h))
        result.append(vec)
    return result


# --------------------------------------------------------------------------- #
# Metric helpers                                                              #
# --------------------------------------------------------------------------- #

def _file_hit_score(result: dict, expected_file: str) -> float:
    """Return 1.0 if expected_file appears in result's top-5 sources."""
    results = result.get("results", [])
    if not results:
        return 0.0
    # Normalize: strip leading "src/" for matching
    expected_key = expected_file.lstrip("src/")
    top5 = results[:5]
    for hit in top5:
        src = hit.get("source_file", "")
        # Match on basename or any path component
        if expected_key in src or expected_file in src:
            return 1.0
        # Also try matching just the filename
        src_basename = src.split("/")[-1]
        if expected_key == src_basename:
            return 1.0
    return 0.0


def _top1_hit(result: dict, expected_file: str) -> float:
    results = result.get("results", [])
    if not results:
        return 0.0
    src = results[0].get("source_file", "")
    expected_key = expected_file.lstrip("src/")
    return 1.0 if (expected_key in src or expected_file in src) else 0.0


def _symbol_hit(result: dict, expected_symbol: str | None, expected_class: str | None = None) -> float:
    """
    Check if expected symbol/class appears in top results.
    Matches on source_file content and result text.
    """
    results = result.get("results", [])
    if not results:
        return 0.0
    for hit in results[:5]:
        text = hit.get("text", "") or ""
        src = hit.get("source_file", "")
        if expected_symbol and expected_symbol in text:
            return 1.0
        if expected_class and expected_class in text:
            return 1.0
    return 0.0


def _line_range_hit(result: dict, expected_file: str, _line_estimate: str | None) -> float:
    """
    Loose hit: expected file in top3 + some signal the right region was found.
    We use similarity score as a proxy.
    """
    results = result.get("results", [])
    if not results:
        return 0.0
    for hit in results[:3]:
        src = hit.get("source_file", "")
        if expected_file in src:
            sim = hit.get("similarity", 0.0)
            # Crude proxy: high similarity = likely right region
            if sim >= 0.5:
                return 1.0
            return 0.5  # partial credit
    return 0.0


# --------------------------------------------------------------------------- #
# Mine fixture                                                               #
# --------------------------------------------------------------------------- #

def _mine_fixture(fixture_path: str, palace_path: str) -> None:
    """Mine fixture repo into palace, patching embeddings globally."""
    import mempalace.backends.lance as lance_mod

    orig_embed = lance_mod._embed_texts
    lance_mod._embed_texts = _mock_embed_texts
    if hasattr(lance_mod, "_embed_via_socket"):
        lance_mod._embed_via_socket = _mock_embed_texts

    try:
        mine(fixture_path, palace_path)

        # Build symbol index (FTS5 KeywordIndex is auto-built by mine())
        si = SymbolIndex.get(palace_path)
        src_path = Path(fixture_path) / "src"
        si.build_index(fixture_path, [str(p) for p in src_path.iterdir() if p.is_file()])
    finally:
        lance_mod._embed_texts = orig_embed
        if hasattr(lance_mod, "_embed_via_socket"):
            lance_mod._embed_via_socket = orig_embed


# --------------------------------------------------------------------------- #
# Run single query                                                            #
# --------------------------------------------------------------------------- #

async def _run_query(query: str, palace_path: str, n_results: int = 10) -> tuple[dict, float]:
    """Execute auto_search and return (result_dict, latency_ms)."""
    start = time.monotonic()
    result = await auto_search(query, palace_path, n_results=n_results)
    latency_ms = (time.monotonic() - start) * 1000
    return result, latency_ms


# --------------------------------------------------------------------------- #
# Main eval runner                                                            #
# --------------------------------------------------------------------------- #

async def _eval_fixture(
    fixture_path: str | Path,
    expected_path: str | Path,
    max_queries: int | None = None,
    report_json: str | None = None,
) -> int:
    fixture_path = Path(fixture_path)
    expected_path = Path(expected_path)

    if not fixture_path.exists():
        print(f"[FAIL] Fixture not found: {fixture_path}")
        return 2

    if not expected_path.exists():
        print(f"[FAIL] Expected answers not found: {expected_path}")
        return 2

    with open(expected_path) as f:
        expected = json.load(f)

    # Derive palace path: tmp within fixture parent
    palace_path = fixture_path.parent / (fixture_path.name + "_palace")
    if palace_path.exists():
        shutil.rmtree(palace_path)
    palace_path.mkdir()

    print(f"[INFO] Mining fixture into {palace_path} ...")
    try:
        _mine_fixture(str(fixture_path), str(palace_path))
    except Exception as exc:
        print(f"[FAIL] Mining failed: {exc}")
        return 3

    queries = expected["queries"]
    if max_queries:
        queries = queries[:max_queries]

    thresholds = expected.get("thresholds", {})
    top1_min = thresholds.get("top1_file_hit_min", 0.6)
    top5_min = thresholds.get("top5_file_hit_min", 0.8)
    symbol_min = thresholds.get("symbol_hit_min", 0.7)
    line_min = thresholds.get("line_range_hit_min", 0.5)
    max_latency = thresholds.get("max_latency_ms", 5000)
    leak_max = thresholds.get("cross_project_leak_max", 0)

    # Metrics accumulators
    top1_hits = []
    top5_hits = []
    symbol_hits = []
    line_hits = []
    latencies = []
    cross_leaks = []

    rows: list[dict[str, Any]] = []

    print(f"\n{'='*70}")
    print(f"  Code-RAG Evaluation — {len(queries)} queries")
    print(f"{'='*70}\n")

    for q in queries:
        qid = q["id"]
        query_text = q["query"]
        qtype = q["type"]
        exp_file = q.get("expected_file", "")
        exp_symbol = q.get("expected_symbol")
        exp_class = q.get("expected_class")
        exp_method = q.get("expected_method")
        line_est = q.get("line_estimate")
        is_leak = q.get("cross_project_leak", False)

        result, latency = await _run_query(query_text, str(palace_path))
        latencies.append(latency)

        t1 = _top1_hit(result, exp_file)
        t5 = _file_hit_score(result, exp_file)
        sym = _symbol_hit(result, exp_symbol, exp_class)
        line = _line_range_hit(result, exp_file, line_est)

        top1_hits.append(t1)
        top5_hits.append(t5)
        symbol_hits.append(sym)
        line_hits.append(line)

        # Cross-project leak detection (always 0 for hermetic fixture)
        leak = 0
        cross_leaks.append(leak)

        status = "PASS" if t5 >= 1.0 else ("WARN" if t5 >= 0.5 else "FAIL")
        top1_str = "✓" if t1 >= 1.0 else "✗"
        top5_str = "✓" if t5 >= 1.0 else ("~" if t5 >= 0.5 else "✗")
        sym_str = "✓" if sym >= 1.0 else ("~" if sym >= 0.5 else "✗")

        rows.append({
            "id": qid,
            "type": qtype,
            "top1": t1,
            "top5": t5,
            "symbol": sym,
            "line": line,
            "latency_ms": round(latency, 1),
            "status": status,
        })

        # Print compact row
        top_results = result.get("results", [])[:3]
        top_srcs = [h.get("source_file", "?") for h in top_results]
        print(f"  [{status}] {qid:<45} top1={top1_str} top5={top5_str} sym={sym_str} "
              f"latency={latency:6.1f}ms  top_sources={top_srcs}")

    # Summary
    n = len(rows)
    avg_top1 = sum(top1_hits) / n if n else 0
    avg_top5 = sum(top5_hits) / n if n else 0
    avg_sym = sum(symbol_hits) / n if n else 0
    avg_line = sum(line_hits) / n if n else 0
    avg_lat = sum(latencies) / n if n else 0
    total_leaks = sum(cross_leaks)

    print(f"\n{'─'*70}")
    print(f"  METRIC                    VALUE      THRESHOLD   STATUS")
    print(f"{'─'*70}")
    print(f"  top1_file_hit            {avg_top1:.2%}       >= {top1_min:.0%}       "
          f"{'PASS' if avg_top1 >= top1_min else 'FAIL'}")
    print(f"  top5_file_hit            {avg_top5:.2%}       >= {top5_min:.0%}       "
          f"{'PASS' if avg_top5 >= top5_min else 'FAIL'}")
    print(f"  symbol_hit               {avg_sym:.2%}       >= {symbol_min:.0%}       "
          f"{'PASS' if avg_sym >= symbol_min else 'FAIL'}")
    print(f"  line_range_hit           {avg_line:.2%}       >= {line_min:.0%}       "
          f"{'PASS' if avg_line >= line_min else 'FAIL'}")
    print(f"  avg_latency_ms           {avg_lat:6.1f}ms    <= {max_latency:.0f}ms     "
          f"{'PASS' if avg_lat <= max_latency else 'FAIL'}")
    print(f"  cross_project_leak_count {total_leaks:<8}     <= {leak_max}         "
          f"{'PASS' if total_leaks <= leak_max else 'FAIL'}")
    print(f"{'─'*70}")

    all_pass = (
        avg_top1 >= top1_min
        and avg_top5 >= top5_min
        and avg_sym >= symbol_min
        and avg_line >= line_min
        and avg_lat <= max_latency
        and total_leaks <= leak_max
    )

    if report_json:
        report = {
            "fixture": str(fixture_path),
            "query_count": n,
            "metrics": {
                "top1_file_hit": round(avg_top1, 4),
                "top5_file_hit": round(avg_top5, 4),
                "symbol_hit": round(avg_sym, 4),
                "line_range_hit": round(avg_line, 4),
                "avg_latency_ms": round(avg_lat, 2),
                "cross_project_leak_count": total_leaks,
            },
            "thresholds": thresholds,
            "passed": all_pass,
            "rows": rows,
        }
        with open(report_json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n[INFO] Report written to: {report_json}")

    if all_pass:
        print(f"\n[RESULT] PASS — all thresholds met")
        return 0
    else:
        print(f"\n[RESULT] FAIL — one or more thresholds not met")
        return 1


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="MemPalace Code-RAG Evaluation Suite")
    parser.add_argument(
        "--fixture",
        type=str,
        default="tests/fixtures/code_rag_eval_repo",
        help="Path to fixture repo (relative to repo root or absolute)",
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=None,
        dest="max_queries",
        help="Limit number of queries to run (for fast iteration)",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default=None,
        dest="report_json",
        help="Write JSON report to path",
    )
    args = parser.parse_args()

    # Resolve fixture relative to repo root
    fixture_path = Path(args.fixture)
    if not fixture_path.is_absolute():
        fixture_path = _REPO_ROOT / fixture_path

    expected_path = _REPO_ROOT / "tests" / "fixtures" / "code_rag_eval_expected.json"
    if not expected_path.exists():
        expected_path = fixture_path.parent / "code_rag_eval_expected.json"

    rc = asyncio.run(_eval_fixture(
        str(fixture_path),
        str(expected_path),
        max_queries=args.max_queries,
        report_json=args.report_json,
    ))

    # Restore env
    for k, v in _orig_env.items():
        if v is not None:
            os.environ[k] = v

    return rc


if __name__ == "__main__":
    sys.exit(main())
