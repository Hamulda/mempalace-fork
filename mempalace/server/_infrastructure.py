"""
Non-blocking WAL write and status cache — shared runtime helpers.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import json
import os
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("mempalace_mcp")

# WAL async executor — offloads file I/O from async tool handlers
wal_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mp_wal")

# Background work executor — bounded, prevents thread storm on M1/8GB
bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mp_bg")

# Status cache — wings/rooms aggregation cached for 60s
status_cache: dict = {"data": None, "ts": 0.0}
STATUS_CACHE_TTL: float = 60.0


# ─── WAL ────────────────────────────────────────────────────────────────────

def get_wal_path(wal_dir: str | None = None) -> Path:
    """Return WAL file path, creating directory if needed."""
    wal_path = Path(wal_dir or os.path.expanduser("~/.mempalace/wal"))
    wal_path.mkdir(parents=True, exist_ok=True)
    try:
        wal_path.chmod(0o700)
    except (OSError, NotImplementedError):
        pass
    return wal_path / "write_log.jsonl"


def wal_log(operation: str, params: dict, result: dict = None, wal_file: Path | None = None):
    """Append a write operation to the write-ahead log."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation,
        "params": params,
        "result": result,
    }
    wal_path = wal_file or get_wal_path()
    try:
        with open(wal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        try:
            wal_path.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
    except Exception as e:
        logger.error("WAL write failed: %s", e)


def wal_log_async(operation: str, params: dict, result: dict = None, wal_file: Path | None = None):
    """Non-blocking WAL write — offloads file I/O from async tool handlers."""
    wal_executor.submit(wal_log, operation, params, result, wal_file)


# ─── Status cache ────────────────────────────────────────────────────────────

def invalidate_status_cache():
    status_cache["data"] = None
    status_cache["ts"] = 0.0
