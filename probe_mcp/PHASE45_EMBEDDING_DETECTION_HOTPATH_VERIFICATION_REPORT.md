# PHASE45 — Embedding Detection Hotpath Verification Report

**Date:** 2026-04-30
**Status:** ✅ COMPLETE — all checks pass, 30/30 tests green

---

## Verification Results

```
30 passed, 2 warnings in 8.11s
chromadb in sys.modules: False
```

---

## 1. Audit: detect_current_provider() Call Sites

**Only 2 call sites in the entire codebase:**

| Location | Line | Context | Path |
|----------|------|---------|------|
| `lance.py` | 1804 | `_do_add` — validate provider/dims before write | WRITE |
| `lance.py` | 1909 | post-write `ensure_meta` — write embedding_meta.json | WRITE |

**Never called in:** search, retrieve, hybrid_search, auto_search, or any read path.
**searcher.py:** Zero references to `detect_current_provider` or `embed_metadata`.
**All backends:** No call sites outside lance.py write methods.

---

## 2. Write-Path / Status Only — Confirmed

`detect_current_provider()` is gated behind two write-only guards:
- `_do_add` validation (L1804) — runs before every write batch, guarded by try/except
- `ensure_meta` post-write (L1909) — idempotent, only updates timestamp on subsequent calls

Search operations use `EmbeddingProvider` from cached state only. No search path triggers detection.

---

## 3. TTL Cache Confirmed Working

**Cache mechanism (`embed_metadata.py` L68-124):**
- Module-level `_CachedDetection` (frozen dataclass) with `expires_at = monotonic + TTL`
- TTL default: **30s** (`MEMPALACE_EMBED_PROVIDER_CACHE_TTL` env-overrideable)
- Cache key: `"{sock}:{mock}:{eval_mode}"` — includes socket path + env vars
- Hits counter: `_cache_hits` incremented on every cache hit

**Write coalescer scenario:** 20 consecutive batches inside TTL → 1 probe + 19 cache hits.
The `WriteCoalescer` drains batches at up to 20/s; with 30s TTL, all hit cache after first.

---

## 4. No fastembed Import Under Skip Conditions

**Detection priority chain (lightest first):**

1. **Cache hit** — 0ms, no import (TTL not expired)
2. **MOCK_EMBED / MEMPALACE_EVAL_MODE** — returns immediately, no probe
3. **Daemon socket probe** — Unix socket send/recv, no Python import
4. **_detect_embed_provider from embed_daemon** — importable function, no fastembed
5. **MEMPALACE_EMBED_PROVIDER env hint** — `os.environ.get()` + VALID_PROVIDERS check, no import
6. **Fallback: fastembed import** — only when ALL above paths fail

**fastembed NOT imported when:**
- `MEMPALACE_EMBED_PROVIDER` is set → path 5 short-circuits before path 6
- `embedding_meta.json` exists → daemon probe or env hint sufficient
- daemon socket unavailable → path 4 succeeds before path 6 (MLX or embed_daemon)
- cache is hot → path 1 returns immediately

---

## 5. No Chroma Anywhere — Confirmed

```python
# During all detection paths tested:
MOCK_EMBED=1          → chromadb not in sys.modules
MEMPALACE_EVAL_MODE   → chromadb not in sys.modules
MEMPALACE_EMBED_PROVIDER=mlx → chromadb not in sys.modules
MEMPALACE_EMBED_PROVIDER=fastembed_cpu → chromadb not in sys.modules
(no env)               → chromadb not in sys.modules

# sys.modules check on import mempalace:
'chromadb' in sys.modules → False
```

---

## 6. New Test File: test_embedding_detection_hotpath.py

**16 tests added across 4 classes:**

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestWritePathOnly` | 3 | searcher.py no detect, all backends scanned, exact lance.py line verification |
| `TestTtlCacheHotWrite` | 2 | 5 consecutive calls = 1 probe + 4 hits; 20 batch drain = 1 probe + 19 hits |
| `TestNoFastembedImport` | 5 | env hint, daemon probe, meta.json, embed_daemon success, forced fallback |
| `TestNoChromaAnywhere` | 5 (parametrized) | all 5 env configurations, chroma never imported |
| `TestCacheKey` | 1 | socket path included in cache key |

**All 16 new tests pass.** Combined with existing 14-budget tests: **30/30 green.**

---

## Files Created

- `tests/test_embedding_detection_hotpath.py` — 16 tests, hotpath verification

## Files Modified

- `mempalace/server/_symbol_tools.py` — staged (PHASE43 contract unification)
- `mempalace/server/response_contract.py` — staged (PHASE43 contract unification)

## Hard Rules Compliance

| Rule | Status |
|------|--------|
| No Chroma | ✅ — chromadb never imported |
| Python 3.14 only | ✅ — no version-gated syntax |
| No heavy model load | ✅ — detection chain short-circuits before model load |
| No new dependencies | ✅ — only stdlib + pytest |
| Tests over refactor | ✅ — 16 tests added, no refactor needed |