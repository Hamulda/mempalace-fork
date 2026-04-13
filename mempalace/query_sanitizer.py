"""
query_sanitizer.py — Sanitize semantic search queries before hitting ChromaDB.

Goals:
  - Strip null bytes and control characters
  - Truncate excessively long queries
  - Detect and neutralize prompt injection patterns
  - Normalize whitespace
  - Return clean string (never raise — always returns something usable)
"""

import re

MAX_QUERY_LENGTH = 512

# Patterns that indicate prompt injection attempts
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(previous|all|above)\s+instructions", re.I),
    re.compile(r"(system\s*prompt|you\s+are\s+now|act\s+as|roleplay\s+as)", re.I),
    re.compile(r"(;\s*DROP|;\s*DELETE|;\s*INSERT|;\s*UPDATE|SELECT\s+\*)", re.I),
    re.compile(r"\]\s*;|\{\{.*\}\}", re.I),  # template injection
]


def sanitize_query(query: str) -> str:
    """
    Clean a search query. Returns sanitized string.
    Never raises — always returns a usable (possibly truncated/cleaned) query.
    """
    if not isinstance(query, str):
        return ""

    # Strip null bytes and control characters (keep newlines for multi-line queries)
    query = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", query)

    # Normalize whitespace
    query = re.sub(r"[ \t]+", " ", query).strip()

    # Truncate
    if len(query) > MAX_QUERY_LENGTH:
        query = query[:MAX_QUERY_LENGTH].rstrip()

    # Detect injection — neutralize by returning empty-safe fallback
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(query):
            return ""

    return query
