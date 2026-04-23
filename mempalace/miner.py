#!/usr/bin/env python3
"""
miner.py — Files everything into the palace.

Reads mempalace.yaml from the project directory to know the wing + rooms.
Routes each file to the right room based on content.
Stores verbatim chunks as drawers. No summaries. Ever.

Storage: Uses get_backend(config.backend) — LanceDB is canonical primary,
ChromaDB is legacy compat via the same abstraction layer.
"""

import os
import sys
import hashlib
import fnmatch
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from .config import MempalaceConfig
from .backends import get_backend
from .palace import SKIP_DIRS
from .symbol_index import SymbolIndex

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
        if len(chunk_content) < MIN_CHUNK_SIZE:
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


def process_file(
    filepath: Path,
    project_path: Path,
    collection,
    wing: str,
    rooms: list,
    agent: str,
    dry_run: bool,
    palace_path: str = None,
) -> tuple:
    """Read, chunk, route, and file one file. Returns (drawer_count, room_name).

    Implements revision-based ingest:
    - Existing chunks for this source_file are looked up (is_latest=True)
    - New chunks that supersede old ones set is_latest=False on old, supersedes_id on new
    - Content-hash matching prevents unnecessary tombstones when content is unchanged
    """

    source_file = str(filepath)

    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, None

    content = content.strip()
    if len(content) < MIN_CHUNK_SIZE:
        return 0, None

    room = detect_room(filepath, content, rooms, project_path)

    # Use code-aware chunking
    chunks = chunk_with_metadata(content, source_file)
    if not chunks:
        return 0, None

    if dry_run:
        print(f"    [DRY RUN] {filepath.name} → room:{room} ({len(chunks)} drawers)")
        return len(chunks), room

    # Compute revision_id for this file
    revision_id = _compute_file_revision(source_file, content)
    timestamp = datetime.utcnow().isoformat() + "Z"

    try:
        source_mtime = os.path.getmtime(source_file)
    except OSError:
        source_mtime = None

    # Revision-based ingest: tombstone old chunks for this source_file
    try:
        existing = collection.get(
            where={"source_file": source_file, "is_latest": True},
            include=["metadatas", "ids"]
        )
    except Exception:
        existing = {"ids": [], "metadatas": []}

    # Build mapping from content_hash -> list of (old_id, old_meta)
    # Using a list instead of dict to handle duplicate hashes at different positions.
    # When multiple old chunks share the same content_hash, all must be superseded.
    old_chunks_by_hash: dict[str, list[tuple]] = defaultdict(list)
    if existing and existing.get("ids"):
        for old_id, old_meta in zip(existing["ids"], existing["metadatas"]):
            if old_meta:
                old_hash = old_meta.get("content_hash", "")
                if old_hash:
                    old_chunks_by_hash[old_hash].append((old_id, old_meta))

    # Prepare new chunks with code-aware metadata
    documents, ids, metadatas = [], [], []
    for idx, chunk in enumerate(chunks):
        content_hash = _compute_content_hash(chunk["content"])
        drawer_id = f"drawer_{wing}_{room}_{hashlib.sha256((source_file + str(idx)).encode()).hexdigest()[:24]}"

        metadata = {
            "wing": wing,
            "room": room,
            "source_file": source_file,
            "chunk_index": idx,
            "added_by": agent,
            "agent_id": agent,
            "timestamp": timestamp,
            "origin_type": "observation",
            "is_latest": True,
            "supersedes_id": "",
            # Code-aware fields
            "language": detect_language(source_file),
            "line_start": chunk.get("line_start", 0),
            "line_end": chunk.get("line_end", 0),
            "symbol_name": chunk.get("symbol_name", ""),
            "symbol_scope": chunk.get("symbol_scope", ""),
            "chunk_kind": chunk.get("chunk_kind", "mixed"),
            "revision_id": revision_id,
            "content_hash": content_hash,
        }
        if source_mtime is not None:
            metadata["source_mtime"] = source_mtime

        # Tombstone: find old chunk(s) with same content_hash.
        # ALL matching old chunks are superseded by this new chunk.
        # We store all superseded IDs as pipe-separated string (single-string contract).
        superseded_ids_for_chunk = []
        if content_hash in old_chunks_by_hash:
            for old_id, old_meta in old_chunks_by_hash[content_hash]:
                superseded_ids_for_chunk.append(old_id)
            # Store all IDs as pipe-separated string; parse during tombstoning.
            metadata["supersedes_id"] = "|".join(superseded_ids_for_chunk)
            metadata["is_latest"] = True

        documents.append(chunk["content"])
        ids.append(drawer_id)
        metadatas.append(metadata)

    # Upsert new chunks
    collection.upsert(documents=documents, ids=ids, metadatas=metadatas)

    # Tombstone old chunks that have no matching new content_hash.
    # An old chunk is tombstoned when it was not matched by any new chunk.
    # This is now a set operation: all old IDs minus superseded IDs = survivors to tombstone.
    all_old_ids = set()
    for old_list in old_chunks_by_hash.values():
        for old_id, _ in old_list:
            all_old_ids.add(old_id)

    # Superseded IDs are those referenced by new chunks via supersedes_id.
    # supersedes_id is pipe-separated when multiple old chunks share the same hash.
    superseded_ids = set()
    for meta in metadatas:
        raw = meta.get("supersedes_id", "")
        if raw:
            for sid in raw.split("|"):
                if sid:
                    superseded_ids.add(sid)

    # Tombstone every old chunk that was NOT superseded — single batch upsert
    tombstone_ids, tombstone_docs, tombstone_metas = [], [], []
    for old_hash, old_list in old_chunks_by_hash.items():
        for old_id, old_meta in old_list:
            if old_id not in superseded_ids:
                tombstone_ids.append(old_id)
                tombstone_docs.append(
                    old_meta.get("source_content", old_meta.get("document", ""))
                )
                tombstone_metas.append({"is_latest": False})

    if tombstone_ids:
        try:
            collection.upsert(
                documents=tombstone_docs,
                ids=tombstone_ids,
                metadatas=tombstone_metas,
            )
        except Exception:
            pass  # Best-effort tombstone

    return len(documents), room


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


