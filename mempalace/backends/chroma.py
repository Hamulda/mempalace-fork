"""
ChromaDB backend — REMOVED.

ChromaDB backend has been removed. LanceDB is the only supported backend.

If you have existing ChromaDB data, migrate it first:
    pip install chromadb
    python -m mempalace.migrate chroma-to-lance --palace <path>
"""

raise ImportError(
    "ChromaDB backend has been removed. "
    "LanceDB is the only supported backend. "
    "This Lance-only build cannot migrate Chroma data. "
    "Use an older release/commit with Chroma support, or export data manually and re-mine into LanceDB."
)
