#!/usr/bin/env python3
"""
diagnostics.py — Operational diagnostics and repair utilities for MemPalace.

Provides:
- validate_symbol_index(): check orphaned/missing files vs symbol index
- validate_keyword_index(): check FTS5 count vs LanceDB count for drift
- validate_runtime_state(): query cache size, daemon status, memory pressure
- rebuild_symbol_index(): clear + rebuild symbol index from source files
- rebuild_keyword_index(): clear + rebuild FTS5 index from LanceDB content
- validate_skills_registration(): verify all expected skill files exist

All validate_* functions are diagnostics-only (no writes).
All rebuild_* functions backup before destructive action.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import MempalaceConfig
from .memory_guard import MemoryGuard, MemoryPressure
from .symbol_index import SymbolIndex
from .lexical_index import KeywordIndex


# ── File discovery helpers ────────────────────────────────────────────────────

def _walk_project(project_path: str, respect_gitignore: bool = True) -> list[str]:
    """
    Walk project_path and return list of readable file paths.
    Mirrors the file-discovery logic in miner.py (GitignoreMatcher).
    """
    from .miner import (
        GitignoreMatcher,
        load_gitignore_matcher,
        is_gitignored,
        should_skip_dir,
        READABLE_EXTENSIONS,
        SKIP_FILENAMES,
        MAX_FILE_SIZE,
    )

    project_path = str(Path(project_path).expanduser().resolve())
    files: list[str] = []
    gitignore_cache: dict = {}

    for dirpath, dirnames, filenames in os.walk(project_path):
        dirpath_path = Path(dirpath)

        # Filter directory names in-place to prevent descending skipped dirs
        dirnames[:] = [
            d for d in dirnames
            if not should_skip_dir(d) and not d.startswith(".")
        ]

        # Load gitignore matcher for this directory
        matcher = load_gitignore_matcher(dirpath_path, gitignore_cache)

        for fname in filenames:
            if fname in SKIP_FILENAMES or fname.startswith("."):
                continue

            fpath = dirpath_path / fname

            # Check gitignore
            if matcher and is_gitignored(fpath, [matcher], is_dir=False):
                continue

            # Check extension
            if fpath.suffix.lower() not in READABLE_EXTENSIONS:
                continue

            # Check size
            try:
                if fpath.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            files.append(str(fpath))

    return files


# ── Diagnostics (read-only) ───────────────────────────────────────────────────

def validate_symbol_index(palace_path: str, project_path: str) -> dict:
    """
    Validate symbol index against actual files on disk.

    Checks:
    - Files in symbol_index but missing from disk (orphaned)
    - Files on disk but missing from symbol_index (not indexed)

    Diagnostics only — no writes.
    """
    palace_path = str(Path(palace_path).expanduser().resolve())
    project_path = str(Path(project_path).expanduser().resolve())

    result = {
        "orphaned_files": [],
        "missing_from_index": [],
        "stats": {},
    }

    # Get indexed files from symbol_index
    idx = SymbolIndex.get(palace_path)
    indexed_files: set[str] = set()
    with idx._lock:
        if idx._conn:
            try:
                cur = idx._conn.execute(
                    "SELECT DISTINCT file_path FROM symbol_index"
                )
                for row in cur.fetchall():
                    indexed_files.add(row[0])
            except Exception:
                pass

    # Check each indexed file exists on disk
    for fp in sorted(indexed_files):
        if not os.path.isfile(fp):
            result["orphaned_files"].append(fp)

    # Discover project files and check against index
    project_files = set(_walk_project(project_path, respect_gitignore=True))
    for fp in sorted(project_files):
        if fp not in indexed_files:
            result["missing_from_index"].append(fp)

    result["stats"] = idx.stats()
    result["stats"]["orphaned_count"] = len(result["orphaned_files"])
    result["stats"]["missing_count"] = len(result["missing_from_index"])

    return result


def validate_keyword_index(palace_path: str) -> dict:
    """
    Validate FTS5 keyword index against LanceDB collection.

    Checks:
    - FTS5 document count vs LanceDB count (mismatch = drift)
    - Sample check: random document_ids from FTS5 exist in LanceDB

    Diagnostics only.
    """
    palace_path = str(Path(palace_path).expanduser().resolve())

    result = {
        "fts5_count": 0,
        "lance_count": 0,
        "counts_match": False,
        "sample_check_passed": False,
        "sample_errors": [],
        "stats": {},
    }

    # Get FTS5 count
    ki = KeywordIndex.get(palace_path)
    result["fts5_count"] = ki.count()

    # Get LanceDB count
    cfg = MempalaceConfig()
    backend_type = cfg.backend
    if backend_type not in ("lance", "chroma"):
        backend_type = "lance"

    lance_count = 0
    try:
        from .backends import get_backend
        backend = get_backend(backend_type)
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
        lance_count = col.count()
    except Exception as e:
        result["stats"]["lance_error"] = str(e)

    result["lance_count"] = lance_count
    result["counts_match"] = result["fts5_count"] == result["lance_count"]

    # Sample check: grab document_ids from FTS5 and verify they exist in LanceDB
    if result["fts5_count"] > 0:
        try:
            conn = sqlite3.connect(ki.db_path, timeout=5.0)
            cur = conn.execute(
                "SELECT document_id FROM drawers_fts LIMIT 10"
            )
            sample_ids = [row[0] for row in cur.fetchall()]
            conn.close()

            if sample_ids and lance_count > 0:
                # Try to fetch sample ids from LanceDB
                try:
                    backend = get_backend(backend_type)
                    col = backend.get_collection(palace_path, cfg.collection_name, create=False)
                    sample_data = col.get(ids=sample_ids[:5], include=["documents"])
                    found_ids = sample_data.get("ids", [])
                    result["sample_check_passed"] = len(found_ids) == len(sample_ids)
                except Exception as e:
                    result["sample_errors"].append(str(e))
        except Exception as e:
            result["sample_errors"].append(str(e))

    result["stats"]["fts5_count"] = result["fts5_count"]
    result["stats"]["lance_count"] = result["lance_count"]

    return result


def validate_runtime_state(palace_path: str) -> dict:
    """
    Validate runtime state: query cache, MemoryGuard, daemon status.

    Returns: {
        "query_cache_size": N,
        "daemon_running": bool,
        "memory_pressure": str,
        "palace_initialized": bool,
    }

    Diagnostics only.
    """
    palace_path = str(Path(palace_path).expanduser().resolve())

    result = {
        "query_cache_size": 0,
        "daemon_running": False,
        "memory_pressure": "unknown",
        "palace_initialized": False,
    }

    # Query cache stats
    try:
        from .query_cache import get_query_cache
        cache = get_query_cache()
        result["query_cache_size"] = len(cache._cache)
    except Exception:
        pass

    # Memory pressure
    try:
        guard = MemoryGuard.get()
        result["memory_pressure"] = guard.pressure.value
    except Exception:
        pass

    # Daemon status
    try:
        from .backends.lance import _daemon_is_running
        result["daemon_running"] = _daemon_is_running()
    except Exception:
        pass

    # Palace initialized
    result["palace_initialized"] = os.path.isdir(palace_path)

    return result


def validate_skills_registration(skills_dir: str) -> dict:
    """
    Validate that all expected skill files are present and readable.

    Expected skills: init.md, status.md, mine.md, help.md, memory_protocol.md,
    handoff.md, conflict-check.md, bug-pattern-capture.md, search.md,
    before-edit.md, after-edit.md, takeover.md, repo-wakeup.md,
    decision-capture.md, symbol-search.md

    Diagnostics only.
    """
    skills_dir = str(Path(skills_dir).expanduser().resolve())

    EXPECTED = {
        "init.md",
        "status.md",
        "mine.md",
        "help.md",
        "memory_protocol.md",
        "handoff.md",
        "conflict-check.md",
        "bug-pattern-capture.md",
        "search.md",
        "before-edit.md",
        "after-edit.md",
        "takeover.md",
        "repo-wakeup.md",
        "decision-capture.md",
        "symbol-search.md",
    }

    result = {
        "missing": [],
        "empty": [],
        "duplicates": [],
        "total_expected": len(EXPECTED),
        "total_found": 0,
    }

    found: dict[str, int] = {}
    for fname in os.listdir(skills_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(skills_dir, fname)
        if os.path.isfile(fpath):
            found[fname] = found.get(fname, 0) + 1

    for expected in sorted(EXPECTED):
        if expected not in found:
            result["missing"].append(expected)
        else:
            result["total_found"] += 1
            fpath = os.path.join(skills_dir, expected)
            try:
                size = os.path.getsize(fpath)
                if size == 0:
                    result["empty"].append(expected)
            except OSError:
                result["empty"].append(expected)

    # Check for duplicates (same name appearing multiple times — unlikely but safe)
    for fname, count in found.items():
        if count > 1:
            result["duplicates"].append((fname, count))

    return result


# ── Repairs (destructive, with backup) ───────────────────────────────────────

def _timestamped_backup(db_path: str) -> Optional[str]:
    """Create a timestamped backup of a database file, return backup path or None."""
    if not os.path.exists(db_path):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_{ts}"
    try:
        shutil.copy2(db_path, backup_path)
        return backup_path
    except Exception:
        return None


def rebuild_symbol_index(palace_path: str, project_path: str) -> dict:
    """
    Clear and rebuild symbol_index from source files.

    Repair — destructive. Backs up current index first.
    Returns: {"symbols_indexed": N, "files_indexed": N}
    """
    palace_path = str(Path(palace_path).expanduser().resolve())
    project_path = str(Path(project_path).expanduser().resolve())

    result = {"symbols_indexed": 0, "files_indexed": 0, "backup_path": None}

    idx = SymbolIndex.get(palace_path)

    # Backup current index
    backup_path = _timestamped_backup(idx.db_path)
    result["backup_path"] = backup_path

    # Clear existing index
    idx.clear()

    # Walk project and build file list
    file_paths = _walk_project(project_path, respect_gitignore=True)

    # Build index
    idx.build_index(project_path, file_paths)

    # Collect stats
    stats = idx.stats()
    result["symbols_indexed"] = stats.get("total_symbols", 0)
    result["files_indexed"] = stats.get("total_files", 0)

    return result


def rebuild_keyword_index(palace_path: str) -> dict:
    """
    Clear and rebuild keyword_index from LanceDB drawer content.

    Reads all drawers from LanceDB with offset pagination, extracts text content,
    upserts into FTS5 index via bulk_upsert().

    Repair — destructive. Backs up current index first.
    Returns: {"documents_indexed": N}
    """
    palace_path = str(Path(palace_path).expanduser().resolve())

    result = {"documents_indexed": 0, "backup_path": None}

    ki = KeywordIndex.get(palace_path)

    # Backup current index
    backup_path = _timestamped_backup(ki.db_path)
    result["backup_path"] = backup_path

    # Clear existing index
    ki.clear()

    # Read drawers from LanceDB
    cfg = MempalaceConfig()
    backend_type = cfg.backend
    if backend_type not in ("lance", "chroma"):
        backend_type = "lance"

    BATCH = 5000
    offset = 0
    all_entries: list[dict] = []

    try:
        from .backends import get_backend
        backend = get_backend(backend_type)
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)

        while True:
            batch = col.get(
                limit=BATCH,
                offset=offset,
                include=["documents", "metadatas"],
            )
            batch_ids = batch.get("ids", [])
            if not batch_ids:
                break

            batch_docs = batch.get("documents", []) or []
            batch_metas = batch.get("metadatas", []) or []

            for doc_id, doc, meta in zip(batch_ids, batch_docs, batch_metas):
                entry = {
                    "document_id": doc_id,
                    "content": doc or "",
                    "wing": meta.get("wing", "") if meta else "",
                    "room": meta.get("room", "") if meta else "",
                    "language": meta.get("language", "") if meta else "",
                }
                all_entries.append(entry)

            offset += len(batch_ids)
            if len(batch_ids) < BATCH:
                break

    except Exception as e:
        result["error"] = str(e)
        return result

    # Bulk upsert into FTS5
    if all_entries:
        ki.bulk_upsert(all_entries)

    result["documents_indexed"] = ki.count()
    return result