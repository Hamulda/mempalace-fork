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

import ast
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


# ── tree-sitter parser helper ──────────────────────────────────────────────────

def _get_tree_sitter_parser(language: str) -> "object | None":
    """
    Get a tree-sitter Parser for the given language.

    Tries:
      1. tree_sitter_languages.get_parser(language)  (preferred, bundled parsers)
      2. tree_sitter.Language + tree_sitter.Parser (manual loading)
    Returns None if tree-sitter is unavailable or the language parser can't be loaded.
    """
    if not _ensure_tree_sitter():
        return None

    try:
        from tree_sitter_languages import get_parser

        try:
            parser = get_parser(language)
            if parser is not None:
                return parser
        except Exception:
            pass

        # Fallback: manual Language + Parser
        from tree_sitter_languages import get_language
        from tree_sitter import Parser

        lang = get_language(language)
        parser = Parser()
        parser.set_language(lang)
        return parser

    except Exception:
        return None


# ── Diagnostics ───────────────────────────────────────────────────────────────

def tree_sitter_diagnostics() -> dict:
    """
    Return diagnostic info about tree-sitter availability and Python parser.

    Returns:
        {
            "available": bool,
            "parser_backend": str | None,
            "python_parser_works": bool,
            "error": str | None,
        }
    """
    available = _ensure_tree_sitter()

    if not available:
        return {
            "available": False,
            "parser_backend": None,
            "python_parser_works": False,
            "error": "tree_sitter or tree_sitter_languages not installed",
        }

    # Try to get Python parser via get_parser
    try:
        from tree_sitter_languages import get_parser, get_language

        parser = get_parser("python")
        if parser is not None:
            # Quick smoke test: parse a trivial Python snippet
            try:
                tree = parser.parse(b"def foo(): pass")
                if tree.root_node is not None:
                    return {
                        "available": True,
                        "parser_backend": "tree_sitter_languages.get_parser",
                        "python_parser_works": True,
                        "error": None,
                    }
            except Exception as e:
                return {
                    "available": True,
                    "parser_backend": "tree_sitter_languages.get_parser",
                    "python_parser_works": False,
                    "error": f"parse failed: {e}",
                }

        # Fallback: Language + Parser
        lang = get_language("python")
        from tree_sitter import Parser

        parser2 = Parser()
        parser2.set_language(lang)
        tree2 = parser2.parse(b"def foo(): pass")
        if tree2.root_node is not None:
            return {
                "available": True,
                "parser_backend": "Language+Parser (tree_sitter)",
                "python_parser_works": True,
                "error": None,
            }

        return {
            "available": True,
            "parser_backend": "unknown",
            "python_parser_works": False,
            "error": "parser returned None root_node",
        }

    except Exception as e:
        return {
            "available": True,
            "parser_backend": None,
            "python_parser_works": False,
            "error": str(e),
        }


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

def _extract_py_tree_sitter(content: str) -> dict | None:
    """Extract Python symbols with parent/fqn using tree-sitter.

    Returns None if tree-sitter is unavailable or parsing failed,
    signalling to extract_code_structure to fall back to regex.
    """
    parser = _get_tree_sitter_parser("python")
    if parser is None:
        return None

    # Build parent map and id→node map via single DFS pass
    parent_map: dict[int, Optional[int]] = {}
    id_to_node: dict[int, object] = {}

    try:
        tree = parser.parse(bytes(content, "utf-8"))
    except Exception:
        return None

    root = tree.root_node
    if root is None:
        return None

    stack: list[tuple[object, Optional[int]]] = [(root, None)]
    while stack:
        n, parent = stack.pop()
        nid = id(n)
        parent_map[nid] = parent
        id_to_node[nid] = n
        for child in n.children:
            stack.append((child, nid))

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

    def _fqns_from_ancestors(node_id: int) -> list[str]:
        """Walk ancestors using parent_map (O(depth), no linear search)."""
        parts = []
        cur_id = parent_map.get(node_id)
        while cur_id is not None:
            n = id_to_node.get(cur_id)
            if n is None:
                break
            if n.type in ("function_definition", "async_generator_function_definition", "class_definition"):
                name_node = _child(n, "identifier")
                if name_node:
                    parts.insert(0, _node_text(name_node))
            cur_id = parent_map.get(cur_id)
        return parts

    SYMBOL_TYPES = ("function_definition", "async_generator_function_definition", "class_definition")

    for node in root.preorder():
        nid = id(node)
        if node.type not in SYMBOL_TYPES:
            continue

        name_node = _child(node, "identifier")
        if not name_node:
            continue

        name = _node_text(name_node)
        prefix_parts = _fqns_from_ancestors(nid)
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
            if parent_node is not None:
                parent_n = id_to_node.get(parent_node)
                if parent_n is not None and parent_n.type == "module":
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


