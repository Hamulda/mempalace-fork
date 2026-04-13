"""
Base collection interface for MemPalace storage backends.

All backends (ChromaDB, LanceDB, etc.) must implement this ABC
to ensure compatibility with the rest of the MemPalace stack.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseCollection(ABC):
    """
    Abstract collection interface.

    ChromaDB compatibility is the reference contract:
    - add()        — insert new records (raises on duplicate ids)
    - upsert()     — insert or replace by id
    - query()      — vector/hybrid search, returns nested-list format
    - get()        — record lookup by id/where, returns flat-list format
    - delete()     — remove records
    - count()      — total record count
    """

    @abstractmethod
    def add(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Insert new records. Raises ValueError if any id already exists."""

    @abstractmethod
    def upsert(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Insert or replace records by id."""

    @abstractmethod
    def query(
        self,
        query_texts: Optional[List[str]] = None,
        n_results: int = 10,
        where: Optional[Dict[str, Any]] = None,
        include: Optional[List[str]] = None,
        **kwargs,
    ) -> Dict[str, List[List[Any]]]:
        """
        Search for records.

        Returns ChromaDB-compatible nested-list format:
            {
                "ids": [[id1, id2, ...]],
                "documents": [[doc1, doc2, ...]],
                "metadatas": [[{...}, {...}, ...]],
                "distances": [[0.1, 0.2, ...]]
            }
        """

    @abstractmethod
    def get(
        self,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        include: Optional[List[str]] = None,
        **kwargs,
    ) -> Dict[str, List[Any]]:
        """
        Get records by id or where filter.

        Returns ChromaDB-compatible flat-list format:
            {
                "ids": [id1, id2],
                "documents": [doc1, doc2],
                "metadatas": [{...}, {...}]
            }
        """

    @abstractmethod
    def delete(self, ids: Optional[List[str]] = None, where: Optional[Dict[str, Any]] = None, **kwargs) -> None:
        """Delete records by id or where filter."""

    @abstractmethod
    def count(self) -> int:
        """Return total number of records."""

    @abstractmethod
    def get_by_id(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Get a single record by id. Returns None if not found."""
