#!/usr/bin/env python3
"""
MemPalace M1 Runtime Doctor — hardware/environment validation for MacBook Air M1 8GB.

Run: python scripts/m1_runtime_doctor.py [--json]

Reports Python version, memory stats, import status, palace info.
Must not trigger heavy model loads (reranker, sentence-transformers).
"""
from __future__ import annotations

import json
import os
import pathlib
import platform
import sys

# ── psutil (optional, used for memory stats) ───────────────────────────────────
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

# ── Standard library introspection ─────────────────────────────────────────────
PYTHON_VERSION = sys.version
PYTHON_EXECUTABLE = sys.executable
PLATFORM_SYSTEM = platform.system()
PLATFORM_RELEASE = platform.release()

# Process memory
process = psutil.Process() if _PSUTIL_AVAILABLE else None
PROC_RSS_MB: float | None = None
AVAILABLE_MEM_MB: float | None = None
SWAP_USED_MB: float | None = None
SWAP_TOTAL_MB: float | None = None

if _PSUTIL_AVAILABLE:
    try:
        mem = psutil.virtual_memory()
        AVAILABLE_MEM_MB = getattr(mem, 'available', None)
        if AVAILABLE_MEM_MB is not None:
            AVAILABLE_MEM_MB /= 1024 / 1024
        proc_mem = process.memory_info()
        PROC_RSS_MB = proc_mem.rss / 1024 / 1024

        swap = psutil.swap_memory()
        SWAP_USED_MB = swap.used / 1024 / 1024 if swap.used else None
        SWAP_TOTAL_MB = swap.total / 1024 / 1024 if swap.total else None
    except Exception:
        pass

# ── Import checks (lazy, no heavy model loads) ─────────────────────────────────
LANCEDB_VERSION: str | None = None
LANCEDB_IMPORT_ERROR: str | None = None
try:
    import lancedb
    LANCEDB_VERSION = getattr(lancedb, '__version__', 'unknown')
except ImportError as exc:
    LANCEDB_IMPORT_ERROR = str(exc)

PYARROW_VERSION: str | None = None
try:
    import pyarrow as pa
    PYARROW_VERSION = getattr(pa, '__version__', 'unknown')
except ImportError:
    pass

FASTEMCP_AVAILABLE: bool = False
FASTEMCP_VERSION: str | None = None
try:
    import fastmcp
    FASTEMCP_AVAILABLE = True
    FASTEMCP_VERSION = getattr(fastmcp, '__version__', 'unknown')
except ImportError:
    pass

FASTEMBED_AVAILABLE: bool = False
FASTEMBED_VERSION: str | None = None
try:
    import fastembed
    FASTEMBED_AVAILABLE = True
    FASTEMBED_VERSION = getattr(fastembed, '__version__', 'unknown')
except ImportError:
    pass

MLX_AVAILABLE: bool = False
MLX_VERSION: str | None = None
try:
    import mlx.core as mx
    MLX_AVAILABLE = True
    MLX_VERSION = getattr(mx, '__version__', 'unknown')
except ImportError:
    pass

SENTENCE_TRANSFORMERS_AVAILABLE: bool = False
try:
    import sentence_transformers
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    pass

CHROMADB_IN_MODULES: bool = 'chromadb' in sys.modules

# ── Palace info ─────────────────────────────────────────────────────────────────
DEFAULT_PALACE_PATH = os.path.expanduser("~/.mempalace/palace")
try:
    from mempalace.config import MempalaceConfig
    cfg = MempalaceConfig()
    PALACE_PATH = cfg.palace_path
    BACKEND = cfg.backend
    COLLECTION_NAME = cfg.collection_name
except Exception:
    PALACE_PATH = DEFAULT_PALACE_PATH
    BACKEND = "lance"
    COLLECTION_NAME = "mempalace_drawers"

# ── Lance collection count (lazy, no model load) ───────────────────────────────
LANCE_COLLECTION_COUNT: int | None = None
LANCE_FTS5_COUNT: int | None = None
SYMBOL_INDEX_STATS: dict | None = None

if LANCEDB_VERSION:
    try:
        from mempalace.backends.lance import LanceBackend
        backend = LanceBackend()
        if os.path.exists(PALACE_PATH):
            try:
                col = backend.get_collection(PALACE_PATH, COLLECTION_NAME, create=False)
                LANCE_COLLECTION_COUNT = col.count() if hasattr(col, 'count') else None
            except Exception:
                pass

            # FTS5 count via KeywordIndex
            try:
                from mempalace.lexical_index import KeywordIndex
                idx = KeywordIndex.get(PALACE_PATH)
                if hasattr(idx, '_conn') and idx._conn:
                    cur = idx._conn.execute("SELECT COUNT(*) FROM keyword_index")
                    LANCE_FTS5_COUNT = cur.fetchone()[0] if cur.fetchone() else None
            except Exception:
                pass

            # SymbolIndex stats
            try:
                from mempalace.symbol_index import SymbolIndex
                sym_idx = SymbolIndex.get(PALACE_PATH)
                SYMBOL_INDEX_STATS = sym_idx.stats() if hasattr(sym_idx, 'stats') else None
            except Exception:
                pass
    except Exception:
        pass


