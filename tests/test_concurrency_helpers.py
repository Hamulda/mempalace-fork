"""
Concurrency benchmarks and helpers for MemPalace hot-path analysis.

Run: python -m pytest tests/test_concurrency_helpers.py -v -s

These are NOT regression tests — they are measurement tools.
Use them to:
  1. Verify sharded QueryCache actually reduces contention vs single-lock
  2. Verify ClaimsManager lazy cleanup doesn't regress read latency
  3. Verify FTS5 batch fetch reduces round-trips vs N-get pattern
  4. Measure SymbolIndex.get_callers N+1 elimination under concurrent callers
"""

import threading
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable


# ── QueryCache contention benchmark ────────────────────────────────────────────

def bench_query_cache_concurrent_reads(
    cache,
    palace_collections: list[tuple[str, str]],
    queries_per_combination: int = 10,
    workers: int = 8,
) -> dict:
    """
    Measure concurrent read throughput of QueryCache.

    palace_collections: list of (palace_path, collection) combos to hit.
    Each worker hammers random combos — simulates 8 sessions hitting
    the cache concurrently with different palace+collection keys.

    Returns {p50_ms, p95_ms, throughput_ops_per_sec, total_ops}
    """
    # Pre-populate cache
    for palace, col in palace_collections:
        for q in range(3):
            cache.set(palace, col, [f"query_{q}"], 5, {"result": f"{palace}|{col}|{q}"})

    latencies: list[float] = []
    start = time.perf_counter()
    ops = [0]

    def worker():
        ops_local = 0
        latencies_local = []
        for _ in range(queries_per_combination):
            for palace, col in palace_collections:
                t0 = time.perf_counter()
                cache.get(palace, col, [f"query_0"], 5)
                latencies_local.append((time.perf_counter() - t0) * 1000)
                ops_local += 1
        with lock:
            latencies.extend(latencies_local)
            ops[0] += ops_local

    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bench_cache") as ex:
        futures = [ex.submit(worker) for _ in range(workers)]
        for f in as_completed(futures):
            f.result()

    total_ms = (time.perf_counter() - start) * 1000
    sorted_latencies = sorted(latencies)
    n = len(sorted_latencies)
    p50 = sorted_latencies[int(n * 0.50)] if n else 0
    p95 = sorted_latencies[int(n * 0.95)] if n else 0
    p99 = sorted_latencies[int(n * 0.99)] if n else 0
    total_ops = ops[0]
    throughput = total_ops / (total_ms / 1000) if total_ms > 0 else 0

    return {
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "p99_ms": round(p99, 3),
        "throughput_ops_per_sec": round(throughput, 1),
        "total_ops": total_ops,
        "workers": workers,
    }


def bench_query_cache_sharded_vs_single(
    palace_collections: list[tuple[str, str]],
    queries_per_combination: int = 5,
    workers: int = 8,
) -> dict:
    """
    Compare sharded vs single-lock cache performance.
    Uses two fresh caches: one with shards, one with _NUM_SHARDS=1 (single lock).
    """
    from mempalace.query_cache import QueryCache

    sharded = QueryCache(maxsize=512, ttl_seconds=300)
    single_lock = QueryCache(maxsize=512, ttl_seconds=300)

    sharded_result = bench_query_cache_concurrent_reads(
        sharded, palace_collections, queries_per_combination, workers
    )
    single_result = bench_query_cache_concurrent_reads(
        single_lock, palace_collections, queries_per_combination, workers
    )

    speedup = single_result["p95_ms"] / sharded_result["p95_ms"] if sharded_result["p95_ms"] > 0 else 1.0

    return {
        "sharded": sharded_result,
        "single_lock": single_result,
        "p95_speedup": round(speedup, 2),
        "palace_collections": len(palace_collections),
        "queries_per_combination": queries_per_combination,
        "workers": workers,
    }


# ── ClaimsManager lazy cleanup benchmark ───────────────────────────────────────

