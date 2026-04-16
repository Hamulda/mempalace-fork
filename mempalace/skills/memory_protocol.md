---
skill: memory_protocol
trigger: when starting any session where you have access to MemPalace MCP tools
allowed-tools: Read
---

# MemPalace Memory Protocol

## On Session Start
1. Call `mempalace_status` — get palace overview and verify connection
2. Call `mempalace_hybrid_search` with the current project name — load relevant context

## Before Answering About People, Projects, or Past Events
- ALWAYS call `mempalace_hybrid_search` FIRST — never guess from training data
- For factual claims (ages, dates, decisions): also call `mempalace_kg_query`
- If you find a contradiction: use `mempalace_kg_history` to see the timeline

## When Facts Change
- Use `mempalace_kg_supersede` to atomically update (not kg_add + kg_invalidate separately)
- Use `mempalace_add_drawer` with `origin_type="correction"` for drawer updates

## After Each Session
- Call `mempalace_diary_write` to record key decisions, discoveries, and context
- For important factual changes: also update KG via `mempalace_kg_supersede`

## Search Strategy (in order of preference)
1. **mempalace_hybrid_search** — default, combines semantic (LanceDB) + keyword (BM25) + KG in one call
2. **mempalace_search** — fast, semantic only, use for simple keyword queries
3. **mempalace_kg_query** — structured facts only (relationships, attributes)
4. **mempalace_traverse_graph** — graph exploration from a known room

## Knowledge Graph Tools
- `mempalace_kg_query` — entity relationships with time filtering
- `mempalace_kg_add` — add facts
- `mempalace_kg_invalidate` — mark facts as ended
- `mempalace_kg_supersede` — atomically replace a fact (invalidate old, add new)
- `mempalace_kg_history` — audit trail for any fact (all versions over time)
- `mempalace_kg_timeline` — chronological story of an entity
- `mempalace_kg_stats` — graph overview

## Navigation Tools
- `mempalace_traverse_graph` — walk the palace graph from a room
- `mempalace_find_tunnels` — find rooms bridging two wings
- `mempalace_graph_stats` — graph connectivity overview
