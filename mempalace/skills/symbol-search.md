---
skill: symbol-search
trigger: when searching for code symbols (functions, classes, variables)
---

# Symbol Search Guide

When searching for code-related items in the palace, use symbol tools first for precision.

## When to Use Symbol Tools

Use symbol tools when:
- You know the name of a function, class, or variable
- You want to find "where X is defined"
- You want to find "who calls/uses X"
- You're editing code and want context about a specific file

## Tool Priority

### 1. mempalace_find_symbol — Exact Lookup

For known symbol names, get exact definition location:
```
mempalace_find_symbol(symbol_name="process_file")
```

Returns:
- file_path
- line_start, line_end
- symbol_type (function, class, const, etc.)
- file_signature (module docstring if available)
- imports, exports from the file

### 2. mempalace_search_symbols — Pattern Search

For partial names or regex patterns:
```
mempalace_search_symbols(pattern="process*")
mempalace_search_symbols(pattern="^validate_")
```

Supports glob patterns (`*`, `_`) and regex anchors (`^`).

### 3. mempalace_file_symbols — File Contents

Get all symbols defined in a specific file:
```
mempalace_file_symbols(file_path="/src/auth.py")
```

Returns:
- All function/class/const definitions
- Imports and exports for the file
- File signature (module docstring)

### 4. mempalace_callers — Find References

Find files that import or call a symbol:
```
mempalace_callers(symbol_name="validate_token", project_root="/path/to/project")
```

Returns files that reference the symbol via imports or text search.

## Symbol Types

The symbol index extracts:
- `function` — function definitions
- `class` — class definitions
- `async_def` — async function definitions
- `const` — top-level const/let assignments (heuristic: uppercase name)
- `struct` — struct definitions (Rust, Go)
- `definition` — generic definitions

## Workflow: Code Navigation

### Before editing a file
```
mempalace_file_symbols(file_path="/src/auth.py")  # understand structure
mempalace_find_symbol(symbol_name="validate_token")  # find specific function
```

### While editing
```
mempalace_search_symbols(pattern="*handler*")  # find related functions
mempalace_callers(symbol_name="my_function")  # find who calls this
```

### After editing (for architectural changes)
```
mempalace_capture_decision(...)  # capture the decision
mempalace_recent_changes(...)  # track change frequency
```

## Language Support

Symbol extraction works for:
- Python (.py, .pyi)
- JavaScript / TypeScript (.js, .jsx, .ts, .tsx)
- Go (.go)
- Rust (.rs)
- Generic (most C-family languages)

## Limitations

- Symbol extraction uses regex patterns, not a full AST parser
- Method-level scope tracking is basic (class methods tracked, not full call graph)
- For complex type-aware analysis, use LSP integration (future Phase)
- Import resolution is file-based, not absolute (some heuristics used)

## When to Fall Back to Semantic Search

If symbol tools return no results, fall back to:
```
mempalace_search("validate_token")  # semantic search
mempalace_code_search(query="validate_token", language="Python")  # code search
```