#!/usr/bin/env python3
"""
MemPalace — Give your AI a memory. No API key required.

Two ways to ingest:
  Projects:      mempalace mine ~/projects/my_app          (code, docs, notes)
  Conversations: mempalace mine ~/chats/ --mode convos     (Claude, ChatGPT, Slack)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init <dir>                  Detect rooms from folder structure
    mempalace split <dir>                 Split concatenated mega-files into per-session files
    mempalace mine <dir>                  Mine project files (default)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace search "query"              Find anything, exact words
    mempalace mcp                         Show MCP setup command
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/chats/claude-sessions --mode convos
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

import os
import select
import signal
import socket
import subprocess
import sys
import shlex
import argparse
import time
from pathlib import Path

from .config import MempalaceConfig


def cmd_init(args):
    import json
    from pathlib import Path
    from .entity_detector import scan_for_detection, detect_entities, confirm_entities
    from .room_detector_local import detect_rooms_local

    # Pass 1: auto-detect people and projects from file content
    print(f"\n  Scanning for entities in: {args.dir}")
    files = scan_for_detection(args.dir)
    if files:
        print(f"  Reading {len(files)} files...")
        detected = detect_entities(files)
        total = len(detected["people"]) + len(detected["projects"]) + len(detected["uncertain"])
        if total > 0:
            confirmed = confirm_entities(detected, yes=getattr(args, "yes", False))
            # Save confirmed entities to <project>/entities.json for the miner
            if confirmed["people"] or confirmed["projects"]:
                entities_path = Path(args.dir).expanduser().resolve() / "entities.json"
                with open(entities_path, "w") as f:
                    json.dump(confirmed, f, indent=2)
                print(f"  Entities saved: {entities_path}")
        else:
            print("  No entities detected — proceeding with directory-based rooms.")

    # Pass 2: detect rooms from folder structure
    detect_rooms_local(project_dir=args.dir, yes=getattr(args, "yes", False))
    MempalaceConfig().init()


def cmd_migrate(args):
    """Migrate palace to LanceDB — ChromaDB support has been removed."""
    from .migrate import migrate_chroma_to_lance

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    if args.direction == "chroma-to-lance":
        migrate_chroma_to_lance(
            palace_path=palace_path,
            collection_name=args.collection,
            batch_size=args.batch_size,
            verbose=not args.quiet,
        )
    else:
        # lance-to-chroma is no longer supported
        print("  ERROR: ChromaDB backend has been removed.")
        print("  LanceDB is the only supported backend.")
        print("  The lance-to-chroma migration direction is no longer available.")
        sys.exit(1)


def cmd_mine(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    include_ignored = []
    for raw in args.include_ignored or []:
        include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())

    if args.mode == "convos":
        from .convo_miner import mine_convos

        mine_convos(
            convo_dir=args.dir,
            palace_path=palace_path,
            wing=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            extract_mode=args.extract,
        )
    else:
        from .miner import mine

        mine(
            project_dir=args.dir,
            palace_path=palace_path,
            wing_override=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            respect_gitignore=not args.no_gitignore,
            include_ignored=include_ignored,
        )


def cmd_search(args):
    from .searcher import search, search_memories, SearchError

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    path_format = getattr(args, "path_format", "full")
    try:
        if getattr(args, "format", "pretty") == "lines":
            # Lines format: output one result per line for hook consumption
            result = search_memories(
                query=args.query,
                palace_path=palace_path,
                wing=args.wing,
                room=args.room,
                n_results=args.results,
            )
            if "error" not in result:
                for hit in result.get("results", []):
                    text = hit.get("text", "")[:120]
                    wing_name = hit.get("wing", "?")
                    room_name = hit.get("room", "?")
                    print(f"[{wing_name}/{room_name}] {text}")
        elif path_format == "rel":
            # Pretty format with repo-relative paths
            result = search_memories(
                query=args.query,
                palace_path=palace_path,
                wing=args.wing,
                room=args.room,
                n_results=args.results,
            )
            if "error" in result:
                print(f"\n  Search error: {result['error']}")
                raise SearchError(result["error"])
            hits = result.get("results", [])
            if not hits:
                print(f'\n  No results found for: "{args.query}"')
                return
            print(f"\n{'=' * 60}")
            print(f'  Results for: "{args.query}"')
            if args.wing:
                print(f"  Wing: {args.wing}")
            if args.room:
                print(f"  Room: {args.room}")
            print(f"{'=' * 60}\n")
            for i, hit in enumerate(hits, 1):
                source = hit.get("repo_rel_path") or hit.get("source_file", "?")
                wing_name = hit.get("wing", "?")
                room_name = hit.get("room", "?")
                similarity = hit.get("similarity", 0)
                doc = hit.get("text", "")
                print(f"  [{i}] {wing_name} / {room_name}")
                print(f"      Source: {source}")
                print(f"      Match:  {similarity}")
                print()
                for line in doc.strip().split("\n"):
                    print(f"      {line}")
                print()
                print(f"  {'─' * 56}")
            print()
        else:
            search(
                query=args.query,
                palace_path=palace_path,
                wing=args.wing,
                room=args.room,
                n_results=args.results,
            )
    except SearchError:
        sys.exit(1)


def cmd_wakeup(args):
    """Show L0 (identity) + L1 (essential story) — the wake-up context."""
    from .layers import MemoryStack

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    stack = MemoryStack(palace_path=palace_path)

    text = stack.wake_up(wing=args.wing)
    tokens = len(text) // 4
    print(f"Wake-up text (~{tokens} tokens):")
    print("=" * 50)
    print(text)


def cmd_split(args):
    """Split concatenated transcript mega-files into per-session files."""
    from .split_mega_files import main as split_main
    import sys

    # Rebuild argv for split_mega_files argparse
    argv = ["--source", args.dir]
    if args.output_dir:
        argv += ["--output-dir", args.output_dir]
    if args.dry_run:
        argv.append("--dry-run")
    if args.min_sessions != 2:
        argv += ["--min-sessions", str(args.min_sessions)]

    old_argv = sys.argv
    sys.argv = ["mempalace split"] + argv
    try:
        split_main()
    finally:
        sys.argv = old_argv


def cmd_status(args):
    """Show full MemPalace status including memory pressure.

    Uses LanceDB as the canonical storage backend.
    """
    from .memory_guard import MemoryGuard, MemoryPressure
    from .config import MempalaceConfig
    from .backends import get_backend
    import psutil

    config = MempalaceConfig()
    palace_path = os.path.expanduser(args.palace) if args.palace else config.palace_path

    # ── Memory ───────────────────────────────────────────────────────────
    vm = psutil.virtual_memory()
    total_gib = vm.total / (1024**3)

    pressure = MemoryPressure.NOMINAL
    if hasattr(MemoryGuard, '_instance') and MemoryGuard._instance:
        guard = MemoryGuard.get()
        pressure = guard.pressure
        used_ratio = guard.used_ratio
    else:
        used_ratio = vm.percent / 100.0

    used_gib = used_ratio * total_gib
    used_pct = used_ratio * 100

    pressure_icon = {"nominal": "✅", "warn": "⚠️", "critical": "🚨"}
    icon = pressure_icon.get(pressure.value, "?")

    print("🏰 MemPalace Status")
    print()
    print(f"Memory: {used_pct:.0f}% used ({used_gib:.1f}GB / {total_gib:.0f}GB) {icon} {pressure.value.upper()}")

    # psutil: vm.swapped doesn't exist — use swap_memory() (sin=swap-in bytes, sout=swap-out bytes)
    swap = psutil.swap_memory()
    swap_mb = (swap.sin + swap.sout) / (1024**2)
    print(f"Swap: {swap_mb:.1f} MB")

    # ── Embedding Daemon ────────────────────────────────────────────────
    from .embed_daemon import get_socket_path
    import socket as _socket, json
    sock_path = get_socket_path()
    _daemon_running = False
    if os.path.exists(sock_path):
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(sock_path)
            payload = json.dumps({"texts": []}).encode("utf-8")
            s.sendall(len(payload).to_bytes(4, "big") + payload)
            resp = b""
            while len(resp) < 4:
                chunk = s.recv(4 - len(resp))
                if not chunk:
                    break
                resp += chunk
            _daemon_running = len(resp) == 4
            s.close()
        except Exception:
            pass
    if _daemon_running:
        pid_path = sock_path.replace(".sock", ".pid")
        try:
            pid = int(Path(pid_path).read_text())
            print(f"Embedding daemon: ✅ running (PID {pid})")
        except Exception:
            print("Embedding daemon: ✅ running")
    else:
        print("Embedding daemon: ❌ not running")

    # ── Palace ──────────────────────────────────────────────────────────
    # Canonical backend: Lance is primary storage.
    # Read from config unless --palace is explicitly passed.
    backend_type = config.backend  # from config.json / env
    if backend_type not in ("lance", "chroma"):
        backend_type = "lance"  # canonical default if config is weird

    # ChromaDB palaces must migrate first — no longer supported
    if backend_type == "chroma":
        print(f"Backend: chroma (LEGACY — no longer supported)")
        print(f"  ChromaDB backend has been removed. LanceDB is the only supported backend.")
        print(f"  If you have existing ChromaDB data, migrate it first:")
        print(f"    pip install chromadb")
        print(f"    python -m mempalace.migrate chroma-to-lance --palace {palace_path}")
        return

    if os.path.isdir(palace_path):
        try:
            backend = get_backend(backend_type)
            col = backend.get_collection(palace_path, config.collection_name, create=False)
            count = col.count()
            print(f"Backend: lance")
            print(f"Memories: {count:,}")
            try:
                from .symbol_index import SymbolIndex
                from .lexical_index import KeywordIndex
                si_stats = SymbolIndex.get(palace_path).stats()
                ki_count = KeywordIndex.get(palace_path).count()
                print(f"Symbol index: {si_stats.get('total_symbols', 0):,} symbols in {si_stats.get('total_files', 0):,} files")
                print(f"FTS5 index:   {ki_count:,} documents")
            except Exception:
                pass
            print(f"Last optimize: check logs")
        except FileNotFoundError:
            print(f"Palace not initialized at {palace_path}")
        except Exception as e:
            print(f"Backend: {backend_type} (error reading: {e})")
    else:
        print(f"Palace: not initialized at {palace_path}")


def cmd_repair(args):
    """Rebuild palace vector index from stored data.

    Uses LanceDB as the canonical backend.
    """
    import shutil
    from .backends import get_backend

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return

    cfg = MempalaceConfig()
    backend_type = cfg.backend
    if backend_type not in ("lance", "chroma"):
        backend_type = "lance"

    # ChromaDB is no longer supported
    if backend_type == "chroma":
        print(f"\n  ERROR: ChromaDB backend has been removed.")
        print("  LanceDB is the only supported backend.")
        print(f"  Palace at {palace_path} uses ChromaDB and cannot be repaired.")
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair (LanceDB)")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")
    print(f"  Backend: lance")

    # Try to read existing drawers via Lance backend
    try:
        backend = get_backend("lance")
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
        total = col.count()
        print(f"  Drawers found: {total}")
    except FileNotFoundError:
        print(f"  Palace not found at {palace_path} — may need to be re-mined.")
        return
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print("  Cannot recover — palace may need to be re-mined from source files.")
        return

    if total == 0:
        print("  Nothing to repair.")
        return

    # Extract all drawers in batches via Lance backend
    print("\n  Extracting drawers...")
    batch_size = 5000
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0
    while True:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        batch_ids = batch.get("ids", [])
        if not batch_ids:
            break
        all_ids.extend(batch_ids)
        all_docs.extend(batch.get("documents", []) or [])
        all_metas.extend(batch.get("metadatas", []) or [])
        offset += len(batch_ids)
        if len(batch_ids) < batch_size:
            break
    print(f"  Extracted {len(all_ids)} drawers")

    # Backup and rebuild via Lance backend
    palace_path = palace_path.rstrip(os.sep)
    backup_path = palace_path + ".backup"
    if os.path.exists(backup_path):
        shutil.rmtree(backup_path)
    print(f"  Backing up to {backup_path}...")
    shutil.copytree(palace_path, backup_path)

    print("  Rebuilding collection...")
    new_col = backend.get_collection(palace_path, cfg.collection_name, create=True)

    filed = 0
    for i in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[i : i + batch_size]
        batch_docs = all_docs[i : i + batch_size]
        batch_metas = all_metas[i : i + batch_size]
        new_col.add(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
        filed += len(batch_ids)
        print(f"  Re-filed {filed}/{len(all_ids)} drawers...")

    # Force keyword index rebuild after repair
    # KeywordIndex (SQLite FTS5) is the canonical lexical engine.
    try:
        from .diagnostics import rebuild_keyword_index
        result = rebuild_keyword_index(palace_path, batch_size=2000)
        print(f"  KeywordIndex rebuilt: {result['documents_indexed']} documents, {result['batches']} batches")
    except Exception:
        pass

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print(f"  Backup saved at {backup_path}")
    print(f"\n{'=' * 55}\n")


def cmd_hook(args):
    """Run hook logic: reads JSON from stdin, outputs JSON to stdout."""
    from .hooks_cli import run_hook

    run_hook(hook_name=args.hook, harness=args.harness, transport=args.transport)


def cmd_instructions(args):
    """Output skill instructions to stdout."""
    from .instructions_cli import run_instructions

    run_instructions(name=args.name)


def cmd_embed_daemon(args):
    """Manage the MemPalace embedding daemon (start/stop/status/benchmark)."""
    from .backends.lance import _daemon_is_running
    from .embed_daemon import get_socket_path

    sock_path = get_socket_path()

    if args.action == "benchmark":
        _run_embed_benchmark()
        return

    if args.action == "start":
        # Check if already running
        if _daemon_is_running():
            print(f"Embedding daemon is already running at {sock_path}")
            return

        print(f"Starting embedding daemon...")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "mempalace.embed_daemon"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

            timeout_s = float(os.environ.get(
                "MEMPALACE_EMBED_DAEMON_STARTUP_TIMEOUT",
                os.environ.get("MEMPALACE_EMBED_SOCKET_TIMEOUT", "120"),
            ))
            deadline = time.monotonic() + timeout_s
            ready = False
            while time.monotonic() < deadline:
                # use select so the readline loop cannot hang indefinitely
                # if the daemon emits nothing (pipe fills up and blocks writes)
                rready, _, _ = select.select([proc.stdout], [], [], 0.5)
                if rready:
                    line = proc.stdout.readline().decode("utf-8", errors="ignore").strip()
                    if line == "READY":
                        ready = True
                        break
                if proc.poll() is not None:
                    err = proc.stderr.read().decode("utf-8", errors="ignore")
                    print(f"Failed to start daemon: {err}")
                    return

            if ready:
                print(f"Embedding daemon started at {sock_path}")
            else:
                print(f"Daemon did not emit READY within {timeout_s:.0f}s — killing child process")
                try:
                    proc.kill()
                except Exception:
                    pass

        except Exception as e:
            print(f"Error starting daemon: {e}")

    elif args.action == "stop":
        from .embed_daemon import get_pid_path
        pid_path = get_pid_path()
        try:
            pid = int(Path(pid_path).read_text())
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon (PID {pid})")
        except FileNotFoundError:
            print("Daemon not running (no PID file)")
        except ProcessLookupError:
            print("Daemon not running (stale PID file)")
        except Exception as e:
            print(f"Error stopping daemon: {e}")

    elif args.action == "status":
        if _daemon_is_running():
            from .embed_daemon import get_pid_path
            pid_path = get_pid_path()
            try:
                pid = int(Path(pid_path).read_text())
                print(f"Embedding daemon is running (PID {pid}) at {sock_path}")
            except Exception:
                print(f"Embedding daemon is running at {sock_path}")
        else:
            print(f"Embedding daemon is not running (socket: {sock_path})")

    elif args.action == "doctor":
        from .embed_daemon import run_embed_doctor
        ok = run_embed_doctor()
        sys.exit(0 if ok else 1)


def _run_embed_benchmark():
    """Run 100 embeddings and measure CoreML vs CPU performance."""
    import time
    import psutil

    from .backends.lance import _embed_texts

    texts = [f"benchmark text {i}" for i in range(100)]

    # Detect provider via process CPU usage
    proc = psutil.Process()
    cpu_before = proc.cpu_percent(interval=0.1)

    start = time.perf_counter()
    embeddings = _embed_texts(texts)
    elapsed = time.perf_counter() - start

    cpu_after = proc.cpu_percent(interval=0.1)
    cpu_usage = max(cpu_before, cpu_after)

    # Simple heuristic: CoreML uses ANE (very low CPU%), CPU uses high CPU%
    provider = "CoreML (ANE/Metal)" if cpu_usage < 30 else "CPU"

    ms_per = (elapsed / 100) * 1000
    print(f"Provider: {provider}")
    print(f"100 embeddings: {elapsed:.2f}s total, {ms_per:.1f}ms per embedding")
    print(f"CPU usage during inference: {cpu_usage:.0f}%")


def cmd_serve(args):
    """Run MemPalace MCP server over HTTP — canonical streamable-http transport.

    Stdio mode (single-session / dev only):
        mcp = create_server(shared_server_mode=False)
        mcp.run()   # stdio transport — NO session coordinators in this mode
    Multi-session (Claude Code coordination): use --serve, which activates
    SessionRegistry, WriteCoordinator, ClaimsManager, HandoffManager, and
    DecisionTracker on a shared streamable-http server.

    Canonical transport: FastMCP streamable-http (NOT a custom Starlette wrapper).
    All sessions connect to the same /mcp endpoint on the shared server.
    """
    from .server.factory import create_server
    mcp = create_server(shared_server_mode=True)
    mcp.run(transport="streamable-http", host=args.host, port=args.port)


def cmd_optimize(args):
    """Force LanceDB compaction/optimization."""
    from .config import MempalaceConfig
    from .backends import get_backend

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    backend = get_backend("lance")
    col = backend.get_collection(palace_path, args.collection, create=False)
    try:
        col.run_optimize()
        print(f"LanceDB optimize completed for {args.collection} in {palace_path}")
    except FileNotFoundError:
        print(f"Palace not found: {palace_path}")
    except Exception as e:
        print(f"Optimize failed: {e}")


def cmd_cleanup(args):
    """Remove old non-latest memories and expired knowledge graph facts."""
    from datetime import datetime, timedelta
    from .config import MempalaceConfig
    from .backends import get_backend
    from .knowledge_graph import KnowledgeGraph

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    cfg = MempalaceConfig()
    cutoff = (datetime.utcnow() - timedelta(days=args.days)).isoformat() + "Z"
    kg_cutoff = (datetime.utcnow() - timedelta(days=args.kg_days)).isoformat()

    print(f"Cleanup cutoff: {cutoff} (drawers), {kg_cutoff} (KG)")

    # Memory-safe cleanup — process in batches of 5000 to prevent RAM spike.
    # Only deletes old (timestamp < cutoff) AND is_latest=False drawers.
    try:
        backend_type = cfg.backend
        if backend_type == "chroma":
            sys.stderr.write("Cleanup: ChromaDB backend is no longer supported.\n")
            sys.exit(1)
        backend = get_backend(backend_type)
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
        deleted = 0
        BATCH = 5000
        offset = 0
        while True:
            # Memory pressure check before each batch
            try:
                from .memory_guard import MemoryGuard, MemoryPressure
                guard = MemoryGuard.get()
                if guard.pressure == MemoryPressure.CRITICAL:
                    sys.stderr.write(f"Cleanup paused: memory pressure CRITICAL\n")
                    break
            except Exception:
                pass

            batch_data = col.get(include=["metadatas"], limit=BATCH, offset=offset)
            batch_ids = batch_data.get("ids", [])
            if not batch_ids:
                break

            metas = batch_data.get("metadatas", [])
            to_delete_batch = []
            for i, meta in enumerate(metas):
                ts = meta.get("timestamp", "")
                is_latest = meta.get("is_latest", True)
                if ts and ts < cutoff and not is_latest:
                    to_delete_batch.append(batch_ids[i])

            if to_delete_batch:
                if not args.dry_run:
                    col.delete(ids=to_delete_batch)
                deleted += len(to_delete_batch)
                # advance offset only by the number of DELETED items so the next
                # batch starts after the surviving records that followed them;
                # using a fixed BATCH here would cause later batches to be re-read
                # at the wrong position after deletions shift indices
                offset += len(to_delete_batch)
            else:
                offset += BATCH

            if len(batch_ids) < BATCH:
                break

        print(f"Cleanup: {deleted} drawers eligible for deletion")
    except Exception as e:
        import sys
        sys.stderr.write(f"Cleanup error: {e}\n")

    # KG cleanup — POUZE expired triples (valid_to IS NOT NULL) a staré
    try:
        kg_path = os.path.join(palace_path, "knowledge_graph.sqlite3")
        kg = KnowledgeGraph(db_path=kg_path)
        conn = kg._conn()
        rows = conn.execute(
            "SELECT id FROM triples WHERE valid_to IS NOT NULL AND valid_to < ? AND extracted_at < ?",
            (kg_cutoff, kg_cutoff),
        ).fetchall()
        kg_to_delete = [r["id"] for r in rows]
        print(f"KG: {len(kg_to_delete)} expired triples eligible for deletion")
        if not args.dry_run and kg_to_delete:
            for tid in kg_to_delete:
                conn.execute("DELETE FROM triples WHERE id = ?", (tid,))
            conn.commit()
            print(f"  Deleted {len(kg_to_delete)} triples")
    except Exception as e:
        import sys
        sys.stderr.write(f"KG cleanup error: {e}\n")

    if args.dry_run:
        print("DRY RUN — nothing was deleted.")


def _install_launchd_plist(
    label: str,
    program: list,
    env: dict = None,
    log_prefix: str = "mempalace",
) -> None:
    """Create and load a launchd plist for macOS background services."""
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return

    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    env_dict = ""
    if env:
        env_items = "\n".join(
            f"        <key>{k}</key>\n        <string>{v}</string>"
            for k, v in env.items()
        )
        env_dict = f"""
    <key>EnvironmentVariables</key>
    <dict>
{env_items}
    </dict>"""

    program_args = "\n".join(f"        <string>{p}</string>" for p in program)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/{log_prefix}.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{log_prefix}-err.log</string>{env_dict}
</dict>
</plist>"""

    plist_path.write_text(plist_content)

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True, capture_output=True)


