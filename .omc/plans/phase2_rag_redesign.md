# Phase 2: Code-Aware RAG for MemPalace

## Analysis Summary

### What needs to change

| Component | Current | Phase 2 Target |
|-----------|---------|----------------|
| **Chunking** | Fixed 800-char with paragraph awareness | Language-aware structural split (functions/classes for code, paragraphs for prose) |
| **Metadata** | No language/line/symbol fields | `language`, `line_start`, `line_end`, `symbol_name`, `symbol_kind` per chunk |
| **Revision model** | `is_latest` exists but never updated on re-mine; old chunks orphaned | Tombstone old chunks on re-mine; `content_hash` tracking; `revision_id` |
| **Retrieval** | Single pipeline for all content | Split: `code_search()` vs `diary_search()` vs `decisions_search()` |
| **Lexical layer** | In-memory `rank_bm25` rebuilt on every search | SQLite FTS5 persistent index, updated on upsert |
| **MCP tools** | 27 tools, no code-specific retrieval | Add `mempalace_search_code`, `mempalace_file_context` |

### Key files to modify
- `mempalace/miner.py` — structural chunking, tombstone logic, code metadata
- `mempalace/backends/base.py` — extend canonical contract with code-aware fields
- `mempalace/searcher.py` — split retrieval paths, FTS5 lexical layer
- `mempalace/fastmcp_server.py` — new MCP tools for code retrieval
- `mempalace/backends/lance.py` — FTS5 index population

---

## Implementation

### Step 1: Extend canonical metadata contract (base.py)
Add optional fields for code-aware metadata:
- `language: str` — detected from file extension
- `line_start: int` — 1-based start line
- `line_end: int` — 1-based end line
- `symbol_name: str` — nearest function/class definition name
- `symbol_scope: str` — module.class.method dotted path
- `chunk_kind: str` — "code_block", "prose", "comment", "docstring"
- `revision_id: str` — file revision identifier (file hash or mtime bucket)
- `content_hash: str` — SHA256 of chunk content (for tombstone detection)

### Step 2: Structural code chunking (miner.py)
Add `split_code_structurally()`:
- Detect language from extension
- For Python: split on `def `, `class `, `async def ` at line start (not in string/comment)
- For JS/TS: split on `function `, `const `, `class `, `async ` at line start
- For Java/C/Go: split on brace-block boundaries and function declarations
- Fallback: current paragraph chunking for non-code files
- Each chunk gets `line_start`, `line_end`, `language`, `chunk_kind` metadata

### Step 3: Revision-based ingest (miner.py)
In `process_file()`:
1. Before upserting new chunks, query existing records where `source_file` matches AND `is_latest=True`
2. Compute `content_hash` (SHA256) for each new chunk
3. For old records that have NO matching new content_hash → tombstone them (`is_latest=False`, `supersedes_id` chain)
4. New chunks reference their predecessor via `supersedes_id`
5. Set `revision_id` to current file hash (SHA256 of first 4KB + mtime)

### Step 4: SQLite FTS5 lexical layer (new file: `mempalace/lexical_index.py`)
- `KeywordIndex` class manages FTS5 virtual table in `{palace_path}/keyword_index.sqlite3`
- Schema: `drawer_id, content, wing, room, language`
- `upsert_drawer(doc_id, content, wing, room, language)` — called after each LanceDB upsert
- `search(query, n_results, wing, room, language)` — returns ranked drawer_ids with BM25 scores
- `delete_drawer(doc_id)` — called when a drawer is deleted/tombstoned
- Tokenizer: `porter unicode61` (Porter stemmer + Unicode 61 normalization)
- On search: fallback to in-memory BM25 if FTS5 unavailable

### Step 5: Split retrieval paths (searcher.py)
Add three distinct search paths:

**`code_search(query, language=None, symbol=None, file_path=None, n_results=10)`**
- Filters: wing=repo, is_latest=True
- Uses: vector search + FTS5 (for exact identifier match) + optional symbol filter
- Ranking: vector similarity weighted by recency and symbol exact-match bonus

**`diary_search(query, session_id=None, n_results=10)`**
- Filters: wing IN (session, archive), is_latest=True
- Uses: FTS5 + BM25 + time-decay vector
- Ranking: BM25 + recency

**`decisions_search(query, category=None, n_results=10)`**
- Filters: wing=decision, is_latest=True
- Uses: vector + FTS5
- Ranking: importance score + recency

### Step 6: Query routing
In `hybrid_search()`, detect query type:
- Code-like: contains `def `, `function `, `class `, `import `, `=>`, `.py`, `.js`, `()`, camelCase patterns
- Route to appropriate specialized path
- Keep `hybrid_search` as fallback for ambiguous queries

### Step 7: MCP tools (fastmcp_server.py)
Add/extend:
- `mempalace_search_code(query, language=None, symbol=None, file_path=None)` → calls `code_search()`
- `mempalace_file_context(file_path, line_start=None, line_end=None)` → reads file slice directly, no DB needed
- Extend `mempalace_remember_code` with `language`, `line_start`, `line_end` params

---

## Design Decisions

1. **Why not tree-sitter?** Too heavy for M1 8GB. Regex heuristics with high recall are sufficient for Phase 2. tree-sitter is Phase 3 candidate.

2. **Why SQLite FTS5 over in-memory BM25?** Persists across restarts, handles unlimited corpus, native on M1 (no external service), BM25 scores are queryable with filters (wing/room/language).

3. **Why SHA256 content_hash for tombstone?** Content-addressable identity is more robust than source_file+chunk_index (which changes when file is re-chunked differently).

4. **Why split retrieval paths?** Different content types have different retrieval needs. Code retrieval benefits from exact identifier matching (FTS5) and structural proximity. Diary/decision retrieval benefits from temporal context and keyword relevance.

---

## Tests to add
- `test_structural_chunking_python()` — verify Python function boundaries are respected
- `test_structural_chunking_js()` — verify JS/TS class/function boundaries
- `test_revision_tombstone()` — verify old chunks are tombstoned when file is re-mined
- `test_content_hash_tombstone()` — verify content identity (same content = no tombstone on re-mining)
- `test_code_search_filters()` — verify language/symbol filters in code search
- `test_fts5_index_population()` — verify FTS5 is updated on upsert
- `test_query_routing()` — verify code-like vs prose-like query routing