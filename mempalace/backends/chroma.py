"""
ChromaDB backend for MemPalace.

Provides the original ChromaDB-based storage implementation.
"""

from typing import Any, Dict, List, Optional
import chromadb

from .base import BaseCollection


class ChromaCollection(BaseCollection):
    """
    ChromaDB-backed collection implementing BaseCollection.

    Wraps a chromadb.Collection to expose the BaseCollection interface
    with full ChromaDB semantics.
    """

    def __init__(self, collection: chromadb.Collection):
        self._col = collection

    def add(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._col.add(documents=documents, ids=ids, metadatas=metadatas)

    def upsert(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._col.upsert(documents=documents, ids=ids, metadatas=metadatas)

    def query(
        self,
        query_texts: Optional[List[str]] = None,
        n_results: int = 10,
        where: Optional[Dict[str, Any]] = None,
        include: Optional[List[str]] = None,
        **kwargs,
    ) -> Dict[str, List[List[Any]]]:
        query_kwargs = {
            "query_texts": query_texts or [""],
            "n_results": n_results,
            "include": include or ["documents", "metadatas", "distances"],
        }
        if where:
            query_kwargs["where"] = where
        return self._col.query(**query_kwargs)

    def get(
        self,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        include: Optional[List[str]] = None,
        **kwargs,
    ) -> Dict[str, List[Any]]:
        get_kwargs = {
            "ids": ids,
            "include": include or ["documents", "metadatas"],
        }
        if where:
            get_kwargs["where"] = where
        if limit:
            get_kwargs["limit"] = limit
        return self._col.get(**get_kwargs)

    def delete(
        self, ids: Optional[List[str]] = None, where: Optional[Dict[str, Any]] = None, **kwargs
    ) -> None:
        del_kwargs: Dict[str, Any] = {}
        if ids:
            del_kwargs["ids"] = ids
        if where:
            del_kwargs["where"] = where
        self._col.delete(**del_kwargs)

    def count(self) -> int:
        return self._col.count()

    def get_by_id(self, record_id: str) -> Optional[Dict[str, Any]]:
        result = self._col.get(ids=[record_id])
        if not result.get("ids"):
            return None
        return {
            "id": result["ids"][0],
            "document": result["documents"][0],
            "metadata": result["metadatas"][0] if result.get("metadatas") else {},
        }


class ChromaBackend:
    """
    ChromaDB backend factory.

    Provides get_collection() which returns a ChromaCollection
    wrapping a chromadb.Collection instance.
    """

    def get_collection(
        self, palace_path: str, collection_name: str = "mempalace_drawers", create: bool = False
    ):
        """
        Get or create a ChromaDB collection.

        Args:
            palace_path: Path to the ChromaDB data directory.
            collection_name: Name of the collection.
            create: If True, create the collection if it doesn't exist.
        """
        import os

        os.makedirs(palace_path, exist_ok=True)
        try:
            os.chmod(palace_path, 0o700)
        except (OSError, NotImplementedError):
            pass

        client = chromadb.PersistentClient(path=palace_path)
        if create:
            col = client.get_or_create_collection(collection_name)
        else:
            col = client.get_collection(collection_name)
        return ChromaCollection(col)