def cmd_setup(args):
    """One-time MemPalace setup for production use on macOS."""
    import platform
    import subprocess

    print("MemPalace Setup\n")

    # 1. Config
    config = MempalaceConfig()
    config_file = config.init()
    print(f"Config: {config_file}")

    # 2. Pre-download fastembed model
    print("\nDownloading embedding model (~33MB)...")
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding(
            model_name="BAAI/bge-small-en-v1.5",
            cache_dir=os.path.expanduser("~/.cache/fastembed"),
        )
        list(model.embed(["warmup"]))
        print("Embedding model ready")
        del model
    except ImportError:
        print("fastembed not installed. Run: pip install 'mempalace[lance]'")

    # 3. launchd for embedding daemon (macOS only)
    if platform.system() == "Darwin":
        print("\nInstalling launchd services...")
        _install_launchd_plist(
            label="ai.mempalace.embed-daemon",
            program=[sys.executable, "-m", "mempalace.embed_daemon"],
            log_prefix="mempalace-embed",
        )
        print("Embedding daemon installed as launchd service")

        # 4. HTTP MCP server (opt-in)
        http_setup = input("\nInstall HTTP MCP server for shared multi-session access? [y/N]: ")
        if http_setup.lower() == "y":
            port = input("   Port (default 8765): ").strip() or "8765"
            _install_launchd_plist(
                label="ai.mempalace.mcp-server",
                program=[sys.executable, "-m", "mempalace", "serve", "--port", port],
                env={"MEMPALACE_TRANSPORT": "http"},
                log_prefix="mempalace-mcp",
            )
            print(f"HTTP MCP server installed on port {port}")
            print(f"""
Claude Code HTTP config (~/.claude/config.json):

{{
  "mcpServers": {{
    "mempalace": {{
      "type": "http",
      "url": "http://127.0.0.1:{port}/mcp"
    }}
  }}
}}
""")
        else:
            print("\nSkipping HTTP MCP server install.")
    print("\nlaunchd is macOS-only. On Linux, use systemd.")

    print("\nSetup complete! Restart terminal or run:")
    print("  launchctl start ai.mempalace.embed-daemon")


