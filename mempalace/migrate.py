"""
migrate.py — LanceDB-only palace maintenance.

ChromaDB backend has been removed. This module provides LanceDB-only
maintenance operations (re-embed) and deprecated migration stubs.

Production MemPalace operations (search, write, repair, diagnostics) use
LanceDB exclusively.
"""

from __future__ import annotations

import os
import sys
import argparse


def migrate_chroma_to_lance(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    batch_size: int = 100,
    verbose: bool = True,
) -> int:
    """
    Migrate a ChromaDB palace to LanceDB.

    **REMOVED**: ChromaDB backend is no longer supported.
    LanceDB is the only available backend.

    To run this migration you must:
        pip install chromadb
        python -m mempalace.migrate chroma-to-lance --palace <path>

    This function is a stub and will raise RuntimeError.
    """
    raise RuntimeError(
        "ChromaDB backend has been removed. "
        "Cannot import chromadb — it is no longer a mempalace dependency.\n"
        "If you have existing ChromaDB data, migrate manually:\n"
        "  pip install chromadb\n"
        "  python -m mempalace.migrate chroma-to-lance --palace <path>\n"
        "Or use the standalone migration script in docs/chroma_migration_legacy.py"
    )


def migrate_lance_to_chroma(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    batch_size: int = 500,
    verbose: bool = True,
) -> int:
    """
    Migrate a LanceDB palace back to ChromaDB.

    **REMOVED**: ChromaDB is no longer a supported backend.
    LanceDB is the only available backend. This direction is no longer possible.
    """
    raise RuntimeError(
        "ChromaDB backend has been removed. LanceDB is the only supported backend.\n"
        "The lance-to-chroma migration direction is no longer available."
    )


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
