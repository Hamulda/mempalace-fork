#!/usr/bin/env python3
"""
Claude Code pre-compact hook.
Uloží snapshot konverzace před /compact do MemPalace.
"""
import json
import sys
import os
import datetime
import urllib.request


def main():
    try:
        hook_data = json.load(sys.stdin)
    except Exception:
        hook_data = {}

    conversation_text = hook_data.get("conversation", "")
    session_id = hook_data.get("session_id", "unknown")
    project = hook_data.get("cwd", os.getcwd())

    if not conversation_text:
        sys.exit(0)

    snapshot_memory = {
        "content": (
            f"[PRE-COMPACT SNAPSHOT {datetime.datetime.now().isoformat()}]\n\n"
            f"Project: {project}\nSession: {session_id}\n\n"
            f"{conversation_text[:4000]}"
        ),
        "metadata": {
            "type": "compact_snapshot",
            "session_id": session_id,
            "project": project,
            "timestamp": datetime.datetime.now().isoformat(),
        },
    }

    mcp_url = os.environ.get("MEMPALACE_URL", "http://127.0.0.1:8766")

    try:
        req_data = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "mempalace_add_drawer",
                    "arguments": {
                        "wing": "system",
                        "room": "snapshots",
                        "content": snapshot_memory["content"],
                        "added_by": "pre_compact_hook",
                    },
                },
                "id": 1,
            }
        ).encode()

        req = urllib.request.Request(
            f"{mcp_url}/mcp",
            data=req_data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        print(f"✅ MemPalace: snapshot saved before /compact", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ MemPalace snapshot failed: {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
