"""
migrate.py — Migrate MemPalace palace storage between backends.

# LEGACY COMPAT — ChromaDB migration path
#
# ChromaDB is no longer used as a primary backend. This module exists solely
# to migrate existing ChromaDB palaces to LanceDB. All production MemPalace
# operations (search, write, repair, diagnostics) use LanceDB exclusively.
#
# Usage:
#     python -m mempalace.migrate chroma-to-lance [--palace PATH] [--collection NAME]
#     python -m mempalace.migrate lance-to-chroma [--palace PATH] [--collection NAME]
"""

from __future__ import annotations

import os
import sys
import shutil
import argparse
from pathlib import Path


def migrate_chroma_to_lance(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    batch_size: int = 100,
    verbose: bool = True,
) -> int:
    """
    Migrate a ChromaDB palace to LanceDB.

    Args:
        palace_path: Path to the palace data directory.
        collection_name: ChromaDB collection name.
        batch_size: Records per batch (rate-limit for embedding model).
        verbose: Print progress.

    Returns:
        Number of records migrated.
    """
    # Import here so the rest of mempalace works without lancedb installed
    from mempalace.backends import get_backend, _LANCE_AVAILABLE

    if not _LANCE_AVAILABLE:
        raise ImportError(
            "LanceDB backend is not installed. "
            "Install with: pip install 'mempalace[lance]'"
        )

    import chromadb

    if verbose:
        print(f"\n{'=' * 55}")
        print("  MemPalace — ChromaDB → LanceDB Migration")
        print(f"{'=' * 55}\n")
        print(f"  Palace:  {palace_path}")
        print(f"  Collection: {collection_name}")
        print(f"  Batch size: {batch_size}")

    # ── Read from ChromaDB ─────────────────────────────────────────────────
    client = chromadb.PersistentClient(path=palace_path)
    try:
        chroma_col = client.get_collection(collection_name)
    except Exception as e:
        raise RuntimeError(f"Could not open ChromaDB collection: {e}") from e

    total = chroma_col.count()
    if total == 0:
        print("  Collection is empty — nothing to migrate.")
        return 0

    if verbose:
        print(f"  Records to migrate: {total}\n")

    # Extract all records in batches
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0
    while offset < total:
        batch = chroma_col.get(
            limit=batch_size,
            offset=offset,
            include=["documents", "metadatas"],
        )
        batch_ids = batch.get("ids", [])
        if not batch_ids:
            break
        all_ids.extend(batch_ids)
        all_docs.extend(batch.get("documents", []))
        all_metas.extend(batch.get("metadatas", []))
        offset += len(batch_ids)
        if verbose:
            print(f"  Read {offset}/{total} records...")
        if len(batch_ids) < batch_size:
            break

    if verbose:
        print(f"  Extracted {len(all_ids)} records from ChromaDB\n")

    # ── Write to LanceDB ─────────────────────────────────────────────────────
    backend = get_backend("lance")
    lance_col = backend.get_collection(palace_path, collection_name, create=True)

    migrated = 0
    for i in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[i : i + batch_size]
        batch_docs = all_docs[i : i + batch_size]
        batch_metas = all_metas[i : i + batch_size]

        try:
            lance_col.upsert(
                documents=batch_docs,
                ids=batch_ids,
                metadatas=batch_metas,
            )
            migrated += len(batch_ids)
            if verbose:
                print(f"  Migrated {migrated}/{total} records...")
        except Exception as e:
            print(f"  Error migrating batch at offset {i}: {e}")
            raise

    # ── Rebuild keyword index after migration ─────────────────────────────────
    # KeywordIndex (SQLite FTS5) is the canonical lexical engine. LanceDB FTS
    # is not queried by any search path and has been removed.
    try:
        from .diagnostics import rebuild_keyword_index
        result = rebuild_keyword_index(palace_path, batch_size=2000)
        if verbose:
            print(f"  KeywordIndex rebuilt: {result['documents_indexed']} documents, {result['batches']} batches")
    except Exception as e:
        if verbose:
            print(f"  KeywordIndex rebuild warning (non-critical): {e}")

    # ── Backup ChromaDB data ───────────────────────────────────────────────
    chroma_sqlite = Path(palace_path) / "chroma.sqlite3"
    if chroma_sqlite.exists():
        backup_path = str(chroma_sqlite) + ".bak"
        shutil.move(str(chroma_sqlite), backup_path)
        if verbose:
            print(f"  Backed up chroma.sqlite3 → chroma.sqlite3.bak")

    print(f"\n{'=' * 55}")
    print(f"  Migration complete. {migrated} records → LanceDB.")
    print(f"  Set backend='lance' in ~/.mempalace/config.json")
    print(f"  or: export MEMPALACE_BACKEND=lance")
    print(f"{'=' * 55}\n")

    return migrated


