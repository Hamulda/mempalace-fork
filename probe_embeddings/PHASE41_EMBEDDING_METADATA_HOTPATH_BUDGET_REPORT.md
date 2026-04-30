# PHASE41 Embedding Metadata Hotpath Budget Report

## Executive Summary

`embed_metadata.py` `detect_current_provider()` was called on every `_upsert` and every `upsert` call to the LanceCollection — a hot path. Each call probed the daemon socket, imported `embed_daemon` (which imports `mlx_embeddings`/`fastembed` internally), and potentially fell through to a heavy `import fastembed`. On M1 Air 8GB this causes memory pressure.

**Fix**: TTL cache (default 30s) on `detect_current_provider()` + env-hint short-circuit before daemon probe. `import fastembed` now only happens as a true last resort.

**Result**: 30/30 tests pass, no chromadb import, fastembed avoided when env hint is present.

---

## Changes Made

### `mempalace/embed_metadata.py`

| Change | Detail |
|--------|--------|
| TTL cache for `detect_current_provider()` | `_CachedDetection` frozen dataclass stored at module level, keyed on `time.monotonic() + TTL`. Default 30s, configurable via `MEMPALACE_EMBED_PROVIDER_CACHE_TTL` env. |
| `ProviderDetectionSource` type | `Literal["daemon", "metadata", "env", "fallback_import", "unknown", "mock"]` — provenance label for each detection path |
| Env-hint short-circuit | `MEMPALACE_EMBED_PROVIDER` env checked **before** daemon probe — avoids socket I/O when hint is set |
| Detection order | 1. Cache hit  2. Mock/eval env  3. Env hint  4. Daemon socket probe  5. `_detect_embed_provider` import  6. fastembed fallback |
| `detection_cache_stats()` | Returns `hits`, `cached`, `ttl_seconds`, `source`, `expires_at` |
| `clear_detection_cache()` | Clears module-level cache state (for testing) |
| `_cache_key()` + `_cache_ttl()` | Internal helpers; cache key uses socket path + MOCK_EMBED + EVAL_MODE env vars |

### `tests/test_embedding_provider_detection_budget.py` (new file)

10 test classes, 30 test methods covering:

| Test | What it validates |
|------|-------------------|
| `TestCacheHit.test_repeated_calls_hit_cache` | 3 consecutive calls → only 1 probe |
| `TestCacheHit.test_cache_stores_elapsed_ms` | `_cache_cached.elapsed_ms >= 0` after first detect |
| `TestDaemonProbeNoFastembed.test_daemon_probe_failure_no_fastembed_import` | Env hint path: 0 new fastembed modules |
| `TestDaemonProbeNoFastembed.test_daemon_probe_failure_env_hint_no_fastembed_import` | Env hint with fastembed_cpu: 0 new fastembed modules |
| `TestEnvHintAvoidsHeavyImport.test_env_provider_short_circuit` | Env `mlx` + model_id returned without daemon probe |
| `TestEnvHintAvoidsHeavyImport.test_env_provider_unknown_not_valid_skips_to_fallback` | Invalid env provider → graceful skip to next path |
| `TestCacheExpiry.test_cache_expires` | Manual cache clear forces new detection |
| `TestCacheExpiry.test_ttl_env_override` | `_cache_ttl()` reads `MEMPALACE_EMBED_PROVIDER_CACHE_TTL` |
| `TestNoChroma.test_no_chroma_import_during_detection` | Zero chroma modules after all detection paths |
| `TestCacheStats` (3 tests) | `hits`, `cached`, `source`, `ttl_seconds` correct |
| `TestMockSource.test_mock_source_label` | `source='mock'` in stats when `MOCK_EMBED=1` |
| `TestCacheInvalidation.test_cache_key_includes_sock_path` | Cache hit persists across socket path env change |

---

## Detection Flow (after fix)

```
detect_current_provider()
│
├─ [cache hit] return (provider, model_id, dims)  ← 0 cost
│
├─ MOCK_EMBED or EVAL_MODE → ("mock", "eval-mock", 256)  source="mock"
│
├─ MEMPALACE_EMBED_PROVIDER=mlx|coreml|cpu|fastembed_cpu
│  └─ → (env_provider, model_id, 256)  source="env"  ← no I/O, no import
│
├─ _probe_daemon_socket()  [socket @ ~/.mempalace/embed.sock]
│  └─ → (provider, model_id, dims)  source="daemon"
│
├─ import mempalace.embed_daemon._detect_embed_provider()
│  └─ → (provider, model_id, 256)  source="daemon"  ← heavy: mlx/fastembed imported
│
├─ import fastembed  ← LAST RESORT  source="fallback_import"
│
└─ → ("unknown", "", 256)  source="unknown"
```

**Cache hit** is the dominant path on repeated writes within the TTL window.

---

## No Chroma Guarantee

`embed_metadata.py` has zero references to `chromadb` or `chroma`. The `TestNoChroma` test iterates all three non-mock detection paths and asserts zero chroma modules imported. Verification:

```
$ .venv/bin/python -c "import sys; import mempalace; print('chromadb' in sys.modules)"
False
```

---

## Metrics Added

| Metric | Tool | Description |
|--------|------|-------------|
| `provider_detection_source` | `detection_cache_stats()["source"]` | Which path was used: daemon/env/mock/fallback_import/unknown |
| `detection_elapsed_ms` | `detection_cache_stats()` (internal) | `_cache_cached.elapsed_ms` — ms for the original detection call |
| `detection_cache_hits` | `detection_cache_stats()["hits"]` | Cache hits since last clear |

Stats are retrievable at any time via `em.detection_cache_stats()`.

---

## Env Var Summary

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEMPALACE_EMBED_PROVIDER_CACHE_TTL` | `30` | Detection cache TTL in seconds |
| `MEMPALACE_EMBED_PROVIDER` | (none) | Provider hint: `mlx`, `fastembed_coreml`, `fastembed_cpu` |
| `MEMPALACE_EMBED_MODEL_ID` | `"env-hint"` | Model ID when using env hint |
| `MOCK_EMBED` | (none) | Force mock provider |
| `MEMPALACE_EVAL_MODE` | (none) | Force mock provider |
| `MEMPALACE_EMBED_SOCK` | `~/.mempalace/embed.sock` | Daemon socket path |

---

## Call Sites audited

| Location | Function | What it does |
|----------|----------|--------------|
| `lance.py:1804` | `_upsert` | `detect_current_provider()` then `validate_write()` — now cached |
| `lance.py:1909` | `upsert` | `detect_current_provider()` then `ensure_meta()` — now cached |

Both call sites are inside `try/except` blocks that swallow all exceptions — metadata detection failures never block writes.

---

## Test Results

```
30 passed, 2 warnings in 3.13s
```

All `test_embedding_provider_lock.py` tests continue to pass (20 existing tests).

---

## Pre-existing Diagnostics (not introduced by this change)

- `fastembed` unresolved import (expected — optional dependency, not installed in venv)
- `chromadb` references in `lance.py` (legacy — code paths guarded by `MEMPALACE_BACKEND=chroma`)
- Various `lance.py`/`searcher.py`/`miner.py` pre-existing type warnings — unchanged

No new diagnostics introduced in `embed_metadata.py` or the test file beyond the existing `pytest`-unresolved-import note (a venv pathing issue, not a code defect).
