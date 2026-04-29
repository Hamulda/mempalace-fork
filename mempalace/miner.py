#!/usr/bin/env python3
"""
miner.py — Files everything into the palace.

Reads mempalace.yaml from the project directory to know the wing + rooms.
Routes each file to the right room based on content.
Stores verbatim chunks as drawers. No summaries. Ever.

Storage: LanceDB-only via get_backend(config.backend).
"""

import logging
import os
import sys
import hashlib
import fnmatch
import time
import json as _json
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from .config import MempalaceConfig
from .backends import get_backend
from .palace import SKIP_DIRS
from .symbol_index import SymbolIndex
from .mining_manifest import MiningManifest, _quick_hash

logger = logging.getLogger(__name__)

# =============================================================================
# MINE PROFILING
# =============================================================================

class MineStats:
    """
    Low-overhead profiler for mempalace mine.

    Enabled via MEMPALACE_MINE_PROFILE=1.
    Writes JSON report to MEMPALACE_MINE_PROFILE_JSON if set.
    """

    PROGRESS_EVERY = 25  # print summary every N files

    def __init__(self):
        self.enabled = os.environ.get("MEMPALACE_MINE_PROFILE", "0") == "1"
        self.total_files = 0
        self.processed_files = 0
        self.skipped_files = 0
        self.error_files = 0
        self.total_drawers_added = 0

        # Phase totals (seconds)
        self._phase_totals = {
            "read_file_s": 0.0,
            "detect_room_s": 0.0,
            "chunk_s": 0.0,
            "revision_existing_get_s": 0.0,
            "prepare_metadata_s": 0.0,
            "collection_upsert_s": 0.0,
            "tombstone_upsert_s": 0.0,
            "total_s": 0.0,
        }

        # Per-file records
        self._file_records: list[dict] = []

        # Error list
        self._errors: list[dict] = []

        # Top-N tracking
        self._slowest_files: list[tuple[float, dict]] = []  # (total_s, record)
        self._largest_chunk_files: list[tuple[int, dict]] = []  # (chunk_count, record)

        self._last_progress_time = time.monotonic()
        self._start_wall = self._last_progress_time

    def record_file(self, record: dict) -> None:
        """Called after each process_file completes (or errors)."""
        if not self.enabled:
            return

        self.total_files += 1

        if record["status"] == "error":
            self.error_files += 1
            if len(self._errors) < 50:
                self._errors.append({
                    "source_file": record.get("source_file", "unknown"),
                    "phase": record.get("phase", ""),
                    "error": str(record.get("error", ""))[:200],
                })
            self._update_topN(record, also_errors=False)
            return

        if record["status"] == "skipped":
            self.skipped_files += 1
            return

        # success
        self.processed_files += 1
        self.total_drawers_added += record.get("chunk_count", 0)

        # Accumulate phase totals
        for phase in self._phase_totals:
            if phase in record:
                self._phase_totals[phase] += record[phase]

        self._update_topN(record, also_errors=True)

        # Periodic progress every PROGRESS_EVERY files
        if self.total_files % self.PROGRESS_EVERY == 0:
            self._print_progress()

    def _update_topN(self, record: dict, also_errors: bool) -> None:
        """Maintain top 20 slowest and top 20 largest-chunk files."""
        self._slowest_files.append((record.get("total_s", 0.0), record))
        self._slowest_files.sort(key=lambda x: x[0], reverse=True)
        self._slowest_files = self._slowest_files[:20]

        if record.get("chunk_count", 0) > 0:
            cc = record["chunk_count"]
            self._largest_chunk_files.append((cc, record))
            self._largest_chunk_files.sort(key=lambda x: x[0], reverse=True)
            self._largest_chunk_files = self._largest_chunk_files[:20]

    def _print_progress(self) -> None:
        """Print periodic summary without flooding stdout."""
        elapsed = time.monotonic() - self._start_wall
        rate = self.total_files / elapsed if elapsed > 0 else 0
        print(
            f"  [PROFILE {self.total_files} files, {elapsed:.1f}s, {rate:.2f} files/sec]  "
            f"processed={self.processed_files}  errors={self.error_files}  "
            f"upsert_avg={self._avg_upsert_s():.3f}s",
            flush=True,
        )

    def _avg_upsert_s(self) -> float:
        total = self._phase_totals.get("collection_upsert_s", 0.0)
        n = self.processed_files
        return total / n if n > 0 else 0.0

    def final_report(self) -> dict:
        """Return the final report dict (and write JSON if configured)."""
        phase_totals = dict(self._phase_totals)
        phase_totals["wallclock_elapsed_s"] = time.monotonic() - self._start_wall
        phase_totals["files_per_sec"] = self.total_files / phase_totals["wallclock_elapsed_s"] if phase_totals["wallclock_elapsed_s"] > 0 else 0

        profile_json = os.environ.get("MEMPALACE_MINE_PROFILE_JSON", "")

        slowest_files = [
            {
                "source_file": r[1].get("source_file", ""),
                "room": r[1].get("room", ""),
                "chunk_count": r[1].get("chunk_count", 0),
                "total_s": round(r[0], 3),
                "read_file_s": round(r[1].get("read_file_s", 0), 3),
                "collection_upsert_s": round(r[1].get("collection_upsert_s", 0), 3),
            }
            for r in self._slowest_files
        ]

        largest_chunk_files = [
            {
                "source_file": r[1].get("source_file", ""),
                "room": r[1].get("room", ""),
                "chunk_count": r[1].get("chunk_count", 0),
                "total_s": round(r[0], 3),
            }
            for r in self._largest_chunk_files
        ]

        report = {
            "total_runtime_s": round(phase_totals["wallclock_elapsed_s"], 2),
            "total_files": self.total_files,
            "processed_files": self.processed_files,
            "skipped_files": self.skipped_files,
            "error_files": self.error_files,
            "total_drawers_added": self.total_drawers_added,
            "files_per_sec": round(phase_totals["files_per_sec"], 3),
            "phase_totals": {k: round(v, 4) for k, v in phase_totals.items()},
            "slowest_files": slowest_files,
            "largest_chunk_files": largest_chunk_files,
            "errors": self._errors,
        }

        return report

    def print_summary(self) -> None:
        """Print human-readable end-of-run summary table."""
        if not self.enabled:
            return

        r = self.final_report()
        print(f"\n{'=' * 55}", flush=True)
        print("  Mine Profile Summary", flush=True)
        print(f"{'─' * 55}", flush=True)
        print(f"  Files:     {r['total_files']} total  "
              f"{r['processed_files']} processed  {r['skipped_files']} skipped  {r['error_files']} errors", flush=True)
        print(f"  Runtime:   {r['total_runtime_s']:.2f}s  ({r['files_per_sec']:.2f} files/sec)", flush=True)
        print(f"  Drawers:   {r['total_drawers_added']}", flush=True)
        print(f"{'─' * 55}", flush=True)
        print("  Phase totals (seconds):", flush=True)
        phases = ["read_file_s", "detect_room_s", "chunk_s", "revision_existing_get_s",
                  "prepare_metadata_s", "collection_upsert_s", "tombstone_upsert_s", "total_s"]
        for phase in phases:
            val = r["phase_totals"].get(phase, 0.0)
            if val > 0:
                print(f"    {phase:30s} {val:8.3f}s", flush=True)

        if r["slowest_files"]:
            print(f"{'─' * 55}", flush=True)
            print("  Top 10 slowest files:", flush=True)
            print(f"    {'source_file':45s} {'room':15s} {'chunks':6s} {'total_s':8s}", flush=True)
            for f in r["slowest_files"][:10]:
                name = f["source_file"][-45:] if len(f["source_file"]) > 45 else f["source_file"]
                print(f"    {name:45s} {f['room'] or '?':15s} {f['chunk_count']:6d} {f['total_s']:8.3f}s", flush=True)

        if r["largest_chunk_files"]:
            print(f"{'─' * 55}", flush=True)
            print("  Top 10 largest (by chunk count):", flush=True)
            print(f"    {'source_file':45s} {'room':15s} {'chunks':6s}", flush=True)
            for f in r["largest_chunk_files"][:10]:
                name = f["source_file"][-45:] if len(f["source_file"]) > 45 else f["source_file"]
                print(f"    {name:45s} {f['room'] or '?':15s} {f['chunk_count']:6d}", flush=True)

        if r["errors"]:
            print(f"{'─' * 55}", flush=True)
            print(f"  Errors ({len(r['errors'])}):", flush=True)
            for e in r["errors"][:10]:
                print(f"    {e['source_file']}: [{e['phase']}] {e['error'][:80]}", flush=True)

        print(f"{'=' * 55}\n", flush=True)