def cmd_mcp(args):
    """Show how to wire MemPalace into MCP-capable hosts."""
    base_server_cmd = "python -m mempalace.fastmcp_server"

    if args.palace:
        resolved_palace = str(Path(args.palace).expanduser())
        server_cmd = f"{base_server_cmd} --palace {shlex.quote(resolved_palace)}"
    else:
        server_cmd = base_server_cmd

    print("MemPalace MCP quick setup:")
    print(f"  claude mcp add mempalace -- {server_cmd}")
    print("\nRun the server directly:")
    print(f"  {server_cmd}")

    if not args.palace:
        print("\nOptional custom palace:")
        print(f"  claude mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print(f"  {base_server_cmd} --palace /path/to/palace")


def cmd_compress(args):
    """Compress drawers in a wing using AAAK Dialect.

    Uses LanceDB as the canonical backend.
    """
    from .dialect import Dialect
    from .backends import get_backend

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    # Load dialect (with optional entity config)
    config_path = args.config
    if not config_path:
        for candidate in ["entities.json", os.path.join(palace_path, "entities.json")]:
            if os.path.exists(candidate):
                config_path = candidate
                break

    if config_path and os.path.exists(config_path):
        dialect = Dialect.from_config(config_path)
        print(f"  Loaded entity config: {config_path}")
    else:
        dialect = Dialect()

    # LanceDB is the only supported backend
    cfg = MempalaceConfig()
    backend_type = cfg.backend
    if backend_type == "chroma":
        print(f"\n  ERROR: ChromaDB backend has been removed.")
        print(f"  LanceDB is the only supported backend.")
        sys.exit(1)
    if backend_type not in ("lance",):
        backend_type = "lance"  # canonical default

    try:
        backend = get_backend(backend_type)
        col = backend.get_collection(palace_path, cfg.collection_name, create=False)
        count = col.count()
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        sys.exit(1)

    print(f"  Using backend: lance")

    # Query drawers in batches to avoid SQLite variable limit (~999)
    where = {"wing": args.wing} if args.wing else None
    _BATCH = 500
    docs, metas, ids = [], [], []
    offset = 0
    while True:
        try:
            kwargs = {"include": ["documents", "metadatas"], "limit": _BATCH, "offset": offset}
            if where:
                kwargs["where"] = where
            batch = col.get(**kwargs)
        except Exception as e:
            if not docs:
                print(f"\n  Error reading drawers: {e}")
                sys.exit(1)
            break
        batch_docs = batch.get("documents", [])
        if not batch_docs:
            break
        docs.extend(batch_docs)
        metas.extend(batch.get("metadatas", []))
        ids.extend(batch.get("ids", []))
        offset += len(batch_docs)
        if len(batch_docs) < _BATCH:
            break

    if not docs:
        wing_label = f" in wing '{args.wing}'" if args.wing else ""
        print(f"\n  No drawers found{wing_label}.")
        return

    print(
        f"\n  Compressing {len(docs)} drawers"
        + (f" in wing '{args.wing}'" if args.wing else "")
        + "..."
    )
    print()

    total_original = 0
    total_compressed = 0
    compressed_entries = []

    for doc, meta, doc_id in zip(docs, metas, ids):
        compressed = dialect.compress(doc, metadata=meta)
        stats = dialect.compression_stats(doc, compressed)

        total_original += stats["original_chars"]
        total_compressed += stats["summary_chars"]

        compressed_entries.append((doc_id, compressed, meta, stats))

        if args.dry_run:
            wing_name = meta.get("wing", "?")
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "?")).name
            print(f"  [{wing_name}/{room_name}] {source}")
            print(f"    {stats['original_tokens_est']}t -> {stats['summary_tokens_est']}t ({stats['size_ratio']:.1f}x)")
            print(f"    {compressed}")
            print()

    # Store compressed versions (unless dry-run) — uses same backend
    if not args.dry_run:
        try:
            comp_col = backend.get_collection(palace_path, "mempalace_compressed", create=True)
            for doc_id, compressed, meta, stats in compressed_entries:
                comp_meta = dict(meta)
                comp_meta["compression_ratio"] = round(stats["size_ratio"], 1)
                comp_meta["original_tokens"] = stats["original_tokens_est"]
                comp_col.upsert(
                    ids=[doc_id],
                    documents=[compressed],
                    metadatas=[comp_meta],
                )
            print(
                f"  Stored {len(compressed_entries)} compressed drawers in 'mempalace_compressed' collection."
            )
        except Exception as e:
            print(f"  Error storing compressed drawers: {e}")
            sys.exit(1)

    # Summary
    ratio = total_original / max(total_compressed, 1)
    orig_tokens = Dialect.count_tokens("x" * total_original)
    comp_tokens = Dialect.count_tokens("x" * total_compressed)
    print(f"  Total: {orig_tokens:,}t -> {comp_tokens:,}t ({ratio:.1f}x compression)")
    if args.dry_run:
        print("  (dry run -- nothing stored)")


