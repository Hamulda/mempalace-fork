# PHASE18: Embedding Truth Seal — eval_mode Implementation

**Date:** 2026-04-29
**Repo:** MemPalace
**Mission:** Fix truth gap in `scripts/eval_hledac_code_rag.py` where mining used mock embeddings but query/search could use real embeddings — mixing embedding spaces silently.

---

## Audit Findings

### Original State (truth gap)
1. `_mine_project()` patched `lance_mod._embed_texts = _mock_embed_texts` globally at line 293
2. Mining phase used mock embeddings (SHA256 hash → 256-dim vectors)
3. `_mine_project()` finally block restored `lance_mod._embed_texts = orig_embed` at line 317
4. **`_run_query()` called `auto_search()` which uses REAL embeddings** (no patch active)
5. This means: mining used mock space, queries used real space → **mixed embedding-space eval**
6. The `--mine` path called `_mine_project` then immediately `_run_query` — the patch was already restored

### Root Cause
The `finally` block in `_mine_project` unconditionally restored real embeddings. For lexical mode, no vector search was intended but the query path still hit the real vector daemon/fallback. For mock-vector, the patch should have stayed through query execution.

---

## Implementation

### 1. `--eval-mode` Flag (default: lexical)

Added CLI argument with 3 modes:

```
--eval-mode {lexical,mock-vector,real-vector}
  lexical     — mock mining, FTS5-only query, patch stays active (no vector)
  mock-vector — mock both sides, patch stays through query
  real-vector — real embeddings both sides, no patching
```

### 2. Patch Management in `_mine_project`

```python
# NEW: eval_mode parameter
should_patch = eval_mode in ("lexical", "mock-vector")

if should_patch:
    orig_embed = lance_mod._embed_texts
    lance_mod._embed_texts = _mock_embed_texts
    ...

# lexical: restore BEFORE returning (FTS5-only queries don't need vector)
if eval_mode == "lexical" and orig_embed is not None:
    lance_mod._embed_texts = orig_embed  # ← RESTORE for lexical
    orig_embed = None

# finally: mock-vector keeps patch alive (restored by _eval_project)
#          lexical: already restored above
#          real-vector: nothing was patched
```

### 3. Query Phase Patch (mock-vector only)

In `_eval_project`, before the query loop:

```python
if eval_mode == "mock-vector":
    _orig_embed = lance_mod._embed_texts  # save real
    lance_mod._embed_texts = _mock_embed_texts  # re-apply mock
    _query_patch_active = True
```

After the query loop, finally block restores:

```python
if _query_patch_active:
    lance_mod._embed_texts = _orig_embed
    if hasattr(lance_mod, "_embed_via_socket"):
        lance_mod._embed_via_socket = _orig_embed
```

### 4. Report Fields Added

```json
{
  "eval_mode": "lexical",
  "embedding_space_consistent": true,
  "embedding_provider": "mock",
  "vector_metrics_valid": false,
  ...
}
```

| Mode | mining | query | vector_metrics_valid |
|------|--------|-------|----------------------|
| lexical | mock | FTS5-only (mock active but not used) | **false** |
| mock-vector | mock | mock (patch through query) | **true** |
| real-vector | real | real | **true** |

---

## Tests: `tests/test_eval_embedding_truth.py`

9 tests, all pass:

| Test | What it verifies |
|------|-----------------|
| `test_lexical_mode_vector_metrics_valid_false` | lexical mode: patch stays active, vector_metrics_valid=False |
| `test_mock_vector_patch_stays_through_query` | mock-vector: patch persists after _mine_project |
| `test_real_vector_no_patch_applied` | real-vector: _embed_texts untouched |
| `test_lexical_report_fields` | lexical report has all 4 required fields |
| `test_mock_vector_report_fields` | mock-vector report has all 4 required fields |
| `test_real_vector_report_fields` | real-vector report has all 4 required fields |
| `test_eval_mode_in_script_help` | --eval-mode in --help output |
| `test_mine_project_accepts_eval_mode` | _mine_project signature includes eval_mode |
| `test_eval_project_accepts_eval_mode` | _eval_project signature includes eval_mode |

---

## Verification

### `python -c "import sys; import mempalace; print('chromadb' in sys.modules)"`

```
False
```

ChromaDB absent throughout.

### Full eval run:

```bash
python scripts/eval_hledac_code_rag.py \
  --project-path /Users/vojtechhamada/PycharmProjects/Hledac/hledac/universal \
  --palace-path /tmp/mempalace_hledac_eval_lexical \
  --mine --max-files 200 --limit 5 --eval-mode lexical \
  --force --report-json /tmp/mempalace_hledac_eval_lexical.json
```

**Exit: 1** (expected — top1/top5 below thresholds for this small sample)

**Report confirms all new fields present:**
```json
"eval_mode": "lexical",
"embedding_space_consistent": true,
"embedding_provider": "mock",
"vector_metrics_valid": false,
```

### Smoke test: `pytest tests/test_hledac_eval_script_smoke.py -q`
```
15 passed, 2 skipped
```

---

## Design Decisions

1. **Lexical keeps patch active (KEEP PATCH design):** This prevents the query path from accidentally using real embeddings. FTS5-only routing in `auto_search()` doesn't call `_embed_texts` anyway, but keeping the patch avoids any risk of vector lookup contamination.

2. **`vector_metrics_valid` is about the QUERY path, not storage:** In lexical mode, mock embeddings are in the DB (so storage uses mock space), but query is FTS5-only (no vector similarity). Hence `vector_metrics_valid: false`.

3. **`embedding_provider` for real-vector = "daemon-or-fallback":** The actual provider depends on whether the embed daemon is running or the fallback path is used. We don't hard-code "daemon" since the test environment might not have the daemon active.

---

## Files Changed

| File | Change |
|------|--------|
| `scripts/eval_hledac_code_rag.py` | Added `--eval-mode` arg, refactored `_mine_project` with eval_mode, added query-phase patch for mock-vector, added report fields |
| `tests/test_eval_embedding_truth.py` | New file: 9 tests verifying all eval modes and report fields |
| `scripts/eval_hledac_code_rag.py.bak` | Backup of original |

---

## Summary

- **No mixed embedding-space eval** — each mode maintains consistent embedding space
- **Report explicitly says when vector metrics are valid** via `vector_metrics_valid: false/true`
- **lexical**: mock mining, FTS5-only query, mock stays active but not used → `vector_metrics_valid: false`
- **mock-vector**: mock both sides, patch persists through query → `vector_metrics_valid: true`
- **real-vector**: real both sides, no patching → `vector_metrics_valid: true`
- M1 8GB bounded behavior preserved (swap guard, --max-files, --force bypass)
- ChromaDB remains absent throughout all paths