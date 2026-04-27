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
    "If you have existing ChromaDB data, migrate it first with: "
    "pip install chromadb && python -m mempalace.migrate chroma-to-lance --palace <path>"
)
