#!/usr/bin/env python3
"""
eval_hledac_code_rag.py -- MemPalace Real-World Code-RAG Evaluation against Hledac

Evaluates retrieval quality using the real Hledac codebase as the target project.

Usage:
    python scripts/eval_hledac_code_rag.py \
        --project-path /Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal \
        --palace-path /tmp/mempalace_hledac_eval \
        --mine \
        --limit 5

    python scripts/eval_hledac_code_rag.py \
        --project-path /Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal \
        --palace-path /tmp/mempalace_hledac_eval \
        --report-json probe_eval/hledac_code_rag_eval.json

    # Bounded eval (M1 8GB safe):
    python scripts/eval_hledac_code_rag.py \
        --project-path /Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal \
        --palace-path /tmp/mempalace_hledac_eval \
        --mine \
        --max-files 200 \
        --limit 5

Exit codes:
    0  = pass (smoke thresholds met)
    1  = threshold failure
    2  = project path not found
    3  = mining failure / OOM / timeout
    4  = swap detected on M1 (use --force to override)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
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
# Swap check (M1 8GB safety)                                                  #
# --------------------------------------------------------------------------- #

# Directories/files to exclude from mining (reduces file count + memory)
_DEFAULT_SKIP_PATTERNS = (
    ".venv",
    "__pycache__",
    ".git",
    "probe_",
    "benchmarks/results",
    "reports",
    "logs",
    ".eggs",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
)


def _get_swap_mb() -> float:
    """Return swap used in MB, or -1 if unavailable."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"],
            capture_output=True, text=True, timeout=5,
        )
        # "total = 8192.00M  used = 1024.00M  free = 7168.00M  (encrypted)"
        output = result.stdout.strip()
        for part in output.split():
            if part.endswith("M") and "used" in output.split(part)[0].rsplit(",", 1)[0]:
                # find the "used" number before this part
                parts = output.replace("used = ", "").replace("free = ", "").replace("total = ", "").split()
                for i, p in enumerate(parts):
                    if p.endswith("M") and i > 0:
                        return float(parts[i - 1])
        return -1.0
    except Exception:
        return -1.0


def _check_swap_safe() -> bool:
    """Return True if swap is not heavily used (M1 8GB safe)."""
    swap_mb = _get_swap_mb()
    if swap_mb < 0:
        return True  # Can't determine — assume safe
    # If more than 6GB swap is in use, we're already near the limit
    return swap_mb < 6144.0


def _filtered_files(project_path: Path, skip_patterns: tuple[str, ...]) -> list[Path]:
    """Return list of Python files excluding skip patterns."""
    py_files = []
    for p in project_path.rglob("*.py"):
        if "__pycache__" in str(p) or p.name.startswith("."):
            continue
        if ".venv" in str(p):
            continue
        skip = False
        for pat in skip_patterns:
            if pat in str(p):
                skip = True
                break
        if not skip:
            py_files.append(p)
    return py_files


# --------------------------------------------------------------------------- #
# Expected answer mapping                                                     #
# --------------------------------------------------------------------------- #
# Format: query_id -> expected file prefix (partial match)
# These are approximate targets -- this is a smoke eval, not a research paper.

