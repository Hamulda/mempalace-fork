"""
ast_extractor.py — AST-aware code structure extraction with regex fallback.

Provides extract_code_structure() which returns rich symbol metadata including:
  - symbols: name, kind, line_start, line_end, parent, fqn
  - imports, exports, file_signature
  - extraction_backend: "tree_sitter" or "regex"

Tree-sitter is optional: if import fails, falls back to existing regex logic.
Supports Python first; JS/TS if tree-sitter parsers are available.

Usage:
    from mempalace.code_index.ast_extractor import extract_code_structure
    result = extract_code_structure(content, source_file)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# ── tree-sitter availability ───────────────────────────────────────────────────

_TS_AVAILABLE: bool | None = None


def _ensure_tree_sitter() -> bool:
    """Ensure tree-sitter is importable. Returns True if available."""
    global _TS_AVAILABLE
    if _TS_AVAILABLE is not None:
        return _TS_AVAILABLE
    try:
        import tree_sitter  # noqa: F401
        from tree_sitter_languages import get_language  # noqa: F401
        _TS_AVAILABLE = True
        return True
    except ImportError:
        _TS_AVAILABLE = False
        return False


# ── Regex extraction helpers ──────────────────────────────────────────────────

def _line_number(content: str, char_offset: int) -> int:
    return content[:char_offset].count("\n") + 1


_PY_DEF_RE = re.compile(r"^(\s*)(def|class|async\s+def)\s+(\w+)", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE)
_PY_DIRECT_IMPORT_RE = re.compile(r"^from\s+([\w.]+)\s+import\s+([^\n]+)", re.MULTILINE)
_PY_EXPORT_RE = re.compile(r"^(\w+)\s*=", re.MULTILINE)

_JS_FN_RE = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE)
_JS_CLASS_RE = re.compile(r"^(?:export\s+)?class\s+(\w+)", re.MULTILINE)
_JS_CONST_RE = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)", re.MULTILINE)
_JS_IMPORT_RE = re.compile(r"^(?:import|export\s+)\s*(?:\{[^}]*\}|[^\n]+?)\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE)

_GO_FUNC_RE = re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)", re.MULTILINE)
_GO_IMPORT_RE = re.compile(r"^\s*import\s+\"([^\"]+)\"", re.MULTILINE)

_RUST_FN_RE = re.compile(r"^(?:pub\s+)?fn\s+(\w+)", re.MULTILINE)
_RUST_STRUCT_RE = re.compile(r"^(?:pub\s+)?struct\s+(\w+)", re.MULTILINE)
_RUST_IMPORT_RE = re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE)

_GENERIC_DEF_RE = re.compile(
    r"^(?:public|private|protected|static|abstract|final)?\s*(?:class|interface|enum|struct|func|function)\s+(\w+)",
    re.MULTILINE,
)
_GENERIC_IMPORT_RE = re.compile(r"^\s*(?:import|require)\s+['\"]([^'\"]+)['\"]", re.MULTILINE)


def _file_signature(content: str) -> str:
    first = content.lstrip()[:500]
    if first.startswith('"""'):
        end = first.find('"""', 3)
        if end > 0:
            return first[3:end].strip()
    if first.startswith("'''"):
        end = first.find("'''", 3)
        if end > 0:
            return first[3:end].strip()
    if first.startswith("#!"):
        return first.split("\n", 1)[0].lstrip()[2:].strip()
    return ""


# ── tree-sitter Python extraction ─────────────────────────────────────────────

