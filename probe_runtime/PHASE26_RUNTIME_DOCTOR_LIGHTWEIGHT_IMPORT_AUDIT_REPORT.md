# PHASE26 Runtime Doctor Lightweight Import Audit Report

**Date:** 2026-04-30
**Mission:** Ensure `scripts/m1_runtime_doctor.py` does not import heavy ML/model libraries just to check availability.

## Changes Made

### `scripts/m1_runtime_doctor.py`

| Package | Before | After | Reason |
|---------|--------|-------|--------|
| `sentence_transformers` | direct `import` | `importlib.util.find_spec` | triggers `torch` load on M1 |
| `fastembed` | direct `import` | `importlib.util.find_spec` | can trigger model downloads |
| `mlx` | direct `import` | kept — MLX itself is lightweight | acceptable |
| `lancedb` | direct `import` | kept | necessary for version report |
| `pyarrow` | direct `import` | kept | necessary for version report |
| `fastmcp` | direct `import` | kept | necessary for availability flag |

**New fields in report:**
```python
"heavy_imports_avoided": True,
"checked_by_spec": ["fastembed", "sentence_transformers"],
```

### `tests/test_m1_runtime_doctor_lightweight.py` (new)

4 tests added:
- `test_heavy_packages_not_imported` — doctor runs without heavy imports
- `test_find_spec_used_for_heavy_packages` — verifies `find_spec` is called for heavy packages
- `test_sentence_transformers_not_in_sysmodules_after_doctor` — subprocess confirms no torch/sentence_transformers in sys.modules
- `test_report_has_lightweight_fields` — verifies new JSON fields present

## Verification

```
$ python scripts/m1_runtime_doctor.py --json | python -c "import sys,json; d=json.load(sys.stdin); print('heavy_imports_avoided:', d.get('heavy_imports_avoided')); print('checked_by_spec:', d.get('checked_by_spec'))"
heavy_imports_avoided: True
checked_by_spec: ['fastembed', 'sentence_transformers']

$ python -c "import sys; print('sentence_transformers' in sys.modules, 'torch' in sys.modules)"
False False

$ ~/.pyenv/shims/pytest tests/test_m1_runtime_doctor.py tests/test_m1_runtime_doctor_counts.py tests/test_m1_runtime_doctor_lightweight.py -v --override-ini='addopts='
12 passed in 1.28s
```

## Test Results

| Test | Result |
|------|--------|
| test_doctor_script_imports_without_crash | PASSED |
| test_doctor_json_output_has_required_keys | PASSED |
| test_chromadb_not_in_modules_after_import | PASSED |
| test_heavy_packages_not_imported | PASSED |
| test_find_spec_used_for_heavy_packages | PASSED |
| test_sentence_transformers_not_in_sysmodules_after_doctor | PASSED |
| test_report_has_lightweight_fields | PASSED |
| +5 from test_m1_runtime_doctor_counts.py | PASSED |

**Status: PASS** — doctor stays lightweight, no accidental torch/sentence_transformers import, JSON has new fields.