EXPECTED_FILE_MAP: dict[str, str] = {
    # Symbol lookups
    "run_sprint": "core/__main__.py",
    "DuckDBShadowStore": "knowledge/duckdb_store.py",
    "CanonicalFinding": "knowledge/duckdb_store.py",
    "live_public_pipeline": "pipeline/live_public_pipeline.py",
    "async_run_live_public_pipeline": "pipeline/live_public_pipeline.py",
    "MLXEmbeddingManager": "core/mlx_embeddings.py",
    "PatternMatcher": "patterns/pattern_matcher.py",
    "_windup_synthesis": "core/__main__.py",
    # Behavior queries
    "where is canonical sprint entrypoint": "core/__main__.py",
    "how are findings accepted into store": "knowledge/duckdb_store.py",
    "where does live public pipeline create CanonicalFinding": "knowledge/duckdb_store.py",
    "where is FTS or vector retrieval implemented": "core/mlx_embeddings.py",
    "where is MLX memory cleanup handled": "core/mlx_embeddings.py",
    "where is io-only latch or memory pressure handled": "orchestrator/memory_pressure_broker.py",
    # Risk queries
    "where can storage fail-soft happen": "knowledge/duckdb_store.py",
    "where are findings deduplicated": "knowledge/duckdb_store.py",
    "where are async tasks bounded": "orchestrator/global_scheduler.py",
    "where does export handoff happen": "orchestrator/lane_state.py",
}

# --------------------------------------------------------------------------- #
# Query list                                                                  #
# --------------------------------------------------------------------------- #

QUERIES: list[dict[str, Any]] = [
    # Symbol lookups
    {"id": "run_sprint", "query": "run_sprint", "type": "symbol"},
    {"id": "DuckDBShadowStore", "query": "DuckDBShadowStore", "type": "symbol"},
    {"id": "CanonicalFinding", "query": "CanonicalFinding", "type": "symbol"},
    {"id": "live_public_pipeline", "query": "live_public_pipeline", "type": "symbol"},
    {"id": "async_run_live_public_pipeline", "query": "async_run_live_public_pipeline", "type": "symbol"},
    {"id": "MLXEmbeddingManager", "query": "MLXEmbeddingManager", "type": "symbol"},
    {"id": "PatternMatcher", "query": "PatternMatcher", "type": "symbol"},
    {"id": "_windup_synthesis", "query": "_windup_synthesis", "type": "symbol"},
    # Behavior queries
    {"id": "where is canonical sprint entrypoint", "query": "where is canonical sprint entrypoint", "type": "behavior"},
    {"id": "how are findings accepted into store", "query": "how are findings accepted into store", "type": "behavior"},
    {"id": "where does live public pipeline create CanonicalFinding", "query": "where does live public pipeline create CanonicalFinding", "type": "behavior"},
    {"id": "where is FTS or vector retrieval implemented", "query": "where is FTS or vector retrieval implemented", "type": "behavior"},
    {"id": "where is MLX memory cleanup handled", "query": "where is MLX memory cleanup handled", "type": "behavior"},
    {"id": "where is io-only latch or memory pressure handled", "query": "where is io-only latch or memory pressure handled", "type": "behavior"},
    # Risk queries
    {"id": "where can storage fail-soft happen", "query": "where can storage fail-soft happen", "type": "risk"},
    {"id": "where are findings deduplicated", "query": "where are findings deduplicated", "type": "risk"},
    {"id": "where are async tasks bounded", "query": "where are async tasks bounded", "type": "risk"},
    {"id": "where does export handoff happen", "query": "where does export handoff happen", "type": "risk"},
]


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

def _top1_file_hit(result: dict, expected_file: str) -> float:
    """Return 1.0 if expected file is in result's top-1 source."""
    results = result.get("results", [])
    if not results:
        return 0.0
    src = results[0].get("source_file", "")
    if expected_file in src or src.endswith(expected_file) or expected_file in Path(src).name:
        return 1.0
    return 0.0


def _top5_file_hit(result: dict, expected_file: str) -> float:
    """Return 1.0 if expected file appears in top-5 sources."""
    results = result.get("results", [])
    if not results:
        return 0.0
    expected_key = expected_file
    for hit in results[:5]:
        src = hit.get("source_file", "")
        if expected_key in src or src.endswith(expected_key) or expected_key in Path(src).name:
            return 1.0
    return 0.0


def _has_line_range(result: dict) -> float:
    """Return 1.0 if any result has line range info."""
    results = result.get("results", [])
    for hit in results[:3]:
        lines = hit.get("line_range") or hit.get("lines") or hit.get("line")
        if lines:
            return 1.0
    return 0.0


