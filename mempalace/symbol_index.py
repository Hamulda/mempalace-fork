#!/usr/bin/env python3
"""
symbol_index.py — Cross-reference index for symbol/import/export lookup.

Provides:
- extract_symbols(): parse source file for defined symbols, imports, exports
- SymbolIndex: SQLite index mapping (symbol_name, file_path, line_start) → metadata
  - find_symbol(name): exact match, returns all (file, line) pairs
  - search_symbols(pattern): SQL LIKE pattern search over symbol names
  - get_file_symbols(file_path): all symbols defined in a file
  - get_callers(symbol_name, project_path): import-based caller heuristic
  - build_index(project_path, file_paths): full index build from file list
  - update_file(file_path, content): extract and upsert symbols for one file

Symbol identity model:
- Primary key: (symbol_name, file_path, line_start)
- Two symbols with the same name at different line numbers in the same file
  are both preserved (no silent overwrites)
- Limitations: regex extraction cannot distinguish class scope from global scope,
  or nested functions with the same name — use tree-sitter/LSP for precise scope

Used by: MCP tools (fastmcp_server.py), wakeup_context for active scope detection.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from .code_index.ast_extractor import extract_code_structure as _extract_code_structure
except ImportError:
    from .symbol_index import extract_symbols as _extract_legacy
    _extract_code_structure = None  # type: ignore[assignment]


# =============================================================================
# SYMBOL EXTRACTION
# =============================================================================

# Language-specific patterns for symbol definition extraction
_PY_DEF_RE = re.compile(r'^(\s*)(def|class|async\s+def)\s+(\w+)', re.MULTILINE)
_PY_IMPORT_RE = re.compile(r'^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))', re.MULTILINE)
_PY_DIRECT_IMPORT_RE = re.compile(r'^from\s+([\w.]+)\s+import\s+([^\n]+)', re.MULTILINE)
_PY_IMPORT_AS_RE = re.compile(r'^import\s+([\w.]+)(?:\s+as\s+\w+)?', re.MULTILINE)
_PY_EXPORT_RE = re.compile(r'^(\w+)\s*=', re.MULTILINE)  # top-level assignments (heuristic)

_JS_FN_RE = re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)', re.MULTILINE)
_JS_CLASS_RE = re.compile(r'^(?:export\s+)?class\s+(\w+)', re.MULTILINE)
_JS_CONST_RE = re.compile(r'^(?:export\s+)?(?:const|let|var)\s+(\w+)', re.MULTILINE)
_JS_IMPORT_RE = re.compile(r'^(?:import|export\s+)\s*(?:\{[^}]*\}|[^\n]+?)\s+from\s+[\'"]([^\'"]+)[\'"]', re.MULTILINE)

_GO_FUNC_RE = re.compile(r'^func\s+(?:\([^)]+\)\s+)?(\w+)', re.MULTILINE)
_GO_IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"', re.MULTILINE)

_RUST_FN_RE = re.compile(r'^(?:pub\s+)?fn\s+(\w+)', re.MULTILINE)
_RUST_STRUCT_RE = re.compile(r'^(?:pub\s+)?struct\s+(\w+)', re.MULTILINE)
_RUST_IMPORT_RE = re.compile(r'^\s*use\s+([\w:]+)', re.MULTILINE)

_GENERIC_DEF_RE = re.compile(r'^(?:public|private|protected|static|abstract|final)?\s*(?:class|interface|enum|struct|func|function)\s+(\w+)', re.MULTILINE)
_GENERIC_IMPORT_RE = re.compile(r'^\s*(?:import|require)\s+[\'"]([^\'"]+)[\'"]', re.MULTILINE)


def _line_number(content: str, char_offset: int) -> int:
    """Convert character offset to 1-based line number."""
    return content[:char_offset].count("\n") + 1


def _extract_py_symbols(content: str) -> dict:
    symbols = []
    imports = []
    direct_imports = []
    exports = []
    file_sig = ""

    for match in _PY_DEF_RE.finditer(content):
        indent, kind, name = match.group(1), match.group(2), match.group(3)
        is_top_level = indent == "" or indent.startswith("    ") is False
        sym_type = "class" if kind == "class" else "function"
        # keyword (def/class) starts after indent whitespace; ^ matches after preceding newline
        keyword_pos = match.start() + len(indent)
        line_num = _line_number(content, keyword_pos)
        symbols.append({"name": name, "type": sym_type, "line": line_num})

    for match in _PY_IMPORT_RE.finditer(content):
        mod = match.group(1) or match.group(2)
        if mod:
            imports.append(mod)

    # Extract direct imports: from module import symbol1, symbol2
    for match in _PY_DIRECT_IMPORT_RE.finditer(content):
        rest = match.group(2)  # everything after "from module import "
        # Split by comma, strip whitespace, remove "as alias" parts
        for part in rest.split(","):
            part = part.strip()
            if not part:
                continue
            # Remove "as alias" suffix
            as_idx = part.find(" as ")
            if as_idx > 0:
                part = part[:as_idx].strip()
            # Skip parenthesized forms (like "from x import (a, b)")
            if part.startswith("(") or part.endswith(")"):
                part = part.strip("()").strip()
            if part:
                direct_imports.append(part)

    for match in _PY_EXPORT_RE.finditer(content):
        name = match.group(1)
        if name[0].isupper() and name not in ("True", "False", "None"):
            exports.append(name)

    # File signature: first docstring or shebang line
    first_lines = content.lstrip()[:500]
    if first_lines.startswith('"""'):
        end = first_lines.find('"""', 3)
        if end > 0:
            file_sig = first_lines[3:end].strip()
    elif first_lines.startswith("'''"):
        end = first_lines.find("'''", 3)
        if end > 0:
            file_sig = first_lines[3:end].strip()
    elif first_lines.startswith("#!"):
        file_sig = first_lines.split("\n", 1)[0].lstrip()[2:].strip()

    return {"symbols": symbols, "imports": imports, "direct_imports": direct_imports, "exports": exports, "file_signature": file_sig}