def cmd_diag(args):
    """Run all diagnostics on palace indexes and runtime state."""
    from .diagnostics import (
        validate_symbol_index,
        validate_keyword_index,
        validate_runtime_state,
        validate_skills_registration,
    )

    config = MempalaceConfig()
    palace_path = os.path.expanduser(args.palace) if args.palace else config.palace_path
    project_path = os.path.expanduser(args.project) if args.project else os.path.dirname(config.palace_path)

    skills_dir = os.path.join(os.path.dirname(__file__), "skills")

    print("MemPalace Diagnostics")
    print("=" * 50)
    print(f"Palace:  {palace_path}")
    print(f"Project: {project_path}")
    print()

    all_passed = True

    # Symbol index validation
    if os.path.isdir(project_path):
        print("[symbol_index]")
        try:
            result = validate_symbol_index(palace_path, project_path)
            stats = result.get("stats", {})
            orphaned = result.get("orphaned_files", [])
            missing = result.get("missing_from_index", [])

            print(f"  Total symbols: {stats.get('total_symbols', '?')}")
            print(f"  Total files:   {stats.get('total_files', '?')}")
            print(f"  Orphaned (in index, missing on disk): {len(orphaned)}")
            if orphaned:
                for fp in orphaned[:5]:
                    print(f"    - {fp}")
                if len(orphaned) > 5:
                    print(f"    ... and {len(orphaned) - 5} more")
            print(f"  Missing from index (on disk, not indexed): {len(missing)}")
            if missing:
                for fp in missing[:5]:
                    print(f"    - {fp}")
                if len(missing) > 5:
                    print(f"    ... and {len(missing) - 5} more")

            if orphaned or missing:
                all_passed = False
        except Exception as e:
            print(f"  ERROR: {e}")
            all_passed = False
        print()
    else:
        print("[symbol_index]  SKIPPED (project path not accessible)")
        print()

    # Keyword index validation
    print("[keyword_index / FTS5]")
    try:
        result = validate_keyword_index(palace_path)
        print(f"  FTS5 documents:  {result.get('fts5_count', '?')}")
        print(f"  LanceDB count:   {result.get('lance_count', '?')}")
        if result.get("counts_match"):
            print("  Status: ✅ counts match")
        else:
            mismatch = abs(result.get("fts5_count", 0) - result.get("lance_count", 0))
            print(f"  Status: ❌ MISMATCH (difference: {mismatch})")
            all_passed = False
        if result.get("sample_errors"):
            for err in result["sample_errors"]:
                print(f"  Sample check error: {err}")
    except Exception as e:
        print(f"  ERROR: {e}")
        all_passed = False
    print()

    # Runtime state
    print("[runtime_state]")
    try:
        result = validate_runtime_state(palace_path)
        cache_size = result.get("query_cache_size", 0)
        daemon = result.get("daemon_running", False)
        pressure = result.get("memory_pressure", "unknown")
        initialized = result.get("palace_initialized", False)

        print(f"  Palace initialized: {initialized}")
        print(f"  Query cache size:   {cache_size}")
        print(f"  Embedding daemon:  {'✅ running' if daemon else '❌ not running'}")
        print(f"  Memory pressure:    {pressure}")
    except Exception as e:
        print(f"  ERROR: {e}")
        all_passed = False
    print()

    # Skills validation
    print("[skills_registration]")
    try:
        result = validate_skills_registration(skills_dir)
        total = result.get("total_expected", 0)
        found = result.get("total_found", 0)
        missing = result.get("missing", [])
        empty = result.get("empty", [])

        print(f"  Skills: {found}/{total} found")
        if missing:
            print(f"  Missing: {', '.join(sorted(missing))}")
            all_passed = False
        if empty:
            print(f"  Empty files: {', '.join(sorted(empty))}")
            all_passed = False

        if not missing and not empty:
            print("  Status: ✅ all skills present")
    except Exception as e:
        print(f"  ERROR: {e}")
        all_passed = False
    print()

    print("=" * 50)
    if all_passed:
        print("Result: ✅ All diagnostics passed")
    else:
        print("Result: ❌ Issues found")

    if not all_passed:
        raise SystemExit(1)


