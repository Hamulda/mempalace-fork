"""
Hook logic for MemPalace — Python implementation of session-start, stop, and precompact hooks.

Reads JSON from stdin, outputs JSON to stdout.
Supported hooks: session-start, stop, precompact
Supported harnesses: claude-code, codex (extensible to cursor, gemini, etc.)
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STATE_DIR = Path.home() / ".mempalace" / "hook_state"

STOP_BLOCK_REASON = (
    "AUTO-SAVE checkpoint. Save key topics, decisions, quotes, and code "
    "from this session to your memory system. Organize into appropriate "
    "categories. Use verbatim quotes where possible. Continue conversation "
    "after saving."
)

PRECOMPACT_BLOCK_REASON = (
    "COMPACTION IMMINENT. Save ALL topics, decisions, quotes, code, and "
    "important context from this session to your memory system. Be thorough "
    "\u2014 after compaction, detailed context will be lost. Organize into "
    "appropriate categories. Use verbatim quotes where possible. Save "
    "everything, then allow compaction to proceed."
)


def _sanitize_session_id(session_id: str) -> str:
    """Only allow alnum, dash, underscore to prevent path traversal."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized or "unknown"


def _count_human_messages(transcript_path: str) -> int:
    """Count human messages in a JSONL transcript, skipping command-messages."""
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            if "<command-message>" in content:
                                continue
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "") for b in content if isinstance(b, dict)
                            )
                            if "<command-message>" in text:
                                continue
                        count += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return 0
    return count


def _log(message: str):
    """Append to hook state log file."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "hook.log"
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def _output(data: dict):
    """Print JSON to stdout with consistent formatting (pretty-printed)."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _maybe_auto_ingest():
    """If MEMPAL_DIR is set and exists, run mempalace mine in background."""
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        try:
            log_path = STATE_DIR / "hook.log"
            with open(log_path, "a") as log_f:
                subprocess.Popen(
                    [sys.executable, "-m", "mempalace", "mine", mempal_dir],
                    stdout=log_f,
                    stderr=log_f,
                )
        except OSError:
            pass


SUPPORTED_HARNESSES = {"claude-code", "codex"}


def _parse_harness_input(data: dict, harness: str) -> dict:
    """Parse stdin JSON according to the harness type."""
    if harness not in SUPPORTED_HARNESSES:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(1)
    return {
        "session_id": _sanitize_session_id(str(data.get("session_id", "unknown"))),
        "stop_hook_active": data.get("stop_hook_active", False),
        "transcript_path": str(data.get("transcript_path", "")),
    }


def hook_stop(data: dict, harness: str, transport: str = "cli"):
    """Stop hook: block every N messages for auto-save."""
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    stop_hook_active = parsed["stop_hook_active"]
    transcript_path = parsed["transcript_path"]

    # If already in a save cycle, let through (infinite-loop prevention)
    if str(stop_hook_active).lower() in ("true", "1", "yes"):
        _output({})
        return

    # Count human messages
    exchange_count = _count_human_messages(transcript_path)

    # Track last save point
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_save_file = STATE_DIR / f"{session_id}_last_save"
    last_save = 0
    if last_save_file.is_file():
        try:
            last_save = int(last_save_file.read_text().strip())
        except (ValueError, OSError):
            last_save = 0

    since_last = exchange_count - last_save

    _log(f"Session {session_id}: {exchange_count} exchanges, {since_last} since last save")

    from mempalace.config import MempalaceConfig
    cfg = MempalaceConfig()
    save_interval = cfg.hook_save_interval
    save_interval_min = cfg.hook_save_interval_min

    if since_last >= save_interval and exchange_count >= save_interval_min:
        # Update last save point
        try:
            last_save_file.write_text(str(exchange_count), encoding="utf-8")
        except OSError:
            pass

        _log(f"TRIGGERING SAVE at exchange {exchange_count}")

        # Optional: auto-ingest if MEMPAL_DIR is set
        _maybe_auto_ingest()

        _output({"decision": "block", "reason": STOP_BLOCK_REASON})
    else:
        _output({})


def hook_session_start(data: dict, harness: str, transport: str = "cli"):
    """Session start hook: inject relevant memories as session context."""
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"SESSION START for session {session_id}")

    # Initialize session state directory
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from mempalace.config import MempalaceConfig
        cfg = MempalaceConfig()

        if not cfg.hook_session_start_inject:
            _output({})
            return

        # Get project name from CWD — safe fallback
        cwd = str(data.get("cwd", "")).strip()
        project_name = os.path.basename(cwd) if cwd else ""
        if not project_name:
            _output({})
            return

        # Thread-based timeout wrapper for search
        import concurrent.futures
        from mempalace.searcher import search_memories
        result_data = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(
                    search_memories,
                    query=project_name,
                    palace_path=cfg.palace_path,
                    n_results=cfg.hook_session_start_top_k,
                )
                result_data = future.result(timeout=5.0)
        except concurrent.futures.TimeoutError:
            _log(f"SESSION START search timed out after 5s for project: {project_name}")
            result_data = {}
        except Exception as e:
            _log(f"SESSION START direct search failed: {e}")
            result_data = {}

        lines = [r["text"][:120] for r in result_data.get("results", [])]

        if not lines:
            _output({})
            return

        context = "## Relevant memories\n" + "\n".join(f"- {l}" for l in lines)
        _output({"context": context})

    except Exception as e:
        _log(f"SESSION START inject failed (silent): {e}")
        return


def hook_precompact(data: dict, harness: str, transport: str = "cli"):
    """Precompact hook: always block with comprehensive save instruction."""
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"PRE-COMPACT triggered for session {session_id}")

    # Optional: auto-ingest synchronously before compaction (so memories land first)
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        try:
            log_path = STATE_DIR / "hook.log"
            mine_timeout = int(os.environ.get("MEMPALACE_MINE_TIMEOUT", "120"))
            with open(log_path, "a") as log_f:
                subprocess.run(
                    [sys.executable, "-m", "mempalace", "mine", mempal_dir],
                    stdout=log_f,
                    stderr=log_f,
                    timeout=mine_timeout,
                )
        except (OSError, subprocess.TimeoutExpired) as e:
            _log(f"mine timed out or failed during precompact: {e}")

    # Always block -- compaction = save everything
    _output({"decision": "block", "reason": PRECOMPACT_BLOCK_REASON})


def run_hook(hook_name: str, harness: str, transport: str = "cli"):
    """Main entry point: read stdin JSON, dispatch to hook handler."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _log("WARNING: Failed to parse stdin JSON, proceeding with empty data")
        data = {}

    hooks = {
        "session-start": hook_session_start,
        "stop": hook_stop,
        "precompact": hook_precompact,
    }

    handler = hooks.get(hook_name)
    if handler is None:
        print(f"Unknown hook: {hook_name}", file=sys.stderr)
        sys.exit(1)

    handler(data, harness, transport)