# ── stdlib ast Python extraction ──────────────────────────────────────────────

def _extract_py_stdlib_ast(content: str) -> dict:
    """Extract Python symbols using the stdlib ast module.

    Provides class/function/async function detection with correct parent scope
    and fully-qualified names (FQN). Also extracts:
      - import_refs: {module, names, alias, line}
      - call_refs: {caller_fqn, callee_name, callee_attr, line}
      - class_inheritance: {name, bases} per class
      - decorators: {name, line, parent_fqn, symbol_kind} per decorated symbol

    This is the M1 Air safe path when tree-sitter is unavailable.
    """
    try:
        tree = ast.parse(content)
    except Exception:
        return _extract_py_regex(content)

    symbols = []
    imports_list = []
    direct_imports_list = []
    exports = []
    import_refs = []
    call_refs_out: list[dict] = []
    decorators: list[dict] = []
    inheritance: list[dict] = []

    # Stack entries: (node, parent_scope_list)
    stack: list[tuple[object, list[str]]] = []

    # Seed with top-level body and empty parent scope
    stack.append((tree.body, []))

    # Scope tracking for caller_fqn resolution
    call_to_scope: dict[int, str] = {}

    class ScopeTracker(ast.NodeVisitor):
        """Track enclosing scope (fqn) for every Call node."""

        def __init__(self):
            super().__init__()
            self._scope_stack: list[str] = []  # stack of fqns

        def visit_ClassDef(self, node: ast.ClassDef):
            fqn = ".".join(self._scope_stack + [node.name]) if self._scope_stack else node.name
            self._scope_stack.append(fqn)
            self.generic_visit(node)
            self._scope_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef):
            fqn = ".".join(self._scope_stack + [node.name]) if self._scope_stack else node.name
            self._scope_stack.append(fqn)
            self.generic_visit(node)
            self._scope_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            fqn = ".".join(self._scope_stack + [node.name]) if self._scope_stack else node.name
            self._scope_stack.append(fqn)
            self.generic_visit(node)
            self._scope_stack.pop()

        def visit_Call(self, node: ast.Call):
            # Capture enclosing scope (class or function name, not full fqn)
            caller = self._scope_stack[-1] if self._scope_stack else ""
            call_to_scope[id(node)] = caller
            self.generic_visit(node)

    tracker = ScopeTracker()
    tracker.visit(tree)

    while stack:
        nodes, parents = stack.pop()
        for node in nodes:
            # ── class ──────────────────────────────────────────────────────────
            if isinstance(node, ast.ClassDef):
                fqn = ".".join(parents + [node.name]) if parents else node.name
                parent_name = parents[-1] if parents else None

                # Inheritance
                bases = []
                for b in node.bases:
                    try:
                        bases.append(ast.unparse(b))
                    except Exception:
                        bases.append("")
                if any(bases):
                    inheritance.append({"name": node.name, "bases": bases, "line": node.lineno})

                # Decorators
                for dec in node.decorator_list:
                    dec_name = _ast_name_or_attr(dec)
                    if dec_name:
                        decorators.append({"name": dec_name, "line": dec.lineno, "parent_fqn": fqn, "symbol_kind": "class"})

                symbols.append({
                    "name": node.name,
                    "kind": "class",
                    "line_start": node.lineno,
                    "line_end": node.lineno + 1,
                    "parent": parent_name,
                    "fqn": fqn,
                })
                stack.append((node.body, parents + [node.name]))

            # ── function (sync or async) ────────────────────────────────────────
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fqn = ".".join(parents + [node.name]) if parents else node.name
                parent_name = parents[-1] if parents else None

                # Decorators
                for dec in node.decorator_list:
                    dec_name = _ast_name_or_attr(dec)
                    if dec_name:
                        decorators.append({"name": dec_name, "line": dec.lineno, "parent_fqn": fqn, "symbol_kind": "function"})

                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                symbols.append({
                    "name": node.name,
                    "kind": kind,
                    "line_start": node.lineno,
                    "line_end": node.lineno + 1,
                    "parent": parent_name,
                    "fqn": fqn,
                })
                stack.append((node.body, parents))

            # ── import statements ─────────────────────────────────────────────
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports_list.append(alias.name)
                    import_refs.append({
                        "module": alias.name,
                        "names": [],
                        "alias": alias.asname or "",
                        "line": node.lineno,
                    })

            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod:
                    imports_list.append(mod)
                names = [a.name for a in node.names]
                for a in node.names:
                    direct_imports_list.append(a.name)
                import_refs.append({
                    "module": mod,
                    "names": names,
                    "alias": "",
                    "line": node.lineno,
                })

            # ── top-level assignment (export heuristic) ────────────────────────
            elif isinstance(node, ast.Assign):
                if not parents:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            name = target.id
                            if name and name[0].isupper() and name not in ("True", "False", "None"):
                                exports.append(name)

    # Resolve call_refs using recorded scope info
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            caller_fqn = call_to_scope.get(id(node), "")
            callee_name, callee_attr = _resolve_callee(node.func)
            if callee_name:  # Only record actual resolvable calls
                call_refs_out.append({
                    "caller_fqn": caller_fqn,
                    "callee_name": callee_name,
                    "callee_attr": callee_attr or "",
                    "line": node.lineno or 0,
                })

    return {
        "symbols": symbols,
        "imports": imports_list,
        "direct_imports": direct_imports_list,
        "exports": exports,
        "file_signature": _file_signature(content),
        "extraction_backend": "stdlib_ast",
        "import_refs": import_refs,
        "call_refs": call_refs_out,
        "class_inheritance": inheritance,
        "decorators": decorators,
    }


