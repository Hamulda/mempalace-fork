#!/usr/bin/env python3
"""
symbol_index.py — Cross-reference index for symbol/import/export lookup.

Provides:
- extract_symbols(): parse source file for defined symbols, imports, exports
- SymbolIndex: SQLite index mapping symbol_name → file locations
  - find_symbol(name): exact or partial match
  - search_symbols(pattern): regex search over symbol names
  - get_file_symbols(file_path): all symbols defined in a file
  - build_index(project_path): full index build from mined files
  - update_index(file_path): incremental update for one file

Used by: MCP tools (fastmcp_server.py), wakeup_context for active scope detection.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional


# =============================================================================
# SYMBOL EXTRACTION
# =============================================================================

# Language-specific patterns for symbol definition extraction
_PY_DEF_RE = re.compile(r'^(\s*)(def|class|async\s+def)\s+(\w+)', re.MULTILINE)
_PY_IMPORT_RE = re.compile(r'^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))', re.MULTILINE)
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

    return {"symbols": symbols, "imports": imports, "exports": exports, "file_signature": file_sig}


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
    line_start INTEGER,
    line_end INTEGER,
    file_signature TEXT,
    imports TEXT,
    exports TEXT,
    indexed_at TEXT,
    UNIQUE(symbol_name, file_path)
);
CREATE INDEX IF NOT EXISTS idx_symbol_name ON symbol_index(symbol_name);
CREATE INDEX IF NOT EXISTS idx_file_path ON symbol_index(file_path);
"""


class SymbolIndex:
    """
    SQLite-backed cross-reference index for symbol/import/export lookup.
    Thread-safe: all operations use per-instance lock to serialize access.

    DB file: {palace_path}/symbol_index.sqlite3
    """

    _instances: dict[str, "SymbolIndex"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, palace_path: str):
        self.palace_path = palace_path
        self.db_path = str(Path(palace_path).expanduser().resolve() / "symbol_index.sqlite3")
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SYMBOL_INDEX_SCHEMA)
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

    def find_symbol(self, symbol_name: str) -> list[dict]:
        """
        Find all definitions of symbol_name (exact match).
        Returns list of dicts with file_path, line_start, line_end, symbol_type, file_signature.
        """
        with self._lock:
            if not self._conn:
                return []
            try:
                cur = self._conn.execute(
                    """SELECT symbol_name, symbol_type, file_path, line_start, line_end,
                              file_signature, imports, exports
                       FROM symbol_index
                       WHERE symbol_name = ?
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
                    }
                    for r in rows
                ]
            except Exception:
                return []

    def search_symbols(self, pattern: str) -> list[dict]:
        """
        Search symbol names matching regex pattern.
        Returns list of dicts with symbol_name, file_path, symbol_type.
        """
        with self._lock:
            if not self._conn:
                return []
            try:
                if pattern.startswith("^"):
                    where_clause = "symbol_name LIKE ?"
                    like_pattern = pattern[1:] + "%"
                elif "%" in pattern or "_" in pattern:
                    where_clause = "symbol_name LIKE ?"
                    like_pattern = pattern
                else:
                    where_clause = "symbol_name LIKE ?"
                    like_pattern = f"%{pattern}%"

                cur = self._conn.execute(
                    f"""SELECT symbol_name, symbol_type, file_path, line_start, line_end
                       FROM symbol_index
                       WHERE {where_clause}
                       ORDER BY symbol_name
                       LIMIT 100""",
                    (like_pattern,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "symbol_name": r[0],
                        "symbol_type": r[1],
                        "file_path": r[2],
                        "line_start": r[3],
                        "line_end": r[4],
                    }
                    for r in rows
                ]
            except Exception:
                return []

    def get_file_symbols(self, file_path: str) -> dict:
        """
        Get all symbols defined in a file.
        Returns dict with symbols, imports, exports, file_signature.
        """
        with self._lock:
            if not self._conn:
                return {"symbols": [], "imports": [], "exports": [], "file_signature": ""}
            try:
                cur = self._conn.execute(
                    """SELECT symbol_name, symbol_type, line_start, line_end, imports, exports, file_signature
                       FROM symbol_index
                       WHERE file_path = ?
                       ORDER BY line_start""",
                    (file_path,),
                )
                rows = cur.fetchall()
                if not rows:
                    return {"symbols": [], "imports": [], "exports": [], "file_signature": ""}
                symbols = [
                    {"name": r[0], "type": r[1], "line_start": r[2], "line_end": r[3]}
                    for r in rows
                ]
                imports_raw = rows[0][4] or ""
                exports_raw = rows[0][5] or ""
                file_sig = rows[0][6] or ""
                imports = imports_raw.split(",") if imports_raw else []
                exports = exports_raw.split(",") if exports_raw else []
                return {
                    "symbols": symbols,
                    "imports": [i for i in imports if i],
                    "exports": [e for e in exports if e],
                    "file_signature": file_sig,
                }
            except Exception:
                return {"symbols": [], "imports": [], "exports": [], "file_signature": ""}

    def get_callers(self, symbol_name: str, project_path: str) -> list[dict]:
        """
        Find files that reference a symbol (via import statement or text occurrence).
        Returns list of dicts with file_path, line, context.
        """
        with self._lock:
            if not self._conn:
                return []

        defs = self.find_symbol(symbol_name)
        if not defs:
            return []

        callers = []
        for def_ in defs:
            def_file = def_["file_path"]
            try:
                rel = Path(def_file).relative_to(project_path).as_posix()
            except ValueError:
                rel = def_file

            module_name = str(Path(rel).with_suffix("")).replace("/", ".").replace("\\", ".")

            with self._lock:
                if not self._conn:
                    break
                try:
                    cur = self._conn.execute(
                        """SELECT file_path, imports FROM symbol_index WHERE imports LIKE ? LIMIT 50""",
                        (f"%{module_name}%",),
                    )
                    for row in cur.fetchall():
                        if row[0] != def_file:
                            callers.append({
                                "file_path": row[0],
                                "imported_module": module_name,
                                "called_symbol": symbol_name,
                            })
                except Exception:
                    break
        return callers[:20]

    def update_file(self, file_path: str, content: str):
        """
        Extract symbols from content and upsert into index.
        """
        with self._lock:
            if not self._conn:
                return
            try:
                extracted = extract_symbols(content, file_path)
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

                self._conn.execute("DELETE FROM symbol_index WHERE file_path = ?", (file_path,))

                for sym in extracted.get("symbols", []):
                    imports_str = ",".join(extracted.get("imports", []))
                    exports_str = ",".join(extracted.get("exports", []))
                    self._conn.execute(
                        """INSERT OR REPLACE INTO symbol_index
                           (symbol_name, symbol_type, file_path, line_start, line_end,
                            file_signature, imports, exports, indexed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            sym["name"],
                            sym.get("type", "definition"),
                            file_path,
                            sym.get("line", 0),
                            0,
                            extracted.get("file_signature", ""),
                            imports_str,
                            exports_str,
                            now,
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