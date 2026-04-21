# MemPalace

AI memory system. Store everything, find anything. Local, free, no API key.

---

## Workflow-First (for Claude Code)

**The primary path** (use these tools in this order):

1. `mempalace_file_status` — quick orientation before claiming
2. `mempalace_begin_work` — claim the file
3. `mempalace_prepare_edit` — get symbol context
4. **[make the edit]** — the model does the editing
5. `mempalace_finish_work` — release + diary + decision (single-file)

For multi-file / context handoff:
- `mempalace_publish_handoff` — atomic publish + release all claims in one call

For takeover:
- `mempalace_takeover_work` — accept handoff + claim paths in one call

**Low-level tools** (expert escape-hatch — only when primary won't do):
- `mempalace_claim_path` — refresh TTL on existing claim
- `mempalace_release_claim` — manual release without diary
- `mempalace_conflict_check` — explicit check beyond workflow tools
- `mempalace_push_handoff` — handoff without atomic claim release
- `mempalace_pull_handoffs` — list handoffs without accepting
- `mempalace_accept_handoff` — accept without auto-claiming paths
- `mempalace_complete_handoff` — mark done without publish flow
- `mempalace_edit_guidance` — convert any workflow_result → plain guidance

**Every workflow tool returns** `workflow_state` with:
- `current_phase`: `claim_acquired | context_ready | editing | finished | published | takeover`
- `next_tool`: the single best next action (never null on success)
- `conflict_status`: `none | self_claim | other_claim | hotspot`
- `handoff_pending`: boolean

---

## Slash Commands

| Command              | Description                    |
|----------------------|--------------------------------|
| /mempalace:init      | Install and set up MemPalace   |
| /mempalace:search    | Search your memories           |
| /mempalace:mine      | Mine projects and conversations|
| /mempalace:status    | Palace overview and stats      |
| /mempalace:help      | This help message              |

---

## MCP Tools (27)

### Palace (read)
- mempalace_status -- Palace status and stats
- mempalace_list_wings -- List all wings
- mempalace_list_rooms -- List rooms in a wing
- mempalace_get_taxonomy -- Get the full taxonomy tree
- mempalace_search -- Search memories by query
- mempalace_check_duplicate -- Check if a memory already exists
- mempalace_get_aaak_spec -- Get the AAAK specification
- mempalace_eval -- Evaluate retrieval quality (hit rates, avg similarity, wing precision)

### Palace (write)
- mempalace_add_drawer -- Add a new memory (drawer)
- mempalace_delete_drawer -- Delete a memory (drawer)

### Knowledge Graph
- mempalace_kg_query -- Query the knowledge graph
- mempalace_kg_add -- Add a knowledge graph entry
- mempalace_kg_invalidate -- Invalidate a knowledge graph entry
- mempalace_kg_timeline -- View knowledge graph timeline
- mempalace_kg_stats -- Knowledge graph statistics

### Navigation
- mempalace_traverse -- Traverse the palace structure
- mempalace_find_tunnels -- Find cross-wing connections
- mempalace_graph_stats -- Graph connectivity statistics

### Agent Diary
- mempalace_diary_write -- Write a diary entry
- mempalace_diary_read -- Read diary entries

---

## New in this fork

- **mempalace_hybrid_search** — combines semantic (LanceDB) + keyword (BM25) + KG in one call
- **mempalace_kg_supersede** — atomically replace a fact (invalidate old, add new)
- **mempalace_kg_history** — audit trail for any fact (all versions over time)
- **mempalace_eval** — evaluate retrieval quality: hit rates, avg similarity, wing precision
- **mempalace_remember_code** — store code with description for better semantic search
- **mempalace_consolidate** — find and optionally merge duplicate memories by topic
- **mempalace_export_claude_md** — export memories to CLAUDE.md format

## CLI Commands

    mempalace init <dir>                  Initialize a new palace
    mempalace mine <dir>                  Mine a project (default mode)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace search "query"              Search your memories
    mempalace split <dir>                 Split large transcript files
    mempalace wake-up                     Load palace into context
    mempalace compress                    Compress palace storage
    mempalace status                      Show palace status
    mempalace repair                      Rebuild vector index
    mempalace mcp                         Show MCP setup command
    mempalace hook run                    Run hook logic (for harness integration)
    mempalace instructions <name>         Output skill instructions

---

## Auto-Save Hooks

- Stop hook -- Automatically saves memories every 15 messages. Counts human
  messages in the session transcript (skipping command-messages). When the
  threshold is reached, blocks the AI with a save instruction. Uses
  ~/.mempalace/hook_state/ to track save points per session. If
  stop_hook_active is true, passes through to prevent infinite loops.

- PreCompact hook -- Emergency save before context compaction. Always blocks
  with a comprehensive save instruction because compaction means the AI is
  about to lose detailed context.

Hooks read JSON from stdin and output JSON to stdout. They can be invoked via:

    echo '{"session_id":"abc","stop_hook_active":false,"transcript_path":"..."}' | mempalace hook run --hook stop --harness claude-code

---

## Architecture

    Wings (projects/people)
      +-- Rooms (topics)
            +-- Closets (summaries)
                  +-- Drawers (verbatim memories)

    Halls connect rooms within a wing.
    Tunnels connect rooms across wings.

The palace is stored locally using LanceDB for vector search and SQLite for
metadata. No cloud services or API keys required.

---

## Getting Started

1. /mempalace:init -- Set up your palace
2. /mempalace:mine -- Mine a project or conversation
3. /mempalace:search -- Find what you stored
