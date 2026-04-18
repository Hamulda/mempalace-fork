"""
Base collection interface for MemPalace storage backends.

All backends (ChromaDB, LanceDB, etc.) must implement this ABC
to ensure compatibility with the rest of the MemPalace stack.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════════════════════
# CANONICAL METADATA CONTRACT
# ═══════════════════════════════════════════════════════════════════════════════
#
# All MemPalace writers (add_drawer, diary_write, remember_code, convo miner,
# project miner) MUST produce metadata conforming to this contract.
#
# MANDATORY FIELDS (all writers must populate):
#   wing          — str: logical group (project name, agent name, etc.)
#   room          — str: finer category within wing (topic, aspect, etc.)
#   source_file   — str: provenance — file path or identifier this memory came from
#   added_by      — str: agent/user who filed this (e.g. "mcp", "mempalace", "user_name")
#   agent_id      — str: same as added_by (for filter compatibility)
#   timestamp     — str: UTC ISO8601 when filed (e.g. "2026-04-14T10:30:00.000Z")
#   is_latest     — bool: True for current version, False for superseded/historical
#   supersedes_id — str: id of the record this supersedes (empty string if new)
#   origin_type   — str: why this was created
#                    Values: "observation" | "diary_entry" | "code_memory" | "convo"
#   chunk_index   — int: for multi-chunk files; 0 for single-unit entries
#
# OPTIONAL FIELDS (populated by specific writers when applicable):
#   source_mtime   — float: mtime of source_file at time of mining (for change detection)
#   entities      — str: JSON-encoded list of detected entity names
#   description   — str: human description (used by code_memory entries)
#   importance     — float: relevance weight 0.0-1.0 (used by L1 generation)
#   ingest_mode   — str: "convos" | "general" | "exchange" (convo miner only)
#   extract_mode  — str: "exchange" | "general" (convo miner only)
#
# CODE-AWARE FIELDS (populated by project miner for code files):
#   language      — str: programming language detected from file extension
#                    (e.g. "Python", "JavaScript", "TypeScript", "Go", "Rust", "Java")
#   line_start    — int: 1-based start line of this chunk in the source file
#   line_end      — int: 1-based end line of this chunk in the source file
#   symbol_name   — str: name of nearest function/class definition (if applicable)
#   symbol_scope  — str: dotted path of containing scope (e.g. "MyClass.my_method")
#   chunk_kind    — str: kind of content in this chunk
#                    Values: "code_block" | "prose" | "comment" | "docstring" | "mixed"
#   revision_id   — str: revision identifier for the source file (SHA256 of first 4KB + mtime)
#   content_hash  — str: SHA256 of the chunk content itself (used for tombstone detection)
#
# TIMESTAMP POLICY:
#   Use timestamp (UTC ISO8601 with Z suffix) consistently everywhere.
#   filed_at is DEPRECATED — use timestamp only. Both may coexist during
#   the F176 migration window; code that reads must handle both.
#
# is_latest / supersedes_id SEMANTICS:
#   When a new version of an existing memory is created (e.g. file re-mined):
#     1. Set is_latest=False on the old record
#     2. Set supersedes_id on the new record to old record's id
#     3. Set is_latest=True on the new record
#   This enables "show me only current facts" queries via is_latest=True filter.
#
# BACKWARD COMPATIBILITY:
#   Readers must tolerate missing optional fields (default to empty/None).
#   Writers should only add fields they can populate correctly.
#
# ═══════════════════════════════════════════════════════════════════════════════


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
