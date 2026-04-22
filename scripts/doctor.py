#!/usr/bin/env python3
"""
MemPalace Doctor — canonical setup validation.

Run: python scripts/doctor.py

Checks:
1. Plugin manifest is valid JSON and has no mcpServers (plugin does NOT spawn MCP)
2. .mcp.json has correct HTTP transport pointing to port 8765
3. No stale milla-jovovich references anywhere
4. All commands exist and reference canonical paths
5. CLI defaults to port 8765
6. Keywords are clean (no chromadb drift)
"""
from __future__ import annotations

import json
import pathlib
import sys
import re

REPO = pathlib.Path(__file__).parent.parent.resolve()
ERRORS: list[str] = []
WARNINGS: list[str] = []


def err(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def check_plugin_json() -> None:
    """plugin.json must NOT have mcpServers — plugin is skills+hooks only."""
    p = REPO / ".claude-plugin" / "plugin.json"
    if not p.exists():
        err(f"Missing: {p}")
        return

    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        err(f"plugin.json is invalid JSON: {e}")
        return

    if "mcpServers" in data:
        err("plugin.json must NOT have mcpServers — plugin is skills/hooks/docs only. MCP server is external.")

    if data.get("name") != "mempalace":
        err(f"plugin.json name must be 'mempalace', got: {data.get('name')}")

    repo = data.get("repository", "")
    if "milla-jovovich" in repo:
        err(f"plugin.json repository points to old upstream: {repo}")

    if "Hamulda" not in repo and "hamulda" not in repo.lower():
        warn(f"plugin.json repository may not be the fork: {repo}")

    author = data.get("author", {}).get("name", "")
    if author == "milla-jovovich":
        err(f"plugin.json author is old upstream: {author}")

    keywords = data.get("keywords", [])
    if "chromadb" in keywords:
        err(f"plugin.json keywords contain 'chromadb' — LanceDB is canonical")
    if "lancedb" not in keywords:
        warn(f"plugin.json keywords missing 'lancedb'")

    # Check skills and commands declarations
    skills = data.get("skills", "")
    commands = data.get("commands", "")
    if not skills:
        warn("plugin.json missing 'skills' declaration — skills may not auto-discover")
    if not commands:
        warn("plugin.json missing 'commands' declaration — commands may not auto-discover")


def check_mcp_json() -> None:
    """.mcp.json must have HTTP transport on port 8765."""
    p = REPO / ".claude-plugin" / ".mcp.json"
    if not p.exists():
        err(f"Missing: {p}")
        return

    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        err(f".mcp.json is invalid JSON: {e}")
        return

    srv = data.get("mempalace", {})
    transport = srv.get("transport")
    url = srv.get("url", "")

    if transport != "http":
        err(f".mcp.json transport must be 'http', got: {transport}")

    if "8765" not in url:
        err(f".mcp.json url must use port 8765, got: {url}")

    if not url.startswith("http://127.0.0.1"):
        warn(f".mcp.json url should be localhost-only: {url}")


def check_stale_references() -> None:
    """No milla-jovovich references in any text file."""
    for md in REPO.rglob("*.md"):
        if ".git" in str(md):
            continue
        content = md.read_text(errors="ignore")
        if "milla-jovovich/mempalace" in content and "mempalace-fork" not in content and "mempalace-fork" not in str(md):
            err(f"Stale upstream ref in {md.relative_to(REPO)}: milla-jovovich/mempalace")
            break

    for py in REPO.rglob("*.py"):
        if ".git" in str(py) or "tests/" in str(py) or "scripts/" in str(py):
            continue
        content = py.read_text(errors="ignore")
        if 'github.com/milla-jovovich/mempalace' in content and "mempalace-fork" not in str(py):
            err(f"Stale upstream ref in {py.relative_to(REPO)}")
            break


def check_commands() -> None:
    """All 5 commands must exist and be non-empty."""
    cmd_dir = REPO / ".claude-plugin" / "commands"
    expected = {"help.md", "init.md", "mine.md", "search.md", "status.md"}

    if not cmd_dir.exists():
        err(f"Missing commands dir: {cmd_dir}")
        return

    actual = {f.name for f in cmd_dir.iterdir() if f.suffix == ".md"}
    missing = expected - actual
    if missing:
        err(f"Missing commands: {missing}")

    for f in cmd_dir.iterdir():
        if f.suffix == ".md" and f.stat().st_size == 0:
            err(f"Empty command file: {f.name}")


def check_cli_port_default() -> None:
    """cli.py default port must be 8765."""
    cli_path = REPO / "mempalace" / "cli.py"
    if not cli_path.exists():
        err(f"Missing cli.py: {cli_path}")
        return

    content = cli_path.read_text()
    if "default=8766" in content:
        err("cli.py still has default port 8766 — must be 8765")
    if "8766" in content and "8765" not in content:
        err("cli.py references 8766 but not 8765")


def check_skill_md_workflow() -> None:
    """SKILL.md must guide to workflow-first path."""
    skill_path = REPO / ".claude-plugin" / "skills" / "mempalace" / "SKILL.md"
    if not skill_path.exists():
        err(f"Missing SKILL.md: {skill_path}")
        return

    content = skill_path.read_text()
    tier1_tools = [
        "mempalace_file_status",
        "mempalace_begin_work",
        "mempalace_prepare_edit",
        "mempalace_finish_work",
        "mempalace_publish_handoff",
    ]
    for tool in tier1_tools:
        if tool not in content:
            warn(f"SKILL.md missing Tier 1 tool: {tool}")

    # Should NOT lead with raw search tools
    lines = content.split("\n")
    for line in lines[:20]:
        if "Tier 3" in line or "search" in line.lower():
            continue
        if line.strip().startswith("|") and "mempalace_search" in line:
            warn("SKILL.md leads with Tier 3 search — should be Tier 1 workflow first")


def main() -> int:
    check_plugin_json()
    check_mcp_json()
    check_stale_references()
    check_commands()
    check_cli_port_default()
    check_skill_md_workflow()

    print("MemPalace Doctor")
    print("=" * 40)
    print(f"Repo: {REPO.name}")
    print()

    if ERRORS:
        print(f"❌ {len(ERRORS)} ERROR(S):")
        for e in ERRORS:
            print(f"  • {e}")
        print()

    if WARNINGS:
        print(f"⚠️  {len(WARNINGS)} WARNING(S):")
        for w in WARNINGS:
            print(f"  • {w}")
        print()

    if not ERRORS and not WARNINGS:
        print("✅ All checks passed — canonical setup is valid")

    return 1 if ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
