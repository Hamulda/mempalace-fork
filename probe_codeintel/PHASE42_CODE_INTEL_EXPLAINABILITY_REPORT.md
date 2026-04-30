# PHASE42 — Code Intel Explainability Report

**Date:** 2026-04-30
**Files Modified:** `mempalace/symbol_index.py`, `mempalace/server/_symbol_tools.py`
**Files Created:** `tests/test_code_intel_explainability.py`
**Tests:** 28 passed (call_graph + explainability), 26 passed (contract)

---

## 1. Audit Findings

### symbol_index.py

| Issue | Location | Severity | Fix |
|-------|----------|----------|-----|
| `call_refs` insert defaulted to `confidence="medium"` | line 810 | MED | Changed default to `"high"` — AST calls warrant high confidence |
| `match_type` not stored in call_refs table | `_CALL_REFS_SCHEMA` line 260 | MED | `match_type` added by `get_callers_ast()` result construction (line 901) |

### _symbol_tools.py (mempalace_callers tool)

| Issue | Location | Severity | Fix |
|-------|----------|----------|-----|
| No `why` field on results | line 86-115 | MED | Added `why` field with AST-call explanation per result |
| No `callee_fqn` field on results | line 86-115 | MED | Added `callee_fqn = symbol_name` to all results |
| Fallback results lacked `why` | line 102-107 | MED | Added `why` to import-ref fallback entries |

### ast_extractor.py

| Observation | Notes |
|------------|-------|
| `call_refs` produced by `_extract_py_stdlib_ast()` | No `confidence` field set — uses caller-provided default |
| `_extract_py_tree_sitter()` does not emit `call_refs` | Falls back to stdlib ast |
| `confidence` NOT in call_refs dict from extractor | Defaulted to `"medium"` in `update_file()` insert |

---

## 2. Changes Made

### symbol_index.py — confidence default fix

```python
# Before
cr.get("confidence", "medium")

# After
cr.get("confidence", "high")  # AST calls are high confidence
```

**Rationale:** Actual AST-detected function calls are the most reliable reference type. `get_callers_ast()` already returns `match_type="ast_call"`. High confidence is warranted.

### _symbol_tools.py — explainability metadata

Added to every caller result before returning:

| Field | Value | why |
|-------|-------|-----|
| `match_type` | `"ast_call"` | For primary AST results |
| `confidence` | `"high"` | AST call = precise |
| `why` | `"AST call: {caller_fqn}() calls {symbol_name}() at line {line}"` | Human-readable explanation |
| `callee_fqn` | `symbol_name` | Identifies the queried symbol |

For import-ref fallback results (via `get_callers()`):

| Field | Value | why |
|-------|-------|-----|
| `match_type` | `"import_ref"` | Import-based heuristic |
| `confidence` | `"low"` | Module-level guess, not a call |
| `why` | `"import-based heuristic: module imports suggest {symbol} may be called"` | Explains uncertainty |
| `callee_fqn` | `symbol_name` | Identifies the queried symbol |

---

## 3. Schema Contract

### get_callers_ast() result dict

```python
{
    "source_file": str,       # absolute path
    "caller_fqn": str,        # e.g. "PaymentService.process"
    "callee_name": str,       # function/method name queried
    "callee_attr": str,       # attribute on which callee is called (e.g. "AuthManager", "self")
    "line": int,              # 1-based line number of the call
    "confidence": str,        # "high" | "medium" | "low"
    "match_type": str,        # "ast_call"
}
```

### mempalace_callers tool response

```python
{
    "symbol_name": str,       # the symbol queried
    "callers": [
        {
            # All fields from get_callers_ast, plus:
            "match_type": "ast_call" | "import_ref",
            "confidence": "high" | "low",
            "why": str,          # e.g. "AST call: PaymentService.process() calls login() at line 12"
            "callee_fqn": str,   # symbol_name being queried
            "source_file": str,
            "repo_rel_path": str,
        }
    ],
    "count": int
}
```

---

## 4. Test Results

```
tests/test_code_intel_call_graph.py     — 21 passed (after updating 2 "medium" → "high" assertions)
tests/test_code_intel_explainability.py — 7 classes, all passing
tests/test_mcp_response_contract.py    — 26 passed
chromadb in sys.modules                — False
```

### test_code_intel_explainability.py coverage

| Class | Tests |
|-------|-------|
| `TestSchemaStable` | AST result has all fields, import fallback has file_path + called_symbol |
| `TestMatchTypeCorrectness` | ast_call match_type, comment excluded, import-only excluded |
| `TestConfidenceCalibration` | AST = high, method chain = high/medium, import fallback = imported_module |
| `TestWhyField` | why contains caller_fqn + line, _symbol_tools layer adds why |
| `TestMcpToolIntegration` | Full response structure with callee_fqn |
| `TestMixedResults` | Import-ref has consistent core schema |

---

## 5. Match Type Reference

| match_type | confidence | when used |
|------------|-----------|-----------|
| `ast_call` | high | Actual function/method call detected via AST (stdlib ast or tree-sitter) |
| `import_ref` | low | Symbol imported but not called (fallback via get_callers) |
| `text_ref` | low | Not yet implemented (regex-based text search) |
| `comment_ref` | excluded | Comment-only mentions do not appear in callers at all |

---

## 6. Cases

### Case 1: Actual AST call → high confidence + why
```python
# service.py
AuthManager.login(user)
```
Result:
```json
{
  "caller_fqn": "PaymentService.process",
  "callee_name": "login",
  "line": 12,
  "confidence": "high",
  "match_type": "ast_call",
  "why": "AST call: PaymentService.process() calls login() at line 12"
}
```

### Case 2: Import-only reference → low confidence + why
```python
# import_only.py
from auth import AuthManager
```
Fallback result (via get_callers):
```json
{
  "file_path": "import_only.py",
  "imported_module": "auth",
  "called_symbol": "AuthManager",
  "import_type": "direct",
  "confidence": "low",
  "match_type": "import_ref",
  "why": "import-based heuristic: module imports suggest AuthManager may be called"
}
```

### Case 3: Comment-only mention → excluded
```python
# comment.py
# This file mentions AuthManager in a comment only.
```
`comment.py` does **not** appear in callers list for `login`.

---

## 7. Backward Compatibility

- `get_callers_ast()` result schema unchanged — `confidence` was already present (now defaults to `"high"` instead of `"medium"`)
- `mempalace_callers` tool response: added fields only, no breaking changes
- `get_callers()` (import-based fallback) unchanged — returns same dict shape; confidence/match_type added by `_symbol_tools` layer
