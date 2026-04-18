"""
namespaces.py — Canonical namespace abstraction for MemPalace.

Phase 1 establishes a logical namespace abstraction (wing/room) on top of the
existing single-collection storage. This is a *soft* abstraction — physical
storage schema is unchanged — and provides a road map for Phase 2 physical
separation (per-namespace LanceDB tables).

The 5 Canonical Namespaces
=========================

===========  ========  ============================  =========  ======================
Namespace    Wing      Room                         origin_    Description
                                    Data Type     type
===========  ========  ============================  =========  ======================
repo_chunks  repo     per-file-path (normalized)   code_      Source code file chunks
                                              memory
session_     session   {session_id}                 observ-    Per-session memories
memory                                         ation
handoffs     handoff  {from_session_id}             observ-    Cross-session handoffs
                                                 ation
decisions    decision  {category}                   observ-    Architectural decisions
                                                 ation
chat_        archive  {YYYY-MM}                    convo      Archived conversation
archive                                                  history
===========  ========  ============================  =========  ======================

Usage
-----
Writers call :func:`resolve_namespace` with origin_type and context::

    wing, room = resolve_namespace(
        origin_type="code_memory",
        source_file="/path/to/my_project/src/main.py",
    )
    # → ("repo", "path_to_my_project_src_main_py")

:func:`normalize_room_name` is available for ad-hoc room normalization.
:func:`is_valid_namespace` validates wing/room pairs before writing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MemoryNamespace(Enum):
    """
    The 5 canonical logical namespaces in MemPalace.

    Each namespace maps to a wing/room pair used for filtering at query time.
    Physical storage may colocate multiple namespaces in a single LanceDB
    collection; the namespace is encoded in the ``wing`` field.
    """

    REPO_CHUNKS = "repo_chunks"
    SESSION_MEMORY = "session_memory"
    HANDOFFS = "handoffs"
    DECISIONS = "decisions"
    CHAT_ARCHIVE = "chat_archive"


@dataclass
class NamespaceConfig:
    """
    Configuration for a single canonical namespace.

    Attributes
    ----------
    wing:
        The wing value written to every record in this namespace.
        Used as the primary filter at query time.
    room:
        The room template. May contain placeholders such as ``{session_id}``
        or ``{YYYY-MM}`` that are substituted at write time by
        :func:`resolve_namespace`.
    namespace:
        The :class:`MemoryNamespace` enum member.
    description:
        Human-readable description of what this namespace holds.
    suggested_origin_types:
        The ``origin_type`` values that typically route to this namespace.
        These are advisory hints for callers, not strict enforcement.
    ttl_days:
        Soft retention hint in days. ``None`` means no expiry.
        (Phase 2 physical separation will enforce this via TTL indexes.)
    """

    wing: str
    room: str
    namespace: MemoryNamespace
    description: str
    suggested_origin_types: list[str]
    ttl_days: Optional[int]


# -------------------------------------------------------------------------------------------------
# Canonical namespace configurations (hardcoded — Phase 1)
# -------------------------------------------------------------------------------------------------

NAMESPACE_CONFIGS: dict[MemoryNamespace, NamespaceConfig] = {
    MemoryNamespace.REPO_CHUNKS: NamespaceConfig(
        wing="repo",
        room="{source_file_normalized}",
        namespace=MemoryNamespace.REPO_CHUNKS,
        description="Source code chunks indexed from project files",
        suggested_origin_types=["code_memory"],
        ttl_days=None,
    ),
    MemoryNamespace.SESSION_MEMORY: NamespaceConfig(
        wing="session",
        room="{session_id}",
        namespace=MemoryNamespace.SESSION_MEMORY,
        description="Per-session observations and memories",
        suggested_origin_types=["observation"],
        ttl_days=30,
    ),
    MemoryNamespace.HANDOFFS: NamespaceConfig(
        wing="handoff",
        room="{from_session_id}",
        namespace=MemoryNamespace.HANDOFFS,
        description="Cross-session handoff documents",
        suggested_origin_types=["observation"],
        ttl_days=14,
    ),
    MemoryNamespace.DECISIONS: NamespaceConfig(
        wing="decision",
        room="{category}",
        namespace=MemoryNamespace.DECISIONS,
        description="Architectural and design decisions",
        suggested_origin_types=["observation"],
        ttl_days=None,
    ),
    MemoryNamespace.CHAT_ARCHIVE: NamespaceConfig(
        wing="archive",
        room="{YYYY-MM}",
        namespace=MemoryNamespace.CHAT_ARCHIVE,
        description="Archived conversation history",
        suggested_origin_types=["convo"],
        ttl_days=90,
    ),
}


# -------------------------------------------------------------------------------------------------
# Physical namespace separation (Phase 5)
# -------------------------------------------------------------------------------------------------
# Mapping from wing to LanceDB collection name.
# Each namespace gets its own collection for physical separation.

WING_TO_COLLECTION: dict[str, str] = {
    "repo": "repo_chunks",
    "session": "session_memory",
    "handoff": "handoffs",
    "decision": "decisions",
    "archive": "chat_archive",
}


def get_collection_name_for_wing(wing: Optional[str]) -> str:
    """
    Resolve wing to the corresponding LanceDB collection name.

    If wing is None or not recognized, returns the default collection name.

    Args:
        wing: The wing value (e.g., "repo", "session", "handoff", "decision", "archive")

    Returns:
        Collection name for the wing, or "mempalace_drawers" as default
    """
    if wing is None:
        return "mempalace_drawers"
    return WING_TO_COLLECTION.get(wing, "mempalace_drawers")


# -------------------------------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------------------------------

# Max room name length (LanceDB VARCHAR limit + safety margin)
_MAX_ROOM_LEN = 128

# Pattern: alphanumeric + underscore only
_ROOM_SANITIZE_RE = re.compile(r"[^a-z0-9_]")


def normalize_room_name(value: Optional[str]) -> str:
    """
    Normalize ``value`` into a safe, consistent room name.

    Applied transformations (in order):
      1. Strip leading/trailing whitespace.
      2. Replace path separators ``/`` and ``\\`` with ``_``.
      3. Replace dots ``.`` with ``_`` (prevents path-traversal confusion).
      4. Lowercase.
      5. Collapse multiple consecutive underscores into one.
      6. Strip leading/trailing underscores.
      7. Truncate to :const:`_MAX_ROOM_LEN` (128) characters.

    Parameters
    ----------
    value:
        The raw string to normalize. ``None`` or empty string returns ``"default"``.

    Returns
    -------
    str
        A safe room name containing only ``[a-z0-9_]``.

    Examples
    --------
    >>> normalize_room_name("/path/to/my_project/src/main.py")
    'path_to_my_project_src_main_py'
    >>> normalize_room_name("session-abc-123")
    'session_abc_123'
    >>> normalize_room_name(None)
    'default'
    >>> normalize_room_name("")
    'default'
    """
    if not value:
        return "default"

    normalized = value.strip()

    # Replace path separators, dots, and hyphens with underscores
    normalized = normalized.replace("/", "_").replace("\\", "_").replace(".", "_").replace("-", "_")

    # Lowercase
    normalized = normalized.lower()

    # Remove any other disallowed characters
    normalized = _ROOM_SANITIZE_RE.sub("", normalized)

    # Collapse multiple underscores
    normalized = re.sub(r"__+", "_", normalized)

    # Strip leading/trailing underscores
    normalized = normalized.strip("_")

    # Empty after normalization → fallback
    if not normalized:
        return "default"

    # Truncate
    return normalized[:_MAX_ROOM_LEN]


def is_valid_namespace(wing: str, room: str) -> bool:
    """
    Validate that ``wing`` and ``room`` conform to namespace conventions.

    Conventions
    -----------
    - ``wing``: 1–32 chars, lowercase alphanumeric + underscore.
    - ``room``: 1–128 chars, lowercase alphanumeric + underscore.
    - Room must not be ``"default"`` (reserved fallback).

    This function is advisory — MemPalace accepts arbitrary wing/room pairs
    for backward compatibility — but writers should call it before writing
    to catch configuration errors early.

    Parameters
    ----------
    wing:
        The wing value to validate.
    room:
        The room value to validate.

    Returns
    -------
    bool
        ``True`` if the pair conforms to conventions, ``False`` otherwise.

    Examples
    --------
    >>> is_valid_namespace("repo", "path_to_my_project_src_main_py")
    True
    >>> is_valid_namespace("repo", "/path/to/file.py")   # contains slash
    False
    >>> is_valid_namespace("", "room")
    False
    """
    if not wing or len(wing) > 32:
        return False
    if not room or room == "default" or len(room) > _MAX_ROOM_LEN:
        return False

    valid_pattern = re.compile(r"^[a-z0-9_]+$")
    return bool(valid_pattern.match(wing)) and bool(valid_pattern.match(room))


def resolve_namespace(
    origin_type: str,
    source_file: Optional[str] = None,
    session_id: Optional[str] = None,
    category: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> tuple[str, str]:
    """
    Resolve ``origin_type`` + context → canonical ``(wing, room)`` pair.

    This is the **canonical mapping function** used by all writers.
    Callers pass context and receive the correct wing/room values to write.

    Resolution Logic
    ----------------
    ===========  ================  =================================================================
    origin_type  Additional       Result
                context
    ===========  ================  =================================================================
    ``code_memory``  ``source_file``  ``("repo", normalized(source_file))``
    ``convo``        ``timestamp``     ``("archive", YYYY-MM from timestamp)``
    ``observation``  ``source_file``  ``("decision", category or "general")`` if "decision" in
                 contains "decision"   source_file; otherwise ``("handoff", from_session_id)`` if
                 "handoff" in          "handoff" in source_file; otherwise
                 source_file           ``("session", session_id)``
    ``observation``  ``session_id``    ``("session", session_id)``
    (fallback)                        ``("session", session_id or "default")``
    ===========  ================  =================================================================

    Parameters
    ----------
    origin_type:
        The type of memory being stored.
        Accepted values: ``"code_memory"``, ``"observation"``, ``"convo"``.
    source_file:
        Path to the source file (used for ``code_memory`` and some
        ``observation`` variants). Used to derive the room for ``repo_chunks``
        and to detect handoff/decision markers.
    session_id:
        The session identifier. Used as the room for ``session_memory`` and
        as the ``from_session_id`` for ``handoffs``.
    category:
        Optional category string used as the room for ``decisions``.
    timestamp:
        ISO-8601 timestamp string (``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS``).
        Used to derive the month for ``chat_archive``.

    Returns
    -------
    tuple[str, str]
        ``(wing, room)`` — the canonical wing/room pair to write.

    Raises
    ------
    ValueError:
        If ``origin_type`` is not recognized and no session_id is available
        to fall back on.

    Examples
    --------
    >>> resolve_namespace("code_memory", source_file="/src/main.py")
    ('repo', 'src_main_py')

    >>> resolve_namespace("convo", timestamp="2026-04-17T10:30:00")
    ('archive', '2026-04')

    >>> resolve_namespace("observation", session_id="sess-abc")
    ('session', 'sess_abc')

    >>> resolve_namespace("observation",
    ...     source_file="/notes/decision-architecture.md",
    ...     category="architecture")
    ('decision', 'architecture')
    """
    origin_type = origin_type.strip().lower() if origin_type else ""

    # ---------------------------------------------------------------------------------------------
    # code_memory → repo_chunks
    # ---------------------------------------------------------------------------------------------
    if origin_type == "code_memory":
        wing = NAMESPACE_CONFIGS[MemoryNamespace.REPO_CHUNKS].wing
        room = normalize_room_name(source_file) if source_file else "default"
        return (wing, room)

    # ---------------------------------------------------------------------------------------------
    # convo → chat_archive
    # ---------------------------------------------------------------------------------------------
    if origin_type == "convo":
        wing = NAMESPACE_CONFIGS[MemoryNamespace.CHAT_ARCHIVE].wing
        if timestamp:
            # Extract YYYY-MM from common ISO formats
            m = re.match(r"(\d{4}-\d{2})", timestamp)
            if m:
                room = m.group(1)
            else:
                room = "default"
        else:
            room = "default"
        return (wing, room)

    # ---------------------------------------------------------------------------------------------
    # observation — detect decision / handoff markers in source_file
    # ---------------------------------------------------------------------------------------------
    if origin_type == "observation":
        sf = (source_file or "").lower()

        # decision marker in source path → decisions
        if "decision" in sf:
            wing = NAMESPACE_CONFIGS[MemoryNamespace.DECISIONS].wing
            room = normalize_room_name(category) if category else "general"
            return (wing, room)

        # handoff marker in source path → handoffs
        if "handoff" in sf:
            wing = NAMESPACE_CONFIGS[MemoryNamespace.HANDOFFS].wing
            # room = from_session_id (the session that created the handoff)
            room = normalize_room_name(session_id) if session_id else "default"
            return (wing, room)

        # plain observation + session_id → session_memory
        if session_id:
            wing = NAMESPACE_CONFIGS[MemoryNamespace.SESSION_MEMORY].wing
            room = normalize_room_name(session_id)
            return (wing, room)

    # ---------------------------------------------------------------------------------------------
    # Fallback — session_memory with available session_id or "default"
    # ---------------------------------------------------------------------------------------------
    wing = NAMESPACE_CONFIGS[MemoryNamespace.SESSION_MEMORY].wing
    room = normalize_room_name(session_id) if session_id else "default"
    return (wing, room)