def _extract_py_tree_sitter(content: str) -> dict:
    """Extract Python symbols with parent/fqn using tree-sitter."""
    from tree_sitter_languages import get_language

    lang = get_language("python")

    # Build parent map via DFS
    def _build_parent_map(node) -> dict:
        result = {}
        stack = [(node, None)]
        while stack:
            n, parent = stack.pop()
            result[id(n)] = parent
            for child in n.children:
                stack.append((child, n))
        return result

    try:
        tree = lang.parse(bytes(content, "utf-8"))
    except Exception:
        return {
            "symbols": [],
            "imports": [],
            "direct_imports": [],
            "exports": [],
            "file_signature": "",
            "extraction_backend": "tree_sitter",
        }

    root = tree.root_node
    parent_map = _build_parent_map(root)

    def _child(node, *types):
        for c in node.children:
            if c.type in types:
                return c
        return None

    def _node_text(node):
        return node.text.decode("utf-8") if node else ""

    def _line(node):
        return (node.start_point[0] + 1) if node else 0

    def _end_line(node):
        return (node.end_point[0] + 1) if node else 0

    def _fqn_prefix(node_id) -> list[str]:
        parts = []
        parent_id = parent_map.get(node_id)
        while parent_id is not None:
            for n in (root.preorder()):
                if id(n) == parent_id:
                    if n.type in ("function_definition", "async_generator_function_definition", "class_definition"):
                        name_node = _child(n, "identifier")
                        if name_node:
                            parts.insert(0, _node_text(name_node))
                    parent_id = parent_map.get(parent_id)
                    break
            else:
                break
        return parts

    symbols = []
    seen_ids = set()

    for node in root.preorder():
        if id(node) in seen_ids:
            continue
        seen_ids.add(id(node))

        if node.type not in ("function_definition", "async_generator_function_definition", "class_definition"):
            continue

        name_node = _child(node, "identifier")
        if not name_node:
            continue

        name = _node_text(name_node)

        # Build fqn prefix from ancestors
        prefix_parts = []
        cur_id = parent_map.get(id(node))
        while cur_id is not None:
            for n in (root.preorder()):
                if id(n) == cur_id:
                    if n.type in ("function_definition", "async_generator_function_definition", "class_definition"):
                        nn = _child(n, "identifier")
                        if nn:
                            prefix_parts.insert(0, _node_text(nn))
                    cur_id = parent_map.get(cur_id)
                    break
            else:
                break

        fqn = ".".join(prefix_parts + [name]) if prefix_parts else name

        parent_name = prefix_parts[-1] if prefix_parts else None

        kind = "class" if node.type == "class_definition" else "function"
        if node.type == "async_generator_function_definition":
            kind = "async_generator"

        symbols.append({
            "name": name,
            "kind": kind,
            "line_start": _line(node),
            "line_end": _end_line(node),
            "parent": parent_name,
            "fqn": fqn,
        })

    # Imports via tree-sitter
    imports = []
    direct_imports = []
    for node in root.preorder():
        if node.type == "import_statement":
            for c in node.children:
                if c.type == "dotted_name":
                    imports.append(_node_text(c))
                elif c.type == "aliased_import":
                    dn = _child(c, "dotted_name")
                    if dn:
                        imports.append(_node_text(dn))
        elif node.type == "import_from_statement":
            mod = _child(node, "dotted_name")
            if mod:
                imports.append(_node_text(mod))
            for c in node.children:
                if c.type == "identifier":
                    direct_imports.append(_node_text(c))
                elif c.type == "import_as_name":
                    id_node = _child(c, "identifier")
                    if id_node:
                        direct_imports.append(_node_text(id_node))

    # Exports: top-level uppercase assignments
    exports = []
    for node in root.preorder():
        if node.type == "assignment":
            parent_node = parent_map.get(id(node))
            if parent_node and parent_node.type == "module":
                name_node = _child(node, "identifier")
                if name_node:
                    n = _node_text(name_node)
                    if n[0].isupper() and n not in ("True", "False", "None"):
                        exports.append(n)

    return {
        "symbols": symbols,
        "imports": imports,
        "direct_imports": direct_imports,
        "exports": exports,
        "file_signature": _file_signature(content),
        "extraction_backend": "tree_sitter",
    }


# ── Regex extractors (mirror symbol_index.py behavior) ───────────────────────

def _extract_py_regex(content: str) -> dict:
    symbols = []
    imports = []
    direct_imports = []
    exports = []

    for match in _PY_DEF_RE.finditer(content):
        indent, kind, name = match.group(1), match.group(2), match.group(3)
        sym_type = "class" if kind == "class" else "function"
        keyword_pos = match.start() + len(indent)
        line_num = _line_number(content, keyword_pos)
        symbols.append({
            "name": name,
            "kind": sym_type,
            "line_start": line_num,
            "line_end": line_num + 1,
            "parent": None,
            "fqn": name,
        })

    for match in _PY_IMPORT_RE.finditer(content):
        mod = match.group(1) or match.group(2)
        if mod:
            imports.append(mod)

    for match in _PY_DIRECT_IMPORT_RE.finditer(content):
        rest = match.group(2)
        for part in rest.split(","):
            part = part.strip()
            if not part:
                continue
            as_idx = part.find(" as ")
            if as_idx > 0:
                part = part[:as_idx].strip()
            if part.startswith("(") or part.endswith(")"):
                part = part.strip("()").strip()
            if part:
                direct_imports.append(part)

    for match in _PY_EXPORT_RE.finditer(content):
        name = match.group(1)
        if name[0].isupper() and name not in ("True", "False", "None"):
            exports.append(name)

    return {
        "symbols": symbols,
        "imports": imports,
        "direct_imports": direct_imports,
        "exports": exports,
        "file_signature": _file_signature(content),
        "extraction_backend": "regex",
    }


def _extract_js_regex(content: str) -> dict:
    symbols = []
    imports = []

    for match in _JS_FN_RE.finditer(content):
        symbols.append({
            "name": match.group(1),
            "kind": "function",
            "line_start": _line_number(content, match.start()),
            "line_end": _line_number(content, match.start()) + 1,
            "parent": None,
            "fqn": match.group(1),
        })
    for match in _JS_CLASS_RE.finditer(content):
        symbols.append({
            "name": match.group(1),
            "kind": "class",
            "line_start": _line_number(content, match.start()),
            "line_end": _line_number(content, match.start()) + 1,
            "parent": None,
            "fqn": match.group(1),
        })
    for match in _JS_CONST_RE.finditer(content):
        symbols.append({
            "name": match.group(1),
            "kind": "const",
            "line_start": _line_number(content, match.start()),
            "line_end": _line_number(content, match.start()) + 1,
            "parent": None,
            "fqn": match.group(1),
        })
    for match in _JS_IMPORT_RE.finditer(content):
        if match.group(1):
            imports.append(match.group(1))

    return {
        "symbols": symbols,
        "imports": imports,
        "direct_imports": [],
        "exports": [],
        "file_signature": "",
        "extraction_backend": "regex",
    }


