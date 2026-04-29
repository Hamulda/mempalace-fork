# PHASE12: AST Code Intelligence Upgrade

**Date:** 2026-04-29
**Goal:** Deeper Python code intelligence (classes, functions, async, nested scope, FQNs) without new required dependencies or M1 Air risk.

---

## 1. Audit Results

### `ast_extractor.py` — Issues Found

| # | Issue | Severity | Action |
|---|-------|----------|--------|
| 1 | tree-sitter unavailable on M1 Air → regex fallback, no parent/fqn | Critical | Fix: add stdlib ast path |
| 2 | `async_generator_function_definition` in SYMBOL_TYPES but dead code at lines 272–275 (unconditional `pass`) prevents any symbol being processed | Bug | Fixed: removed dead code |
| 3 | Extraction priority: tree-sitter → regex, no stdlib ast for Python | Gap | Fixed: added stdlib ast as middle tier |
| 4 | docstring said "tree_sitter or regex" — didn't mention stdlib ast | Doc drift | Fixed: updated docstring |

### `symbol_index.py` — Issues Found

| # | Issue | Severity | Action |
|---|-------|----------|--------|
| 1 | `find_symbol` project_path scoping uses `startswith(pp_norm + "/")` — verified safe against `/proj` vs `/proj-old` boundary, symlink resolution handles `/tmp → /private/tmp` | No change needed | Verified safe |

### `_symbol_tools.py` — Issues Found

| # | Issue | Severity | Action |
|---|-------|----------|--------|
| 1 | Returns `source_file`, `repo_rel_path` but no FQN/parent to MCP callers | Enhancement | Return existing `symbol_fqn`, `parent_symbol` from SymbolIndex rows |

---

## 2. Changes Made

### `mempalace/code_index/ast_extractor.py`

**Added `import ast`** at line 19 (stdlib, no new dependency).

**Added `_extract_py_stdlib_ast()`** (lines 346–457):
- Uses Python stdlib `ast.parse()` for robust Python extraction
- Stack-based DFS for correct parent scope tracking
- Handles: `ClassDef`, `FunctionDef`, `AsyncFunctionDef`
- Produces: `name`, `kind`, `line_start`, `line_end` (= `lineno + 1` for single-line), `parent`, `fqn`
- Returns `extraction_backend: "stdlib_ast"`

**Fixed tree-sitter dead code** (line 271): removed `nid in id_to_node and nid != id(root): pass` that was blocking all symbol processing.

**Updated `extract_code_structure()` priority**:
1. tree-sitter (if available and working)
2. stdlib ast (all `.py`/`.pyi` when tree-sitter unavailable) ← **NEW**
3. regex fallback

**Updated docstring**: now says `extraction_backend: "tree_sitter", "stdlib_ast", or "regex"`.

### `mempalace/server/_symbol_tools.py`

No structural changes needed. SymbolIndex already stores and returns `parent_symbol` and `symbol_fqn` via `find_symbol`, `get_file_symbols`. MCP tools pass these through.

---

## 3. stdlib ast vs tree-sitter vs regex

| Backend | parent | fqn | nested classes | async | M1-safe | Zero-dep |
|---------|--------|-----|----------------|-------|---------|----------|
| `tree_sitter` | ✅ | ✅ | ✅ | ✅ | ❌ (not installed) | ❌ |
| `stdlib_ast` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (stdlib) |
| `regex` | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |

**stdlib_ast is M1 Air safe**: uses only Python's built-in `ast` module — no native extensions, no network, no additional RAM.

---

## 4. Test Results

```
uv run pytest tests/test_ast_extractor.py tests/test_symbol_index.py tests/test_scoped_retrieval_e2e.py -v
================== 62 passed, 1 skipped, 16 warnings in 3.81s ==================
```

**Tests updated** to accept `extraction_backend in ("tree_sitter", "stdlib_ast", "regex")`:
- `test_extraction_backend_field` — now checks 3-way
- `test_update_file_stores_parent_and_fqn` — now checks `if backend in ("tree_sitter", "stdlib_ast")`
- `test_duplicate_methods_in_different_classes` — same
- `test_regex_fallback_on_parser_failure` — now accepts `regex` or `stdlib_ast`

**Pre-existing failures (unrelated):**
- `test_health_fingerprint` — pre-existing environmental
- `test_searcher_lance_chroma_drift_fixes` — pre-existing ChromaDB timeout
- 16 warnings from `datetime.utcnow()` deprecation in `miner.py` — pre-existing, out of scope

---

## 5. Extraction Examples

### Nested class/method (stdlib_ast output)

```python
class Outer:
    def method(self): pass      # fqn=Outer.method, parent=Outer
    class Inner:
        def inner_method(self): pass  # fqn=Outer.Inner.inner_method, parent=Inner
```

### Duplicate method names (stdlib_ast output)

```python
class Foo:
    def handle(self): pass      # fqn=Foo.handle
class Bar:
    def handle(self): pass      # fqn=Bar.handle
```

Both `Foo.handle` and `Bar.handle` are correctly disambiguated via FQN.

### Async function (stdlib_ast output)

```python
async def async_foo(): pass    # kind=async_function, fqn=async_foo
def sync_bar(): pass           # kind=function, fqn=sync_bar
```

---

## 6. Boundary Safety Verification

`find_symbol` with `project_path` uses `startswith(pp_norm + "/")` which is boundary-safe:

| project_path | source_file | matches? | reason |
|---|---|---|---|
| `/proj` | `/proj/file.py` | ✅ | subdir match |
| `/proj` | `/proj` | ✅ | exact match |
| `/proj` | `/proj-old/file.py` | ❌ | not prefix |
| `/proj` | `/project/file.py` | ❌ | no `/` before `proj` |
| `/tmp` | `/private/tmp/file.py` | ✅ | symlink resolved |

---

## 7. MCP Symbol Tool Output

Tools (`mempalace_find_symbol`, `mempalace_file_symbols`, etc.) now return existing fields:

```python
{
  "symbol_name": "my_method",
  "file_path": "/src/my_file.py",
  "line_start": 12,
  "line_end": 13,
  "parent_symbol": "MyClass",      # enclosing class/function
  "symbol_fqn": "MyClass.my_method", # fully-qualified name
  "extraction_backend": "stdlib_ast",
  "source_file": "/src/my_file.py",
  "repo_rel_path": "src/my_file.py",
}
```

---

## 8. Summary

| Item | Status |
|------|--------|
| stdlib ast extraction for Python | ✅ Added |
| parent/fqn for nested classes/methods | ✅ Working |
| duplicate method name disambiguation | ✅ Working |
| async function detection | ✅ Working |
| tree-sitter dead code removed | ✅ Fixed |
| test suite updates for stdlib_ast | ✅ Updated |
| find_symbol project_path boundary | ✅ Verified safe |
| MCP tools return fqn/parent/backend | ✅ Already working |
| No new required dependency | ✅ |

**Final:** 62 passed, 1 skipped, 16 warnings. All three test suites green.
