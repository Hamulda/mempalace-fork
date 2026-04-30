# PHASE32: Path Metadata Index — Implementation Report

## What was built

A new SQLite auxiliary index (`path_index`) mirrors the KeywordIndex pattern for O(1) path lookups without scanning LanceDB metadata.

### New files

| File | Purpose |
|------|---------|
| `mempalace/path_index.py` | PathIndex class — singleton, thread-safe SQLite index |
| `tests/test_path_index.py` | 26 tests covering CRUD, search priority, isolation, tombstone |

### Modified files

| File | Change |
|------|--------|
| `mempalace/backends/lance.py` | `_sync_path_index_upsert()` + `_sync_path_index_delete()` wired into upsert/add/delete flows |
| `mempalace/searcher.py` | `_path_metadata_search()` — Phase 1 now uses PathIndex before LanceDB scan |

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS path_index (
  document_id TEXT PRIMARY KEY,
  source_file TEXT NOT NULL,
  repo_rel_path TEXT,
  basename TEXT NOT NULL,        -- explicit (not generated column)
  language TEXT,
  chunk_kind TEXT,
  symbol_name TEXT,
  line_start INTEGER,
  line_end INTEGER,
  wing TEXT,
  room TEXT,
  is_latest INTEGER DEFAULT 1
);
CREATE INDEX idx_path_source_file ON path_index(source_file);
CREATE INDEX idx_path_repo_rel_path ON path_index(repo_rel_path);
CREATE INDEX idx_path_basename ON path_index(basename);
```

---

## Sync on writes

```
upsert() / _do_add()
  └─> _sync_fts5upsert()     [unchanged]
  └─> _sync_path_index_upsert()  [NEW — extracts metadata, upserts path_index rows]

delete()
  └─> _sync_fts5delete()    [unchanged]
  └─> _sync_path_index_delete()  [NEW — deletes by document_id]
```

PathIndex sync is fire-and-forget (failures logged, not raised). Staleness recoverable via future rebuild.

---

## Search fallback order (updated)

```
_path_metadata_search()
  Phase 1: PathIndex.search_path()  — fast SQLite exact/suffix/basename
  Phase 2: Bounded LanceDB metadata scan (max 5000 chunks)
  Phase 3: Bounded FTS5 content fallback
```

---

## Test results

```
tests/test_path_index.py                   26 passed
tests/test_lance_codebase_rag_e2e.py       22 passed
tests/test_scoped_retrieval_e2e.py         all passed
tests/test_source_code_ranking.py           1 pre-existing failure (unrelated)
tests/test_source_code_ranking_preslice.py all passed
chromadb in sys.modules                    False ✓
```

---

## Key design decisions

1. **Explicit `basename` column** — avoids SQLite generated column compatibility issues. Auto-extracted from `source_file` if not provided.

2. **PathIndex upsert uses `INSERT OR REPLACE`** — same document_id overwrites, no duplicates.

3. **`is_latest` filter on all searches** — tombstoned rows excluded by default; `mark_tombstoned()` sets flag to 0 rather than deleting.

4. **project_path filter is STRICT** — `source_file LIKE prefix%` in SQL, not post-filter. Only rows within the project scope are returned.

5. **No new public APIs** — `PathIndex` is internal; write sync happens transparently in `lance.py`.

---

## Files created / modified

- `mempalace/path_index.py` — created
- `tests/test_path_index.py` — created
- `mempalace/backends/lance.py` — modified
- `mempalace/searcher.py` — modified
