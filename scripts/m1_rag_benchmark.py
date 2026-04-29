#!/usr/bin/env python3
"""
MemPalace M1 RAG Benchmark — performance measurement for MacBook Air M1 8GB.

Modes:
  --fixture synthetic-small   Use built-in synthetic data (no external deps)
  --project-path <path>      Mine a real project directory
  --mine                     Run mining phase
  --queries                  Use built-in test queries

Metrics:
  - mine wall time, files processed, chunks inserted
  - RSS before/after, peak RSS
  - search p50/p95 latency
  - error count, zero-result query count
  - chromadb imported yes/no
  - swap warning yes/no

ABORT: if swap is detected during run, stop and report.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import tempfile
import shutil
import threading

# ── psutil (optional) ──────────────────────────────────────────────────────────
try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

# ── Memory helpers ─────────────────────────────────────────────────────────────
def get_rss_mb() -> float | None:
    if not _PSUTIL:
        return None
    try:
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return None

def check_swap() -> tuple[bool, float | None]:
    """Returns (swap_detected, swap_used_mb)."""
    if not _PSUTIL:
        return False, None
    try:
        swap = psutil.swap_memory()
        used = swap.used / 1024 / 1024
        return used > 0, used
    except Exception:
        return False, None

# ── Synthetic fixture ─────────────────────────────────────────────────────────
SYNTHETIC_FILES: list[tuple[str, str]] = [
    ("auth.py", "def authenticate(username, password):\n    return username == 'admin'\n"),
    ("db.py", "class Database:\n    def connect(self): pass\n"),
    ("utils.py", "def retry(n): return n\ndef parse(x): return x\n"),
    ("main.py", "import sys\nfrom auth import authenticate\nfrom db import Database\ndef main(): pass\n"),
    ("config.json", '{"db": "localhost", "port": 5432}\n'),
]

BUILTIN_QUERIES = [
    "authenticate function",
    "Database class",
    "retry utility",
    "main entry point",
    "config format",
]

def run_synthetic_mine(palace_path: str) -> dict:
    """Run mining on synthetic fixture. Returns stats dict."""
    from mempalace.config import MempalaceConfig
    from mempalace.miner import mine

    start = time.monotonic()
    rss_before = get_rss_mb()

    tmpdir = tempfile.mkdtemp(prefix="mempalace_synth_")
    try:
        # Write synthetic files
        for fname, content in SYNTHETIC_FILES:
            (pathlib.Path(tmpdir) / fname).write_text(content)

        # Mine
        mine(
            project_dir=tmpdir,
            palace_path=palace_path,
            wing_override="technical",
            agent="benchmark",
            limit=0,
            dry_run=False,
            respect_gitignore=False,
            include_ignored=[],
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    wall_s = time.monotonic() - start
    rss_after = get_rss_mb()

    # Count inserted chunks
    chunks = 0
    try:
        from mempalace.backends.lance import LanceBackend
        cfg = MempalaceConfig()
        backend = LanceBackend()
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
        chunks = col.count() if hasattr(col, 'count') else 0
    except Exception:
        pass

    return {
        "wall_time_s": round(wall_s, 3),
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "files_processed": len(SYNTHETIC_FILES),
        "chunks_inserted": chunks,
    }

def run_project_mine(palace_path: str, project_path: str) -> dict:
    """Run mining on a real project. Returns stats dict."""
    from mempalace.config import MempalaceConfig
    from mempalace.miner import mine

    start = time.monotonic()
    rss_before = get_rss_mb()

    try:
        mine(
            project_dir=project_path,
            palace_path=palace_path,
            wing_override=None or "",
            agent="benchmark",
            limit=0,
            dry_run=False,
            respect_gitignore=True,
            include_ignored=[],
        )
    except Exception as exc:
        return {
            "wall_time_s": round(time.monotonic() - start, 3),
            "rss_before_mb": rss_before,
            "rss_after_mb": get_rss_mb(),
            "files_processed": 0,
            "chunks_inserted": 0,
            "error": str(exc),
        }

    wall_s = time.monotonic() - start
    rss_after = get_rss_mb()

    chunks = 0
    try:
        from mempalace.backends.lance import LanceBackend
        cfg = MempalaceConfig()
        backend = LanceBackend()
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
        chunks = col.count() if hasattr(col, 'count') else 0
    except Exception:
        pass

    return {
        "wall_time_s": round(wall_s, 3),
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "files_processed": None,
        "chunks_inserted": chunks,
    }

def run_search_benchmark(palace_path: str, queries: list[str], concurrency: int) -> dict:
    """Run search benchmark with given queries and concurrency."""
    from mempalace.searcher import search_memories

    latencies: list[float] = []
    errors = 0
    zero_results = 0

    if concurrency == 1:
        for q in queries:
            swap_detected, _ = check_swap()
            if swap_detected:
                return {"aborted": True, "reason": "swap detected"}

            start = time.monotonic()
            try:
                result = search_memories(query=q, palace_path=palace_path, n_results=5)
                lat = time.monotonic() - start
                latencies.append(lat)
                if "error" in result:
                    errors += 1
                if not result.get("results"):
                    zero_results += 1
            except Exception:
                errors += 1
    else:
        # Thread pool for concurrency
        def worker(q: str, results: list):
            swap_detected, _ = check_swap()
            if swap_detected:
                results.append({"swap_abort": True})
                return
            start = time.monotonic()
            try:
                result = search_memories(query=q, palace_path=palace_path, n_results=5)
                lat = time.monotonic() - start
                results.append({"latency": lat, "error": "error" in result, "zero": not result.get("results")})
            except Exception:
                results.append({"error": True})

        import threading
        results: list = []
        lock = threading.Lock()
        batch_size = (len(queries) + concurrency - 1) // concurrency

        def batch_thread(qs: list[str]):
            local_results = []
            for q in qs:
                worker(q, local_results)
            with lock:
                results.extend(local_results)

        threads = []
        for i in range(concurrency):
            batch = queries[i*batch_size:(i+1)*batch_size]
            if batch:
                t = threading.Thread(target=batch_thread, args=(batch,))
                t.start()
                threads.append(t)

        for t in threads:
            t.join()

        # Check abort
        if any(r.get("swap_abort") for r in results):
            return {"aborted": True, "reason": "swap detected"}

        for r in results:
            if r.get("error"):
                errors += 1
            elif r.get("zero"):
                zero_results += 1
                latencies.append(r.get("latency", 0))
            else:
                latencies.append(r.get("latency", 0))

    latencies.sort()
    n = len(latencies)
    p50 = latencies[n // 2] if n > 0 else None
    p95 = latencies[int(n * 0.95)] if n > 0 else None

    return {
        "p50_latency_ms": round(p50 * 1000, 1) if p50 else None,
        "p95_latency_ms": round(p95 * 1000, 1) if p95 else None,
        "total_queries": len(queries),
        "error_count": errors,
        "zero_result_count": zero_results,
        "aborted": False,
    }

def run_concurrent_mining(palace_path: str, project_path: str, concurrency: int, duration_s: int):
    """Run concurrent mining sessions for a duration."""
    from mempalace.config import MempalaceConfig
    from mempalace.miner import mine
    import threading

    start = time.monotonic()
    results = []
    errors_list = []

    def worker(idx: int):
        try:
            mine(
                project_dir=project_path,
                palace_path=palace_path,
                wing_override=None or "",
                agent=f"benchmark-{idx}",
                limit=0,
                dry_run=False,
                respect_gitignore=True,
                include_ignored=[],
            )
            results.append({"success": True})
        except Exception as exc:
            errors_list.append(str(exc))
            results.append({"success": False, "error": str(exc)})

    threads = []
    for i in range(concurrency):
        t = threading.Thread(target=worker, args=(i,))
        t.start()
        threads.append(t)

    # Wait for duration
    for t in threads:
        t.join(timeout=duration_s)

    return {
        "concurrency": concurrency,
        "duration_s": round(time.monotonic() - start, 1),
        "sessions_started": concurrency,
        "errors": len(errors_list),
    }

def write_report(report: dict, output_dir: str) -> str:
    """Write JSON report to output_dir/benchmark_report.json."""
    import pathlib
    script_dir = pathlib.Path(__file__).parent.resolve()
    abs_dir = str(script_dir.parent / output_dir)
    os.makedirs(abs_dir, exist_ok=True)
    out_path = os.path.join(abs_dir, "benchmark_report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    return out_path

def print_report(report: dict) -> None:
    print("=" * 60)
    print("MemPalace M1 RAG Benchmark")
    print("=" * 60)

    if report.get("swap_warning"):
        print("⚠️  SWAP DETECTED — benchmark may not reflect real M1 performance")

    if report.get("mine"):
        m = report["mine"]
        print(f"\nMine phase:")
        print(f"  Wall time:   {m.get('wall_time_s', 'N/A')}s")
        print(f"  Files:       {m.get('files_processed', 'N/A')}")
        print(f"  Chunks:      {m.get('chunks_inserted', 'N/A')}")
        rss_before = m.get('rss_before_mb')
        rss_after = m.get('rss_after_mb')
        if rss_before and rss_after:
            print(f"  RSS delta:   {rss_after - rss_before:+.1f} MB")
        if m.get('error'):
            print(f"  Error:       {m['error']}")

    if report.get("search"):
        s = report["search"]
        print(f"\nSearch phase:")
        print(f"  p50 latency: {s.get('p50_latency_ms', 'N/A')} ms")
        print(f"  p95 latency: {s.get('p95_latency_ms', 'N/A')} ms")
        print(f"  Errors:      {s.get('error_count', 0)}")
        print(f"  Zero-result: {s.get('zero_result_count', 0)}")
        if s.get("aborted"):
            print(f"  ⚠️  ABORTED: {s.get('reason')}")

    print(f"\nChromadb imported: {report.get('chromadb_in_modules', False)}")
    print(f"Output: {report.get('output_path')}")
    print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="MemPalace M1 RAG Benchmark")
    parser.add_argument("--fixture", choices=["synthetic-small"], help="Use synthetic fixture")
    parser.add_argument("--project-path", help="Mine a real project")
    parser.add_argument("--palace-path", help="Palace path (default: ~/.mempalace/palace)")
    parser.add_argument("--mine", action="store_true", help="Run mining phase")
    parser.add_argument("--queries", action="store_true", help="Run query benchmark")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrency level (1,2,4,6)")
    parser.add_argument("--duration-seconds", type=int, default=60, help="Max duration for concurrent test")
    parser.add_argument("--rerank", action="store_true", help="Load reranker (default: off)")
    parser.add_argument("--output-dir", default="probe_runtime", help="Output directory for reports")
    args = parser.parse_args()

    # Default palace path
    palace_path = os.path.expanduser(args.palace_path or "~/.mempalace/palace")

    # Swap pre-check
    swap_detected, swap_mb = check_swap()
    swap_warning = swap_detected

    # Abort if swap already active
    if swap_detected:
        report = {
            "swap_warning": True,
            "swap_used_mb": swap_mb,
            "aborted_early": True,
            "reason": "swap already active at start",
        }
        print_report(report)
        out_path = write_report(report, args.output_dir)
        report["output_path"] = out_path
        sys.exit(1)

    # Build report
    report: dict = {
        "concurrency": args.concurrency,
        "duration_seconds": args.duration_seconds,
        "rerank_enabled": args.rerank,
        "chromadb_in_modules": "chromadb" in sys.modules,
        "swap_warning": False,
    }

    # NOTE: Reranker is NOT loaded by default. If --rerank is passed,
    # caller is responsible for triggering reranker load explicitly.
    # (No auto-warmup of BGE reranker-v2-m3 in this benchmark)

    rss_before = get_rss_mb()
    report["rss_before_mb"] = rss_before

    # Mine phase
    if args.mine:
        if args.fixture == "synthetic-small":
            mine_result = run_synthetic_mine(palace_path)
        elif args.project_path:
            mine_result = run_project_mine(palace_path, args.project_path)
        else:
            mine_result = run_synthetic_mine(palace_path)

        report["mine"] = mine_result

        # Post-mine swap check
        swap_detected, swap_mb = check_swap()
        if swap_detected:
            report["swap_warning"] = True
            report["aborted_phase"] = "search"
            print_report(report)
            write_report(report, args.output_dir)
            sys.exit(1)

    # Search phase
    if args.queries:
        search_result = run_search_benchmark(palace_path, BUILTIN_QUERIES, args.concurrency)
        report["search"] = search_result

        swap_detected, swap_mb = check_swap()
        if swap_detected:
            report["swap_warning"] = True

    rss_after = get_rss_mb()
    report["rss_after_mb"] = rss_after

    # Write report
    out_path = write_report(report, args.output_dir)
    report["output_path"] = out_path

    print_report(report)

if __name__ == "__main__":
    main()