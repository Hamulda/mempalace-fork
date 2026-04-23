# Welcome to MemPalace

## How We Use Claude

Based on Vojtech Hamada's usage over the last 30 days:

Work Type Breakdown:
  Debug Fix       ███████████████████░░░░░░░░  35%
  Build Feature   ██████████████░░░░░░░░░░░░░░  28%
  Improve Quality ███████████░░░░░░░░░░░░░░░░░  22%
  Plan Design     █████░░░░░░░░░░░░░░░░░░░░░░░  10%
  Write Docs      ███░░░░░░░░░░░░░░░░░░░░░░░░░   5%

Top Skills & Commands:
  /clear        █████████████████████████████  137x/month
  /effort       ██████░░░░░░░░░░░░░░░░░░░░░░░  12x/month
  /compact      ██░░░░░░░░░░░░░░░░░░░░░░░░░░░   4x/month
  /python-development:python-anti-patterns  █░░░░░░░░░░░░░░░░░░░░░░░░░░   1x/month
  /agent-teams:multi-reviewer-patterns     █░░░░░░░░░░░░░░░░░░░░░░░░░░   1x/month
  /team-onboarding                        █░░░░░░░░░░░░░░░░░░░░░░░░░░   1x/month

Top MCP Servers:
  ripgrep            █████████████████████████████  153 calls
  CodeGraphContext   ███░░░░░░░░░░░░░░░░░░░░░░░░   8 calls
  codebase-memory-mcp  ███░░░░░░░░░░░░░░░░░░░░░░░░   7 calls
  ast-grep           █░░░░░░░░░░░░░░░░░░░░░░░░░░   2 calls
  fetch              █░░░░░░░░░░░░░░░░░░░░░░░░░░   1 call

## Your Setup Checklist

### Codebases
- [ ] mempalace-fork — https://github.com/hamulda/mempalace-fork
- [ ] claude-code-lsps — local plugin (sibling)
- [ ] claude-code-workflows — local plugin (sibling)
- [ ] omc — local plugin (sibling, oh-my-claudecode orchestration layer)

### MCP Servers to Activate
- [ ] ripgrep — fast code search. Available by default in Claude Code; no extra setup needed.
- [ ] CodeGraphContext — code structure analysis (callers/callees, complexity, dead code). Part of the claude-plugins-official package.
- [ ] codebase-memory-mcp — semantic code search and knowledge graph. Part of the claude-plugins-official package.
- [ ] ast-grep — AST-based structural search and refactoring. Part of the claude-plugins-official package.
- [ ] fetch — web URL fetching. Available by default.

### Skills to Know About
- /clear — clears the conversation context. Used *extremely* frequently (137x/month) — not a sign something is wrong, just how context is managed here.
- /effort — estimate task effort before starting. Used 12x/month for scoping sprint work.
- /compact — compact the conversation transcript. Used 4x/month to keep context lean.
- /python-development:python-anti-patterns — Python-specific quality checklist. Used when reviewing or writing Python.
- /agent-teams:multi-reviewer-patterns — coordinate parallel code reviews across Security/Performance/Architecture/Testing. Used for multi-dimensional reviews.
- /team-onboarding — generate this guide.

## Team Tips

### MemPalace Team Composition

**Work Profile**: This project is ~60% debug/quality and ~40% feature build. Most sprints touch multiple files across storage, search, and server layers simultaneously.

**Suggested team size**: 2–3 teammates for sprint work.

### Communication Protocols

**Message teammates by name** (not UUID). Use `message` for routine coordination — direct, no broadcast unless ALL teammates are blocked.

**Broadcast ONLY when**: a shared schema changed, a critical bug blocks everyone, or a baseline test started failing. Routine status → direct message or TaskUpdate.

**Integration points to announce** (message teammates directly at these milestones):
1. Backend interface changed (`lance.py`, `chroma.py`, `base.py`)
2. Search/reranker contract changed (`searcher.py`)
3. Server tool signature changed (`server/_*.py`)
4. Any `async` flow that spawns background threads (`write_coordinator.py`, `embed_daemon.py`)

### File Ownership Boundaries

Assign teammates ownership of non-overlapping modules to avoid merge conflicts:

| Owner | Modules / Files |
|-------|-----------------|
| Teammate A | `backends/`, `memory_guard.py`, `write_coordinator.py`, `write_coalescer.py` |
| Teammate B | `searcher.py`, `lexical_index.py`, `query_cache.py`, `query_sanitizer.py` |
| Teammate C | `server/`, `fastmcp_server.py`, `cli.py`, `miner.py`, `convo_miner.py` |

If a task crosses boundaries (e.g., backend + searcher), assign to one owner and have the other acknowledge via direct message before proceeding.

### MemPalace-Specific Conventions

- **Backend abstraction**: All storage goes through `backends/base.py` — never bypass it.
- **Async patterns**: Use `asyncio.gather` for parallel operations (see `searcher.py:hybrid_search_async` for the canonical pattern). Do NOT introduce new background thread pools without checking `write_coordinator.py` and `embed_daemon.py` first.
- **Memory pressure**: `memory_guard.py` is always-on. If your code allocates large structures, keep it under the Layer1 budget and add it to `memory_guard.py` tracked allocations.
- **Tests**: Run `tests/test_claim_enforcement.py` before claiming a task complete — it is the primary correctness gate for storage and write path changes.
- **No feature flags**: MemPalace has no feature flag infrastructure. Changes are always-on. Design accordingly.

### Anti-Patterns for This Project

| Anti-Pattern | Why It Hurts Here |
|---|---|
| Bypassing `backends/base.py` | Storage backends are swapped at runtime; direct imports break Chroma/Lance switching |
| New `threading.Thread` without bounded executor | M1 8GB UMA — unbounded threads cause swap death |
| Silent `try/except pass` | Silent failures have caused claim enforcement bugs before |
| Not running `test_claim_enforcement.py` | It catches storage + write path regressions |

### Workflow

1. **Assign tasks by file ownership** using TaskUpdate (`owner` field)
2. **Check in at milestones** — not every step. A message like "backends done, integration tested" is sufficient.
3. **Verify before claiming done** — run the relevant test file before marking complete
4. **Blockers**: message directly; broadcast only if everyone is blocked

### Starter Task

Look at the currently modified files in the git status (git diff --stat). Those represent in-flight sprint work — claim one module's changes and verify tests pass.

## Get Started

### Your First Task

Run the claim enforcement test suite to verify the current baseline is green. This is the primary correctness gate for all MemPalace sprints:

```bash
cd /Users/vojtechhamada/.claude/plugins/marketplaces/mempalace
python -m pytest tests/test_claim_enforcement.py -q
```

Report back: how many tests passed, how long it took, and whether any were skipped or failed. That gives the team lead a baseline to work from.

### Check In

After running the tests, message the team lead with:
- Your name / which module owner slot you're filling (A, B, or C from the table above)
- Test result summary
- Any immediate questions about scope or boundaries
