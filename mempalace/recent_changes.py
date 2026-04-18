#!/usr/bin/env python3
"""
recent_changes.py — Lightweight git recent-changes parser.

Provides:
- get_recent_changes(project_path, n=20): recently changed files
- get_hot_spots(project_path, n=10): files changed most in last 30 days
- build_change_summary(project_path): human-readable change summary

Uses: git log parsing only (no gitanalytics framework).
Used by: wakeup_context for repo-delta awareness, MCP tools.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


_git_lock = threading.Lock()


def _run_git(project_path: str, args: list[str]) -> str:
    """Run git command and return stdout. Returns "" on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_recent_changes(project_path: str, n: int = 20) -> list[dict]:
    """
    Return recently changed files from git log.

    Returns list of dicts with:
    - file_path: relative path from project root
    - commit_hash: short hash
    - commit_date: ISO date string
    - commit_message: first line of commit message
    - files_changed: number of files in this commit
    """
    project_path = str(Path(project_path).expanduser().resolve())

    output = _run_git(project_path, [
        "log", "--oneline", "--format=%H|%ad|%s", "--date=iso",
        "-n", str(n * 2),  # fetch more, filter below
    ])

    if not output:
        return []

    changes = []
    seen_files = set()
    lines = output.split("\n")

    # Get file lists for recent commits
    commit_hashes = []
    for line in lines:
        if "|" not in line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        commit_hash = parts[0].strip()
        commit_date = parts[1].strip()
        commit_message = parts[2].strip() if len(parts) > 2 else ""
        commit_hashes.append((commit_hash, commit_date, commit_message))

    # Get file lists for each commit (limit to n)
    for commit_hash, commit_date, commit_message in commit_hashes[:n]:
        file_output = _run_git(project_path, ["diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash])
        if not file_output:
            continue

        file_list = [f.strip() for f in file_output.split("\n") if f.strip()]
        if not file_list:
            continue

        for fp in file_list:
            if fp in seen_files:
                continue
            seen_files.add(fp)

            # Check age — skip commits older than 90 days
            try:
                commit_dt = datetime.fromisoformat(commit_date.replace(" ", "T").split(".")[0])
                if datetime.now() - commit_dt > timedelta(days=90):
                    continue
            except Exception:
                pass

            changes.append({
                "file_path": fp,
                "commit_hash": commit_hash[:7],
                "commit_date": commit_date,
                "commit_message": commit_message,
                "files_changed": len(file_list),
            })

        if len(changes) >= n:
            break

    return changes[:n]


def get_hot_spots(project_path: str, n: int = 10) -> list[dict]:
    """
    Return files changed most frequently in the last 30 days.
    Returns list of dicts with file_path and change_count.
    """
    project_path = str(Path(project_path).expanduser().resolve())

    # Get commits from last 30 days
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    output = _run_git(project_path, [
        "log", "--since", cutoff, "--name-only", "--format=%H",
    ])

    if not output:
        return []

    # Count file changes
    counts = defaultdict(int)
    current_hash = None

    for line in output.split("\n"):
        if line and not line.startswith(" "):
            current_hash = line.strip()
        elif line.strip() and current_hash:
            fp = line.strip()
            if _is_code_file(fp):
                counts[fp] += 1

    # Sort by frequency
    sorted_files = sorted(counts.items(), key=lambda x: x[1], reverse=True)

    return [
        {"file_path": fp, "change_count": count}
        for fp, count in sorted_files[:n]
    ]


def _is_code_file(fp: str) -> bool:
    """Check if a file path looks like source code."""
    code_extensions = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
        ".go", ".rs", ".java", ".rb", ".php", ".c", ".h",
        ".cpp", ".cc", ".cxx", ".hpp", ".cs", ".swift", ".kt",
        ".scala", ".r", ".R", ".lua", ".pl", ".sh", ".bash",
        ".yaml", ".yml", ".json", ".toml", ".sql", ".md",
    }
    ext = Path(fp).suffix.lower()
    return ext in code_extensions or fp.endswith("Makefile") or fp.endswith("CMakeLists.txt")


def build_change_summary(project_path: str, n: int = 10) -> dict:
    """
    Build a human-readable change summary for the project.

    Returns dict with:
    - recent_files: last N changed files
    - hot_spots: top N most-changed files
    - total_commits_30d: approximate commit count in last 30 days
    - languages: set of languages with recent changes
    """
    recent = get_recent_changes(project_path, n=n)
    hot = get_hot_spots(project_path, n=n)

    languages = set()
    for item in recent:
        lang = Path(item["file_path"]).suffix.lower()
        lang_map = {
            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
            ".go": "Go", ".rs": "Rust", ".java": "Java",
        }
        if lang in lang_map:
            languages.add(lang_map[lang])

    # Count recent commits
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    count_output = _run_git(project_path, ["log", "--since", cutoff, "--oneline"])
    total_commits = len(count_output.split("\n")) if count_output else 0

    return {
        "recent_files": recent,
        "hot_spots": hot,
        "total_commits_30d": total_commits,
        "languages_with_changes": sorted(languages),
    }


def get_file_blame(project_path: str, file_path: str, max_lines: int = 20) -> list[dict]:
    """
    Get most recent committer for each line in a file.
    Returns list of dicts with line_number, commit_hash, author, date.
    """
    project_path = str(Path(project_path).expanduser().resolve())
    fp = str(Path(file_path))

    output = _run_git(project_path, [
        "blame", "--line-porcelain", "-L", f"1,{max_lines}", fp
    ])

    if not output:
        return []

    lines = output.split("\n")
    result = []
    current = {}

    for line in lines:
        if line.startswith("\t"):
            # Content line
            if current:
                current["content"] = line[1:]
                result.append(current)
                current = {}
        elif line.startswith("author "):
            current["author"] = line[7:]
        elif line.startswith("author-time "):
            try:
                ts = int(line.split(" ", 1)[1])
                current["date"] = datetime.fromtimestamp(ts).isoformat()
            except Exception:
                pass
        elif line.startswith("summary "):
            current["commit_message"] = line[8:]
        elif line.startswith("previous "):
            current["prev_hash"] = line[9:].split()[0] if len(line) > 9 else ""
        elif line.startswith("boundary "):
            current["is_boundary"] = True

    return result[:max_lines]