@~/.claude/RTK.md

<!-- context-mode v1 -->
## Context-Mode Defaults (Token Budget)

**Rule: ctx_execute for analysis, Read only for editing.**

| Situation | Tool | Why |
|-----------|------|-----|
| CLI output, API calls, test runs | `ctx_execute` | Token-filtered, sandboxed |
| File content for analysis | `ctx_execute_file` | Summary only enters context |
| Large log/data files | `ctx_execute_file` | Process in sandbox, print findings |
| Fetching web docs | `ctx_fetch_and_index` | Indexed, searchable, no raw HTML |
| File to edit | `Read` | Need full content to modify |
| Git writes, mkdir, navigation | `Bash` | Whitelisted, safe |
| Playwright snapshots | `browser_snapshot(filename)` + `ctx_index` | Never raw to context |

**Decision tree:**
```
About to run a command or read a file?
├── Bash whitelist (git, mkdir, cd)? → Bash
├── Edit this file? → Read
├── Analyze output / run tests / call API? → ctx_execute
├── Read file for analysis (not editing)? → ctx_execute_file
└── Web fetch? → ctx_fetch_and_index
```

**Why this matters here:** `/clear` rate is 4.5/session. Context overflow is the primary efficiency loss. Every large raw output that enters context unfiltered costs tokens permanently. RTK compresses Bash output; it cannot compress tool output already in context.

**Bash whitelist (safe to run directly):**
- File mutations: `mkdir`, `mv`, `cp`, `rm`, `touch`, `chmod`
- Git writes: `git add`, `git commit`, `git push`, `git checkout`, `git branch`, `git merge`
- Navigation: `cd`, `pwd`, `which`
- Process control: `kill`, `pkill`
- Package management: `npm install`, `pip install`

## Skill Activation Triggers

**Invoke before the work, not after:**

| Before... | Invoke | Why |
|-----------|--------|-----|
| Python async, performance, patterns | `/python-development:async-python-patterns` | Avoid async anti-patterns from day 1 |
| Python perf work | `/python-development:python-performance-optimization` | Profile first, then optimize |
| Python design decisions | `/python-development:python-design-patterns` | KISS, DRY, SOLID checks |
| Python resource management | `/python-development:python-resource-management` | Memory, context managers |
| Agent team work | `/agent-teams:team-composition-patterns` | Right size, right agents |
| Team communication | `/agent-teams:team-communication-protocols` | Avoid broadcast spam, use shutdown protocol |
| Multi-reviewer work | `/agent-teams:team-review` | Parallel review dimensions |
| SQL work | `/developer-essentials:sql-optimization-patterns` | Index hints, query plans |
| E2E testing | `/developer-essentials:e2e-testing-patterns` | Playwright best practices |
| Code quality review | `/python-development:python-anti-patterns` | Catch anti-patterns before they land |
| Memory/codebase graph | `/codebase-memory-quality` | Check graph health before critical work |

## File Read Discipline

**Analyze with ctx_execute_file, not Read.** Read dumps full content into context. ctx_execute_file processes in sandbox and prints only findings.

| Goal | Wrong | Right |
|------|-------|-------|
| Find a pattern across files | Read 10 files | `ctx_execute_file` + grep in sandbox |
| Count lines/functions | Read the file | `ctx_execute_file` with analysis code |
| Read a log file | Read 500-line log | `ctx_execute_file` → extract errors only |
| Parse JSON/CSV data | Read into context | `ctx_execute_file` → process and print summary |
| Review diff | Read full diff | `ctx_execute_file` → summarize changes |

**Exception:** Read is correct when you intend to Edit the file next. Context needs the full content to compute the edit.
<!-- /context-mode -->

<!-- rtk-instructions v2 -->
# RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:
```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## RTK Commands by Workflow

### Build & Compile (80-90% savings)
```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (60-99% savings)
```bash
rtk cargo test          # Cargo test failures only (90%)
rtk go test             # Go test failures only (90%)
rtk jest                # Jest failures only (99.5%)
rtk vitest              # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk pytest              # Python test failures only (90%)
rtk rake test           # Ruby test failures only (90%)
rtk rspec               # RSpec test failures only (60%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)
```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)
```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)
```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
```

### Files & Search (60-75% savings)
```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%)
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)
```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)
```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)
```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands
```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category | Commands | Typical Savings |
|----------|----------|-----------------|
| Tests | vitest, playwright, cargo test | 90-99% |
| Build | next, tsc, lint, prettier | 70-87% |
| Git | status, log, diff, add, commit | 59-80% |
| GitHub | gh pr, gh run, gh issue | 26-87% |
| Package Managers | pnpm, npm, npx | 70-90% |
| Files | ls, read, grep, find | 60-75% |
| Infrastructure | docker, kubectl | 85% |
| Network | curl, wget | 65-70% |

Overall average: **60-90% token reduction** on common development operations.
<!-- /rtk-instructions -->