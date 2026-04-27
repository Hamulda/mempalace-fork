"""
retrieval_planner — Phase 3 query classification and filter building.

classify_query() routes each query to the optimal retrieval strategy:
  path         — literal path/file query → FTS5 prefix + metadata get (no vector)
  symbol       — exact symbol name query → SymbolIndex first, then scoped vector
  code_exact   — code keyword query → FTS5 primary, vector supplement
  code_semantic — semantic code query → FTS5 + vector + symbol expansion, RRF merge
  memory       — prose/memory query → vector primary, FTS5 supplement
  mixed        — ambiguous → multi-source RRF merge

build_planner_filters() builds a ChromaDB-style where dict so project/language/wing
filters are pushed into DB queries rather than post-retrieval Python filtering.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

# ── Query-type classification patterns ─────────────────────────────────────────

# File extension / path segment patterns
_PATH_EXT_RE = re.compile(r"\.\w{1,6}$|/[\w\-.]+/?|\.[jl]s\b|\.py\b|\.go\b|\.rs\b|\.ts\b", re.IGNORECASE)
# Memory/prose signals
_PROSE_SIGNALS = re.compile(
    r"\b(what is|how do|explain|tell me|describe|remember|what happened|"
    r"why did|when did|who said|tell about|overview|summary|history)\b",
    re.IGNORECASE,
)
# Code signals (strong — def, class, import, => etc.)
_CODE_SIGNALS = re.compile(
    r"\b(def |class |import |from |require\(|export |let |const |=>|\bself\.|"
    r"::|\bif |else |return |async |await |lambda |with |yield )\b|"
    r"\.\w+\(.*\)",
    re.IGNORECASE,
)


def classify_query(query: str) -> Literal["path", "symbol", "code_exact", "code_semantic", "memory", "mixed"]:
    """
    Classify a query into one of six retrieval intent categories.

    The returned category determines which retrieval path is used:

    - path       : User is looking for a specific file or path literal.
                   Use FTS5 prefix/path lookup first, skip vector.
    - symbol     : User is looking for a symbol (function, class, constant).
                   Query SymbolIndex first, then expand to chunks in same file/line.
    - code_exact : Exact code pattern (def, import, => etc.).
                   FTS5 first, small vector shortlist for disambiguation.
    - code_semantic : Semantic code query (≥3 words with code signal).
                   FTS5 + vector + symbol expansion, RRF merge.
    - memory     : Prose/memory style (short or conversational).
                   Vector-first, FTS5 fallback.
    - mixed      : Cannot determine intent; use full hybrid search.

    Priority order:
    1. slash (/) → path (most unambiguous path signal)
    2. dot without slash → symbol (module-qualified identifier)
    3. def/class keyword → symbol
    4. extension only (no slash) → path
    5. bare identifier → symbol
    6. prose + no code signal → memory
    7. code signal, short → code_exact
    8. code signal, multi-word → code_semantic
    9. ambiguous → mixed
    """
    if not query or not query.strip():
        return "mixed"

    q = query.strip()
    word_count = len(q.split())
    has_path_signal = bool(_PATH_EXT_RE.search(q))
    has_prose_signal = bool(_PROSE_SIGNALS.search(q))
    has_code_signal = bool(_CODE_SIGNALS.search(q))

    # 1. Slash (/) → path (unambiguous path signal)
    if "/" in q:
        return "path"

    # 2. Dot-qualified, no slash, no prose → symbol (e.g. "foo.bar", "ClassName.method")
    # Must check BEFORE extension to avoid "foo.bar" (path extension match) → symbol.
    _DOT_RE = re.compile(r"\w+\.\w+")
    if _DOT_RE.search(q) and word_count <= 2 and not has_prose_signal:
        return "symbol"

    # 3. Extension only, no slash, no dot-qualified → path (e.g. "*.py", "auth.rs")
    if has_path_signal:
        return "path"

    # 4. Def/class keyword prefix → symbol (e.g. "def foo", "class MyClass")
    _DEF_CLASS_RE = re.compile(
        r"(?:^|\b)(?:def|class|function|func|fn|var|const|let|enum|struct|interface|type|module|package)\s+\w+",
        re.IGNORECASE,
    )
    if _DEF_CLASS_RE.search(q):
        return "symbol"

    # 5. Bare identifier (single word, no dots, no slash, no prose) → symbol
    if word_count == 1 and not has_prose_signal:
        return "symbol"

    # 6. Prose + no code signal → memory
    if has_prose_signal and not has_code_signal:
        return "memory"

    # 7. Code signal, short query → code_exact
    if has_code_signal and word_count <= 3:
        return "code_exact"

    # 8. Multi-word query with code vocabulary → code_semantic
    _SEMANTIC_KEYWORDS = re.compile(
        r"\b(how|what|where|why|when|which|who|describe|explain|implement|"
        r"handle|process|manage|create|build|parse|validate|authenticate|"
        r"encrypt|compress|optimize|cache|search|index|retrieve)\b",
        re.IGNORECASE,
    )
    if word_count >= 3 and (has_code_signal or _SEMANTIC_KEYWORDS.search(q)):
        return "code_semantic"

    # 9. Short prose → memory
    if word_count <= 3 and has_prose_signal:
        return "memory"

    # 10. Mixed: ambiguous
    return "mixed"


def build_planner_filters(
    project_path: str | None = None,
    language: str | None = None,
    wing: str | None = None,
    is_latest: bool | None = True,
) -> dict:
    """
    Build a ChromaDB-style WHERE dict with project_path pushed to DB level.

    This pushes filtering DOWN into the DB query (LanceDB where clause) rather
    than post-retrieval Python filtering. For large repos this prevents the
    global top-k from being diluted by irrelevant cross-project chunks.

    Args:
        project_path: Push source_file prefix filter to DB level.
        language: Language filter (e.g. "Python", "JavaScript").
        wing: Wing filter (e.g. "repo", "project").
        is_latest: Filter only latest chunks (default: True).

    Returns:
        ChromaDB-style where dict for use in col.query(where=...) / col.get(where=...).
    """
    conditions = []

    if wing:
        conditions.append({"wing": {"$eq": wing}})

    if is_latest is not None:
        conditions.append({"is_latest": {"$eq": is_latest}})

    if language:
        conditions.append({"language": {"$eq": language}})

    if project_path:
        prefix = str(Path(project_path).resolve()).rstrip("/") + "/"
        conditions.append({"source_file": {"$starts_with": prefix}})

    if len(conditions) == 0:
        return {}

    if len(conditions) == 1:
        return conditions[0]

    return {"$and": conditions}