def cmd_repair_index(args):
    """Rebuild symbol or keyword indexes."""
    from .diagnostics import rebuild_symbol_index, rebuild_keyword_index

    config = MempalaceConfig()
    palace_path = os.path.expanduser(args.palace) if args.palace else config.palace_path
    project_path = os.path.expanduser(args.project) if args.project else os.path.dirname(config.palace_path)

    rebuild_symbol = args.symbol or not (args.symbol or args.keyword)
    rebuild_keyword = args.keyword or not (args.symbol or args.keyword)

    print("MemPalace Index Repair")
    print("=" * 50)
    print(f"Palace:  {palace_path}")
    print(f"Project: {project_path}")
    print(f"Symbol index:  {'rebuild' if rebuild_symbol else 'skip'}")
    print(f"Keyword index: {'rebuild' if rebuild_keyword else 'skip'}")
    print()

    if not os.path.isdir(palace_path):
        print(f"  Palace not found at {palace_path}")
        raise SystemExit(1)

    if rebuild_symbol:
        if not os.path.isdir(project_path):
            print(f"  Project path not accessible: {project_path}")
            raise SystemExit(1)
        print("[symbol_index] Rebuilding...")
        try:
            result = rebuild_symbol_index(palace_path, project_path)
            backup = result.get("backup_path", "none")
            print(f"  ✅ Done: {result.get('symbols_indexed', 0)} symbols in {result.get('files_indexed', 0)} files")
            print(f"  Backup: {backup}")
        except Exception as e:
            print(f"  ❌ ERROR: {e}")
            all_passed = False
        print()

    if rebuild_keyword:
        print("[keyword_index] Rebuilding from LanceDB...")
        try:
            result = rebuild_keyword_index(palace_path)
            backup = result.get("backup_path", "none")
            docs = result.get("documents_indexed", 0)
            if "error" in result:
                print(f"  ❌ ERROR: {result['error']}")
            else:
                print(f"  ✅ Done: {docs} documents indexed in {result.get('batches', 0)} batches")
                print(f"  Backup: {backup}")
        except Exception as e:
            print(f"  ❌ ERROR: {e}")
        print()

    print("=" * 50)
    print("Repair complete.")