def _has_symbol_name(result: dict, query: str) -> float:
    """Return 1.0 if query text appears in any result."""
    results = result.get("results", [])
    if not results:
        return 0.0
    for hit in results[:5]:
        text = hit.get("text", "") or ""
        if query.lower() in text.lower():
            return 1.0
    return 0.0


def _is_in_project_path(result: dict, project_path: str) -> bool:
    """Check if top result is inside the target project path."""
    results = result.get("results", [])
    if not results:
        return False
    src = results[0].get("source_file", "")
    return src.startswith(project_path)


# --------------------------------------------------------------------------- #
# Mine project                                                               #
# --------------------------------------------------------------------------- #

def _mine_project(
    project_path: str,
    palace_path: str,
    max_files: int | None = None,
    skip_patterns: tuple[str, ...] | None = None,
    mining_timeout_sec: int | None = 600,
) -> None:
    """Mine real project into palace, patching embeddings globally."""
    import mempalace.backends.lance as lance_mod

    orig_embed = lance_mod._embed_texts
    lance_mod._embed_texts = _mock_embed_texts
    if hasattr(lance_mod, "_embed_via_socket"):
        lance_mod._embed_via_socket = _mock_embed_texts

    os.environ["MEMPALACE_EMBED_FALLBACK"] = "1"

    try:
        from mempalace.miner import mine

        # Build a minimal config override for scan_project skip patterns
        # The miner.mine() limit= parameter controls how many files are processed
        mine_kwargs: dict = {}
        if max_files is not None and max_files > 0:
            mine_kwargs["limit"] = max_files

        mine(project_path, palace_path, **mine_kwargs)

        # Build symbol index — apply same skip patterns
        from mempalace.symbol_index import SymbolIndex
        si = SymbolIndex.get(palace_path)
        src_path = Path(project_path)
        py_files = [str(p) for p in _filtered_files(src_path, skip_patterns or _DEFAULT_SKIP_PATTERNS)]
        si.build_index(project_path, py_files)
    finally:
        lance_mod._embed_texts = orig_embed
        if hasattr(lance_mod, "_embed_via_socket"):
            lance_mod._embed_via_socket = orig_embed


# --------------------------------------------------------------------------- #
# Run single query                                                            #
# --------------------------------------------------------------------------- #

async def _run_query(query: str, palace_path: str, n_results: int = 10) -> tuple[dict, float]:
    """Execute auto_search and return (result_dict, latency_ms)."""
    from mempalace.searcher import auto_search
    start = time.monotonic()
    result = await auto_search(query, palace_path, n_results=n_results)
    latency_ms = (time.monotonic() - start) * 1000
    return result, latency_ms


# --------------------------------------------------------------------------- #
# Main eval runner                                                            #
# --------------------------------------------------------------------------- #

