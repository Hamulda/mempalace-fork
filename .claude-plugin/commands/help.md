---
description: Show MemPalace help — canonical setup, workflow path, tools, and architecture.
allowed-tools: Bash, Read
---

# MemPalace Help

## Canonical Setup (for 6 parallel Claude Code sessions)

```
1. pip install git+https://github.com/Hamulda/mempalace-fork
2. mempalace serve --host 127.0.0.1 --port 8765  (one shared server)
3. mempalace init ~/palace
4. Install Claude Code plugin: claude plugin marketplace add hamulda/mempalace-fork
```

Verify: `curl http://127.0.0.1:8765/health`

## Workflow Path (use these in this order)

| Step | Tool | Purpose |
|------|------|---------|
| 1 | `mempalace_file_status` | Quick snapshot before editing |
| 2 | `mempalace_begin_work` | Start session, claim files, check conflicts |
| 3 | `mempalace_prepare_edit` | Get symbol context, hot-spot, auto conflict check |
| 4 | **You edit** | Model makes the edit |
| 5 | `mempalace_finish_work` | Release claim, write diary, capture decisions |
| 6 | `mempalace_publish_handoff` | Atomic handoff for multi-file changes |

## Tier 2 — When Tier 1 Won't Do

- `mempalace_claim_path` — refresh TTL on existing claim
- `mempalace_release_claim` — manual release
- `mempalace_conflict_check` — explicit conflict check
- `mempalace_push_handoff` / `mempalace_pull_handoffs` / `mempalace_accept_handoff`

## Tier 3 — Search & Knowledge

- `mempalace_search` — semantic search across all memories
- `mempalace_hybrid_search` — semantic + keyword combined
- `mempalace_code_search` — code-specialized search
- `mempalace_kg_query` — knowledge graph entities
- `mempalace_status` — palace overview

## Architecture

```
Claude Code (6 sessions) → MCP HTTP → mempalace serve (1 process, port 8765)
                                            ↓
                                    SessionRegistry (shared)
                                    WriteCoordinator (WAL)
                                    ClaimsManager (file locks)
                                    HandoffManager (atomic)
                                    DecisionTracker
                                            ↓
                                    LanceDB (~/.mempalace/)
```

## Organization

| Content | Wing | Room |
|---------|------|------|
| Architectural decisions | technical | decisions |
| Code and implementations | technical | code |
| Debates and conclusions | technical | discussions |
| User personal preferences | identity | preferences |

## Server Not Running?

```bash
mempalace serve --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/health
```
