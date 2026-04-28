# POST_REFACTOR_TRUTH_SUMMARY

**Date**: 2026-04-28
**Purpose**: Reality-lock pass after plugin/Lance/retrieval/AST/eval changes.

---

## 1. Probe Reports — Status

| File | Existence | Verdict |
|------|-----------|---------|
| `probe_plugin/PHASE0_PLUGIN_REALITY_REPORT.md` | ✅ exists | Hooks NOT auto-registered; requires manual `settings.json` |
| `probe_plugin/PHASE5_PLUGIN_LIFECYCLE_REPORT.md` | ✅ exists | Lifecycle correctly implemented; registration manual |
| `probe_plugin/PHASE7_SKILLS_REPORT.md` | ✅ exists | 3/5 tasks completed, 2 no-change |

**Conclusion**: Probe reports are truth documents, not sprint fiction. They document real state. Not meant to be deleted.

---

## 2. Canonical State (what is true now)

### Plugin Lifecycle Mode
- **NOT automatic** — hooks require manual registration in `~/.claude/settings.json`
- README correctly notes "(optional, requires manual registration)"
- `mempalace serve` CLI is the recommended manual start mechanism

### Lance Canonical Path
- LanceDB is the **canonical/default** backend (not ChromaDB)
- ChromaDB is legacy-compat only (`pip install mempalace[chromadb]`)
- All write paths use LanceDB-first APIs

### AST Fallback Status
- AST (ast-grep) patterns available in `mempalace/miner.py` and retrieval code
- Falls back gracefully when AST tooling unavailable
- No forced AST dependency in core hot path

### Retrieval Planner Owner
- **`mempalace/retrieval_planner.py`** is the canonical owner of `classify_query()`
- `mempalace/searcher.py` has a **duplicate** `classify_query()` at line 787
  - Both implementations are nearly identical (same categories, same heuristics)
  - `searcher.classify_query` is used internally at line 1191
  - No import bridge — divergent implementations risk drift

### Eval Status
- `mempalace/eval.py` exists as a module
- No persistent eval daemon documented in plugin docs
- RAG evaluation is on-demand only

---

## 3. pyproject.toml Consistency

| Field | Value | Issue |
|-------|-------|-------|
| `requires-python` | `>=3.10` | OK |
| `classifiers` | 3.10–3.14 listed | OK |
| `ruff target-version` | `py314` | Minor mismatch: targets 3.14 but classifier includes 3.10–3.13 |
| `dev` dependencies | `pytest`, `pytest-cov`, `pytest-timeout`, `ruff`, `psutil` | OK |
| `dependency-groups.dev` | also lists `chromadb>=0.6.3` | Extra chromadb in dev group (not in `dev` extras) |

**Recommended M1 Air dev runtime**: Python 3.13 (not 3.14 which is unreleased); use `pip install -e ".[dev]"`.

---

## 4. Plugin Docs Contradictions

| File | Issue |
|------|-------|
| README | Correctly notes hooks are manual; no contradiction |
| `commands/doctor.md` | Contains stale-session cleanup instruction (line 42): `find ~/.mempalace/... -mtime +6h -delete` — this describes the TTL mechanism, not an active command; acceptable |
| `commands/init.md` | Should reference `mempalace serve` not `mcp serve` |
| `commands/help.md` | Aligned |

---

## 5. Action Items (tiny fixes only)

- [ ] `searcher.py:787` duplicate `classify_query` — delegate to `retrieval_planner.classify_query` or mark as internal bridge
- [ ] `pyproject.toml` dev dependency group: remove stray `chromadb>=0.6.3` (already covered by optional dep)
- [ ] `commands/init.md`: check `mempalace serve` reference is correct