# =============================================================================

READABLE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".sh",
    ".csv",
    ".sql",
    ".toml",
}

SKIP_FILENAMES = {
    "mempalace.yaml",
    "mempalace.yml",
    "mempal.yaml",
    "mempal.yml",
    ".gitignore",
    "package-lock.json",
}

CHUNK_SIZE = 800  # chars per drawer
CHUNK_OVERLAP = 100  # overlap between chunks
MIN_CHUNK_SIZE = 50  # skip tiny chunks
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB — skip files larger than this

# Batching for safe mining — reduces per-file collection.get() overhead
_MEMPALACE_MINE_BATCH_FILES = int(os.environ.get("MEMPALACE_MINE_BATCH_FILES", "8"))
_MEMPALACE_MINE_BATCH_DRAWERS = int(os.environ.get("MEMPALACE_MINE_BATCH_DRAWERS", "256"))

# Budget limits (0 = unlimited)
_MEMPALACE_MINE_MAX_FILES = int(os.environ.get("MEMPALACE_MINE_MAX_FILES", "0"))
_MEMPALACE_MINE_MAX_CHUNKS = int(os.environ.get("MEMPALACE_MINE_MAX_CHUNKS", "0"))
_MEMPALACE_MINE_MAX_SECONDS = float(os.environ.get("MEMPALACE_MINE_MAX_SECONDS", "0"))
_MEMPALACE_MINE_ABORT_ON_SWAP_MB = float(os.environ.get("MEMPALACE_MINE_ABORT_ON_SWAP_MB", "0"))

# Language detection from file extension
LANGUAGE_MAP = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".java": "Java", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++",
    ".cs": "C#", ".swift": "Swift", ".kt": "Kotlin", ".scala": "Scala",
    ".r": "R", ".R": "R", ".lua": "Lua", ".pl": "Perl",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".yaml": "YAML", ".yml": "YAML", ".json": "JSON", ".toml": "TOML",
    ".xml": "XML", ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".less": "Less", ".md": "Markdown", ".rst": "reStructuredText", ".txt": "Text",
    ".csv": "CSV", ".tf": "HCL", ".hcl": "HCL", ".dockerfile": "Dockerfile",
}


# =============================================================================
# IGNORE MATCHING
# =============================================================================