def migrate_lance_to_chroma(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    batch_size: int = 500,
    verbose: bool = True,
) -> int:
    """
    Migrate a LanceDB palace back to ChromaDB.

    Args:
        palace_path: Path to the palace data directory.
        collection_name: Collection name.
        batch_size: Records per batch.
        verbose: Print progress.

    Returns:
        Number of records migrated.
    """
    from mempalace.backends import get_backend

    if verbose:
        print(f"\n{'=' * 55}")
        print("  MemPalace — LanceDB → ChromaDB Migration")
        print(f"{'=' * 55}\n")
        print(f"  Palace:  {palace_path}")
        print(f"  Collection: {collection_name}")

    backend = get_backend("lance")
    lance_col = backend.get_collection(palace_path, collection_name, create=False)

    total = lance_col.count()
    if total == 0:
        if verbose:
            print("  Collection is empty — nothing to migrate.")
        return 0

    if verbose:
        print(f"  Records to migrate: {total}\n")

    # Read all records from LanceDB
    all_results = lance_col.get(limit=total)
    all_ids = all_results["ids"]
    all_docs = all_results["documents"]
    all_metas = all_results["metadatas"]

    if verbose:
        print(f"  Extracted {len(all_ids)} records from LanceDB\n")

    # Write to ChromaDB
    chroma_backend = get_backend("chroma")
    chroma_col = chroma_backend.get_collection(palace_path, collection_name, create=True)

    migrated = 0
    for i in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[i : i + batch_size]
        batch_docs = all_docs[i : i + batch_size]
        batch_metas = all_metas[i : i + batch_size]

        try:
            chroma_col.upsert(
                documents=batch_docs,
                ids=batch_ids,
                metadatas=batch_metas,
            )
            migrated += len(batch_ids)
            if verbose:
                print(f"  Migrated {migrated}/{total} records...")
        except Exception as e:
            print(f"  Error migrating batch at offset {i}: {e}")
            raise

    print(f"\n{'=' * 55}")
    print(f"  Migration complete. {migrated} records → ChromaDB.")
    print(f"  Set backend='chroma' in ~/.mempalace/config.json")
    print(f"  or: export MEMPALACE_BACKEND=chroma")
    print(f"{'=' * 55}\n")

    return migrated


def cmd_migrate_embeddings(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    batch_size: int = 100,
    verbose: bool = True,
) -> int:
    """
    Re-embed existing palace data with the new ModernBERT model.

    Use this after upgrading embedding dimensions (e.g., 384 → 256)
    to re-embed existing documents with the new model.

    Args:
        palace_path: Path to the palace data directory.
        collection_name: Collection name.
        batch_size: Records per batch.
        verbose: Print progress.

    Returns:
        Number of records re-embedded.
    """
    from mempalace.backends import get_backend, _LANCE_AVAILABLE

    if not _LANCE_AVAILABLE:
        raise ImportError(
            "LanceDB backend is not installed. "
            "Install with: pip install 'mempalace[lance]'"
        )

    if verbose:
        print(f"\n{'=' * 55}")
        print("  MemPalace — Re-embed with ModernBERT")
        print(f"{'=' * 55}\n")
        print(f"  Palace:  {palace_path}")
        print(f"  Collection: {collection_name}")
        print(f"  Batch size: {batch_size}")

    backend = get_backend("lance")
    lance_col = backend.get_collection(palace_path, collection_name, create=False)

    total = lance_col.count()
    if total == 0:
        if verbose:
            print("  Collection is empty — nothing to re-embed.")
        return 0

    if verbose:
        print(f"  Records to re-embed: {total}\n")

    # Import the embed function to get new embeddings
    try:
        from mempalace.backends.lance import _embed_texts
    except ImportError:
        if verbose:
            print("  Error: Could not import embed function")
        return 0

    # Read all records
    all_results = lance_col.get(limit=total)
    all_ids = all_results["ids"]
    all_docs = all_results["documents"]
    all_metas = all_results["metadatas"]

    if verbose:
        print(f"  Read {len(all_ids)} records\n")

    # Re-embed and update in batches
    reembedded = 0
    for i in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[i : i + batch_size]
        batch_docs = all_docs[i : i + batch_size]
        batch_metas = all_metas[i : i + batch_size]

        try:
            # Re-embed with new model
            new_embeddings = _embed_texts(batch_docs)

            # Update each record with new embedding
            for record_id, doc, meta, new_emb in zip(batch_ids, batch_docs, batch_metas, new_embeddings):
                import json
                lance_col.upsert(
                    ids=[record_id],
                    documents=[doc],
                    metadatas=[meta],
                )
            reembedded += len(batch_ids)
            if verbose:
                print(f"  Re-embedded {reembedded}/{total} records...")
        except Exception as e:
            if verbose:
                print(f"  Error re-embedding batch at offset {i}: {e}")
            raise

    print(f"\n{'=' * 55}")
    print(f"  Re-embedding complete. {reembedded} records updated.")
    print(f"{'=' * 55}\n")

    return reembedded


def main():
    parser = argparse.ArgumentParser(description="MemPalace Backend Migration Tool")
    parser.add_argument(
        "direction",
        choices=["chroma-to-lance", "lance-to-chroma", "reembed"],
        help="Migration direction",
    )
    parser.add_argument(
        "--palace",
        default=None,
        help="Palace path (default: from config or ~/.mempalace/palace)",
    )
    parser.add_argument(
        "--collection",
        default="mempalace_drawers",
        help="Collection name (default: mempalace_drawers)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Batch size for migration (default: 100)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

    args = parser.parse_args()

    if args.palace:
        palace_path = os.path.expanduser(args.palace)
    else:
        from mempalace.config import MempalaceConfig

        palace_path = MempalaceConfig().palace_path

    verbose = not args.quiet

    try:
        if args.direction == "chroma-to-lance":
            migrate_chroma_to_lance(
                palace_path=palace_path,
                collection_name=args.collection,
                batch_size=args.batch_size,
                verbose=verbose,
            )
        elif args.direction == "lance-to-chroma":
            migrate_lance_to_chroma(
                palace_path=palace_path,
                collection_name=args.collection,
                batch_size=args.batch_size,
                verbose=verbose,
            )
        elif args.direction == "reembed":
            cmd_migrate_embeddings(
                palace_path=palace_path,
                collection_name=args.collection,
                batch_size=args.batch_size,
                verbose=verbose,
            )
    except Exception as e:
        print(f"  Migration failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