def main():
    parser = argparse.ArgumentParser(
        description="MemPalace — Give your AI a memory. No API key required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--palace",
        default=None,
        help="Where the palace lives (default: from ~/.mempalace/config.json or ~/.mempalace/palace)",
    )

    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Detect rooms from your folder structure")
    p_init.add_argument("dir", help="Project directory to set up")
    p_init.add_argument(
        "--yes", action="store_true", help="Auto-accept all detected entities (non-interactive)"
    )

    # mine
    p_mine = sub.add_parser("mine", help="Mine files into the palace")
    p_mine.add_argument("dir", help="Directory to mine")
    p_mine.add_argument(
        "--mode",
        choices=["projects", "convos"],
        default="projects",
        help="Ingest mode: 'projects' for code/docs (default), 'convos' for chat exports",
    )
    p_mine.add_argument("--wing", default=None, help="Wing name (default: directory name)")
    p_mine.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Don't respect .gitignore files when scanning project files",
    )
    p_mine.add_argument(
        "--include-ignored",
        action="append",
        default=[],
        help="Always scan these project-relative paths even if ignored; repeat or pass comma-separated paths",
    )
    p_mine.add_argument(
        "--agent",
        default="mempalace",
        help="Your name — recorded on every drawer (default: mempalace)",
    )
    p_mine.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    p_mine.add_argument(
        "--dry-run", action="store_true", help="Show what would be filed without filing"
    )
    p_mine.add_argument(
        "--extract",
        choices=["exchange", "general"],
        default="exchange",
        help="Extraction strategy for convos mode: 'exchange' (default) or 'general' (5 memory types)",
    )

    # search
    p_search = sub.add_parser("search", help="Find anything, exact words")
    p_search.add_argument("query", help="What to search for")
    p_search.add_argument("--wing", default=None, help="Limit to one project")
    p_search.add_argument("--room", default=None, help="Limit to one room")
    p_search.add_argument("--results", "--top-k", type=int, default=5, help="Number of results")
    p_search.add_argument(
        "--format", choices=["pretty", "lines"], default="pretty",
        help="Output format: pretty (default) or lines (one result per line)"
    )
    p_search.add_argument(
        "--path-format", choices=["full", "rel"], default="full",
        help="Path display: full shows absolute source_file, rel shows repo_rel_path (pretty format only)"
    )

    # compress
    p_compress = sub.add_parser(
        "compress", help="Compress drawers using AAAK Dialect (experimental, lossy)"
    )
    p_compress.add_argument("--wing", default=None, help="Wing to compress (default: all wings)")
    p_compress.add_argument(
        "--dry-run", action="store_true", help="Preview compression without storing"
    )
    p_compress.add_argument(
        "--config", default=None, help="Entity config JSON (e.g. entities.json)"
    )

    # wake-up
    p_wakeup = sub.add_parser("wake-up", help="Show L0 + L1 wake-up context (~170 tokens)")
    p_wakeup.add_argument("--wing", default=None, help="Wake-up for a specific project/wing")

    # split
    p_split = sub.add_parser(
        "split",
        help="Split concatenated transcript mega-files into per-session files (run before mine)",
    )
    p_split.add_argument("dir", help="Directory containing transcript files")
    p_split.add_argument(
        "--output-dir",
        default=None,
        help="Write split files here (default: same directory as source files)",
    )
    p_split.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be split without writing files",
    )
    p_split.add_argument(
        "--min-sessions",
        type=int,
        default=2,
        help="Only split files containing at least N sessions (default: 2)",
    )

    # hook
    p_hook = sub.add_parser(
        "hook",
        help="Run hook logic (reads JSON from stdin, outputs JSON to stdout)",
    )
    hook_sub = p_hook.add_subparsers(dest="hook_action")
    p_hook_run = hook_sub.add_parser("run", help="Execute a hook")
    p_hook_run.add_argument(
        "--hook",
        required=True,
        choices=["session-start", "stop", "precompact"],
        help="Hook name to run",
    )
    p_hook_run.add_argument(
        "--harness",
        required=True,
        choices=["claude-code", "codex"],
        help="Harness type (determines stdin JSON format)",
    )
    p_hook_run.add_argument(
        "--transport",
        required=False,
        choices=["cli", "http"],
        default="cli",
        help="Transport method: cli (subprocess) or http (MCP server). Default: cli",
    )

    # instructions
    p_instructions = sub.add_parser(
        "instructions",
        help="Output skill instructions to stdout",
    )
    instructions_sub = p_instructions.add_subparsers(dest="instructions_name")
    for instr_name in ["init", "search", "mine", "help", "status"]:
        instructions_sub.add_parser(instr_name, help=f"Output {instr_name} instructions")

    # repair
    sub.add_parser(
        "repair",
        help="Rebuild palace vector index from stored data (fixes segfaults after corruption)",
    )

    # mcp
    sub.add_parser(
        "mcp",
        help="Show MCP setup command for connecting MemPalace to your AI client",
    )

    # setup
    sub.add_parser(
        "setup",
        help="One-time MemPalace setup for production use (launchd services)",
    )

    # embed-daemon
    p_embed = sub.add_parser(
        "embed-daemon",
        help="Manage the MemPalace embedding daemon",
    )
    p_embed.add_argument(
        "action",
        choices=["start", "stop", "status", "benchmark", "doctor"],
        help="Action: start (background), stop, status check, benchmark, or doctor",
    )

    # serve (HTTP MCP server)
    p_serve = sub.add_parser(
        "serve",
        help="Run MemPalace MCP server over HTTP (canonical streamable-http transport)",
    )
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1)",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to listen on (default: 8765)",
    )

    # optimize
    p_opt = sub.add_parser(
        "optimize",
        help="Force LanceDB compaction/optimization",
    )
    p_opt.add_argument(
        "--palace",
        default=None,
        help="Palace path (default: from config)",
    )
    p_opt.add_argument(
        "--collection",
        default="mempalace_drawers",
        help="Collection name (default: mempalace_drawers)",
    )

    # cleanup
    p_cleanup = sub.add_parser(
        "cleanup",
        help="Remove old non-latest memories and expired knowledge graph facts",
    )
    p_cleanup.add_argument(
        "--days",
        type=int,
        default=90,
        help="Delete drawers older than N days (only non-latest)",
    )
    p_cleanup.add_argument(
        "--kg-days",
        type=int,
        default=30,
        help="Delete expired KG triples older than N days",
    )
    p_cleanup.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview only, no deletion",
    )

    # migrate
    p_migrate = sub.add_parser(
        "migrate",
        help="Migrate ChromaDB palace to LanceDB (chroma-to-lance only)",
    )
    p_migrate.add_argument(
        "direction",
        choices=["chroma-to-lance", "lance-to-chroma"],
        help="Migration direction (lance-to-chroma is no longer supported)",
    )
    p_migrate.add_argument(
        "--collection",
        default="mempalace_drawers",
        help="Collection name (default: mempalace_drawers)",
    )
    p_migrate.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Batch size for migration (default: 100)",
    )
    p_migrate.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

    # status
    sub.add_parser("status", help="Show what's been filed")

    # diag
    p_diag = sub.add_parser("diag", help="Run diagnostics on palace indexes and runtime state")
    p_diag.add_argument("--palace", default=None, help="Palace path (default: from config)")
    p_diag.add_argument("--project", default=None, help="Project path (default: palace parent)")

    # repair-index
    p_repair_index = sub.add_parser(
        "repair-index",
        help="Rebuild symbol or keyword indexes",
    )
    p_repair_index.add_argument("--palace", default=None, help="Palace path (default: from config)")
    p_repair_index.add_argument("--project", default=None, help="Project path (default: palace parent)")
    p_repair_index.add_argument(
        "--symbol", action="store_true", help="Rebuild symbol index only"
    )
    p_repair_index.add_argument(
        "--keyword", action="store_true", help="Rebuild keyword/FTS5 index only"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Handle two-level subcommands
    if args.command == "hook":
        if not getattr(args, "hook_action", None):
            p_hook.print_help()
            return
        cmd_hook(args)
        return

    if args.command == "instructions":
        name = getattr(args, "instructions_name", None)
        if not name:
            p_instructions.print_help()
            return
        args.name = name
        cmd_instructions(args)
        return

    dispatch = {
        "init": cmd_init,
        "mine": cmd_mine,
        "split": cmd_split,
        "search": cmd_search,
        "mcp": cmd_mcp,
        "compress": cmd_compress,
        "wake-up": cmd_wakeup,
        "repair": cmd_repair,
        "migrate": cmd_migrate,
        "status": cmd_status,
        "embed-daemon": cmd_embed_daemon,
        "serve": cmd_serve,
        "optimize": cmd_optimize,
        "cleanup": cmd_cleanup,
        "setup": cmd_setup,
        "diag": cmd_diag,
        "repair-index": cmd_repair_index,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
