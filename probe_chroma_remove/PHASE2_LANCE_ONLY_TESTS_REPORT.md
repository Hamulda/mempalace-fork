# Phase 2: Lance-Only Tests Report

## Changes Made

### test_backend_defaults.py
Rewrote all ChromaDB-legacy tests to Lance-only equivalents.

| Old Test | New Test | Notes |
|----------|----------|-------|
| `test_settings_db_backend_accepts_chroma` | `test_settings_db_backend_rejects_chroma` | Validates Pydantic raises ValidationError for "chroma" |
| `test_config_backend_env_override` | `test_config_backend_warns_on_env_chroma` | Validates warning + returns "lance" |
| `test_get_backend_chroma_lazy` | `test_get_backend_chroma_raises` | Validates clear ValueError |
| `test_chroma_not_eager_on_import` | `test_chromadb_not_in_sys_modules_after_backend_import` | Validates chromadb NOT in sys.modules |
| (new) | `test_chromadb_not_loaded_by_get_backend_chroma_raises` | chromadb NOT loaded even when get_backend("chroma") raises |
| `test_settings_naming_convention` | `test_settings_naming_convention` | Simplified to check only "lance" |
| `test_config_naming_convention` | `test_config_naming_convention_lance` + `test_config_naming_convention_warns_on_chroma` | Split into two clear tests |

### test_backend_contracts.py
Rewrote all ChromaDB-legacy contract tests.

| Old Test | New Test | Notes |
|----------|----------|-------|
| `test_backend_choices_exports` | `test_backend_choices_is_lance_only` | `BACKEND_CHOICES == ("lance",)` |
| `TestChromaLegacyOptIn` | `TestChromaRemoved` | Renamed to reflect removal |
| `test_get_backend_chroma_still_works` | `test_get_backend_chroma_raises_valueerror` | ValueError with clear removal message |
| `test_chroma_not_eager_on_import` | `test_chromadb_not_imported_on_backend_package_import` + `test_chromadb_not_imported_on_get_backend_chroma_call` | Two separate guards |
| `test_env_unknown_backend_not_accepted` | `test_env_unknown_backend_rejected_by_get_backend` | Fixed: now calls `get_backend("not_a_real_backend")` directly |
| `TestMigrationPathPreserved` | `TestMigrationRemoved` (deferred) | Skipped — migrate.py cleanup deferred to Phase 3+ |
| `test_chroma_backend_still_in_backends_init` | (removed) | ChromaBackend/ChromaCollection no longer exported |

## Test Results

```
tests/test_backend_defaults.py: 17 passed, 0 failed
tests/test_backend_contracts.py: 13 passed, 1 skipped, 0 failed
```

**Total: 30 passed, 1 skipped (migration deferred)**

## Acceptance Checks

| Check | Result |
|-------|--------|
| `chromadb` NOT in `sys.modules` after `import mempalace.backends` | ✅ `False` |
| `get_backend("chroma")` raises `ValueError` with clear message | ✅ ValueError: "ChromaDB backend has been removed. LanceDB is the only supported backend." |
| Both test files pass | ✅ 30 passed, 1 skipped |

## Guard Tests Added (3 new)

1. **`test_chromadb_not_in_sys_modules_after_backend_import`**: `import mempalace.backends` does NOT load chromadb into sys.modules
2. **`test_chromadb_not_imported_on_get_backend_chroma_call`**: `get_backend("chroma")` raises WITHOUT loading chromadb into sys.modules
3. **`test_backend_choices_is_lance_only`**: `BACKEND_CHOICES == ("lance",)`

## Deferred (Phase 3+)

- `TestMigrationRemoved` tests are skipped placeholders — `migrate.py` still has `migrate_chroma_to_lance` and `migrate_lance_to_chroma`
- migrate.py cleanup: remove both functions, remove `migrate` CLI subcommand, clean up `cli.py` references

## Files Modified

| File | Change |
|------|--------|
| `tests/test_backend_defaults.py` | Complete rewrite — Lance-only tests |
| `tests/test_backend_contracts.py` | Complete rewrite — Lance-only contracts |

## Backups

All backups in `probe_chroma_remove/`:
- `test_backend_defaults.py.bak_CHROMA_REMOVE`
- `test_backend_contracts.py.bak_CHROMA_REMOVE`
