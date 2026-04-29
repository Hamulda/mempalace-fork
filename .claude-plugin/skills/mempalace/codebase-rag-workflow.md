---
description: Workflow guidance for using MemPalace RAG tools before and during editing tasks.
allowed-tools: Bash, Read
---

# MemPalace Codebase RAG Workflow

## Before Editing

1. Call `mempalace_status` or `/mempalace:doctor` if unsure about server health or palace state.
2. Call `mempalace_search_code` with a path/symbol query first — verify the file exists and is indexed.
3. Call `mempalace_project_context` when working on a repo to set `project_path`.
4. Call `mempalace_find_symbol` for symbol lookup (function, class, variable).
5. Use `mempalace_begin_work` to claim the file before editing — prevents conflicts with other sessions.
6. Use `mempalace_prepare_edit` to get symbol context, hot-spots, and auto conflict check before the patch.
7. After tests pass, call `mempalace_finish_work` to release the claim and write a diary entry.
8. If the work is incomplete, use `mempalace_publish_handoff` so the next session can take over.

## Search Rules

- Always lead with a path/symbol query in `mempalace_search_code` before broad searches.
- **Always pass `project_path`** when working inside a repository — this scopes results correctly and enables security checks.
- Use `mempalace_project_context` when working on a repo to set `project_path`.
- Use `mempalace_find_symbol` for symbol lookup (function, class, variable).
- Use `mempalace_file_context` only with `project_path` or paths in `MEMPALACE_ALLOWED_ROOTS` (colon-separated).
- Use bounded `limit` (5-20 default) to avoid loading massive result sets into memory on M1 8GB.
- Prefer `mempalace_search` for non-code queries (decisions, architecture, preferences).
- Use `mempalace_hybrid_search` only when keyword + semantic is needed together.

### `mempalace_file_context` Security

`mempalace_file_context` has safe defaults: by default (`MEMPALACE_FILE_CONTEXT_ALLOW_ANY=0`), it reads only files under `project_path` (if provided) or paths in `MEMPALACE_ALLOWED_ROOTS` (colon-separated). Path traversal (`../`) is resolved before checking.

To restore the old permissive behavior, set the environment variable `MEMPALACE_FILE_CONTEXT_ALLOW_ANY=1` before starting the server. This is a private/local override — not recommended for shared environments.

## M1-Specific Rules

On MacBook Air M1 8GB (the target hardware):
- Use bounded `limit` (5-20) on all search calls — avoid loading massive result sets into memory.
- Avoid running large mining operations during active coding sessions — mine when idle.
- Reranking (`mempalace_hybrid_search` with `rerank: true`) is expensive (~90MB model load, 3s init). Use only for hard semantic queries where FTS5 keyword match alone is insufficient.
- Do not use `mempalace_export_claude_md` with large room filters — use targeted `mempalace_search` instead.
- Use one shared MemPalace server for all 6 parallel Claude Code sessions — never spawn additional servers.
- Do not use Docker for MemPalace — the server runs natively with MLX for Apple Silicon.
- Do not configure a LaunchAgent for server lifecycle — the plugin hooks handle server start/stop automatically.

## Tool Tiers (Quick Reference)

**Tier 1 — Must use before editing:**
- `mempalace_file_status` — orient before claiming
- `mempalace_begin_work` — claim file
- `mempalace_prepare_edit` — get symbol context + hot-spots
- `mempalace_finish_work` — release + diary

**Tier 2 — As needed:**
- `mempalace_search_code` — code search with filters
- `mempalace_project_context` — repo-level context
- `mempalace_find_symbol` — symbol lookup
- `mempalace_file_symbols` — list symbols in a file

**Tier 3 — General search:**
- `mempalace_search` — semantic memory search
- `mempalace_hybrid_search` — combined keyword + semantic

## Claude Code Pre-Edit Checklist

Before touching any file, follow this sequence:

1. **Orient** — `mempalace_status` or `/mempalace:doctor` — verify server healthy, palace accessible
2. **Context** — `mempalace_project_context` with `project_path` — scope retrieval to the repo
3. **Search** — `mempalace_search_code` with `project_path` — verify file exists and is indexed
4. **Find symbol** — `mempalace_find_symbol` if your task involves a specific symbol
5. **File context** — `mempalace_file_context` with exact `line_start`/`line_end` range (requires `project_path` or allowed roots)
6. **Claim** — `mempalace_begin_work` — acquire exclusive claim before editing
7. **Prepare** — `mempalace_prepare_edit` — symbol context + hot-spots + conflict check
8. **Edit** — make the change
9. **Test** — run tests
10. **Finish** — `mempalace_finish_work` — release claim + diary entry
11. **Handoff** — if incomplete, `mempalace_publish_handoff` for the next session

**Security gates:** `mempalace_file_context` requires `project_path` or `MEMPALACE_ALLOWED_ROOTS`. Without them, access is denied by default.

**M1 8GB limits:** Bounded `limit` on all searches. No large exports or unbounded mining during active coding.