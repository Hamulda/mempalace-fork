"""
Server factory: create_server() wires middleware, registers tools, returns FastMCP instance.

Backward-compatible entry point: mempalace.fastmcp_server.create_server().
"""
import argparse
import os
import sys
import logging
import threading
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.resources import DirectoryResource
from starlette.responses import JSONResponse

from ._infrastructure import wal_log_async, get_wal_path, make_status_cache
from ._search_tools import register_search_tools
from ._write_tools import register_write_tools
from ._kg_tools import register_kg_tools
from ._code_tools import register_code_tools
from ._session_tools import register_session_tools
from ._symbol_tools import register_symbol_tools

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("mempalace_mcp")


def create_server(settings=None, shared_server_mode=False):
    """
    Factory — vytvoří izolovanou FastMCP instanci.

    Použití v produkci: mcp = create_server()
    Použití v testech:  mcp = create_server(settings=test_settings)
    """
    from ..settings import MemPalaceSettings
    from ..config import MempalaceConfig
    from ..backends import get_backend
    from ..middleware import build_middleware_stack

    if settings is None:
        settings = MemPalaceSettings()

    # Canonical palace path (resolved from env or default, same chain as MempalaceConfig).
    # db_path may differ if MEMPALACE_DB_PATH override is set (backward compat), but
    # palace_path is what session managers and config_dir derive from — split-brain prevention.
    palace_path = settings.palace_path
    db_path = Path(settings.db_path)
    db_path.mkdir(parents=True, exist_ok=True)

    # config_dir is derived from palace_path, NOT from db_path (which may have a compat override).
    # This ensures MempalaceConfig.palace_path and session managers always agree with palace_path.
    config = MempalaceConfig(config_dir=str(Path(palace_path).parent))
    backend = get_backend(settings.db_backend)

    middleware_stack = build_middleware_stack(settings)

    server = FastMCP("MemPalace")
    for mw in middleware_stack:
        server.add_middleware(mw)

    # ── Status cache — per-server-instance ──────────────────────────────────
    server._status_cache = make_status_cache()

    # ── Session coordinators (shared_server_mode or HTTP transport) ──────
    if shared_server_mode or settings.transport == "http":
        from ..session_registry import SessionRegistry
        from ..write_coordinator import WriteCoordinator
        from ..claims_manager import ClaimsManager
        from ..handoff_manager import HandoffManager
        from ..decision_tracker import DecisionTracker

        # All coordinators use palace_path directly — single source of truth.
        # config.palace_path would also be correct (same resolution chain), but
        # using palace_path directly avoids any property lookup indirection.
        registry = SessionRegistry(palace_path)
        coordinator = WriteCoordinator(palace_path)
        claims_mgr = ClaimsManager(palace_path)
        handoff_mgr = HandoffManager(palace_path)
        decision_tracker = DecisionTracker(palace_path)

        setattr(server, "_session_registry", registry)
        setattr(server, "_write_coordinator", coordinator)
        setattr(server, "_claims_manager", claims_mgr)
        setattr(server, "_handoff_manager", handoff_mgr)
        setattr(server, "_decision_tracker", decision_tracker)

    # ── Health check ────────────────────────────────────────────────────────
    @server.custom_route("/health", methods=["GET"], name="health")
    async def health_check(request):
        from starlette.requests import Request
        return JSONResponse({"status": "ok", "service": "mempalace"})

    # ── Skills resource ─────────────────────────────────────────────────────
    try:
        skills_path = Path(__file__).parent.parent / "skills"
        if skills_path.exists() and any(skills_path.iterdir()):
            server.add_resource(DirectoryResource(
                name="palace_skills",
                title="MemPalace Skills",
                description="Guides for init, mine, search, status, and help commands",
                path=str(skills_path),
                pattern="*.md",
                uri="mempalace://skills/",
            ))
    except Exception:
        pass

    # ── MemoryGuard ────────────────────────────────────────────────────────
    memory_guard = None
    try:
        from ..memory_guard import MemoryGuard
        memory_guard = MemoryGuard.get()
    except (ImportError, Exception) as e:
        logger.debug("memory_guard unavailable: %s", e)

    # ── Register all tool groups ───────────────────────────────────────────
    register_search_tools(server, backend, config, settings, memory_guard)
    register_write_tools(server, backend, config, settings, memory_guard)
    register_kg_tools(server, backend, config, settings)
    register_code_tools(server, backend, config, settings)
    register_session_tools(server, backend, config, settings)
    register_symbol_tools(server, backend, config, settings)

    # ── Optional reranker warmup (disabled by default — saves ~90MB RAM + ~3s startup) ──
    if settings.reranker_warmup:
        def _warmup_reranker():
            try:
                from ..searcher import warmup_reranker
                warmup_reranker()
            except Exception:
                pass
        threading.Thread(target=_warmup_reranker, daemon=True, name="reranker_warmup").start()

    return server


def _register_all_tools(server, backend, config, settings, memory_guard):
    """Legacy — all tool registration now lives in register_*_tools() per module."""
    pass


# ─── CLI ──────────────────────────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(description="MemPalace FastMCP Server")
    parser.add_argument("--palace", metavar="PATH",
                      help="Path to the palace directory (overrides config file and env var)")
    args, unknown = parser.parse_known_args()
    if unknown:
        logger.debug("Ignoring unknown args: %s", unknown)
    return args


if __name__ == "__main__":
    _args = _parse_args()
    if _args.palace:
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(_args.palace)

    mcp = create_server()

    from ..settings import settings
    if settings.transport == "http":
        mcp.run(transport="streamable-http", host=settings.host, port=settings.port)
    else:
        mcp.run()
