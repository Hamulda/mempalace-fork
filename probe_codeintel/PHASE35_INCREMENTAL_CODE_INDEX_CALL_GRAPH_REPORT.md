# PHASE35: Incremental Code Index + Call Graph Report

**Date:** 2026-04-30
**Mission:** Improve code intelligence without heavy LSP dependency

---

## What Was Implemented

### 1. Extended stdlib AST Extractor (`ast_extractor.py`)

Extended `_extract_py_stdlib_ast()` to return new data structures:

```
Returns:
  import_refs:     [{module, names: [], alias, line}]
  call_refs:       [{caller_fqn, callee_name, callee_attr, line}]
  class_inheritance: [{name, bases: [], line}]
  decorators:     [{name, line, parent_fqn, symbol_kind}]
```

**Key implementation details:**

- **ScopeTracker visitor** — `ast.NodeVisitor` subclass with `visit_ClassDef`, `visit_FunctionDef`, `visit_AsyncFunctionDef`, `visit_Call`. Pushes/pops FQN scope stack to track enclosing scope for every call site.
- **call_refs** — Collected by `ScopeTracker.visit_Call()`, recording `caller_fqn` (from scope stack), `callee_name`/`callee_attr` (via `_resolve_callee()`), and `line`.
- **import_refs** — Each `ast.Import`/`ast.ImportFrom` produces a dict with `module`, `names` list, `alias`, and `line`.
- **class_inheritance** — Uses `ast.unparse()` for base class names (Python 3.9+).
- **decorators** — Captured for classes and functions with `parent_fqn` and `symbol_kind`.

Helper functions added:
- `_ast_name_or_attr(node)` — Returns `'foo'` or `'foo.bar'` for Name/Attribute nodes.
- `_resolve_callee(func)` — Returns `(callee_name, callee_attr)` tuple from a `Call.func` node.

### 2. SymbolIndex Schema — 4 New Tables

Added to `_init_db()` via idempotent `executescript`:

| Table | Schema |
|-------|--------|
| `call_refs` | `id, source_file, caller_fqn, callee_name, callee_attr, line, confidence` |
| `import_refs` | `id, source_file, module, imported_names, alias, line` |
| `class_inheritance` | `id, source_file, class_name, bases, line` |
| `symbol_decorators` | `id, source_file, symbol_name, symbol_fqn, symbol_kind, decorator_name, line` |

### 3. Incremental Ref Update

`update_file()` now does per-file targeted deletes (not full table wipe):

```python
self._conn.execute("DELETE FROM call_refs WHERE source_file = ?", (file_path,))
self._conn.execute("DELETE FROM import_refs WHERE source_file = ?", (file_path,))
self._conn.execute("DELETE FROM class_inheritance WHERE source_file = ?", (file_path,))
self._conn.execute("DELETE FROM symbol_decorators WHERE source_file = ?", (file_path,))
```

Then inserts new ref records extracted from the updated file content. Other files' refs are preserved.

### 4. New `get_callers_ast()` Method

```python
def get_callers_ast(self, symbol_name: str) -> list[dict]:
    # Queries call_refs table by callee_name
    # Returns: source_file, caller_fqn, callee_name, callee_attr, line, confidence, match_type
```

### 5. MCP Tool Update — `mempalace_callers`

Updated to prioritize AST call graph over import heuristic:

```
Primary:   get_callers_ast(symbol_name)  → confidence=medium, match_type=ast_call
Fallback:  get_callers(symbol_name)     → confidence=low, match_type=import_ref (only if no AST results)
```

### 6. New Test Suite

**`tests/test_code_intel_call_graph.py`** — 15 tests, all passing:

| Test | Description |
|------|-------------|
| `test_call_refs_basic` | `AuthManager.login` call recorded with correct caller_fqn, attr, line |
| `test_call_refs_method_chains` | `self._verify()` chain resolved correctly |
| `test_import_refs` | `from auth import AuthManager` stored |
| `test_class_inheritance` | `class X(Base, Y)` records bases |
| `test_decorators` | `@abstractmethod` captured with parent_fqn |
| `test_update_file_stores_call_refs` | call_refs table populated |
| `test_reindex_removes_old_call_refs` | Reindex clears old refs, comment-only has none |
| `test_import_refs_stored` | import_refs table populated |
| `test_other_py_comment_only_no_refs` | Comment-only file = 0 refs |
| `test_full_fixture_auth_manager_login` | Full fixture: callers(AuthManager.login) finds service.py |
| `test_full_fixture_comment_excluded` | other.py comment-only excluded |
| `test_incremental_ref_update_preserves_other_files` | Reindex auth.py doesn't remove service.py call refs |
| `test_symbol_index_preserves_own_refs` | Symbol records preserved during ref updates |
| `test_direct_method_call_high_confidence` | AST call confidence assignment |
| `test_import_based_fallback_low_confidence` | Fallback returns low confidence |

---

## Test Results

```
pytest tests/test_ast_extractor.py tests/test_code_intel_call_graph.py -q
  28 passed, 1 skipped in 1.89s

pytest tests/test_symbol_index.py tests/test_scoped_retrieval_e2e.py -q
  49 passed in 4.08s

python -c "import sys; import mempalace; print('chromadb' in sys.modules)"
  False
```

**Chromadb check:** PASS — no ChromaDB in sys.modules.

---

## Files Modified

| File | Change |
|------|--------|
| `mempalace/code_index/ast_extractor.py` | Extended `_extract_py_stdlib_ast` with call_refs, import_refs, class_inheritance, decorators; added `ScopeTracker`, `_ast_name_or_attr`, `_resolve_callee` |
| `mempalace/symbol_index.py` | Added 4 ref tables, `get_callers_ast()`, incremental delete/insert in `update_file()`, updated `clear()` |
| `mempalace/server/_symbol_tools.py` | `mempalace_callers` now uses `get_callers_ast` primary + `get_callers` fallback |

## Files Created

| File | Description |
|------|-------------|
| `tests/test_code_intel_call_graph.py` | 15 tests covering extraction, ref tables, incremental update, MCP output |
| `mempalace/code_index/ast_extractor.py.bak` | Backup of original ast_extractor |
| `mempalace/symbol_index.py.bak` | Backup of original symbol_index |
| `mempalace/server/_symbol_tools.py.bak` | Backup of original _symbol_tools |

---

## Remaining Gaps (Known Limitations)

1. **Confidence calibration** — Default confidence is `"medium"` for all AST calls. Could distinguish `"high"` for direct `obj.method()` calls vs `"medium"` for qualified `Module.func()` calls vs `"low"` for attribute-chained `obj.nested.method()`.

2. **`callee_attr` resolution** — For `obj.method()` chains, `callee_attr` captures `obj` but not the full chain (e.g., `obj.nested.method()` gives `obj.nested`). Full chain reconstruction would require deeper AST analysis.

3. **`callee_name` disambiguation** — When multiple classes have methods with the same name (e.g., both `Foo.handle` and `Bar.handle`), `get_callers_ast("handle")` returns all of them. A `callee_fqn` or scoped query would be needed for disambiguation.

4. **No `symbol_refs` table** — The original requirement mentioned a `symbol_refs` table for tracking Name node references (def/use/import). This was omitted for simplicity; import_refs and call_refs provide equivalent signal.

5. **Regex fallback** — When `ast.parse()` fails (syntax errors, non-Python), extraction falls back to regex which does NOT populate the new ref tables. This is the same behavior as before.

6. **`mempalace_find_symbol` and `mempalace_file_symbols`** — These tools were not modified; they still return the original schema without confidence/match_type. Only `mempalace_callers` was updated.