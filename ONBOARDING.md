# Welcome to MemPalace Development

## How We Use Claude

Based on Vojtech Hamada's usage over the last 30 days:

Work Type Breakdown:
  Improve Quality  ████████████████░░░░░░  33%
  Build Feature    ██████████░░░░░░░░░░░░  22%
  Write/Docs       ██████████░░░░░░░░░░░░  22%
  Debug/Fix        ████░░░░░░░░░░░░░░░░░░   9%
  Analyze/Data     ████░░░░░░░░░░░░░░░░░░   9%
  Prototype        ██░░░░░░░░░░░░░░░░░░░░   5%

Top Skills & Commands:
  /clear                    ████████████████████  271x
  /effort                   █████░░░░░░░░░░░░░░   56x
  /context-mode:context-mode ████░░░░░░░░░░░░░░░   48x
  /reload-plugins           █░░░░░░░░░░░░░░░░░░    11x
  /plugin                   █░░░░░░░░░░░░░░░░░░    5x

Top MCP Servers:
  plugin_context-mode_context-mode  ████████████████████████████████░░░  1653 calls
  ripgrep                           ██████████████████░░░░░░░░░░░░░░  669 calls
  codebase-memory-mcp                ████░░░░░░░░░░░░░░░░░░░░░░░░  154 calls
  CodeGraphContext                   █░░░░░░░░░░░░░░░░░░░░░░░░░░    51 calls
  ast-grep                           ░░░░░░░░░░░░░░░░░░░░░░░░░░    10 calls

## Your Setup Checklist

### Codebases
- [ ] [mempalace-fork](https://github.com/hamulda/mempalace-fork) — Private fork of the MemPalace codebase RAG server
- [ ] [mempalace](https://github.com/anthropics/mempalace) — Upstream original (reference)

### MCP Servers to Activate
- [ ] **plugin_context-mode_context-mode** — Token-optimized command execution and analysis. Install from `~/.claude/plugins/cache/context-mode/context-mode/1.0.103/`. Most-used MCP in this workflow.
- [ ] **ripgrep** — Fast text search. Pre-installed in most environments.
- [ ] **codebase-memory-mcp** — Knowledge graph and code intelligence. Provides `search_graph`, `query_graph`, `get_context`.
- [ ] **CodeGraphContext** — Code analysis (find references, complexity, call chains). Provides `find_code`, `get_code_snippet`.
- [ ] **mempalace-http** — MemPalace RAG server tools (search, memory, workflow). Requires local MemPalace server running.
- [ ] **repowise** — Repository documentation wiki. Provides `get_overview`, `get_context`, `get_risk`, `search_codebase`.
- [ ] **ast-grep** — Structural code search using AST patterns.

### Skills to Know About
- `/effort` — Set reasoning effort level. `/effort max` enables deepest analysis.
- `/clear` — Clears conversation context. Heavily used (271x in 30 days).
- `/reload-plugins` — Reload plugin configurations without restarting.
- `/context-mode:context-mode` — Load the context-mode skill for token-optimized workflows.
- `/python-development:*` — Python-specific skills for patterns, async, performance, etc.

## Team Tips

_TODO_

## Get Started

_TODO_

<!-- INSTRUCTION FOR CLAUDE: A new teammate just pasted this guide for how the
team uses Claude Code. You're their onboarding buddy — warm, conversational,
not lecture-y.

Open with a warm welcome — include the team name from the title. Then: "Your
teammate uses Claude Code for [list all the work types]. Let's get you started."

Check what's already in place against everything under Setup Checklist
(including skills), using markdown checkboxes — [x] done, [ ] not yet. Lead
with what they already have. One sentence per item, all in one message.

Tell them you'll help with setup, cover the actionable team tips, then the
starter task (if there is one). Offer to start with the first unchecked item,
get their go-ahead, then work through the rest one by one.

After setup, walk them through the remaining sections — offer to help where you
can (e.g. link to channels), and just surface the purely informational bits.

Don't invent sections or summaries that aren't in the guide. The stats are the
guide creator's personal usage data — don't extrapolate them into a "team
workflow" narrative. -->