def _extract_js_symbols(content: str) -> dict:
    symbols = []
    imports = []
    exports = []

    for match in _JS_FN_RE.finditer(content):
        symbols.append({"name": match.group(1), "type": "function", "line": _line_number(content, match.start())})
    for match in _JS_CLASS_RE.finditer(content):
        symbols.append({"name": match.group(1), "type": "class", "line": _line_number(content, match.start())})
    for match in _JS_CONST_RE.finditer(content):
        symbols.append({"name": match.group(1), "type": "const", "line": _line_number(content, match.start())})
    for match in _JS_IMPORT_RE.finditer(content):
        if match.group(1):
            imports.append(match.group(1))

    return {"symbols": symbols, "imports": imports, "exports": exports, "file_signature": ""}


def _extract_go_symbols(content: str) -> dict:
    symbols = []
    imports = []

    for match in _GO_FUNC_RE.finditer(content):
        symbols.append({"name": match.group(1), "type": "function", "line": _line_number(content, match.start())})
    for match in _GO_IMPORT_RE.finditer(content):
        imports.append(match.group(1))

    return {"symbols": symbols, "imports": imports, "exports": [], "file_signature": ""}


def _extract_rust_symbols(content: str) -> dict:
    symbols = []
    imports = []

    for match in _RUST_FN_RE.finditer(content):
        symbols.append({"name": match.group(1), "type": "function", "line": _line_number(content, match.start())})
    for match in _RUST_STRUCT_RE.finditer(content):
        symbols.append({"name": match.group(1), "type": "struct", "line": _line_number(content, match.start())})
    for match in _RUST_IMPORT_RE.finditer(content):
        imports.append(match.group(1))

    return {"symbols": symbols, "imports": imports, "exports": [], "file_signature": ""}


def _extract_generic_symbols(content: str) -> dict:
    symbols = []
    imports = []

    for match in _GENERIC_DEF_RE.finditer(content):
        symbols.append({"name": match.group(1), "type": "definition", "line": _line_number(content, match.start())})
    for match in _GENERIC_IMPORT_RE.finditer(content):
        if match.group(1):
            imports.append(match.group(1))

    return {"symbols": symbols, "imports": imports, "exports": [], "file_signature": ""}


def extract_symbols(content: str, source_file: str) -> dict:
    """
    Extract defined symbols, imports, exports, and file signature from source content.

    Returns dict with keys:
    - symbols: list of {"name", "type", "line"} for functions/classes defined
    - imports: list of module/package names imported
    - exports: list of public names (heuristic: uppercase-first assignments)
    - file_signature: module-level docstring or shebang

    Language is detected from file extension.
    Falls back to generic extraction for unknown extensions.
    """
    ext = Path(source_file).suffix.lower()

    if ext in (".py", ".pyi"):
        return _extract_py_symbols(content)
    elif ext in (".js", ".jsx", ".mjs", ".cjs"):
        return _extract_js_symbols(content)
    elif ext in (".ts", ".tsx", ".mts", ".cts"):
        return _extract_js_symbols(content)
    elif ext == ".go":
        return _extract_go_symbols(content)
    elif ext == ".rs":
        return _extract_rust_symbols(content)
    else:
        return _extract_generic_symbols(content)