def get_report() -> dict:
    """Build and return the full diagnostic report."""
    report = {
        "python_version": PYTHON_VERSION,
        "python_executable": PYTHON_EXECUTABLE,
        "platform_system": PLATFORM_SYSTEM,
        "platform_release": PLATFORM_RELEASE,
        "proc_rss_mb": PROC_RSS_MB,
        "available_mem_mb": AVAILABLE_MEM_MB,
        "swap_used_mb": SWAP_USED_MB,
        "swap_total_mb": SWAP_TOTAL_MB,
        "swap_detected": SWAP_USED_MB is not None and SWAP_USED_MB > 0,
        "lancedb_version": LANCEDB_VERSION,
        "lancedb_import_error": LANCEDB_IMPORT_ERROR,
        "pyarrow_version": PYARROW_VERSION,
        "fastmcp_available": FASTEMCP_AVAILABLE,
        "fastmcp_version": FASTEMCP_VERSION,
        "fastembed_available": FASTEMBED_AVAILABLE,
        "fastembed_version": FASTEMBED_VERSION,
        "mlx_available": MLX_AVAILABLE,
        "mlx_version": MLX_VERSION,
        "sentence_transformers_available": SENTENCE_TRANSFORMERS_AVAILABLE,
        "chromadb_in_modules": CHROMADB_IN_MODULES,
        "default_backend": BACKEND,
        "palace_path": PALACE_PATH,
        "collection_name": COLLECTION_NAME,
        "lance_collection_count": LANCE_COLLECTION_COUNT,
        "fts5_count": LANCE_FTS5_COUNT,
        "symbol_index_stats": SYMBOL_INDEX_STATS,
    }
    return report


def print_report(report: dict) -> None:
    """Print human-readable report."""
    print("=" * 60)
    print("MemPalace M1 Runtime Doctor")
    print("=" * 60)
    print(f"Python:     {report['python_version']}")
    print(f"Executable: {report['python_executable']}")
    print(f"Platform:   {report['platform_system']} {report['platform_release']}")
    print()
    print("Memory:")
    print(f"  Process RSS:    {report['proc_rss_mb']:.1f} MB" if report['proc_rss_mb'] else "  Process RSS:    N/A")
    print(f"  Available:      {report['available_mem_mb']:.0f} MB" if report['available_mem_mb'] else "  Available:      N/A")
    if report['swap_detected']:
        print(f"  ⚠️  SWAP IN USE: {report['swap_used_mb']:.0f} MB / {report['swap_total_mb']:.0f} MB total")
    else:
        print(f"  Swap used:      0 MB (healthy)")
    print()
    print("Imports:")
    print(f"  LanceDB:   {report['lancedb_version'] or '❌ ' + (report['lancedb_import_error'] or 'not installed')}")
    print(f"  PyArrow:   {report['pyarrow_version'] or '❌ not installed'}")
    print(f"  FastMCP:   {'✅ ' + (report['fastmcp_version'] or '') if report['fastmcp_available'] else '❌ not installed'}")
    print(f"  FastEmbed: {'✅' if report['fastembed_available'] else '❌ not installed'}")
    print(f"  MLX:       {'✅' if report['mlx_available'] else '❌ not installed'}")
    print(f"  SentTrans: {'⚠️  available (NO auto-load)' if report['sentence_transformers_available'] else '❌ not installed'}")
    print(f"  ChromaDB:  {'❌ YES (should not be loaded)' if report['chromadb_in_modules'] else '✅ not in sys.modules'}")
    print()
    print("Palace:")
    print(f"  Backend:        {report['default_backend']}")
    print(f"  Palace path:    {report['palace_path']}")
    print(f"  Collection:     {report['collection_name']}")
    print(f"  Lance records:  {report['lance_collection_count']}")
    print(f"  FTS5 entries:   {report['fts5_count']}")
    si_stats = report['symbol_index_stats']
    if si_stats:
        print(f"  SymbolIndex:    {si_stats.get('total_symbols', '?')} symbols, {si_stats.get('total_files', '?')} files")
    else:
        print(f"  SymbolIndex:     N/A")
    print()
    if report['swap_detected']:
        print("⚠️  WARNING: Swap is active. This may degrade performance on M1 8GB.")
        print("   Stop non-essential processes before running MemPalace.")
    print("=" * 60)


def write_report_json(report: dict, output_dir: str) -> str:
    """Write JSON report to output_dir/doctor_report.json under the project root."""
    import os as _os
    script_dir = pathlib.Path(__file__).parent.resolve()
    abs_dir = str(script_dir.parent / output_dir)
    _os.makedirs(abs_dir, exist_ok=True)
    out_path = _os.path.join(abs_dir, "doctor_report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MemPalace M1 Runtime Doctor")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    args = parser.parse_args()

    report = get_report()

    if report.get("swap_detected"):
        # Write report before exiting — doctor_report.json goes to probe_runtime/
        out_path = write_report_json(report, "probe_runtime")
        report["output_path"] = out_path
        print(json.dumps(report, indent=2, default=str))
        sys.exit(1)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)