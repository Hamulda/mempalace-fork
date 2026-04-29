---
description: Mine projects and conversations into MemPalace — code, docs, Claude exports.
allowed-tools: Bash, Read
---

# MemPalace Mine

## Mine Project Files

```bash
mempalace mine ~/projects/myapp
```

For large repos, use `--limit` to bound indexing scope:
```bash
mempalace mine ~/projects/myapp --limit 1000
```

## Mine Conversations

```bash
mempalace mine ~/chats/ --mode convos
mempalace mine ~/chats/ --mode convos --extract general
```

## Auto-Classification

The `general` mode classifies into:
- **decisions** — architectural choices, "we decided to..."
- **milestones** — goals achieved, "finished X"
- **problems** — issues encountered and solutions
- **preferences** — "user prefers dark mode"

## Workflow

After mining, your AI can search and find:
```
mempalace search "why did we switch to GraphQL"
```

## Tips

- Mine early, mine often — MemPalace stores everything verbatim
- First time: `mempalace init ~/palace` to set up structure
- Use `--mode convos` for Claude Code conversation exports
- On M1 8GB: use `--limit` to bound indexing scope. Do not run large mining during active coding — mine when idle.
- The `general` mode classifies into: decisions, milestones, problems, preferences.
