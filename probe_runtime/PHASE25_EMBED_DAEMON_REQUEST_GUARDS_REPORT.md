# PHASE25: Embed Daemon Request Guards

**Date:** 2026-04-30
**Status:** COMPLETE

## Changes Made

### 1. `mempalace/embed_daemon.py` — Request-Size Guards Added

**New constants** (env-configurable):
```
MEMPALACE_EMBED_MAX_REQUEST_BYTES = 2_000_000  (~2MB default)
MEMPALACE_EMBED_MAX_TEXTS = 512               (512 texts default)
MEMPALACE_EMBED_MAX_CHARS_PER_TEXT = 8192     (8K chars/text default)
```

**Guards in `_handle_client`** (4 checks, all return JSON error, no crash):

1. **msg_len guard** — after reading 4-byte length header, reject before reading body if `msg_len > MAX_REQUEST_BYTES` (prevents pre-body allocation attack)

2. **texts type guard** — after JSON parse, reject if `texts` is not a `list` (catches `{"texts": {"a":1}}` or `{"texts": "string"}`)

3. **count guard** — reject if `len(texts) > MAX_TEXTS` (prevents batch OOM)

4. **per-text char guard** — for each text, reject if `len(text) > MAX_CHARS_PER_TEXT` (prevents single oversized text from causing memory pressure)

**Bug fix in `_daemon_sanitize_embeddings`**: repaired embeddings were modified in a local `cleaned` list but never written back to `embeddings[i]`. Added `embeddings[i] = cleaned`.

**Doctor output updated**: `run_embed_doctor()` now prints the 3 limit values.

### 2. `tests/test_embed_daemon_request_guards.py` — New Test Suite

25 tests, all passing:
- `TestRequestValidation` (10): inline validation mirror covering all 5 guard cases
- `TestGuardConstants` (4): env var override of all 3 constants
- `TestSanitizeEmbeddings` (5): finite passthrough, sparse NaN repair, all-NaN raises
- `TestHandleClientGuards` (5): real socketpair + fake numpy model end-to-end
- `test_chromadb_not_in_modules`: confirms no ChromaDB loaded

### 3. Bug Fixed: `_daemon_sanitize_embeddings` Did Nothing

**Before:** Cleaned values were computed into a local `cleaned` list but never assigned back to `embeddings[i]`. The function returned the original (unmodified) embeddings list. A sparse-NaN embedding would pass through with NaN still present.

**After:** `embeddings[i] = cleaned` after renormalization. Verified with test: sparse NaN now correctly repaired and renormalized to unit length.

## Test Results

```
$ pytest tests/test_embed_daemon_request_guards.py -q -o "addopts="
.........................
25 passed in 0.96s

$ pytest tests/test_m1_runtime_doctor.py -q -o "addopts="
...
3 passed in 1.69s

$ python -c "import sys; import mempalace; print('chromadb' in sys.modules)"
False
```

## Backward Compatibility

All existing paths unchanged for valid requests. Error responses use existing JSON shape `{"embeddings": [], "error": "..."}` which the caller already handles.

## ChromaDB

Confirmed absent: `chromadb` not in `sys.modules` after `import mempalace.embed_daemon`.