# ── Helper functions ─────────────────────────────────────────────────────────

def _ast_name_or_attr(node: ast.AST) -> str:
    """Return 'foo' or 'foo.bar' for a Name or Attribute node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        val = _ast_name_or_attr(node.value)
        return f"{val}.{node.attr}" if val else node.attr
    return ""


def _resolve_callee(func: ast.AST) -> tuple[str, str]:
    """Return (callee_name, callee_attr) for a Call.func node.

    Examples:
        login()           → ("login", "")
        self.login()     → ("login", "self")
        obj.auth.login() → ("login", "obj.auth")
    """
    if isinstance(func, ast.Name):
        return (func.id, "")
    if isinstance(func, ast.Attribute):
        callee_attr = _ast_name_or_attr(func.value)
        return (func.attr, callee_attr)
    return ("", "")


# ── Regex extractors (mirror symbol_index.py behavior) ───────────────────────-

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
    - extraction_backend: "tree_sitter", "stdlib_ast", or "regex"

    Extraction priority for Python:
      1. tree-sitter (if available and Python parser works)
      2. stdlib ast (for .py/.pyi when tree-sitter unavailable)
      3. regex fallback for all languages.
    """
    ext = Path(source_file).suffix.lower()

    # Python: try tree-sitter first if available, then stdlib ast, then regex
    if ext in (".py", ".pyi"):
        if _ensure_tree_sitter():
            try:
                result = _extract_py_tree_sitter(content)
                if result is not None:
                    return result
            except Exception:
                pass
        # tree-sitter unavailable or failed — use stdlib ast
        return _extract_py_stdlib_ast(content)
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


# ── CLI doctor ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    diag = tree_sitter_diagnostics()
    print(f"tree-sitter available: {diag['available']}")
    print(f"parser backend:        {diag['parser_backend']}")
    print(f"python parser works:   {diag['python_parser_works']}")
    print(f"error:                {diag['error']}")