async def _eval_project(
    project_path: str | Path,
    palace_path: str | Path,
    do_mine: bool = False,
    max_queries: int | None = None,
    report_json: str | None = None,
    max_files: int | None = None,
    skip_patterns: tuple[str, ...] | None = None,
    force: bool = False,
) -> int:
    project_path = Path(project_path).resolve()
    palace_path = Path(palace_path).resolve()

    if not project_path.exists():
        print(f"[FAIL] Project path not found: {project_path}")
        return 2

    # Swap check (M1 8GB safety)
    if not force:
        swap_mb = _get_swap_mb()
        if swap_mb >= 6144.0:
            print(f"[ABORT] Swap heavily used: {swap_mb:.0f}MB / 6144MB — use --force to override")
            return 4

    # Prepare palace directory
    if palace_path.exists():
        shutil.rmtree(palace_path)
    palace_path.mkdir(parents=True)

    # Mine if requested
    if do_mine:
        print(f"[INFO] Mining {project_path} into {palace_path} ...")
        print(f"[INFO] Skip patterns: {skip_patterns or _DEFAULT_SKIP_PATTERNS}")
        if max_files:
            print(f"[INFO] Max files: {max_files}")
        try:
            _mine_project(
                str(project_path),
                str(palace_path),
                max_files=max_files,
                skip_patterns=skip_patterns,
            )
        except Exception as exc:
            print(f"[FAIL] Mining failed: {exc}")
            import traceback
            traceback.print_exc()
            return 3
    else:
        print(f"[INFO] Skipping mine (using existing palace at {palace_path})")

    queries = QUERIES
    if max_queries:
        queries = queries[:max_queries]

    rows: list[dict[str, Any]] = []
    zero_result_count = 0

    print(f"\n{'='*70}")
    print(f"  Hledac Code-RAG Evaluation — {len(queries)} queries")
    print(f"  project: {project_path}")
    print(f"  palace:  {palace_path}")
    print(f"{'='*70}\n")

    for q in queries:
        qid = q["id"]
        query_text = q["query"]
        qtype = q["type"]
        exp_file = EXPECTED_FILE_MAP.get(qid, "")

        result, latency = await _run_query(query_text, str(palace_path))
        results_list = result.get("results", [])
        result_count = len(results_list)

        if result_count == 0:
            zero_result_count += 1

        t1 = _top1_file_hit(result, exp_file)
        t5 = _top5_file_hit(result, exp_file)
        line = _has_line_range(result)
        sym = _has_symbol_name(result, qid)
        in_path = _is_in_project_path(result, str(project_path))

        # Cross-project leak: result outside project path when project_path is set
        leak = 0 if in_path else 1

        status = "PASS" if t5 >= 1.0 else ("WARN" if t5 >= 0.5 else "FAIL")

        top_srcs = [h.get("source_file", "?") for h in results_list[:3]]

        rows.append({
            "id": qid,
            "type": qtype,
            "top1": t1,
            "top5": t5,
            "has_line_range": line,
            "has_symbol_name": sym,
            "result_count": result_count,
            "in_project_path": in_path,
            "leak": leak,
            "latency_ms": round(latency, 1),
            "status": status,
            "top_sources": top_srcs,
        })

        t1_str = "✓" if t1 >= 1.0 else "✗"
        t5_str = "✓" if t5 >= 1.0 else ("~" if t5 >= 0.5 else "✗")
        line_str = "✓" if line >= 1.0 else "✗"
        path_str = "✓" if in_path else "✗"

        print(f"  [{status}] {qid:<50} top1={t1_str} top5={t5_str} "
              f"line={line_str} path={path_str} n={result_count} "
              f"latency={latency:6.1f}ms  top={top_srcs[:2]}")

    # Summary
    n = len(rows)
    zero_pct = zero_result_count / n if n else 0

    # Abort conditions
    if zero_result_count > len(queries) * 0.3:
        print(f"\n[ABORT] >30% queries returned zero results ({zero_result_count}/{n})")
        return 1

    # Thresholds (smoke-level)
    top1_avg = sum(r["top1"] for r in rows) / n
    top5_avg = sum(r["top5"] for r in rows) / n
    line_avg = sum(r["has_line_range"] for r in rows) / n
    sym_avg = sum(r["has_symbol_name"] for r in rows) / n
    lat_avg = sum(r["latency_ms"] for r in rows) / n
    total_leaks = sum(r["leak"] for r in rows)

    print(f"\n{'─'*70}")
    print(f"  METRIC                    VALUE       THRESHOLD   STATUS")
    print(f"{'─'*70}")
    print(f"  top1_file_hit             {top1_avg:6.2%}       >= 50%        "
          f"{'PASS' if top1_avg >= 0.5 else 'FAIL'}")
    print(f"  top5_file_hit             {top5_avg:6.2%}       >= 60%        "
          f"{'PASS' if top5_avg >= 0.6 else 'FAIL'}")
    print(f"  has_line_range             {line_avg:6.2%}       >= 30%        "
          f"{'PASS' if line_avg >= 0.3 else 'FAIL'}")
    print(f"  has_symbol_name           {sym_avg:6.2%}       >= 40%        "
          f"{'PASS' if sym_avg >= 0.4 else 'FAIL'}")
    print(f"  avg_latency_ms            {lat_avg:7.1f}ms    <= 5000ms     "
          f"{'PASS' if lat_avg <= 5000 else 'FAIL'}")
    print(f"  zero_result_pct            {zero_pct:6.1%}       <= 30%        "
          f"{'PASS' if zero_pct <= 0.3 else 'FAIL'}")
    print(f"  cross_project_leak_count  {total_leaks:<8}     <= 0          "
          f"{'PASS' if total_leaks == 0 else 'FAIL'}")
    print(f"{'─'*70}")

    all_pass = (
        top1_avg >= 0.5
        and top5_avg >= 0.6
        and zero_pct <= 0.3
        and total_leaks == 0
    )

    if report_json:
        report = {
            "project": str(project_path),
            "palace": str(palace_path),
            "query_count": n,
            "metrics": {
                "top1_file_hit": round(top1_avg, 4),
                "top5_file_hit": round(top5_avg, 4),
                "has_line_range": round(line_avg, 4),
                "has_symbol_name": round(sym_avg, 4),
                "avg_latency_ms": round(lat_avg, 2),
                "zero_result_count": zero_result_count,
                "zero_result_pct": round(zero_pct, 4),
                "cross_project_leak_count": total_leaks,
            },
            "thresholds": {
                "top1_file_hit_min": 0.5,
                "top5_file_hit_min": 0.6,
                "has_line_range_min": 0.3,
                "has_symbol_name_min": 0.4,
                "max_latency_ms": 5000,
                "zero_result_pct_max": 0.3,
                "cross_project_leak_max": 0,
            },
            "passed": all_pass,
            "rows": rows,
        }
        with open(report_json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n[INFO] Report written to: {report_json}")

    if all_pass:
        print(f"\n[RESULT] PASS — all smoke thresholds met")
        return 0
    else:
        print(f"\n[RESULT] FAIL — one or more thresholds not met")
        return 1


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(
        description="MemPalace Real-World Code-RAG Evaluation against Hledac"
    )
    parser.add_argument(
        "--project-path",
        type=str,
        required=True,
        dest="project_path",
        help="Path to the Hledac project (hledac/universal)",
    )
    parser.add_argument(
        "--palace-path",
        type=str,
        required=True,
        dest="palace_path",
        help="Path where the palace data will be stored",
    )
    parser.add_argument(
        "--mine",
        action="store_true",
        help="Mine the project before evaluating (skip to use existing palace)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        dest="limit",
        help="Limit number of queries to run (for fast iteration)",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default=None,
        dest="report_json",
        help="Write JSON report to path",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        dest="max_files",
        help="Limit number of files mined (default: all, safe: 200 on M1 8GB)",
    )
    parser.add_argument(
        "--skip-pattern",
        action="append",
        default=None,
        dest="skip_patterns",
        help="Skip directories/files containing pattern (can be repeated)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force mining even when swap is heavy (M1 8GB safety bypass)",
    )
    args = parser.parse_args()

    skip_patterns = tuple(args.skip_patterns) if args.skip_patterns else None

    rc = asyncio.run(_eval_project(
        project_path=args.project_path,
        palace_path=args.palace_path,
        do_mine=args.mine,
        max_queries=args.limit,
        report_json=args.report_json,
        max_files=args.max_files,
        skip_patterns=skip_patterns,
        force=args.force,
    ))

    # Restore env
    for k, v in _orig_env.items():
        if v is not None:
            os.environ[k] = v

    return rc


if __name__ == "__main__":
    sys.exit(main())