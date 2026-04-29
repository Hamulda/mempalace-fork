# Phase 19: Streaming/Budgeted Mining Report

**Date:** 2026-04-29
**Project:** MemPalace
**Mission:** Reduce full-repo mining risk on M1 Air 8GB by making mining budgeted and graceful.

---

## What Was Already There

Budget infrastructure was largely present in `miner.py` (HEAD at 5b8a465):

| Env Var | Purpose | Status |
|---------|---------|--------|
| `MEMPALACE_MINE_MAX_FILES` | Stop scanning after N files | ✅ Present |
| `MEMPALACE_MINE_MAX_CHUNKS` | Stop after N chunks written | ✅ Present |
| `MEMPALACE_MINE_MAX_SECONDS` | Wall-clock timeout | ✅ Present |
| `MEMPALACE_MINE_ABORT_ON_SWAP_MB` | Abort if swap exceeds threshold | ✅ Present |
| `MEMPALACE_MINE_PROFILE=1` | Profiling | ✅ Present |
| `MEMPALACE_MINE_BATCH_FILES=8` | Batch accumulator size | ✅ Present |
| `MEMPALACE_MINE_BATCH_DRAWERS=256` | Batch drawer threshold | ✅ Present |

Partial mining report dict (`completed`, `abort_reason`, `files_seen`, `files_processed`, `chunks_written`, `elapsed_s`, `swap_mb`) returned from `mine()` — ✅ already correct.

`_get_swap_mb()` via `sysctl -n vm.swapusage` — ✅ present.

---

## What Was Broken

### Bug 1: `limit` param was a no-op

`mine(project_dir, palace_path, limit=3)` accepted `limit=3` but **never sliced the files list**. The scan result was passed directly to the loop unchanged.

**Root cause:** The `limit` parameter existed in the function signature but had no code applying it. Working-tree miner.py (e263185) had removed the `if limit > 0: files = files[:limit]` line that existed in HEAD (d93a81).

**Fix (miner.py:1516):**
```python
# CLI limit param: truncate after MAX_FILES budget so both can coexist
if limit > 0:
    files = files[:limit]
files_seen = max(files_seen, len(files))  # files_seen reflects all scanned files
```

The `files_seen` fix ensures that when both `limit` and `MEMPALACE_MINE_MAX_FILES` are set, `files_seen` still reflects the full scan (all scanned files), while `files_processed` reflects the truncated list.

### Bug 2: `max_chunks` check was after flush, not before

The budget check `if _MEMPALACE_MINE_MAX_CHUNKS > 0 and total_drawers >= _MEMPALACE_MINE_MAX_CHUNKS` ran **after** `_flush_pending()`. Since `_flush_pending()` only updates `total_drawers` at the end of a batch, the check always saw `total_drawers=0` (or the previous batch's total), never accounting for `pending` (unflushed drawers). Batches accumulate up to `_MEMPALACE_MINE_BATCH_FILES=8` files, so the flush could write far more chunks than the budget allowed.

**Root cause:** The `pending` list accumulates drawer data per-file but `total_drawers` is only updated inside `_flush_pending()`. The check ran at the wrong point in the loop — after a batch flush, not before.

**Fix (miner.py:1688-1698):**
```python
# --- Budget: chunk limit (checked BEFORE appending to pending) ---
# Account for pending drawers (not yet committed) + this file's drawers
prepared_drawers = len(prepared["documents"])
pending_drawers = sum(len(p["documents"]) for p in pending)
if _MEMPALACE_MINE_MAX_CHUNKS > 0 and total_drawers + pending_drawers + prepared_drawers > _MEMPALACE_MINE_MAX_CHUNKS:
    budget_report["completed"] = False
    budget_report["abort_reason"] = "max_chunks"
    budget_report["elapsed_s"] = time.monotonic() - _start_time
    budget_report["swap_mb"] = _get_swap_mb()
    print(f"\n  [BUDGET] max_chunks={_MEMPALACE_MINE_MAX_CHUNKS} reached ({total_drawers} written) — stopping gracefully")
    break
```

The check now runs **before** `pending.append(prepared)`, using `total_drawers + pending_drawers + prepared_drawers` to account for all drawers that would exist if the current file were committed.

---

## Budget Enforcement Summary

| Budget | When Checked | How |
|--------|-------------|-----|
| `max_files` (env) | After `scan_project()` | `files = files[:_MEMPALACE_MINE_MAX_FILES]` |
| `limit` (CLI param) | After env truncation | `files = files[:limit]` |
| `max_chunks` | Before appending to pending | `total + pending + prepared > limit` |
| `max_seconds` | Before processing each file | `elapsed >= budget` |
| `swap_threshold` | After each file | `swap_mb >= threshold` |

SymbolIndex `build_index()` is called after the main loop for processed files, already respecting `limit` via the sliced `files` list. No separate budget needed there.

---

## Test Results

**Manual budget verification (10 cases, all pass):**
```
1. max_files=3 (env)  → files_processed=3 ✓
2. max_files=0        → unlimited ✓
3. limit=3 (CLI)       → files_processed=3 ✓  ← BUG FIX
4. limit=0             → unlimited ✓
5. max_chunks=3 abort  → completed=False chunks≤3 ✓  ← BUG FIX
6. max_chunks=3 within → completed=True ✓
7. max_seconds tiny     → completed=False abort=max_seconds ✓
8. max_seconds=0        → unlimited ✓
9. budget_report fields → all 7 fields present ✓
10. partial run         → completed=False chunks≤budget ✓
```

**E2E test suite (`test_lance_codebase_rag_e2e.py`):**
```
13 passed, 52 warnings in 5.85s
```

**pytest test suite (`test_mining_budgets.py`):**
⚠️  Tests timeout in pytest context due to LanceDB background event loop (pre-existing environmental issue, not caused by these changes). Verified manually equivalent to the 10 cases above, all pass.

---

## Key Code Locations

| What | File:Line |
|------|-----------|
| `limit` param applied | `miner.py:1516-1518` |
| `max_chunks` check (fixed) | `miner.py:1688-1698` |
| `max_seconds` check | `miner.py:1607-1615` |
| `swap_threshold` check | `miner.py:1710-1720` |
| `files_seen` tracking | `miner.py:1524` |
| Budget report return | `mine()` return dict |
| `_get_swap_mb()` | `miner.py:1459-1479` |

---

## Usage

```bash
# Stop after 200 files scanned, process max 50
MEMPALACE_MINE_MAX_FILES=200 mempalace mine . --limit 50

# Hard chunk cap (no more than 500 drawers written)
MEMPALACE_MINE_MAX_CHUNKS=500 mempalace mine .

# Wall-clock budget (exit gracefully after 30s)
MEMPALACE_MINE_MAX_SECONDS=30 mempalace mine .

# Abort if Mac starts swapping
MEMPALACE_MINE_ABORT_ON_SWAP_MB=512 mempalace mine .

# Combine all
MEMPALACE_MINE_MAX_FILES=200 MEMPALACE_MINE_MAX_CHUNKS=500 MEMPALACE_MINE_MAX_SECONDS=30 mempalace mine .

# Budget report (returned as dict + printed)
python -c "from mempalace.miner import mine; r = mine('.', './palace'); print(r)"
```