class GitignoreMatcher:
    """Lightweight matcher for one directory's .gitignore patterns."""

    def __init__(self, base_dir: Path, rules: list):
        self.base_dir = base_dir
        self.rules = rules

    @classmethod
    def from_dir(cls, dir_path: Path):
        gitignore_path = dir_path / ".gitignore"
        if not gitignore_path.is_file():
            return None

        try:
            lines = gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return None

        rules = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("\\#") or line.startswith("\\!"):
                line = line[1:]
            elif line.startswith("#"):
                continue

            negated = line.startswith("!")
            if negated:
                line = line[1:]

            anchored = line.startswith("/")
            if anchored:
                line = line.lstrip("/")

            dir_only = line.endswith("/")
            if dir_only:
                line = line.rstrip("/")

            if not line:
                continue

            rules.append(
                {
                    "pattern": line,
                    "anchored": anchored,
                    "dir_only": dir_only,
                    "negated": negated,
                }
            )

        if not rules:
            return None

        return cls(dir_path, rules)

    def matches(self, path: Path, is_dir: bool = None):
        try:
            relative = path.relative_to(self.base_dir).as_posix().strip("/")
        except ValueError:
            return None

        if not relative:
            return None

        if is_dir is None:
            is_dir = path.is_dir()

        ignored = None
        for rule in self.rules:
            if self._rule_matches(rule, relative, is_dir):
                ignored = not rule["negated"]
        return ignored

    def _rule_matches(self, rule: dict, relative: str, is_dir: bool) -> bool:
        pattern = rule["pattern"]
        parts = relative.split("/")
        pattern_parts = pattern.split("/")

        if rule["dir_only"]:
            target_parts = parts if is_dir else parts[:-1]
            if not target_parts:
                return False
            if rule["anchored"] or len(pattern_parts) > 1:
                return self._match_from_root(target_parts, pattern_parts)
            return any(fnmatch.fnmatch(part, pattern) for part in target_parts)

        if rule["anchored"] or len(pattern_parts) > 1:
            return self._match_from_root(parts, pattern_parts)

        return any(fnmatch.fnmatch(part, pattern) for part in parts)

    def _match_from_root(self, target_parts: list, pattern_parts: list) -> bool:
        def matches(path_index: int, pattern_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return True

            if path_index == len(target_parts):
                return all(part == "**" for part in pattern_parts[pattern_index:])

            pattern_part = pattern_parts[pattern_index]
            if pattern_part == "**":
                return matches(path_index, pattern_index + 1) or matches(
                    path_index + 1, pattern_index
                )

            if not fnmatch.fnmatch(target_parts[path_index], pattern_part):
                return False

            return matches(path_index + 1, pattern_index + 1)

        return matches(0, 0)


def load_gitignore_matcher(dir_path: Path, cache: dict):
    """Load and cache one directory's .gitignore matcher."""
    if dir_path not in cache:
        cache[dir_path] = GitignoreMatcher.from_dir(dir_path)
    return cache[dir_path]


def is_gitignored(path: Path, matchers: list, is_dir: bool = False) -> bool:
    """Apply active .gitignore matchers in ancestor order; last match wins."""
    ignored = False
    for matcher in matchers:
        decision = matcher.matches(path, is_dir=is_dir)
        if decision is not None:
            ignored = decision
    return ignored


def should_skip_dir(dirname: str) -> bool:
    """Skip known generated/cache directories before gitignore matching."""
    return dirname in SKIP_DIRS or dirname.endswith(".egg-info")


def normalize_include_paths(include_ignored: list) -> set:
    """Normalize comma-parsed include paths into project-relative POSIX strings."""
    normalized = set()
    for raw_path in include_ignored or []:
        candidate = str(raw_path).strip().strip("/")
        if candidate:
            normalized.add(Path(candidate).as_posix())
    return normalized


def is_exact_force_include(path: Path, project_path: Path, include_paths: set) -> bool:
    """Return True when a path exactly matches an explicit include override."""
    if not include_paths:
        return False

    try:
        relative = path.relative_to(project_path).as_posix().strip("/")
    except ValueError:
        return False

    return relative in include_paths


def is_force_included(path: Path, project_path: Path, include_paths: set) -> bool:
    """Return True when a path or one of its ancestors/descendants was explicitly included."""
    if not include_paths:
        return False

    try:
        relative = path.relative_to(project_path).as_posix().strip("/")
    except ValueError:
        return False

    if not relative:
        return False

    for include_path in include_paths:
        if relative == include_path:
            return True
        if relative.startswith(f"{include_path}/"):
            return True
        if include_path.startswith(f"{relative}/"):
            return True

    return False


# =============================================================================
# CONFIG
# =============================================================================


def load_config(project_dir: str) -> dict:
    """Load mempalace.yaml from project directory (falls back to mempal.yaml)."""
    import yaml

    config_path = Path(project_dir).expanduser().resolve() / "mempalace.yaml"
    if not config_path.exists():
        # Fallback to legacy name
        legacy_path = Path(project_dir).expanduser().resolve() / "mempal.yaml"
        if legacy_path.exists():
            config_path = legacy_path
        else:
            print(f"ERROR: No mempalace.yaml found in {project_dir}")
            print(f"Run: mempalace init {project_dir}")
            sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


# =============================================================================
# FILE ROUTING — which room does this file belong to?
# =============================================================================


def detect_room(filepath: Path, content: str, rooms: list, project_path: Path) -> str:
    """
    Route a file to the right room.
    Priority:
    1. Folder path matches a room name
    2. Filename matches a room name or keyword
    3. Content keyword scoring
    4. Fallback: "general"
    """
    relative = str(filepath.relative_to(project_path)).lower()
    filename = filepath.stem.lower()
    content_lower = content[:2000].lower()

    # Priority 1: folder path matches room name or keywords
    path_parts = relative.replace("\\", "/").split("/")
    for part in path_parts[:-1]:  # skip filename itself
        for room in rooms:
            candidates = [room["name"].lower()] + [k.lower() for k in room.get("keywords", [])]
            if any(part == c or c in part or part in c for c in candidates):
                return room["name"]

    # Priority 2: filename matches room name
    for room in rooms:
        if room["name"].lower() in filename or filename in room["name"].lower():
            return room["name"]

    # Priority 3: keyword scoring from room keywords + name
    scores = defaultdict(int)
    for room in rooms:
        keywords = room.get("keywords", []) + [room["name"]]
        for kw in keywords:
            count = content_lower.count(kw.lower())
            scores[room["name"]] += count

    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best

    return "general"


# =============================================================================
# CHUNKING
# =============================================================================


def chunk_text(content: str, source_file: str) -> list:
    """
    Split content into drawer-sized chunks.
    Tries to split on paragraph/line boundaries.
    Returns list of {"content": str, "chunk_index": int}
    """
    # Clean up
    content = content.strip()
    if not content:
        return []

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(content):
        end = min(start + CHUNK_SIZE, len(content))

        # Try to break at paragraph boundary
        if end < len(content):
            newline_pos = content.rfind("\n\n", start, end)
            if newline_pos > start + CHUNK_SIZE // 2:
                end = newline_pos
            else:
                newline_pos = content.rfind("\n", start, end)
                if newline_pos > start + CHUNK_SIZE // 2:
                    end = newline_pos

        chunk = content[start:end].strip()
        if len(chunk) >= MIN_CHUNK_SIZE:
            chunks.append(
                {
                    "content": chunk,
                    "chunk_index": chunk_index,
                }
            )
            chunk_index += 1

        start = end - CHUNK_OVERLAP if end < len(content) else end

    return chunks


# =============================================================================
# LANGUAGE DETECTION
# =============================================================================


def detect_language(source_file: str) -> str:
    """Detect programming language from file extension."""
    ext = Path(source_file).suffix.lower()
    return LANGUAGE_MAP.get(ext, "Text")


def _is_code_line(line: str) -> bool:
    """Check if a line looks like code (not a comment or blank)."""
    stripped = line.strip()
    if not stripped:
        return False
    # Skip single-line comments
    if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith(";"):
        return False
    return True


def _find_string_bounds(content: str, start: int) -> tuple[int, str]:
    """Find the end of a string literal starting at 'start'. Returns (end_pos, quote_char)."""
    quote = content[start]
    end = start + 1
    while end < len(content):
        if content[end] == "\\" and end + 1 < len(content):
            end += 2
            continue
        if content[end] == quote:
            return end + 1, quote
        end += 1
    return end, quote


def _in_string_or_comment(content: str, pos: int) -> bool:
    """Check if position is inside a string or line comment."""
    line = content[:pos]
    # Find the last line start
    last_newline = line.rfind("\n")
    line_start = last_newline + 1 if last_newline >= 0 else 0
    current_line = line[line_start:]

    # Check for line comment
    in_string = False
    quote = None
    i = 0
    while i < len(current_line):
        c = current_line[i]
        if not in_string:
            if c in ('"', "'", "`"):
                in_string = True
                quote = c
            elif c == "#" or current_line[i:i+2] == "//":
                return False  # line comment starts
        else:
            if c == "\\" and i + 1 < len(current_line):
                i += 2
                continue
            if c == quote:
                in_string = False
                quote = None
        i += 1
    return in_string


# =============================================================================
# STRUCTURAL CODE CHUNKING
# =============================================================================

# Pattern definitions for language-specific structural splitting
_PYTHON_PATTERNS = [
    (r'^def\s+(\w+)', 'def'),
    (r'^class\s+(\w+)', 'class'),
    (r'^async\s+def\s+(\w+)', 'async_def'),
    (r'^@(\w+)', 'decorator'),
]

_JS_PATTERNS = [
    (r'^function\s+(\w+)', 'function'),
    (r'^async\s+function\s+(\w+)', 'async_function'),
    (r'^const\s+(\w+)\s*=', 'const'),
    (r'^let\s+(\w+)\s*=', 'let'),
    (r'^class\s+(\w+)', 'class'),
    (r'^export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)', 'export_fn'),
    (r'^export\s+(?:default\s+)?class\s+(\w+)', 'export_class'),
]

_GENERIC_CODE_PATTERNS = [
    (r'^(?:public|private|protected|static|abstract|final)\s+(?:class|interface|enum)', 'java_member'),
    (r'^(?:func|func\s+\([^)]+\))\s+(\w+)', 'go_func'),
    (r'^(?:pub|pub\s+fn|fn)\s+(\w+)', 'rust_fn'),
    (r'^(?:def|class)\s+(\w+)', 'generic_def'),
]

import re

_PATTERNS_BY_LANG = {
    "Python": _PYTHON_PATTERNS,
    "TypeScript": _JS_PATTERNS,
    "JavaScript": _JS_PATTERNS,
    "Java": _GENERIC_CODE_PATTERNS,
    "Go": _GENERIC_CODE_PATTERNS,
    "Rust": _GENERIC_CODE_PATTERNS,
    "C": _GENERIC_CODE_PATTERNS,
    "C++": _GENERIC_CODE_PATTERNS,
    "C#": _GENERIC_CODE_PATTERNS,
}


def split_code_structurally(content: str, source_file: str, max_chunk_chars: int = 1200) -> list:
    """
    Split code content along structural boundaries (function/class definitions).

    For code files: splits at function/class definitions, decorators.
    For non-code files: falls back to paragraph chunking.

    Returns list of dicts with keys: content, line_start, line_end, symbol_name, symbol_scope, chunk_kind
    """
    language = detect_language(source_file)
    ext = Path(source_file).suffix.lower()

    # Non-code files: use paragraph chunking
    non_code_extensions = {
        ".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml", ".xml",
        ".html", ".htm", ".css", ".scss", ".less", ".csv", ".sql",
    }
    if ext in non_code_extensions or language == "Text":
        raw_chunks = chunk_text(content, source_file)
        # Add line info
        lines = content.split("\n")
        pos_to_line = []
        line_start = 0
        for i, line in enumerate(lines):
            pos_to_line.append((line_start, line_start + len(line)))
            line_start += len(line) + 1

        chunks = []
        for ck in raw_chunks:
            # Find line range for this chunk
            content_str = ck["content"]
            start_pos = content.find(content_str[:50])
            if start_pos < 0:
                start_pos = 0
            # Find line numbers
            line_start = 1
            line_end = len(lines)
            acc = 0
            for i, line in enumerate(lines):
                if acc >= start_pos:
                    line_start = i + 1
                    break
                acc += len(line) + 1
            acc2 = acc
            for i, line in enumerate(lines):
                if acc2 >= start_pos + len(content_str):
                    line_end = i + 1
                    break
                acc2 += len(line) + 1
            chunks.append({
                "content": ck["content"],
                "line_start": line_start,
                "line_end": line_end,
                "symbol_name": "",
                "symbol_scope": "",
                "chunk_kind": "prose",
            })
        return chunks

    patterns = _PATTERNS_BY_LANG.get(language, _GENERIC_CODE_PATTERNS)

    # Build regex for this language (numbered groups, not named)
    # Each pattern gets its own group for the whole match, plus inner group for symbol name
    all_patterns = "|".join(f"({p})" for i, (p, _) in enumerate(patterns))
    pattern_re = re.compile(all_patterns, re.MULTILINE)

    # Find all structural split points
    lines = content.split("\n")
    split_points = [0]  # line indices where chunks begin
    # Track (scope_name, scope_indent) pairs. scope_indent is the indentation
    # of the line that opened this scope. When we see a line at or below that
    # indentation, the scope has ended and we pop it.
    current_scope_stack = []  # list of (scope_name, scope_indent)
    symbol_map = {}  # line_index -> (symbol_name, symbol_scope)

    for i, line in enumerate(lines):
        # Compute line indentation (leading whitespace)
        stripped = line.strip()
        stripped_line = stripped
        if not stripped:
            continue
        line_indent = len(line) - len(line.lstrip())

        # Pop scopes whose opening indentation >= current line's indentation.
        # This means we've dedented past that scope's body.
        while current_scope_stack and current_scope_stack[-1][1] >= line_indent:
            current_scope_stack.pop()

        if not stripped or stripped.startswith("#") or stripped.startswith("//") or stripped.startswith(";"):
            continue

        match = pattern_re.match(stripped)
        if match:
            # Find which pattern group matched and get the symbol name
            # Group numbering: group(1) = first pattern, group(2) = its inner (\w+)
            #                  group(3) = second pattern, group(4) = its inner (\w+), etc.
            sym_name = ""
            kind = None
            for gi in range(len(patterns)):
                outer_group = match.group(gi * 2 + 1)
                if outer_group is not None:
                    # This pattern matched
                    name_group = match.group(gi * 2 + 2)
                    sym_name = name_group if name_group else outer_group.split()[1] if outer_group else ""
                    _, kind = patterns[gi]
                    break

            # Build scope string from current stack
            scope_parts = [s[0] for s in current_scope_stack if s[0]]
            scope = ".".join(scope_parts) if scope_parts else ""

            # Decorators are not structural split boundaries and are absorbed
            # into the preceding chunk — don't pollute symbol_map with them.
            if kind != "decorator":
                symbol_map[i] = (sym_name, scope)

            # Class definitions push to scope stack.
            # Rules:
            # 1. At indent 0: always clear all scopes first (we're at module level).
            #    This handles: class Foo → method → class Bar → Bar correctly gets
            #    empty scope instead of inheriting Foo's scope.
            # 2. At SAME indent as current scope's opening: sibling at that level
            #    (e.g. Inner at indent 4 inside Outer's body at indent 4).
            #    Pop the current scope before pushing so they're siblings, not nested.
            # 3. At DEEPER indent than current scope's opening: normal nested push.
            if kind in ("class", "export_class"):
                if line_indent == 0:
                    # Module level: clear ALL class scopes before pushing
                    current_scope_stack.clear()
                elif current_scope_stack and line_indent == current_scope_stack[-1][1]:
                    # Same indent as current scope's opening: sibling class, pop first
                    current_scope_stack.pop()
                current_scope_stack.append((sym_name, line_indent))

            # Only add split point for top-level definitions, not methods inside classes
            is_method_scope = bool(current_scope_stack)  # inside a class body
            is_method_pattern = kind in ("def", "function", "async_def", "async_function", "go_func", "rust_fn", "generic_def")
            if not (is_method_scope and is_method_pattern):
                split_points.append(i)

    # Deduplicate and sort
    split_points = sorted(set(split_points))

    # Build chunks between split points
    chunks = []
    for idx in range(len(split_points)):
        start_line = split_points[idx]
        # Next split point is the start of the NEXT chunk
        next_split = split_points[idx + 1] if idx + 1 < len(split_points) else len(lines)

        # Accumulate lines from start_line, stopping at next split point or max_chunk_chars
        chunk_lines = []
        char_count = 0
        chunk_end = start_line
        for li in range(start_line, len(lines)):
            # Stop at next split point (structural boundary)
            if li >= next_split and chunk_lines:
                chunk_end = li - 1
                break

            line_text = lines[li]
            line_len = len(line_text) + 1

            # Stop if we've exceeded max_chunk_chars and have content
            if char_count + line_len > max_chunk_chars and chunk_lines:
                chunk_end = li - 1
                break

            chunk_lines.append(line_text)
            char_count += line_len
            chunk_end = li

        if not chunk_lines:
            continue

        chunk_content = "\n".join(chunk_lines).strip()
        # Tentative symbol lookup: structural chunks anchored at a def/class line carry a symbol.
        # For prose chunks (no symbol), enforce MIN_CHUNK_SIZE.
        # Small code chunks (e.g. 1-line functions) are still valid — keep them.
        sym_name_tentative, _ = symbol_map.get(start_line, ("", ""))
        if not sym_name_tentative and len(chunk_content) < MIN_CHUNK_SIZE:
            continue

        sym_name, sym_scope = symbol_map.get(start_line, ("", ""))
        if not sym_name and symbol_map:
            # Find the closest earlier line with a real (non-decorator) symbol
            for prev in range(start_line - 1, -1, -1):
                if prev in symbol_map:
                    prev_name, prev_scope = symbol_map[prev]
                    if prev_name:  # Skip empty/decorator entries
                        sym_name, sym_scope = prev_name, prev_scope
                        break

        # Detect chunk kind
        if any(stripped.startswith(("#", "//", "/*", "*/", '"""', "'''")) for stripped in chunk_lines[:5]):
            chunk_kind = "comment"
        elif '"""' in chunk_content or "'''" in chunk_content:
            chunk_kind = "docstring"
        elif "def " in chunk_content or "class " in chunk_content or "function " in chunk_content:
            chunk_kind = "code_block"
        else:
            chunk_kind = "mixed"

        chunks.append({
            "content": chunk_content,
            "line_start": start_line + 1,  # 1-based
            "line_end": chunk_end + 1,
            "symbol_name": sym_name,
            "symbol_scope": sym_scope,
            "chunk_kind": chunk_kind,
        })

    return chunks


def chunk_with_metadata(content: str, source_file: str) -> list:
    """
    Unified entry point: structural chunking for code files, paragraph for prose.

    Returns list of dicts with code-aware metadata.
    """
    ext = Path(source_file).suffix.lower()
    code_extensions = {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mts", ".cts",
        ".java", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".cc",
        ".cxx", ".hpp", ".cs", ".swift", ".kt", ".scala", ".r", ".R",
        ".lua", ".pl", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    }

    if ext in code_extensions or detect_language(source_file) not in ("Text", "Markdown", "YAML", "JSON", "TOML", "HTML", "CSS"):
        return split_code_structurally(content, source_file)
    else:
        raw = chunk_text(content, source_file)
        return [{"content": c["content"], "line_start": 0, "line_end": 0,
                 "symbol_name": "", "symbol_scope": "", "chunk_kind": "prose"}
                for c in raw]


def add_drawer(
    collection, wing: str, room: str, content: str, source_file: str, chunk_index: int, agent: str
):
    """Add one drawer to the palace."""
    drawer_id = f"drawer_{wing}_{room}_{hashlib.sha256((source_file + str(chunk_index)).encode()).hexdigest()[:24]}"
    try:
        metadata = {
            "wing": wing,
            "room": room,
            "source_file": source_file,
            "chunk_index": chunk_index,
            "added_by": agent,
            "agent_id": agent,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "origin_type": "observation",
            "is_latest": True,
            "supersedes_id": "",
        }
        # Store file mtime so we can detect modifications later.
        try:
            metadata["source_mtime"] = os.path.getmtime(source_file)
        except OSError:
            pass
        collection.upsert(
            documents=[content],
            ids=[drawer_id],
            metadatas=[metadata],
        )
        return True
    except Exception:
        raise


# =============================================================================
# PROCESS ONE FILE
# =============================================================================


def _compute_file_revision(source_file: str, content: str) -> str:
    """Compute a revision identifier for a file: SHA256 of first 4KB + mtime."""
    prefix = content[:4096].encode("utf-8")
    try:
        mtime = os.path.getmtime(source_file)
    except OSError:
        mtime = 0
    revision_bytes = prefix + str(mtime).encode("utf-8")
    return hashlib.sha256(revision_bytes).hexdigest()[:32]


def _compute_content_hash(content: str) -> str:
    """Compute SHA256 hash of chunk content for tombstone detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


def _prepare_file_drawers(
    filepath: Path,
    project_path: Path,
    wing: str,
    rooms: list,
    agent: str,
    stats: "MineStats | None" = None,
) -> dict | None:
    """Prepare drawer data for one file without any collection operations.

    Returns a dict with prepared data ready for _commit_batch, or None if the
    file should be skipped/error.
    """
    t0 = time.monotonic()
    source_file = str(filepath)

    try:
        t_read = time.monotonic()
        content = filepath.read_text(encoding="utf-8", errors="replace")
        read_file_s = time.monotonic() - t_read
    except OSError:
        if stats:
            stats.record_file({"status": "error", "source_file": source_file, "phase": "read_file", "error": "OSError", "total_s": time.monotonic() - t0})
        return None

    content = content.strip()
    if len(content) < MIN_CHUNK_SIZE:
        if stats:
            stats.record_file({"status": "skipped", "source_file": source_file, "chunk_count": 0, "room": None, "total_s": time.monotonic() - t0})
        return None

    t_detect = time.monotonic()
    room = detect_room(filepath, content, rooms, project_path)
    detect_room_s = time.monotonic() - t_detect

    t_chunk = time.monotonic()
    chunks = chunk_with_metadata(content, source_file)
    chunk_s = time.monotonic() - t_chunk
    if not chunks:
        if stats:
            stats.record_file({"status": "skipped", "source_file": source_file, "chunk_count": 0, "room": room, "total_s": time.monotonic() - t0})
        return None

    revision_id = _compute_file_revision(source_file, content)
    timestamp = datetime.utcnow().isoformat() + "Z"

    try:
        source_mtime = os.path.getmtime(source_file)
    except OSError:
        source_mtime = None

    t_meta = time.monotonic()
    documents, ids, metadatas = [], [], []
    for idx, chunk in enumerate(chunks):
        content_hash = _compute_content_hash(chunk["content"])
        drawer_id = f"drawer_{wing}_{room}_{hashlib.sha256((source_file + str(idx)).encode()).hexdigest()[:24]}"
        metadata = {
            "wing": wing, "room": room, "source_file": source_file,
            "chunk_index": idx, "added_by": agent, "agent_id": agent,
            "timestamp": timestamp, "origin_type": "observation", "is_latest": True,
            "supersedes_id": "", "language": detect_language(source_file),
            "line_start": chunk.get("line_start", 0), "line_end": chunk.get("line_end", 0),
            "symbol_name": chunk.get("symbol_name", ""), "symbol_scope": chunk.get("symbol_scope", ""),
            "chunk_kind": chunk.get("chunk_kind", "mixed"),
            "revision_id": revision_id, "content_hash": content_hash,
        }
        if source_mtime is not None:
            metadata["source_mtime"] = source_mtime
        # Phase 3 retrieval planner: store project context for push-down filtering
        metadata["project_root"] = str(project_path)
        try:
            metadata["repo_rel_path"] = str(filepath.relative_to(project_path))
        except ValueError:
            metadata["repo_rel_path"] = str(filepath)
        # Build symbol_fqn from name + scope for symbol-first retrieval
        sym_name = chunk.get("symbol_name", "")
        sym_scope = chunk.get("symbol_scope", "")
        if sym_name and sym_scope:
            metadata["symbol_fqn"] = f"{sym_scope}::{sym_name}"
        elif sym_name:
            metadata["symbol_fqn"] = sym_name
        documents.append(chunk["content"])
        ids.append(drawer_id)
        metadatas.append(metadata)
    prepare_metadata_s = time.monotonic() - t_meta

    total_s = time.monotonic() - t0
    if stats:
        stats.record_file({
            "status": "prepared", "source_file": source_file, "room": room,
            "chunk_count": len(documents), "total_s": total_s,
            "read_file_s": read_file_s, "detect_room_s": detect_room_s,
            "chunk_s": chunk_s,
            "prepare_metadata_s": prepare_metadata_s,
        })

    return {
        "source_file": source_file,
        "documents": documents,
        "ids": ids,
        "metadatas": metadatas,
        "room": room,
        "prepare_metadata_s": prepare_metadata_s,
    }


def process_file(
    filepath: Path,
    project_path: Path,
    collection,
    wing: str,
    rooms: list,
    agent: str,
    dry_run: bool,
    palace_path: str = None,
    stats: "MineStats | None" = None,
) -> tuple:
    """Read, chunk, route, and file one file. Returns (drawer_count, room_name).

    Implements revision-based ingest:
    - Existing chunks for this source_file are looked up (is_latest=True)
    - New chunks that supersede old ones set is_latest=False on old, supersedes_id on new
    - Content-hash matching prevents unnecessary tombstones when content is unchanged
    """
    t0 = time.monotonic()
    source_file = str(filepath)

    # Prepare drawer data (read, chunk, route — no collection ops)
    prepared = _prepare_file_drawers(
        filepath=filepath,
        project_path=project_path,
        wing=wing,
        rooms=rooms,
        agent=agent,
        stats=stats,
    )
    if prepared is None:
        return 0, None

    documents = prepared["documents"]
    ids = prepared["ids"]
    metadatas = prepared["metadatas"]
    room = prepared["room"]
    prepare_metadata_s = prepared["prepare_metadata_s"]

    if dry_run:
        print(f"    [DRY RUN] {filepath.name} → room:{room} ({len(documents)} drawers)")
        if stats:
            stats.record_file({"status": "skipped", "source_file": source_file, "chunk_count": len(documents), "room": room, "total_s": time.monotonic() - t0})
        return len(documents), room

    # ── Existing chunk lookup ──────────────────────────────────────────────
    t_get = time.monotonic()
    try:
        existing = collection.get(
            where={"source_file": source_file, "is_latest": True},
            include=["metadatas", "ids"],
        )
    except Exception:
        existing = {"ids": [], "metadatas": []}
    revision_existing_get_s = time.monotonic() - t_get

    old_chunks_by_hash: dict[str, list[tuple]] = defaultdict(list)
    if existing and existing.get("ids"):
        for old_id, old_meta in zip(existing["ids"], existing["metadatas"]):
            if old_meta:
                old_hash = old_meta.get("content_hash", "")
                if old_hash:
                    old_chunks_by_hash[old_hash].append((old_id, old_meta))

    # Update supersedes_id on matching content hashes
    for meta in metadatas:
        content_hash = meta.get("content_hash", "")
        if content_hash in old_chunks_by_hash:
            superseded_ids_for_chunk = [old_id for old_id, _ in old_chunks_by_hash[content_hash]]
            meta["supersedes_id"] = "|".join(superseded_ids_for_chunk)

    # ── Upsert new chunks ──────────────────────────────────────────────────
    t_upsert = time.monotonic()
    collection.upsert(documents=documents, ids=ids, metadatas=metadatas)
    collection_upsert_s = time.monotonic() - t_upsert

    # ── Tombstone stale chunks ─────────────────────────────────────────────
    all_old_ids = set()
    for old_list in old_chunks_by_hash.values():
        for old_id, _ in old_list:
            all_old_ids.add(old_id)
    superseded_ids = set()
    for meta in metadatas:
        raw = meta.get("supersedes_id", "")
        if raw:
            for sid in raw.split("|"):
                if sid:
                    superseded_ids.add(sid)

    tombstone_upsert_s = 0.0
    tombstone_ids, tombstone_docs, tombstone_metas = [], [], []
    for old_hash, old_list in old_chunks_by_hash.items():
        for old_id, old_meta in old_list:
            if old_id not in superseded_ids:
                tombstone_ids.append(old_id)
                tombstone_docs.append(old_meta.get("source_content", old_meta.get("document", "")))
                tombstone_metas.append({"is_latest": False})

    if tombstone_ids:
        t_tomb = time.monotonic()
        try:
            collection.upsert(documents=tombstone_docs, ids=tombstone_ids, metadatas=tombstone_metas)
        except Exception:
            pass
        tombstone_upsert_s = time.monotonic() - t_tomb

    total_s = time.monotonic() - t0
    if stats:
        stats.record_file({
            "status": "added", "source_file": source_file, "room": room,
            "chunk_count": len(documents), "total_s": total_s,
            "read_file_s": prepared.get("read_file_s", 0), "detect_room_s": prepared.get("detect_room_s", 0),
            "chunk_s": prepared.get("chunk_s", 0), "revision_existing_get_s": revision_existing_get_s,
            "prepare_metadata_s": prepare_metadata_s, "collection_upsert_s": collection_upsert_s,
            "tombstone_upsert_s": tombstone_upsert_s,
        })

    return len(documents), room


# =============================================================================
# BATCHED COMMIT
# =============================================================================


def _commit_batch(
    pending: list[dict],
    collection,
    wing: str,
    rooms: list,
    agent: str,
    palace_path: str,
    stats: "MineStats | None",
    manifest: "MiningManifest | None",
    project_path: str,
) -> tuple[int, int, dict]:
    """Commit a batch of prepared file results sequentially.

    Batches the existing-chunk lookup across all pending files using a single
    $or query, then processes each file's tombstones and upserts one at a time.
    Maintains per-file atomicity: one bad file does not poison others.

    Returns (total_drawers, files_committed, phase_totals).
    """
    if not pending:
        return 0, 0, {}

    # ── Phase 1: batch existing-chunk lookup ──────────────────────────────
    # Build a single $or query for all source_files to get existing chunks.
    # Uses LanceDB's SQL id IN (...) path — safe for hex ids.
    t_batch_get = time.monotonic()
    source_files = [p["source_file"] for p in pending]
    if len(source_files) == 1:
        where_clause = {"source_file": source_files[0]}
    else:
        where_clause = {
            "$or": [{"source_file": {"$eq": sf}} for sf in source_files]
        }

    existing = {"ids": [], "metadatas": []}
    try:
        existing = collection.get(
            where={**where_clause, "is_latest": True},
            include=["metadatas", "ids"],
        )
    except Exception:
        pass  # Fail-open: treat as no existing chunks
    batch_get_s = time.monotonic() - t_batch_get

    # Index existing chunks by source_file for O(1) per-file lookup
    by_source: dict[str, list[tuple]] = defaultdict(list)
    if existing and existing.get("ids"):
        for old_id, old_meta in zip(existing["ids"], existing["metadatas"]):
            if old_meta:
                by_source[old_meta.get("source_file", "")].append((old_id, old_meta))

    # ── Phase 2: per-file tombstones + upserts (sequential, single writer) ─
    total_drawers = 0
    files_committed = 0
    phase_totals = defaultdict(float)

    for p in pending:
        sf = p["source_file"]
        documents = p["documents"]
        ids = p["ids"]
        metadatas = p["metadatas"]
        room = p["room"]

        old_chunks_by_hash: dict[str, list[tuple]] = defaultdict(list)
        for old_id, old_meta in by_source.get(sf, []):
            old_hash = old_meta.get("content_hash", "")
            if old_hash:
                old_chunks_by_hash[old_hash].append((old_id, old_meta))

        # Tombstones
        all_old_ids = set()
        for old_list in old_chunks_by_hash.values():
            for old_id, _ in old_list:
                all_old_ids.add(old_id)
        superseded_ids = set()
        for meta in metadatas:
            raw = meta.get("supersedes_id", "")
            if raw:
                for sid in raw.split("|"):
                    if sid:
                        superseded_ids.add(sid)

        tombstone_ids, tombstone_docs, tombstone_metas = [], [], []
        for old_hash, old_list in old_chunks_by_hash.items():
            for old_id, old_meta in old_list:
                if old_id not in superseded_ids:
                    tombstone_ids.append(old_id)
                    tombstone_docs.append(old_meta.get("source_content", old_meta.get("document", "")))
                    tombstone_metas.append({"is_latest": False})

        t_upsert = time.monotonic()
        collection_upsert_s = 0.0
        if documents:
            try:
                collection.upsert(documents=documents, ids=ids, metadatas=metadatas)
            except Exception:
                # Per-file failure isolation: continue to next file
                continue
        collection_upsert_s = time.monotonic() - t_upsert

        t_tomb = time.monotonic()
        if tombstone_ids:
            try:
                collection.upsert(documents=tombstone_docs, ids=tombstone_ids, metadatas=tombstone_metas)
            except Exception:
                pass
        tombstone_upsert_s = time.monotonic() - t_tomb

        total_s = batch_get_s + collection_upsert_s + tombstone_upsert_s
        if stats:
            stats.record_file({
                "status": "added", "source_file": sf, "room": room,
                "chunk_count": len(documents), "total_s": total_s,
                "batch_get_s": batch_get_s,
                "prepare_metadata_s": p.get("prepare_metadata_s", 0),
                "collection_upsert_s": collection_upsert_s,
                "tombstone_upsert_s": tombstone_upsert_s,
            })

        phase_totals["collection_upsert_s"] += collection_upsert_s
        phase_totals["tombstone_upsert_s"] += tombstone_upsert_s
        total_drawers += len(documents)
        files_committed += 1

        # Update manifest for successfully committed file
        if manifest is not None:
            mf = p.get("_manifest")
            if mf:
                try:
                    manifest.update_success(
                        wing, project_path, sf,
                        mf["size_bytes"], mf["mtime_ns"],
                        mf["qh"], len(documents),
                    )
                except Exception:
                    pass  # Fail-open

    phase_totals["batch_get_s"] = batch_get_s
    return total_drawers, files_committed, phase_totals


# =============================================================================
# SCAN PROJECT
# =============================================================================


def scan_project(
    project_dir: str,
    respect_gitignore: bool = True,
    include_ignored: list = None,
) -> list:
    """Return list of all readable file paths."""
    project_path = Path(project_dir).expanduser().resolve()
    files = []
    active_matchers = []
    matcher_cache = {}
    include_paths = normalize_include_paths(include_ignored)

    for root, dirs, filenames in os.walk(project_path):
        root_path = Path(root)

        if respect_gitignore:
            active_matchers = [
                matcher
                for matcher in active_matchers
                if root_path == matcher.base_dir or matcher.base_dir in root_path.parents
            ]
            current_matcher = load_gitignore_matcher(root_path, matcher_cache)
            if current_matcher is not None:
                active_matchers.append(current_matcher)

        dirs[:] = [
            d
            for d in dirs
            if is_force_included(root_path / d, project_path, include_paths)
            or not should_skip_dir(d)
        ]
        if respect_gitignore and active_matchers:
            dirs[:] = [
                d
                for d in dirs
                if is_force_included(root_path / d, project_path, include_paths)
                or not is_gitignored(root_path / d, active_matchers, is_dir=True)
            ]

        for filename in filenames:
            filepath = root_path / filename
            force_include = is_force_included(filepath, project_path, include_paths)
            exact_force_include = is_exact_force_include(filepath, project_path, include_paths)

            if not force_include and filename in SKIP_FILENAMES:
                continue
            if filepath.suffix.lower() not in READABLE_EXTENSIONS and not exact_force_include:
                continue
            if respect_gitignore and active_matchers and not force_include:
                if is_gitignored(filepath, active_matchers, is_dir=False):
                    continue
            # Skip symlinks — prevents following links to /dev/urandom, etc.
            if filepath.is_symlink():
                continue
            # Skip files exceeding size limit
            try:
                if filepath.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            files.append(filepath)
    return files


# =============================================================================
# MAIN: MINE
# =============================================================================


def _get_swap_mb() -> float | None:
    """Return current swap used in MB, or None if unavailable."""
    try:
        import subprocess
        result = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # Output: "Physical: 512MB, Logical: 1GB, Used: 128M, Available: 896M"
            output = result.stdout.strip()
            # Find "Used: X" — parse "128M" or "1.2G"
            import re
            m = re.search(r"Used:\s+([0-9.]+)([MG])", output)
            if m:
                val = float(m.group(1))
                unit = m.group(2)
                return val * (1024 if unit == "G" else 1)
    except Exception:
        pass
    return None


def mine(
    project_dir: str,
    palace_path: str,
    wing_override: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    respect_gitignore: bool = True,
    include_ignored: list = None,
) -> dict:
    """Mine a project directory into the palace. Returns a partial mining report dict."""
    # Mining is daemon-only by default to prevent OOM on M1 8GB.
    # The in-process fallback model (~500MB) causes memory pressure.
    # User can override with: MEMPALACE_EMBED_FALLBACK=1 mempalace mine ...
    os.environ.setdefault("MEMPALACE_EMBED_FALLBACK", "0")

    project_path = Path(project_dir).expanduser().resolve()
    config = load_config(project_dir)

    wing = wing_override or config["wing"]
    rooms = config.get("rooms", [{"name": "general", "description": "All project files"}])

    files = scan_project(
        project_dir,
        respect_gitignore=respect_gitignore,
        include_ignored=include_ignored,
    )

    # Budget: stop scanning after max_files
    files_seen = len(files)
    if _MEMPALACE_MINE_MAX_FILES > 0 and len(files) > _MEMPALACE_MINE_MAX_FILES:
        files = files[:_MEMPALACE_MINE_MAX_FILES]

    # CLI limit param: truncate after MAX_FILES budget so both can coexist
    if limit > 0:
        files = files[:limit]
    files_seen = max(files_seen, len(files))  # files_seen reflects all scanned files

    # Budget report (populated throughout the run)
    budget_report = {
        "completed": True,
        "abort_reason": None,
        "files_seen": files_seen,
        "files_processed": 0,
        "chunks_written": 0,
        "elapsed_s": 0.0,
        "swap_mb": None,
    }

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Rooms:   {', '.join(r['name'] for r in rooms)}")
    print(f"  Files:   {len(files)} (seen: {files_seen})")
    print(f"  Palace:  {palace_path}")
    if _MEMPALACE_MINE_MAX_FILES > 0:
        print(f"  Budget:  max_files={_MEMPALACE_MINE_MAX_FILES}", end="")
        if _MEMPALACE_MINE_MAX_CHUNKS > 0:
            print(f" max_chunks={_MEMPALACE_MINE_MAX_CHUNKS}", end="")
        if _MEMPALACE_MINE_MAX_SECONDS > 0:
            print(f" max_seconds={_MEMPALACE_MINE_MAX_SECONDS}", end="")
        if _MEMPALACE_MINE_ABORT_ON_SWAP_MB > 0:
            print(f" abort_swap={_MEMPALACE_MINE_ABORT_ON_SWAP_MB}MB", end="")
        print()
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    if not respect_gitignore:
        print("  .gitignore: DISABLED")
    if include_ignored:
        print(f"  Include: {', '.join(sorted(normalize_include_paths(include_ignored)))}")
    print(f"{'─' * 55}\n")

    if not dry_run:
        cfg = MempalaceConfig()
        backend = get_backend(cfg.backend)
        collection = backend.get_collection(palace_path, cfg.collection_name, create=True)
    else:
        collection = None

    total_drawers = 0
    files_skipped = 0
    room_counts = defaultdict(int)

    stats = MineStats()

    # Open manifest (fail-open: any error leaves manifest as None)
    manifest = None
    use_force = os.environ.get("MEMPALACE_MINE_FORCE", "0") == "1"
    if palace_path and not use_force:
        try:
            manifest = MiningManifest(palace_path)
        except Exception:
            pass

    # Batching accumulator — reduces per-file collection.get() calls
    pending: list[dict] = []

    def _flush_pending():
        nonlocal total_drawers, files_skipped
        if not pending:
            return
        drawers, committed, _ = _commit_batch(
            pending=pending,
            collection=collection,
            wing=wing,
            rooms=rooms,
            agent=agent,
            palace_path=palace_path,
            stats=stats,
            manifest=manifest,
            project_path=str(project_path),
        )
        total_drawers += drawers
        files_skipped += len(pending) - committed
        for p in pending:
            room_counts[p["room"]] += 1
            print(f"  ✓ {p['source_file'][:50]:50} +{len(p['documents'])}")
        pending.clear()

    exc_raised = False
    _start_time = time.monotonic()
    try:
        for i, filepath in enumerate(files, 1):
            # --- Budget: time limit (check first, before any processing) ---
            if _MEMPALACE_MINE_MAX_SECONDS > 0:
                elapsed = time.monotonic() - _start_time
                if elapsed >= _MEMPALACE_MINE_MAX_SECONDS:
                    budget_report["completed"] = False
                    budget_report["abort_reason"] = "max_seconds"
                    budget_report["elapsed_s"] = elapsed
                    budget_report["swap_mb"] = _get_swap_mb()
                    print(f"\n  [BUDGET] max_seconds={_MEMPALACE_MINE_MAX_SECONDS}s reached after {elapsed:.1f}s — stopping gracefully")
                    break

            # --- Manifest skip: compute fingerprint before expensive work ---
            if manifest is not None and not dry_run:
                try:
                    file_stat = filepath.stat()
                    size_bytes = file_stat.st_size
                    mtime_ns = file_stat.st_mtime_ns
                    qh = _quick_hash(filepath, size_bytes)
                    if qh is not None and manifest.is_unchanged(
                        wing, str(project_path), str(filepath), size_bytes, mtime_ns, qh
                    ):
                        files_skipped += 1
                        if os.environ.get("MEMPALACE_MINE_PROFILE") == "1":
                            print(f"    [skip manifest] {filepath.name}")
                        continue
                except Exception as e:
                    logger.debug("manifest check failed for %s (fail-open): %s", filepath, e)

            if dry_run:
                # Dry run: use original process_file (handles its own output)
                try:
                    drawers, room = process_file(
                        filepath=filepath,
                        project_path=project_path,
                        collection=collection,
                        wing=wing,
                        rooms=rooms,
                        agent=agent,
                        dry_run=dry_run,
                        palace_path=palace_path,
                        stats=stats,
                    )
                except Exception:
                    drawers = 0
                    room = None
                if drawers == 0:
                    files_skipped += 1
                else:
                    total_drawers += drawers
                    room_counts[room] += 1
            else:
                # Real mining: prepare drawer data, accumulate, batch commit
                try:
                    prepared = _prepare_file_drawers(
                        filepath=filepath,
                        project_path=project_path,
                        wing=wing,
                        rooms=rooms,
                        agent=agent,
                        stats=stats,
                    )
                except Exception:
                    if manifest is not None:
                        manifest.update_error(wing, str(project_path), str(filepath))
                    prepared = None

                if prepared is None:
                    files_skipped += 1
                    continue

                # Attach manifest data for _commit_batch to update manifest
                if manifest is not None:
                    try:
                        file_stat = filepath.stat()
                        prepared["_manifest"] = {
                            "size_bytes": file_stat.st_size,
                            "mtime_ns": file_stat.st_mtime_ns,
                            "qh": _quick_hash(filepath, file_stat.st_size),
                        }
                    except Exception:
                        pass  # Fail-open: no manifest data

                # --- Budget: chunk limit (checked BEFORE appending to pending) ---
                # Account for pending drawers (not yet committed) + this file's drawers
                prepared_drawers = len(prepared["documents"])
                pending_drawers = sum(len(p["documents"]) for p in pending)
                if _MEMPALACE_MINE_MAX_CHUNKS > 0 and total_drawers + pending_drawers + prepared_drawers > _MEMPALACE_MINE_MAX_CHUNKS:
                    budget_report["completed"] = False
                    budget_report["abort_reason"] = "max_chunks"
                    budget_report["elapsed_s"] = time.monotonic() - _start_time
                    budget_report["swap_mb"] = _get_swap_mb()
                    print(f"\n  [BUDGET] max_chunks={_MEMPALACE_MINE_MAX_CHUNKS} reached ({total_drawers} written) — stopping gracefully")
                    break

                pending.append(prepared)

                # Flush on thresholds
                drawer_count = sum(len(p["documents"]) for p in pending)
                if len(pending) >= _MEMPALACE_MINE_BATCH_FILES or drawer_count >= _MEMPALACE_MINE_BATCH_DRAWERS:
                    _flush_pending()
                    # total_drawers already updated by _flush_pending via nonlocal

            # --- Budget: time limit (check after each file completes) ---
            if _MEMPALACE_MINE_MAX_SECONDS > 0 and not budget_report["completed"]:
                swap_mb = _get_swap_mb()
                if swap_mb is not None and swap_mb >= _MEMPALACE_MINE_ABORT_ON_SWAP_MB:
                    budget_report["completed"] = False
                    budget_report["abort_reason"] = "swap_threshold"
                    budget_report["elapsed_s"] = time.monotonic() - _start_time
                    budget_report["swap_mb"] = swap_mb
                    print(f"\n  [BUDGET] swap={swap_mb:.0f}MB >= threshold={_MEMPALACE_MINE_ABORT_ON_SWAP_MB}MB — aborting cleanly")
                    break

        # Final flush
        if not dry_run and pending:
            _flush_pending()

        if manifest is not None:
            manifest.close()

        # Build cross-reference symbol index for all files processed
        # (only files that were actually committed, respecting max_files budget)
        if not dry_run and files:
            processed_files = [str(f) for f in files]
            # Respect max_files: SymbolIndex iterates over the sliced list
            if _MEMPALACE_MINE_MAX_FILES > 0:
                processed_files = processed_files[:_MEMPALACE_MINE_MAX_FILES]
            try:
                si = SymbolIndex.get(palace_path)
                si.build_index(str(project_path), processed_files)
                si_stats = si.stats()
                print(f"  Symbol index: {si_stats['total_symbols']} symbols, {si_stats['total_files']} files")
            except Exception:
                pass
    except Exception:
        exc_raised = True
        raise
    finally:
        # Always write profile JSON even on exception — re-raises below
        stats.print_summary()

        profile_json = os.environ.get("MEMPALACE_MINE_PROFILE_JSON", "")
        if profile_json:
            try:
                report = stats.final_report()
                with open(profile_json, "w") as f:
                    _json.dump(report, f, indent=2)
            except Exception:
                pass

        # Finalize budget report (always, even on exception)
        if budget_report["completed"]:
            budget_report["elapsed_s"] = time.monotonic() - _start_time
        budget_report["files_processed"] = len(files) - files_skipped
        budget_report["chunks_written"] = total_drawers
        if budget_report["swap_mb"] is None:
            budget_report["swap_mb"] = _get_swap_mb()

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {len(files) - files_skipped}")
    print(f"  Files skipped (already filed): {files_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    print("\n  By room:")
    for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    {room:20} {count} files")
    if not budget_report["completed"]:
        print(f"\n  Budget abort: {budget_report['abort_reason']}")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")

    return budget_report


# =============================================================================
# STATUS
# =============================================================================


def status(palace_path: str):
    """Show what's been filed in the palace.

    Uses canonical backend factory — Lance is primary.
    """
    from .config import MempalaceConfig

    cfg = MempalaceConfig()
    try:
        backend = get_backend(cfg.backend)
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        return

    # Iterative aggregation — no fixed limit, processes ALL records
    wing_rooms = defaultdict(lambda: defaultdict(int))
    _BATCH = 500
    offset = 0
    while True:
        try:
            r = col.get(limit=_BATCH, offset=offset, include=["metadatas"])
        except Exception as e:
            print(f"\n  Error reading palace: {e}")
            return
        metas = r.get("metadatas", [])
        if not metas:
            break
        for m in metas:
            wing_rooms[m.get("wing", "?")][m.get("room", "?")] += 1
        if len(metas) < _BATCH:
            break
        offset += len(metas)

    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {sum(sum(rooms.values()) for rooms in wing_rooms.values())} drawers")
    print(f"{'=' * 55}\n")
    for wing, rooms in sorted(wing_rooms.items()):
        print(f"  WING: {wing}")
        for room, count in sorted(rooms.items(), key=lambda x: x[1], reverse=True):
            print(f"    ROOM: {room:20} {count:5} drawers")
        print()
    print(f"{'=' * 55}\n")