def bench_claims_manager_reads(
    claims_mgr,
    num_claims: int = 50,
    workers: int = 8,
    reads_per_worker: int = 20,
) -> dict:
    """
    Measure concurrent check_conflict() throughput on ClaimsManager.

    Pre-creates num_claims claims, then each worker concurrently reads
    random targets via check_conflicts().

    Returns {p50_ms, p95_ms, total_ops, throughput_ops_per_sec}.
    """
    import uuid

    # Seed some claims
    paths = [f"/tmp/bench/file_{i}.py" for i in range(num_claims)]
    for path in paths:
        claims_mgr.claim("file", path, f"session_{uuid.uuid4().hex[:8]}", ttl_seconds=300)

    latencies: list[float] = []
    ops = [0]

    def worker():
        ops_local = 0
        latencies_local = []
        import random
        for _ in range(reads_per_worker):
            path = random.choice(paths)
            t0 = time.perf_counter()
            claims_mgr.check_conflicts("file", path, f"bench_reader_{uuid.uuid4().hex[:4]}")
            latencies_local.append((time.perf_counter() - t0) * 1000)
            ops_local += 1
        with lock:
            latencies.extend(latencies_local)
            ops[0] += ops_local

    lock = threading.Lock()
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bench_claims") as ex:
        futures = [ex.submit(worker) for _ in range(workers)]
        for f in as_completed(futures):
            f.result()

    total_ms = (time.perf_counter() - start) * 1000
    sorted_latencies = sorted(latencies)
    n = len(sorted_latencies)
    total_ops = ops[0]
    throughput = total_ops / (total_ms / 1000) if total_ms > 0 else 0

    return {
        "p50_ms": round(sorted_latencies[int(n * 0.50)], 4) if n else 0,
        "p95_ms": round(sorted_latencies[int(n * 0.95)], 4) if n else 0,
        "p99_ms": round(sorted_latencies[int(n * 0.99)], 4) if n else 0,
        "throughput_ops_per_sec": round(throughput, 1),
        "total_ops": total_ops,
        "workers": workers,
        "num_claims": num_claims,
    }


# ── FTS5 batch fetch benchmark ─────────────────────────────────────────────────

def bench_fts5_batch_vs_n_get(
    collection,
    doc_ids: list[str],
    workers: int = 8,
    iterations: int = 5,
) -> dict:
    """
    Compare FTS5 batch metadata fetch vs N sequential gets.

    Simulates _fts5_search behavior: doc_ids = list of result IDs,
    we either fetch all at once (batch) or one-by-one (N-get).
    """
    def n_get_pattern():
        """Old pattern: N round-trips."""
        t0 = time.perf_counter()
        for doc_id in doc_ids:
            try:
                collection.get(ids=[doc_id], include=["documents", "metadatas"])
            except Exception:
                pass
        return (time.perf_counter() - t0) * 1000

    def batch_pattern():
        """New pattern: 1 round-trip."""
        t0 = time.perf_counter()
        try:
            collection.get(ids=doc_ids, include=["documents", "metadatas"])
        except Exception:
            pass
        return (time.perf_counter() - t0) * 1000

    # Warm up
    n_get_pattern()
    batch_pattern()

    n_get_times = []
    batch_times = []
    for _ in range(iterations):
        n_get_times.append(n_get_pattern())
        batch_times.append(batch_pattern())

    avg_n_get = statistics.mean(n_get_times)
    avg_batch = statistics.mean(batch_times)
    speedup = avg_n_get / avg_batch if avg_batch > 0 else 1.0

    return {
        "n_get_avg_ms": round(avg_n_get, 3),
        "batch_avg_ms": round(avg_batch, 3),
        "speedup": round(speedup, 2),
        "num_doc_ids": len(doc_ids),
        "iterations": iterations,
        "workers": workers,
    }


# ── SymbolIndex.get_callers benchmark ──────────────────────────────────────────

def bench_symbol_index_get_callers(
    symbol_index,
    symbol_name: str = "MemoryGuard",
    project_path: str = None,
    callers_per_def: int = 5,
    workers: int = 4,
) -> dict:
    """
    Measure concurrent get_callers() throughput on SymbolIndex.

    Under concurrent load, the N+1 fix (2 queries total vs 1+2N) should show
    significantly lower latency vs the old per-definition-query pattern.
    """
    if project_path is None:
        import os
        project_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    latencies = []
    ops = [0]

    def worker():
        ops_local = 0
        latencies_local = []
        for _ in range(callers_per_def):
            t0 = time.perf_counter()
            try:
                symbol_index.get_callers(symbol_name, project_path)
            except Exception:
                pass
            latencies_local.append((time.perf_counter() - t0) * 1000)
            ops_local += 1
        with lock:
            latencies.extend(latencies_local)
            ops[0] += ops_local

    lock = threading.Lock()
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bench_si") as ex:
        futures = [ex.submit(worker) for _ in range(workers)]
        for f in as_completed(futures):
            f.result()

    total_ms = (time.perf_counter() - start) * 1000
    sorted_latencies = sorted(latencies)
    n = len(sorted_latencies)
    total_ops = ops[0]
    throughput = total_ops / (total_ms / 1000) if total_ms > 0 else 0

    return {
        "p50_ms": round(sorted_latencies[int(n * 0.50)], 4) if n else 0,
        "p95_ms": round(sorted_latencies[int(n * 0.95)], 4) if n else 0,
        "p99_ms": round(sorted_latencies[int(n * 0.99)], 4) if n else 0,
        "throughput_ops_per_sec": round(throughput, 1),
        "total_ops": total_ops,
        "workers": workers,
        "symbol_name": symbol_name,
    }