def _extract_go_regex(content: str) -> dict:
    symbols = []
    imports = []

    for match in _GO_FUNC_RE.finditer(content):
        symbols.append({
            "name": match.group(1),
            "kind": "function",
            "line_start": _line_number(content, match.start()),
            "line_end": _line_number(content, match.start()) + 1,
            "parent": None,
            "fqn": match.group(1),
        })
    for match in _GO_IMPORT_RE.finditer(content):
        imports.append(match.group(1))

    return {
        "symbols": symbols,
        "imports": imports,
        "direct_imports": [],
        "exports": [],
        "file_signature": "",
        "extraction_backend": "regex",
    }


def _extract_rust_regex(content: str) -> dict:
    symbols = []
    imports = []

    for match in _RUST_FN_RE.finditer(content):
        symbols.append({
            "name": match.group(1),
            "kind": "function",
            "line_start": _line_number(content, match.start()),
            "line_end": _line_number(content, match.start()) + 1,
            "parent": None,
            "fqn": match.group(1),
        })
    for match in _RUST_STRUCT_RE.finditer(content):
        symbols.append({
            "name": match.group(1),
            "kind": "struct",
            "line_start": _line_number(content, match.start()),
            "line_end": _line_number(content, match.start()) + 1,
            "parent": None,
            "fqn": match.group(1),
        })
    for match in _RUST_IMPORT_RE.finditer(content):
        imports.append(match.group(1))

    return {
        "symbols": symbols,
        "imports": imports,
        "direct_imports": [],
        "exports": [],
        "file_signature": "",
        "extraction_backend": "regex",
    }


def _extract_generic_regex(content: str) -> dict:
    symbols = []
    imports = []

    for match in _GENERIC_DEF_RE.finditer(content):
        symbols.append({
            "name": match.group(1),
            "kind": "definition",
            "line_start": _line_number(content, match.start()),
            "line_end": _line_number(content, match.start()) + 1,
            "parent": None,
            "fqn": match.group(1),
        })
    for match in _GENERIC_IMPORT_RE.finditer(content):
        if match.group(1):
            imports.append(match.group(1))

    return {
        "symbols": symbols,
        "imports": imports,
        "direct_imports": [],
        "exports": [],
        "file_signature": "",
        "extraction_backend": "regex",
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def extract_code_structure(content: str, source_file: str) -> dict:
    """
    Extract code structure with rich symbol metadata.

    Returns dict with keys:
    - symbols: list of {name, kind, line_start, line_end, parent, fqn}
    - imports: list of module/package names imported
    - direct_imports: list of directly imported symbol names
    - exports: list of public names (heuristic)
    - file_signature: module-level docstring or shebang
    - extraction_backend: "tree_sitter" or "regex"

    Tree-sitter is preferred for Python if available.
    Falls back to regex for all languages.
    """
    ext = Path(source_file).suffix.lower()

    # Python: try tree-sitter first if available
    if ext in (".py", ".pyi"):
        if _ensure_tree_sitter():
            try:
                result = _extract_py_tree_sitter(content)
                if result.get("symbols") is not None:
                    return result
            except Exception:
                pass

    # Regex fallback for all languages
    if ext in (".py", ".pyi"):
        return _extract_py_regex(content)
    elif ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"):
        return _extract_js_regex(content)
    elif ext == ".go":
        return _extract_go_regex(content)
    elif ext == ".rs":
        return _extract_rust_regex(content)
    else:
        return _extract_generic_regex(content)


def is_tree_sitter_available() -> bool:
    """Return True if tree-sitter and language parsers are available."""
    return _ensure_tree_sitter()


# ── Legacy compatibility shim ─────────────────────────────────────────────────

def extract_symbols(content: str, source_file: str) -> dict:
    """
    Legacy compatibility wrapper. Adapts extract_code_structure output
    to the format expected by symbol_index.py (name, type, line fields).

    New code should use extract_code_structure directly.
    """
    result = extract_code_structure(content, source_file)

    return {
        "symbols": [
            {
                "name": s["name"],
                "type": s["kind"],
                "line": s["line_start"],
                "line_start": s["line_start"],
                "line_end": s["line_end"],
                "parent": s.get("parent"),
                "fqn": s.get("fqn"),
            }
            for s in result.get("symbols", [])
        ],
        "imports": result.get("imports", []),
        "direct_imports": result.get("direct_imports", []),
        "exports": result.get("exports", []),
        "file_signature": result.get("file_signature", ""),
        "extraction_backend": result.get("extraction_backend", "regex"),
    }