# =============================================================================
# SYMBOL INDEX — SQLite cross-reference
# =============================================================================

_SYMBOL_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbol_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_name TEXT NOT NULL,
    symbol_type TEXT,
    file_path TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER,
    file_signature TEXT,
    imports TEXT,
    direct_imports TEXT,
    exports TEXT,
    indexed_at TEXT,
    UNIQUE(symbol_name, file_path, line_start)
);
CREATE INDEX IF NOT EXISTS idx_symbol_name ON symbol_index(symbol_name);
CREATE INDEX IF NOT EXISTS idx_file_path ON symbol_index(file_path);
CREATE INDEX IF NOT EXISTS idx_symbol_file_line ON symbol_index(symbol_name, file_path, line_start);
CREATE INDEX IF NOT EXISTS idx_direct_imports ON symbol_index(direct_imports);
"""

_SYMBOL_INDEX_SCHEMA_V2 = """
ALTER TABLE symbol_index ADD COLUMN parent_symbol TEXT;
ALTER TABLE symbol_index ADD COLUMN symbol_fqn TEXT;
ALTER TABLE symbol_index ADD COLUMN extraction_backend TEXT DEFAULT 'regex';
"""


class SymbolIndex:
    """
    SQLite-backed cross-reference index for symbol/import/export lookup.

    Thread-safe: all operations use a per-instance RLock to serialize access.
    The lock is reentrant so nested calls (e.g. get_callers → find_symbol)
    are safe within the same thread.

    DB file: {palace_path}/symbol_index.sqlite3

    Symbol identity: (symbol_name, file_path, line_start) is the unique key.
    This means two symbols with the same name at different line numbers in
    the same file are both preserved — no silent overwrites.
    """

    _instances: dict[str, "SymbolIndex"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, palace_path: str):
        self.palace_path = palace_path
        self.db_path = str(Path(palace_path).expanduser().resolve() / "symbol_index.sqlite3")
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SYMBOL_INDEX_SCHEMA)

        # Idempotent migration: add parent_symbol, symbol_fqn, extraction_backend
        for stmt in _SYMBOL_INDEX_SCHEMA_V2.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists

        self._conn = conn

    @classmethod
    def get(cls, palace_path: str) -> "SymbolIndex":
        """Return cached SymbolIndex instance for palace_path."""
        with cls._instances_lock:
            if palace_path not in cls._instances:
                cls._instances[palace_path] = cls(palace_path)
            return cls._instances[palace_path]

    def _close(self):
        """Close the SQLite connection. For use in tests or forced reset."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def find_symbol(self, symbol_name: str, exact: bool = False) -> list[dict]:
        """
        Find all definitions of symbol_name (exact, case-sensitive match by default).

        Args:
            symbol_name: name of the symbol to find
            exact: if True, uses COLLATE BINARY for guaranteed case-sensitive
                   comparison; if False (default), uses case-sensitive '='
                   which is also case-sensitive in SQLite for ASCII strings.

        Returns one entry per unique (symbol_name, file_path, line_start) triple.
        Multiple definitions with the same name in the same file at different
        line numbers are all preserved — this is the key improvement over the
        old (symbol_name, file_path) uniqueness model.

        Returns list of dicts with: file_path, line_start, line_end, symbol_type,
        file_signature, imports, exports, parent_symbol, symbol_fqn, extraction_backend.
        """
        with self._lock:
            if not self._conn:
                return []
            try:
                # exact=True uses COLLATE BINARY for guaranteed case-sensitive match
                # exact=False (default) uses '=' which is case-sensitive for ASCII
                collation = "COLLATE BINARY" if exact else ""
                cur = self._conn.execute(
                    f"""SELECT symbol_name, symbol_type, file_path, line_start, line_end,
                              file_signature, imports, exports, parent_symbol, symbol_fqn,
                              extraction_backend
                       FROM symbol_index
                       WHERE symbol_name {collation} = ?
                       ORDER BY file_path""",
                    (symbol_name,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "symbol_name": r[0],
                        "symbol_type": r[1],
                        "file_path": r[2],
                        "line_start": r[3],
                        "line_end": r[4],
                        "file_signature": r[5] or "",
                        "imports": r[6] or "",
                        "exports": r[7] or "",
                        "parent_symbol": r[8],
                        "symbol_fqn": r[9],
                        "extraction_backend": r[10] or "regex",
                    }
                    for r in rows
                ]
            except Exception:
                return []

    def search_symbols(self, pattern: str, limit: int = 100) -> list[dict]:
        """
        Search symbol names matching a SQL LIKE pattern.

        Behavior:
        - No wildcards: wraps pattern in %% (contains match)
        - Starts with ^: treated as prefix match (LIKE 'pattern%')
        - Ends with $: treated as suffix match (LIKE '%pattern')
        - Contains % or _: used as SQL LIKE wildcards directly
        - Scoped pattern "ClassName.method" or "Module.ClassName.method":
            searches for symbol named "method" whose file also contains
            a symbol named "ClassName" (heuristic: method belongs to class)

        Args:
            pattern: search pattern with optional ^/$ anchors
            limit: maximum results to return (default 100)

        Returns list of dicts with symbol_name, file_path, symbol_type, line_start, line_end.
        """
        with self._lock:
            if not self._conn:
                return []
            try:
                # Auto-detect scoped search: "ClassName.method" or "Module.ClassName.method"
                scope_filter = None
                search_pattern = pattern
                if "." in pattern and not pattern.startswith("^") and "%" not in pattern:
                    parts = pattern.rsplit(".", 1)
                    if len(parts) == 2 and parts[1]:
                        scope_filter = parts[0]  # e.g., "ClassName" or "Module.ClassName"
                        search_pattern = parts[1]  # the symbol name to search

                # Build LIKE clause for symbol name
                if search_pattern.startswith("^") and search_pattern.endswith("$"):
                    where_clause = "symbol_name LIKE ?"
                    like_pattern = search_pattern[1:-1] + "%"
                elif search_pattern.startswith("^"):
                    where_clause = "symbol_name LIKE ?"
                    like_pattern = search_pattern[1:] + "%"
                elif search_pattern.endswith("$"):
                    where_clause = "symbol_name LIKE ?"
                    like_pattern = "%" + search_pattern[:-1]
                elif "%" in search_pattern or "_" in search_pattern:
                    where_clause = "symbol_name LIKE ?"
                    like_pattern = search_pattern
                else:
                    where_clause = "symbol_name LIKE ?"
                    like_pattern = f"%{search_pattern}%"

                cur = self._conn.execute(
                    f"""SELECT symbol_name, symbol_type, file_path, line_start, line_end
                       FROM symbol_index
                       WHERE {where_clause}
                       ORDER BY symbol_name
                       LIMIT {limit}""",
                    (like_pattern,),
                )
                rows = cur.fetchall()
                results = [
                    {
                        "symbol_name": r[0],
                        "symbol_type": r[1],
                        "file_path": r[2],
                        "line_start": r[3],
                        "line_end": r[4],
                    }
                    for r in rows
                ]

                # Apply scope filter by checking if the containing file
                # has a symbol matching the scope name (heuristic)
                if scope_filter:
                    scope_parts = scope_filter.split(".")
                    target_scope = scope_parts[-1]  # the class name to look for
                    filtered = []
                    for r in results:
                        file_syms = self.get_file_symbols(r["file_path"])
                        file_symbol_names = {s["name"] for s in file_syms.get("symbols", [])}
                        if target_scope in file_symbol_names:
                            filtered.append(r)
                    return filtered

                return results
            except Exception:
                return []

    def get_file_symbols(self, file_path: str) -> dict:
        """
        Get all symbols defined in a file.

        Returns dict with keys:
        - symbols: list of {name, type, line_start, line_end, parent, fqn} for each
          distinct (name, line_start) pair. Multiple definitions with the same name
          at different lines are all included. parent/fqn may be None for regex-only.
        - imports: list of module/package names imported by this file
        - exports: list of public names (uppercase-first assignments, heuristic)
        - file_signature: module-level docstring or shebang
        - extraction_backend: "tree_sitter" or "regex" (first symbol's backend)
        - parent_symbols: list of distinct parent class/function names (may be empty)
        - fqns: list of distinct fully-qualified names (may be empty)

        Note: line_start is 1-based (matching editor convention).
        """
        with self._lock:
            if not self._conn:
                return {"symbols": [], "imports": [], "exports": [], "file_signature": "", "extraction_backend": "regex", "parent_symbols": [], "fqns": []}
            try:
                cur = self._conn.execute(
                    """SELECT symbol_name, symbol_type, line_start, line_end,
                              imports, exports, file_signature,
                              parent_symbol, symbol_fqn, extraction_backend
                       FROM symbol_index
                       WHERE file_path = ?
                       ORDER BY line_start""",
                    (file_path,),
                )
                rows = cur.fetchall()
                if not rows:
                    return {"symbols": [], "imports": [], "exports": [], "file_signature": "", "extraction_backend": "regex", "parent_symbols": [], "fqns": []}
                symbols = [
                    {
                        "name": r[0],
                        "type": r[1],
                        "line_start": r[2],
                        "line_end": r[3],
                        "parent": r[7],
                        "fqn": r[8],
                    }
                    for r in rows
                ]
                imports_raw = rows[0][4] or ""
                exports_raw = rows[0][5] or ""
                file_sig = rows[0][6] or ""
                backend = rows[0][9] or "regex"
                imports = imports_raw.split(",") if imports_raw else []
                exports = exports_raw.split(",") if exports_raw else []
                parent_symbols = list({r[7] for r in rows if r[7]})
                fqns = list({r[8] for r in rows if r[8]})
                return {
                    "symbols": symbols,
                    "imports": [i for i in imports if i],
                    "exports": [e for e in exports if e],
                    "file_signature": file_sig,
                    "extraction_backend": backend,
                    "parent_symbols": parent_symbols,
                    "fqns": fqns,
                }
            except Exception:
                return {"symbols": [], "imports": [], "exports": [], "file_signature": ""}

    def get_callers(self, symbol_name: str, project_path: str) -> list[dict]:
        """
        Find files that reference a symbol, using import-based heuristics.

        This is a BEST-EFFORT heuristic that:
        1. Resolves symbol's file to a module name (relative to project_path)
        2. Searches for files that:
           - directly import the symbol (direct_imports LIKE %symbol_name%) — checked FIRST
           - import the symbol's module (imports LIKE %module%) — checked SECOND

        Returns list of dicts with: file_path, imported_module, called_symbol,
        import_type ("direct" or "module").

        Performance: 2 SQL queries total (was 1 + 2N where N = definition count).
        """
        with self._lock:
            if not self._conn:
                return []

        defs = self.find_symbol(symbol_name)
        if not defs:
            return []

        callers = []
        seen = set()

        # Build module names per definition file
        module_map = {}  # def_file -> module_name
        for def_ in defs:
            def_file = def_["file_path"]
            try:
                rel = Path(def_file).relative_to(project_path).as_posix()
            except ValueError:
                rel = def_file
            module_map[def_file] = str(Path(rel).with_suffix("")).replace("/", ".").replace("\\", ".")

        all_def_files = set(module_map.keys())

        # Batch query 1: all direct imports matching symbol_name across ALL definitions
        # (was run N times — one per definition)
        try:
            cur = self._conn.execute(
                """SELECT file_path, direct_imports FROM symbol_index
                   WHERE direct_imports LIKE ? LIMIT 50""",
                (f"%{symbol_name}%",),
            )
            for row in cur.fetchall():
                fp = row[0]
                if fp in all_def_files:
                    continue
                if fp not in seen:
                    seen.add(fp)
                    def_file = next(iter(all_def_files), "")
                    callers.append({
                        "file_path": fp,
                        "imported_module": module_map.get(def_file, ""),
                        "called_symbol": symbol_name,
                        "import_type": "direct",
                    })
        except Exception:
            pass

        # Batch query 2: all module imports across ALL definitions in ONE SQL query
        # (was run N times — one per definition)
        if module_map:
            module_names = list(module_map.values())
            # Build OR chain for all module names
            like_clauses = " OR ".join(["imports LIKE ?"] * len(module_names))
            params = [f"%{mn}%" for mn in module_names]
            try:
                cur = self._conn.execute(
                    f"""SELECT file_path, imports FROM symbol_index
                        WHERE ({like_clauses}) LIMIT 50""",
                    params,
                )
                for row in cur.fetchall():
                    fp = row[0]
                    if fp in all_def_files:
                        continue
                    if fp not in seen:
                        seen.add(fp)
                        # Find which module_name matched for this file
                        imports_str = row[1] or ""
                        matched_module = next(
                            (mn for mn in module_names if mn in imports_str), module_names[0]
                        )
                        callers.append({
                            "file_path": fp,
                            "imported_module": matched_module,
                            "called_symbol": symbol_name,
                            "import_type": "module",
                        })
            except Exception:
                pass

        return callers[:20]

    def update_file(self, file_path: str, content: str):
        """
        Extract symbols from content and upsert into index.

        Uses extract_code_structure (AST-aware) when available, falls back
        to legacy extract_symbols (regex-based) otherwise.

        Handles files with no symbol definitions (import-only files) by
        storing a single placeholder row so imports are preserved for
        get_callers lookups.
        """
        with self._lock:
            if not self._conn:
                return
            try:
                # Prefer AST-aware extraction when available
                if _extract_code_structure is not None:
                    extracted = _extract_code_structure(content, file_path)
                    backend = extracted.get("extraction_backend", "tree_sitter")
                else:
                    extracted = extract_symbols(content, file_path)
                    backend = "regex"

                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

                self._conn.execute("DELETE FROM symbol_index WHERE file_path = ?", (file_path,))

                symbols = extracted.get("symbols", [])
                imports_str = ",".join(extracted.get("imports", []))
                direct_imports_str = ",".join(extracted.get("direct_imports", []))
                exports_str = ",".join(extracted.get("exports", []))
                file_sig = extracted.get("file_signature", "")

                if symbols:
                    for sym in symbols:
                        line_start = sym.get("line_start", sym.get("line", 0))
                        line_end = sym.get("line_end", line_start + 1)
                        parent_sym = sym.get("parent")
                        symbol_fqn = sym.get("fqn")
                        self._conn.execute(
                            """INSERT OR REPLACE INTO symbol_index
                               (symbol_name, symbol_type, file_path, line_start, line_end,
                                file_signature, imports, direct_imports, exports, indexed_at,
                                parent_symbol, symbol_fqn, extraction_backend)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                sym["name"],
                                sym.get("type", sym.get("kind", "definition")),
                                file_path,
                                line_start,
                                line_end,
                                file_sig,
                                imports_str,
                                direct_imports_str,
                                exports_str,
                                now,
                                parent_sym,
                                symbol_fqn,
                                backend,
                            ),
                        )
                elif imports_str:
                    # Import-only file: insert a placeholder row with empty symbol_name
                    # so the imports column is preserved for get_callers lookups.
                    # line_start=0 indicates this is a file-level import marker.
                    self._conn.execute(
                        """INSERT OR REPLACE INTO symbol_index
                           (symbol_name, symbol_type, file_path, line_start, line_end,
                            file_signature, imports, direct_imports, exports, indexed_at,
                            parent_symbol, symbol_fqn, extraction_backend)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "",  # empty symbol_name = import-only file marker
                            "imports",
                            file_path,
                            0,
                            0,
                            file_sig,
                            imports_str,
                            direct_imports_str,
                            exports_str,
                            now,
                            None,
                            None,
                            backend,
                        ),
                    )
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass

    def build_index(self, project_path: str, file_paths: list[str]):
        """
        Build full index from a list of file paths.
        Call this on first mine or full re-mine.
        """
        for fp in file_paths:
            try:
                content = Path(fp).read_text(encoding="utf-8", errors="replace")
                self.update_file(fp, content)
            except Exception:
                continue

    def clear(self):
        """Clear all entries from the index."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("DELETE FROM symbol_index")
                    self._conn.commit()
                except Exception:
                    pass

    def list_indexed_files(self) -> set[str]:
        """
        Return the set of all file paths currently in the index.

        Public API — diagnostics should use this instead of accessing _conn/_lock.
        """
        with self._lock:
            if not self._conn:
                return set()
            try:
                cur = self._conn.execute("SELECT DISTINCT file_path FROM symbol_index")
                return {row[0] for row in cur.fetchall()}
            except Exception:
                return set()

    def stats(self) -> dict:
        """Return index statistics."""
        with self._lock:
            if not self._conn:
                return {"total_symbols": 0, "total_files": 0}
            try:
                cur = self._conn.execute("SELECT COUNT(*), COUNT(DISTINCT file_path) FROM symbol_index")
                row = cur.fetchone()
                return {"total_symbols": row[0] or 0, "total_files": row[1] or 0}
            except Exception:
                return {"total_symbols": 0, "total_files": 0}