def mine(
    project_dir: str,
    palace_path: str,
    wing_override: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    respect_gitignore: bool = True,
    include_ignored: list = None,
):
    """Mine a project directory into the palace."""

    project_path = Path(project_dir).expanduser().resolve()
    config = load_config(project_dir)

    wing = wing_override or config["wing"]
    rooms = config.get("rooms", [{"name": "general", "description": "All project files"}])

    files = scan_project(
        project_dir,
        respect_gitignore=respect_gitignore,
        include_ignored=include_ignored,
    )
    if limit > 0:
        files = files[:limit]

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Rooms:   {', '.join(r['name'] for r in rooms)}")
    print(f"  Files:   {len(files)}")
    print(f"  Palace:  {palace_path}")
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

    for i, filepath in enumerate(files, 1):
        drawers, room = process_file(
            filepath=filepath,
            project_path=project_path,
            collection=collection,
            wing=wing,
            rooms=rooms,
            agent=agent,
            dry_run=dry_run,
            palace_path=palace_path,
        )
        if drawers == 0 and not dry_run:
            files_skipped += 1
        else:
            total_drawers += drawers
            room_counts[room] += 1
            if not dry_run:
                print(f"  ✓ [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers}")

    # Build cross-reference symbol index for all files
    if not dry_run and files:
        try:
            si = SymbolIndex.get(palace_path)
            si.build_index(str(project_path), [str(f) for f in files])
            stats = si.stats()
            print(f"  Symbol index: {stats['total_symbols']} symbols, {stats['total_files']} files")
        except Exception:
            pass

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {len(files) - files_skipped}")
    print(f"  Files skipped (already filed): {files_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    print("\n  By room:")
    for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    {room:20} {count} files")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")


# =============================================================================
# STATUS
# =============================================================================


def status(palace_path: str):
    """Show what's been filed in the palace.

    Uses canonical backend factory — Lance is primary, Chroma is legacy compat.
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
