# Phase 4: ChromaDB Runtime Removal — Report

**Date:** 2026-04-28
**Scope:** Remove ChromaDB as runtime backend and package dependency
**Status:** ✅ COMPLETE

---

## Changes Made

### 1. `mempalace/backends/chroma.py` — Replaced with stub

**Before:** Full `ChromaCollection` + `ChromaBackend` implementation with live `import chromadb`.

**After:** Single `raise ImportError(...)` stub with zero `chromadb` imports.

```
raise ImportError(
    "ChromaDB backend has been removed. "
    "LanceDB is the only supported backend. "
    "If you have existing ChromaDB data, migrate it first with: "
    "pip install chromadb && python -m mempalace.migrate chroma-to-lance --palace <path>"
)
```

**Guard verified:** `import chromadb` never executes — the stub raises before any chromadb reference.

---

### 2. `pyproject.toml` — Removed chromadb optional dependency

**Before:**
```toml
[project.optional-dependencies]
chromadb = [
    "chromadb>=0.5.0,<0.7",
]
```

**After:** `chromadb` entry removed entirely. No other optional deps affected.

---

### 3. `mempalace/migrate.py` — Chroma migration functions replaced with stubs

- **`migrate_chroma_to_lance()`**: Replaced with `RuntimeError` stub. The error message instructs users to `pip install chromadb` separately before using the standalone migration script. No `chromadb` import in the stub body.

- **`migrate_lance_to_chroma()`**: Replaced with `RuntimeError` stub. This direction is permanently removed.

- **`cmd_migrate_embeddings()`**: Preserved unchanged — Lance-only re-embedding function. No chromadb references.

- Module-level `shutil` and `Path` imports removed (were only used by removed functions).

---

### 4. `mempalace/cli.py` — Chroma paths cleaned up

| Location | Change |
|---|---|
| `cmd_migrate()` | Removed `migrate_lance_to_chroma` import; `lance-to-chroma` branch now prints clear error + `sys.exit(1)` |
| `cmd_migrate()` docstring | Updated to "ChromaDB support has been removed" |
| `migrate` subparser help | Updated to "ChromaDB palace to LanceDB (chroma-to-lance only)" |
| `cmd_status()` | Added early-return for `backend_type == "chroma"` with clear message; removed ChromaDB from docstring |
| `cmd_repair()` | ChromaDB branch now says "removed" not "legacy"; updated docstring |
| `cmd_compress()` | Added chroma guard before `get_backend()` call; updated docstring |
| `cmd_cleanup()` | Added chroma guard before `get_backend()` call |

---

### 5. Guard Verification Results

```
$ python -c "import mempalace; import sys; print('chromadb' in sys.modules)"
False                                          ✅

$ python -c "import mempalace.backends; import sys; print('chromadb' in sys.modules)"
False                                          ✅

$ python -c "from mempalace.backends import get_backend; print(type(get_backend('lance')).__name__)"
LanceBackend                                   ✅

$ python -c "from mempalace.backends import get_backend; get_backend('chroma')"
ValueError: ChromaDB backend has been removed. LanceDB is the only supported backend.
(...)
If you have existing ChromaDB data, migrate it first with: pip install chromadb && python -m mempalace.migrate chroma-to-lance   ✅
```

---

### 6. Static Import Audit

```
$ rg "import chromadb|from chromadb" mempalace/ --stats
mempalace/instructions/init.md  (1 line, documentation — out of scope)
mempalace/migrate.py           (1 line, error message string — no runtime import)
```

**Zero runtime files** import `chromadb`.

---

## Test Results

```
tests/test_backend_defaults.py  ✅
tests/test_backend_contracts.py ✅
tests/test_lance_codebase_rag_e2e.py ✅
tests/test_claim_enforcement.py ✅

43 passed, 1 skipped, 52 warnings  ✅ (expected: 43 passed, 1 skipped or better)
```

---

## Files Not Modified (by scope)

| File/Dir | Reason |
|---|---|
| `tests/benchmarks/*` | Explicitly out of scope |
| `tests/test_bugfixes.py` | Chromadb imports exist but not runtime; not modified per scope |
| `tests/test_cli.py` | Chromadb imports exist but not runtime; not modified per scope |
| `tests/test_convo_miner.py` | Chromadb imports at module level — would need separate PR |
| `tests/test_fastmcp_server.py` | Chromadb imports at module level — would need separate PR |
| `tests/test_miner.py` | Chromadb imports at module level — would need separate PR |
| `tests/test_code_path_fidelity.py` | Chromadb imports at module level — would need separate PR |
| `tests/test_path_fidelity.py` | Chromadb imports at module level — would need separate PR |
| `docs/` | Explicitly out of scope |
| `uv.lock` | Not regenerated (lockfile stability concern noted in scope) |

---

## Pre-existing Issues Noted (Not Introduced by Phase 4)

- `cmd_cleanup()`: Local `import sys` inside function shadows module-level `sys` — Pyright reports `sys` as unbound. Pre-existing.
- `cmd_embed_daemon()`: `proc.stdout.readline()` on possibly-`None` pipe — pre-existing Pyright warning.
- `fastembed` and `psutil` imports reported as missing by Pyright — pre-existing, not introduced by Phase 4.
- `datetime.utcnow()` deprecation in `miner.py:1052` — pre-existing, not introduced by Phase 4.

---

## Phase 4 Sign-off

All Phase 4 objectives achieved:
- ChromaDB is not a runtime backend ✅
- ChromaDB is not a package dependency ✅
- ChromaDB is not importable through any normal code path ✅
- Lance E2E and backend contract tests remain green ✅
