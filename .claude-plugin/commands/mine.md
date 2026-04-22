---
description: Mine projects and conversations into MemPalace — code, docs, Claude exports.
allowed-tools: Bash, Read
---

# MemPalace Mine

## Mine Project Files

```bash
mempalace mine ~/projects/myapp
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
