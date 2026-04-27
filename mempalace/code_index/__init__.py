"""
code_index — Lightweight code intelligence layer.

Modules:
- ast_extractor: extract_code_structure() with tree-sitter (optional) + regex fallback.
                 Produces parent/fqn/import/export metadata for Python, JS, Go, Rust.
"""

from .ast_extractor import extract_code_structure, extract_symbols, is_tree_sitter_available

__all__ = ["extract_code_structure", "extract_symbols", "is_tree_sitter_available"]