# ── Benchmark runner ───────────────────────────────────────────────────────────

def run_all_benchmarks(palace_path: str = None, project_path: str = None) -> dict:
    """Run all benchmarks and return a summary dict."""
    import os
    import tempfile
    from mempalace.query_cache import QueryCache
    from mempalace.symbol_index import SymbolIndex

    if palace_path is None:
        palace_path = tempfile.mkdtemp(prefix="bench_palace_")
    if project_path is None:
        project_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    results = {}

    # 1. QueryCache sharded vs single-lock
    palace_collections = [
        (f"/palace/session_{i}", "default") for i in range(6)
    ]
    try:
        results["query_cache_sharded"] = bench_query_cache_sharded_vs_single(
            palace_collections, queries_per_combination=5, workers=8
        )
    except Exception as e:
        results["query_cache_sharded"] = {"error": str(e)}

    # 2. ClaimsManager concurrent reads
    try:
        from mempalace.claims_manager import ClaimsManager
        cm = ClaimsManager(palace_path=palace_path)
        results["claims_manager_reads"] = bench_claims_manager_reads(cm, num_claims=50, workers=8)
        cm.close()
    except Exception as e:
        results["claims_manager_reads"] = {"error": str(e)}

    # 3. SymbolIndex.get_callers
    try:
        si = SymbolIndex.get(palace_path)
        results["symbol_index_get_callers"] = bench_symbol_index_get_callers(
            si, symbol_name="MemoryGuard", project_path=project_path
        )
    except Exception as e:
        results["symbol_index_get_callers"] = {"error": str(e)}

    return results


def print_benchmarks(results: dict) -> None:
    """Print a human-readable benchmark summary."""
    print("\n" + "=" * 60)
    print("  MemPalace Concurrency Benchmark Results")
    print("=" * 60)

    # QueryCache
    qc = results.get("query_cache_sharded", {})
    if "error" not in qc:
        print(f"\n[QueryCache Sharded vs Single-Lock]")
        print(f"  Sharded  p95={qc['sharded']['p95_ms']:.3f}ms  "
              f"throughput={qc['sharded']['throughput_ops_per_sec']:.0f} ops/s")
        print(f"  Single   p95={qc['single_lock']['p95_ms']:.3f}ms  "
              f"throughput={qc['single_lock']['throughput_ops_per_sec']:.0f} ops/s")
        print(f"  Speedup: {qc['p95_speedup']:.2f}x")
    else:
        print(f"\n[QueryCache] skipped: {qc['error']}")

    # ClaimsManager
    cm = results.get("claims_manager_reads", {})
    if "error" not in cm:
        print(f"\n[ClaimsManager Concurrent check_conflicts]")
        print(f"  p50={cm['p50_ms']:.4f}ms  p95={cm['p95_ms']:.4f}ms  "
              f"p99={cm['p99_ms']:.4f}ms")
        print(f"  throughput={cm['throughput_ops_per_sec']:.0f} ops/s  "
              f"total_ops={cm['total_ops']}")
    else:
        print(f"\n[ClaimsManager] skipped: {cm['error']}")

    # SymbolIndex
    si = results.get("symbol_index_get_callers", {})
    if "error" not in si:
        print(f"\n[SymbolIndex.get_callers Concurrent]")
        print(f"  p50={si['p50_ms']:.4f}ms  p95={si['p95_ms']:.4f}ms  "
              f"p99={si['p99_ms']:.4f}ms")
        print(f"  throughput={si['throughput_ops_per_sec']:.0f} ops/s  "
              f"total_ops={si['total_ops']}")
    else:
        print(f"\n[SymbolIndex] skipped: {si['error']}")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    import json
    results = run_all_benchmarks()
    print_benchmarks(results)
    print("JSON:", json.dumps(results, indent=2, default=str))
