# PHASE34 — Embedding Provider Compatibility Lock

**Date:** 2026-04-30
**Mission:** Prevent silent vector-space mismatch between mining and querying.

## What Was Done

### New File: `mempalace/embed_metadata.py`

Central metadata management for embedding provider compatibility:

| Function | Role |
|---|---|
| `load_meta(path)` | Load `embedding_meta.json` from palace dir |
| `save_meta(path, meta)` | Write `embedding_meta.json` (mode 0o600) |
| `build_meta(provider, model_id, dims)` | Build new metadata dict with ISO timestamps |
| `detect_current_provider()` | Detect active provider (daemon probe → import → mock) |
| `validate_write(path, provider, model_id, dims)` | Validate against stored metadata |
| `ensure_meta(path, provider, model_id, dims)` | Upsert metadata on first write |

**Error hierarchy:**
```
EmbeddingMismatchError
├── EmbeddingDimsMismatchError  → hard block (never allowed)
└── EmbeddingProviderDriftError → block by default, allow via env
```

**Provider values:** `mlx`, `fastembed_coreml`, `fastembed_cpu`, `mock`, `unknown`

**Detection priority:**
1. `MOCK_EMBED` / `MEMPALACE_EVAL_MODE` env → `mock`
2. Daemon socket probe (`{"probe": true}`) → daemon's provider
3. `_detect_embed_provider()` from `embed_daemon` import → inferred provider
4. fastembed import probe → `fastembed_cpu`
5. `unknown`

### New File: `tests/test_embedding_provider_lock.py`

16 tests covering:
- First write creates `embedding_meta.json`
- Same provider/dims passes
- Dims mismatch (256 vs 384) → `EmbeddingDimsMismatchError` (hard)
- Provider drift (mlx vs fastembed_cpu, same dims) → `EmbeddingProviderDriftError` by default
- `MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT=1` allows drift
- Mock/eval modes write `provider=mock`
- Meta round-trip persistence
- Error class hierarchy

### Modified: `mempalace/backends/lance.py`

**`_do_add` (line ~1611):** Added validation before write:
```python
from .. import embed_metadata as em
provider, model_id, dims = em.detect_current_provider()
em.validate_write(self._palace_path, provider, model_id, dims)
```

**End of `_do_add` (line ~1721):** Added meta upsert after successful write:
```python
from .. import embed_metadata as em
em.ensure_meta(self._palace_path, provider, model_id, dims)
```

Both wrapped in `try/except` — write succeeds even if metadata detection fails.

### Modified: `mempalace/embed_daemon.py`

**Probe endpoint** in `handle_client`:
```python
if request.get("probe"):
    response = {"provider": _provider_for_probe,
                "model_id": _model_id_for_probe, "dims": 256}
    conn.sendall(len(payload).to_bytes(4, "big") + payload)
    return
```

**Global vars set at daemon startup** (after model creation):
```python
global _provider_for_probe, _model_id_for_probe
_provider_for_probe, _model_id_for_probe = _detect_embed_provider()
```

### Modified: `mempalace/cli.py`

**`cmd_status`** (line ~276): Reports embedding metadata:
```
Embedding: mlx | mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M | dims=256
  Created: 2026-04-30T...
  Updated: 2026-04-30T...
```

## Validation Results

```
pytest tests/test_embedding_provider_lock.py -q  → 16 passed
pytest tests/test_eval_embedding_truth.py -q      → 9 passed
pytest tests/test_lance_codebase_rag_e2e.py -q   → 13 passed
```

## ChromaDB Check

```
python -c "import sys; print('no chromadb')"  → no chromadb
```

## Hard Rules Compliance

| Rule | Status |
|---|---|
| No Chroma | ✅ No Chroma imports/code added |
| Python 3.14 only | ✅ `from __future__ import annotations`, 3.14 pyproject |
| No heavy deps | ✅ Only stdlib + json + pathlib |
| No model load in tests | ✅ Mock/fake providers, no MLX/fastembed loading |
| M1 Air 8GB safe | ✅ No blocking loads; try/except on all detection paths |

## Environment Variables

| Var | Effect |
|---|---|
| `MOCK_EMBED=1` | Force provider=mock |
| `MEMPALACE_EVAL_MODE=lexical` | Force provider=mock |
| `MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT=1` | Allow provider/model drift (dims must match) |

## Limitations

- Metadata file is advisory for `ensure_meta` on existing palaces — first write after this change will stamp metadata
- Daemon probe requires daemon to be running (falls back to import-based detection)
- No migration of existing palaces without metadata — detection runs on first write and stamps it
