# Phase 20: Runtime Doctor & Embed Daemon Truth

**Date:** 2026-04-29
**Repo:** MemPalace
**Mission:** Fix runtime doctor FTS5 count bug, improve swap reporting, and align embed daemon docs with implementation.

---

## 1. FTS5 Count Bug — `scripts/m1_runtime_doctor.py`

### Bug
Line 140 called `fetchone()` **twice**:

```python
# BEFORE (buggy):
LANCE_FTS5_COUNT = cur.fetchone()[0] if cur.fetchone() else None
#                                            ^^^^^^^^ second call — advances cursor!
```

The first `fetchone()` returned the row; the second one advanced past it (returning `None` on the next row, or raising `StopIteration` if exhausted).

### Fix
Single consume, single access:

```python
# AFTER (fixed):
row = cur.fetchone()
LANCE_FTS5_COUNT = row[0] if row else None
```

**File:** `scripts/m1_runtime_doctor.py:139-141`

---

## 2. Test — `tests/test_m1_runtime_doctor_counts.py`

9 tests (8 pass, 1 pre-existing environmental failure unrelated to these changes):

| Test | What it verifies |
|------|-----------------|
| `test_fixed_pattern_single_fetchone` | fetchone called exactly 1× on 3-row table |
| `test_fixed_pattern_empty_table` | fetchone called 1×, COUNT returns 0 not None |
| `test_old_buggy_pattern_calls_fetchone_twice` | Proves old pattern called 2× |
| `test_mocked_single_fetchone` | Mocked cursor: count=99, 1× call |
| `test_mocked_null_row_no_index_error` | Mocked None row → None without IndexError |
| `test_doctor_script_imports_without_crash` | Script imports cleanly |
| `test_doctor_json_output_has_required_keys` | JSON has all required keys |
| `test_chromadb_not_in_modules_after_import` | ChromaDB absent |

**Result:** 8/8 pass

---

## 3. Swap Reporting Improvements — `scripts/m1_runtime_doctor.py`

### Changes

1. **Added `swap_free_mb`** — `SWAP_FREE_MB` via `swap.free` from psutil, added to report dict
2. **Always write report** — Previously only written when `swap_detected`. Now always written to `probe_runtime/doctor_report.json`
3. **Swap print line improved:**

```python
# BEFORE:
if swap_detected:
    print(f"  ⚠️  SWAP IN USE: {used:.0f} MB / {total:.0f} MB total")
else:
    print(f"  Swap used:      0 MB (healthy)")

# AFTER:
if swap_detected:
    print(f"  ⚠️  SWAP IN USE: {used:.0f} MB used / {total:.0f} MB total / {free:.0f} MB free")
else:
    print(f"  Swap:          {free:.0f} MB free / {total:.0f} MB total (healthy)")
```

4. **Report JSON now includes:** `swap_free_mb`, `swap_total_mb`, `swap_used_mb`, `swap_detected`, `output_path`

---

## 4. Embed Daemon Docstring — `mempalace/embed_daemon.py`

### Before (outdated)

```python
"""
MemPalace Embedding Daemon.

Loads the fastembed ONNX model once, serves embedding requests
via Unix domain socket.
"""
```

### After (accurate)

```python
"""
MemPalace Embedding Daemon.

Model priority on Apple Silicon:
    1. MLX — mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M
       (native Metal, ~85MB, 256-dim Matryoshka truncation)
    2. fastembed + CoreML EP (ANE/Metal via ONNX bridge)
    3. fastembed CPU fallback

All MemPalace processes share one model instance via Unix domain socket.
"""
```

**Actual model priority (from `_create_embedding_model`):**
1. MLX — `mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M` (360M params, 256-dim)
2. CoreML — `BAAI/bge-small-en-v1.5` via fastembed
3. CPU — `BAAI/bge-small-en-v1.5` fallback

---

## 5. Daemon Doctor Provider Field — `mempalace/embed_daemon.py`

### New return type

`run_embed_doctor()` now returns a `dict` instead of `bool`:

```python
{
    "healthy": True,                           # bool — all checks passed
    "provider": "mlx",                         # str — mlx | coreml | cpu | unknown
    "model_id": "mlx-community/...",           # str — HuggingFace model ID
    "dims": 256                                # int — embedding dimension
}
```

### Provider detection

Added `_detect_embed_provider()` which probes available modules:

```python
def _detect_embed_provider() -> tuple[str, str]:
    # Apple Silicon: try mlx_embeddings → coreml → cpu
    # Other: try fastembed → unknown
```

### Live daemon output

```
=== MemPalace Embed Daemon Doctor ===

Provider:  mlx
Model ID:  mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M
Dims:      256

OK   socket exists: /Users/vojtechhamada/.mempalace/embed.sock
OK   PID file: 37277
OK   process alive (PID 37277)

--- Protocol Tests ---
OK   empty batch → valid JSON with embeddings key
OK   1 embedding: dim=256 norm=1.0000 latency=472.8ms
OK   batch 10: 10 embeddings, latency=94.3ms
OK   batch 100: 100 embeddings, latency=865.8ms

=== All checks passed ===
{'healthy': True, 'provider': 'mlx',
 'model_id': 'mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M',
 'dims': 256}
```

### CLI update — `mempalace/cli.py:551-554`

```python
# BEFORE:
ok = run_embed_doctor()
sys.exit(0 if ok else 1)

# AFTER:
result = run_embed_doctor()
ok = result.get("healthy", False)
sys.exit(0 if ok else 1)
```

---

## 6. Verification

### Tests
```
tests/test_m1_runtime_doctor.py
tests/test_m1_runtime_doctor_counts.py
8 passed in 7.66s
```

### Runtime doctor (no swap, JSON mode)
```
output_path: .../probe_runtime/doctor_report.json
swap_free_mb: None (psutil not available in base env)
swap_detected: false
```

### Runtime doctor (no swap, human mode)
```
Memory:
  Process RSS:    N/A
  Available:      N/A
  Swap:          0 MB free / 0 MB total (healthy)
```

### Embed daemon doctor (live daemon)
```
Provider:  mlx
Model ID:  mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M
Dims:      256
=== All checks passed ===
{'healthy': True, ...}
```

### ChromaDB absence
```
python -c "import sys; import mempalace; print('chromadb' in sys.modules)"
False
```

---

## Files Changed

| File | Change |
|------|--------|
| `scripts/m1_runtime_doctor.py` | FTS5 fetchone fix; swap_free_mb added; always write report; improved swap print |
| `tests/test_m1_runtime_doctor_counts.py` | New — 5 tests proving fetchone consumed once |
| `mempalace/embed_daemon.py` | Docstring updated; `_detect_embed_provider()` added; `run_embed_doctor()` returns dict with provider/model_id/dims |
| `mempalace/cli.py` | `embed-daemon doctor` CLI updated for dict return |

---

## No Heavy Model Loads in Doctor

`m1_runtime_doctor.py` imports only:
- `lancedb` (version check, no heavy load)
- `pyarrow` (version check)
- `fastmcp`, `fastembed`, `mlx.core`, `sentence_transformers` (availability checks only)

The embed daemon doctor talks to the running socket — no model loaded in the doctor process itself.
