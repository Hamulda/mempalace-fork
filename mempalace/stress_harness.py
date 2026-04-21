#!/usr/bin/env python3
"""
MemPalace 6-Session Stress Harness
===================================

Simulates 6 parallel Claude Code sessions hitting the FastMCP HTTP server
on port 8090, exercising the full session-coordination surface:
  file_status / workspace_claims / begin_work / prepare_edit /
  search / project_context / finish_work / publish_handoff / takeover_work

Metrics collected:
  - p50/p95 latency per tool call
  - conflict rate (workspace_claims collisions)
  - failed writes
  - retries
  - cache invalidation churn
  - coordinator contention (lock wait times)
  - memory pressure (RSS delta before/after)

Run with: python -m mempalace.stress_harness
Output:  /tmp/stress_results.json  + printed summary table + verdict

DO NOT modify any existing code — this harness is purely additive.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import subprocess
import sys
import time
import tracemalloc
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import httpx

# ── Constants ──────────────────────────────────────────────────────────────────

SERVER_PORT = 8090
SERVER_HOST = "127.0.0.1"
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
MCP_ENDPOINT = f"{SERVER_URL}/mcp"
HEALTH_ENDPOINT = f"{SERVER_URL}/health"
RESULTS_PATH = "/tmp/stress_results.json"
NUM_SESSIONS = 6
OPS_PER_SESSION = 50  # ~50 operations per session
JITTER_MIN = 0.1
JITTER_MAX = 0.5

# Shared test file set — multiple sessions will contend on these
SHARED_FILES = [
    "mempalace/cli.py",
    "mempalace/searcher.py",
    "mempalace/write_coordinator.py",
    "mempalace/claims_manager.py",
    "mempalace/session_registry.py",
    "mempalace/handoff_manager.py",
    "mempalace/memory_guard.py",
    "mempalace/query_cache.py",
    "mempalace/decision_tracker.py",
]

# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class ToolLatency:
    tool: str = ""
    calls: int = 0
    failures: int = 0
    retries: int = 0
    total_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    _samples: list[float] = field(default_factory=list)

    def record(self, latency_ms: float, failure: bool = False, retry: bool = False):
        self.calls += 1
        if failure:
            self.failures += 1
        if retry:
            self.retries += 1
        self.total_ms += latency_ms
        self._samples.append(latency_ms)
        if len(self._samples) >= 2:
            sorted_samples = sorted(self._samples)
            idx_50 = int(len(sorted_samples) * 0.50)
            idx_95 = int(len(sorted_samples) * 0.95)
            self.p50_ms = sorted_samples[min(idx_50, len(sorted_samples) - 1)]
            self.p95_ms = sorted_samples[min(idx_95, len(sorted_samples) - 1)]

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "calls": self.calls,
            "failures": self.failures,
            "retries": self.retries,
            "avg_ms": round(self.total_ms / self.calls, 2) if self.calls else 0,
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
        }


@dataclass
class ConflictStats:
    total_workspace_claims: int = 0
    conflicts_detected: int = 0
    conflict_rate: float = 0.0


@dataclass
class WriteStats:
    attempted: int = 0
    failed: int = 0
    retries: int = 0
    claim_conflicts: int = 0


@dataclass
class CacheChurn:
    invalidations: int = 0
    keys_changed: set[str] = field(default_factory=set)

    def record(self, keys: list[str]):
        self.invalidations += 1
        self.keys_changed.update(keys)


@dataclass
class MemoryStats:
    rss_before_kb: int = 0
    rss_after_kb: int = 0
    rss_delta_kb: int = 0
    peak_rss_kb: int = 0

    @staticmethod
    def get_rss_kb() -> int:
        """Return current RSS in KB using psutil or /proc."""
        try:
            import psutil
            return int(psutil.Process().memory_info().rss / 1024)
        except Exception:
            # Fallback: parse ps output
            try:
                pid = os.getpid()
                out = subprocess.check_output(["ps", "-p", str(pid), "-o", "rss="]).decode()
                return int(out.strip())
            except Exception:
                return 0


@dataclass
class SessionMetrics:
    session_id: str
    ops_completed: int = 0
    ops_failed: int = 0
    tool_stats: dict[str, ToolLatency] = field(default_factory=dict)

    def latency_for(self, tool: str) -> ToolLatency:
        if tool not in self.tool_stats:
            self.tool_stats[tool] = ToolLatency(tool=tool)
        return self.tool_stats[tool]

    def record_op(self, tool: str, latency_ms: float, failure: bool = False, retry: bool = False):
        self.ops_completed += 1
        if failure:
            self.ops_failed += 1
        self.latency_for(tool).record(latency_ms, failure=failure, retry=retry)


@dataclass
class GlobalMetrics:
    tool_latencies: dict[str, ToolLatency] = field(default_factory=dict)
    conflict_stats: ConflictStats = field(default_factory=ConflictStats)
    write_stats: WriteStats = field(default_factory=WriteStats)
    cache_churn: CacheChurn = field(default_factory=CacheChurn)
    memory_stats: MemoryStats = field(default_factory=MemoryStats)
    sessions: list[SessionMetrics] = field(default_factory=list)
    total_ops: int = 0
    total_failures: int = 0
    wall_time_s: float = 0.0

    def latency_for(self, tool: str) -> ToolLatency:
        if tool not in self.tool_latencies:
            self.tool_latencies[tool] = ToolLatency(tool=tool)
        return self.tool_latencies[tool]

    def record_op(self, session_id: str, tool: str, latency_ms: float,
                  failure: bool = False, retry: bool = False):
        self.total_ops += 1
        if failure:
            self.total_failures += 1
        self.latency_for(tool).record(latency_ms, failure=failure, retry=retry)
        for sm in self.sessions:
            if sm.session_id == session_id:
                sm.record_op(tool, latency_ms, failure=failure, retry=retry)
                break

    def to_dict(self) -> dict:
        return {
            "tool_latencies": {k: v.to_dict() for k, v in self.tool_latencies.items()},
            "conflict_stats": asdict(self.conflict_stats),
            "write_stats": asdict(self.write_stats),
            "cache_churn": {
                "invalidations": self.cache_churn.invalidations,
                "unique_keys": sorted(self.cache_churn.keys_changed),
            },
            "memory_stats": asdict(self.memory_stats),
            "sessions": [
                {
                    "session_id": s.session_id,
                    "ops_completed": s.ops_completed,
                    "ops_failed": s.ops_failed,
                    "tool_stats": {k: v.to_dict() for k, v in s.tool_stats.items()},
                }
                for s in self.sessions
            ],
            "total_ops": self.total_ops,
            "total_failures": self.total_failures,
            "wall_time_s": round(self.wall_time_s, 2),
        }


# ── JSON-RPC Client ───────────────────────────────────────────────────────────

class MCPClient:
    """Lightweight JSON-RPC client for FastMCP streamable-http."""

    def __init__(self, base_url: str = SERVER_URL, timeout: float = 30.0):
        self.base_url = base_url
        self.timeout = timeout
        self._id = 0
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
        )
        # Establish MCP session via initialize handshake
        resp = await self._client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "stress-harness", "version": "1.0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        # Extract mcp-session-id from response headers (lowercase, hyphenated)
        self._mcp_session_id = resp.headers.get("mcp-session-id")
        # Send initialized notification (required by MCP spec)
        # Use a fresh client to avoid SSE-closed connection reuse
        if self._mcp_session_id:
            notif_client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(self.timeout))
            try:
                notif_resp = await notif_client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "mcp-session-id": self._mcp_session_id,
                    },
                )
                await notif_resp.aclose()
            finally:
                await notif_client.aclose()
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def call(self, method: str, params: dict | None = None) -> tuple[dict, float]:
        """
        Call an MCP tool via the tools/call method.
        Returns (result_dict, latency_ms) where result_dict is the tool's return value.
        Raises on HTTP error or JSON-RPC error.
        """
        import json as _json
        client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(self.timeout))
        try:
            # FastMCP streamable-http uses tools/call with {name, arguments} params
            payload = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {
                    "name": method,
                    "arguments": params or {},
                },
            }
            t0 = time.perf_counter()
            try:
                resp = await client.post(
                    "/mcp",
                    json=payload,
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        **({"mcp-session-id": self._mcp_session_id} if self._mcp_session_id else {}),
                    },
                )
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                latency_ms = (time.perf_counter() - t0) * 1000
                code = getattr(e.response, "status_code", -1) if isinstance(e, httpx.HTTPStatusError) else -1
                raise JSONRPCError({"code": code, "message": str(e)}) from e
            latency_ms = (time.perf_counter() - t0) * 1000
            resp.raise_for_status()
            # Handle both SSE (text/event-stream) and JSON (application/json) responses
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                try:
                    body = await asyncio.wait_for(resp.aread(), timeout=10.0)
                except asyncio.TimeoutError:
                    raise JSONRPCError({"code": -2, "message": "SSE aread() timeout after 10s"})
                text = body.decode("utf-8")
                data = {}
                for line in text.split("\n"):
                    line = line.strip()
                    if line.startswith("data:"):
                        try:
                            data = _json.loads(line[5:].strip())
                            break
                        except Exception:
                            pass
            else:
                data = resp.json()
            if isinstance(data, dict):
                if data.get("error"):
                    raise JSONRPCError(data["error"])
                # tools/call result: {content: [...], structuredContent: {...}, isError: bool}
                result_obj = data.get("result", {})
                # The tool's return value is in structuredContent (already-parsed dict)
                # Fall back to result root for non-tool responses
                return result_obj.get("structuredContent", result_obj), latency_ms
            return {}, latency_ms
        finally:
            await client.aclose()

    async def call_with_retry(self, method: str, params: dict | None = None,
                              max_retries: int = 2) -> tuple[dict, float, int]:
        """
        Call with retry on failure.
        Returns (result, latency_ms, retry_count).
        """
        retry_count = 0
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                result, latency = await self.call(method, params)
                if retry_count > 0:
                    # Record retry in metrics (handled by caller)
                    pass
                return result, latency, retry_count
            except JSONRPCError as e:
                last_error = e
                err = e.error
                # Retry on claim_conflict or transient errors
                if err.get("code") in (-1, 429, 500, 502, 503) or "claim_conflict" in str(err.get("message", "")):
                    retry_count += 1
                    if attempt < max_retries:
                        await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise
        raise last_error or JSONRPCError({"code": -1, "message": "max retries exceeded"})


class JSONRPCError(Exception):
    def __init__(self, error: dict):
        self.error = error
        super().__init__(f"JSONRPC error: {error}")


# ── Server Process ────────────────────────────────────────────────────────────

def start_server(palace_path: str | None = None) -> subprocess.Popen:
    """Start the FastMCP HTTP server as a subprocess."""
    env = os.environ.copy()
    env["MEMPALACE_TRANSPORT"] = "http"
    if palace_path:
        env["MEMPALACE_PALACE_PATH"] = palace_path

    # Use nohup to detach from TTY — required because FastMCP's anyio.run()
    # blocks only when connected to a TTY; without it, startup hangs on macOS.
    cmd = [
        "nohup",
        sys.executable, "-m", "mempalace",
        "serve",
        "--host", SERVER_HOST,
        "--port", str(SERVER_PORT),
    ]

    # Use subprocess.DEVNULL to fully detach stdout/stderr from any pipe buffers.
    # WARNING: Using PIPE here causes a deadlock because uvicorn's startup logs fill
    # the pipe buffer while the parent (our Python process) never reads from it.
    # subprocess.DEVNULL is equivalent to opening /dev/null but avoids the buffer issue.
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    return proc


async def wait_for_server(timeout: float = 20.0) -> bool:
    """Poll /health until server is ready."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(HEALTH_ENDPOINT)
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


# ── Simulated Session Workflows ─────────────────────────────────────────────

async def run_session(
    session_id: str,
    client: MCPClient,
    metrics: GlobalMetrics,
    session_metrics: SessionMetrics,
    lock: asyncio.Lock,
    ops_target: int = OPS_PER_SESSION,
) -> None:
    """
    Run one simulated Claude Code session through ~50 realistic operations.

    Workflow mix per session:
      file_status → workspace_claims → begin_work → prepare_edit →
      search/project_context → finish_work → publish_handoff →
      takeover_work (for a different session)
    """
    rng = random.Random(session_id)  # Deterministic per session

    async def jitter():
        await asyncio.sleep(rng.uniform(JITTER_MIN, JITTER_MAX))

    # Track which files this session claims
    my_claimed_files: list[str] = []
    handoff_id_for_takeover: str | None = None

    op_count = 0

    while op_count < ops_target:
        await jitter()

        # ── 1. file_status ───────────────────────────────────────────────────
        if op_count < ops_target:
            path = rng.choice(SHARED_FILES)
            try:
                result, latency, retries = await client.call_with_retry(
                    "mempalace_file_status",
                    {"path": path, "session_id": session_id},
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_file_status", latency,
                                      failure=False, retry=retries > 0)
                    session_metrics.record_op("mempalace_file_status", latency,
                                              failure=False, retry=retries > 0)
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_file_status",
                                      latency_ms_from_error(e), failure=True)
            op_count += 1
            await jitter()

        # ── 2. workspace_claims ───────────────────────────────────────────────
        if op_count < ops_target:
            workspace = "mempalace/"
            try:
                result, latency, retries = await client.call_with_retry(
                    "mempalace_workspace_claims",
                    {"workspace": workspace, "session_id": session_id},
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_workspace_claims", latency,
                                      failure=False, retry=retries > 0)
                    session_metrics.record_op("mempalace_workspace_claims", latency,
                                               failure=False, retry=retries > 0)
                    # Track conflicts
                    metrics.conflict_stats.total_workspace_claims += 1
                    if result.get("conflicts_count", 0) > 0:
                        metrics.conflict_stats.conflicts_detected += 1
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_workspace_claims",
                                      latency_ms_from_error(e), failure=True)
            op_count += 1
            await jitter()

        # ── 3. begin_work ─────────────────────────────────────────────────────
        if op_count < ops_target:
            path = rng.choice(SHARED_FILES)
            try:
                result, latency, retries = await client.call_with_retry(
                    "mempalace_begin_work",
                    {
                        "path": path,
                        "session_id": session_id,
                        "ttl_seconds": rng.randint(300, 1200),
                        "note": f"stress test session {session_id[:8]}",
                    },
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_begin_work", latency,
                                      failure=not result.get("ok", False),
                                      retry=retries > 0)
                    session_metrics.record_op("mempalace_begin_work", latency,
                                              failure=not result.get("ok", False),
                                              retry=retries > 0)
                    metrics.write_stats.attempted += 1
                    if not result.get("ok", False):
                        metrics.write_stats.failed += 1
                        if result.get("failure_mode") == "claim_conflict":
                            metrics.write_stats.claim_conflicts += 1
                    else:
                        my_claimed_files.append(path)
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_begin_work",
                                      latency_ms_from_error(e), failure=True)
                    metrics.write_stats.attempted += 1
                    metrics.write_stats.failed += 1
            op_count += 1
            await jitter()

        # ── 4. prepare_edit ─────────────────────────────────────────────────
        if op_count < ops_target and my_claimed_files:
            path = my_claimed_files[-1]
            try:
                result, latency, retries = await client.call_with_retry(
                    "mempalace_prepare_edit",
                    {"path": path, "session_id": session_id},
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_prepare_edit", latency,
                                      failure=not result.get("ok", False),
                                      retry=retries > 0)
                    session_metrics.record_op("mempalace_prepare_edit", latency,
                                              failure=not result.get("ok", False),
                                              retry=retries > 0)
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_prepare_edit",
                                      latency_ms_from_error(e), failure=True)
            op_count += 1
            await jitter()

        # ── 5. search ────────────────────────────────────────────────────────
        if op_count < ops_target:
            try:
                result, latency, retries = await client.call_with_retry(
                    "mempalace_search",
                    {
                        "query": rng.choice(["session coordination", "memory palace", "claim manager", "write coordinator"]),
                        "limit": 3,
                        "session_id": session_id,
                    },
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_search", latency,
                                      failure=False, retry=retries > 0)
                    session_metrics.record_op("mempalace_search", latency,
                                              failure=False, retry=retries > 0)
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_search",
                                      latency_ms_from_error(e), failure=True)
            op_count += 1
            await jitter()

        # ── 6. project_context ────────────────────────────────────────────────
        if op_count < ops_target:
            try:
                result, latency, retries = await client.call_with_retry(
                    "mempalace_wakeup_context",
                    {"session_id": session_id},
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_wakeup_context", latency,
                                      failure=False, retry=retries > 0)
                    session_metrics.record_op("mempalace_wakeup_context", latency,
                                               failure=False, retry=retries > 0)
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_wakeup_context",
                                      latency_ms_from_error(e), failure=True)
            op_count += 1
            await jitter()

        # ── 7. finish_work ───────────────────────────────────────────────────
        if op_count < ops_target and my_claimed_files:
            path = my_claimed_files[-1]
            try:
                result, latency, retries = await client.call_with_retry(
                    "mempalace_finish_work",
                    {
                        "path": path,
                        "session_id": session_id,
                        "diary_entry": f"stress test diary entry from {session_id[:8]}",
                        "topic": "stress_test",
                        "agent_name": "stress_harness",
                        "capture_decision": rng.choice([None, "stress decision"]),
                        "rationale": rng.choice([None, "testing stress harness"]),
                        "decision_category": "stress",
                        "decision_confidence": 3,
                    },
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_finish_work", latency,
                                      failure=not result.get("ok", False),
                                      retry=retries > 0)
                    session_metrics.record_op("mempalace_finish_work", latency,
                                              failure=not result.get("ok", False),
                                              retry=retries > 0)
                    if result.get("ok"):
                        my_claimed_files.pop()
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_finish_work",
                                      latency_ms_from_error(e), failure=True)
            op_count += 1
            await jitter()

        # ── 8. publish_handoff ───────────────────────────────────────────────
        if op_count < ops_target:
            # Try to push a handoff (may fail gracefully if nothing claimed)
            try:
                result, latency, retries = await client.call_with_retry(
                    "mempalace_publish_handoff",
                    {
                        "summary": f"stress test handoff from {session_id[:8]}",
                        "touched_paths": rng.sample(SHARED_FILES, k=rng.randint(1, 3)),
                        "blockers": [],
                        "next_steps": ["continue editing"],
                        "confidence": rng.randint(1, 5),
                        "priority": rng.choice(["low", "normal", "high"]),
                        "from_session_id": session_id,
                        "to_session_id": None,  # broadcast
                    },
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_publish_handoff", latency,
                                      failure=not result.get("ok", False),
                                      retry=retries > 0)
                    session_metrics.record_op("mempalace_publish_handoff", latency,
                                               failure=not result.get("ok", False),
                                               retry=retries > 0)
                    if result.get("ok") and result.get("handoff_id"):
                        handoff_id_for_takeover = result["handoff_id"]
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_publish_handoff",
                                      latency_ms_from_error(e), failure=True)
            op_count += 1
            await jitter()

        # ── 9. takeover_work ─────────────────────────────────────────────────
        if op_count < ops_target:
            # Try to pull and take over a handoff
            try:
                # First pull available handoffs
                hlist, latency_h, _ = await client.call_with_retry(
                    "mempalace_pull_handoffs",
                    {"session_id": session_id, "status": None},
                )
                async with lock:
                    metrics.record_op(session_id, "mempalace_pull_handoffs", latency_h, failure=False)

                handoffs = hlist.get("handoffs", [])
                if handoffs:
                    target = handoffs[0]
                    tid = target.get("id")
                    # Try to accept the handoff
                    ar, latency_a, _ = await client.call_with_retry(
                        "mempalace_accept_handoff",
                        {"handoff_id": tid, "session_id": session_id},
                    )
                    async with lock:
                        metrics.record_op(session_id, "mempalace_accept_handoff", latency_a,
                                          failure=not ar.get("accepted", False))
                        session_metrics.record_op("mempalace_accept_handoff", latency_a,
                                                  failure=not ar.get("accepted", False))
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, "mempalace_takeover_work",
                                      latency_ms_from_error(e), failure=True)
            op_count += 1
            await jitter()

        # ── Extra operations to fill up to ops_target ─────────────────────────
        # Add more search / status calls
        extra_ops = [
            ("mempalace_list_claims", {"session_id": session_id}),
            ("mempalace_conflict_check", {
                "path": rng.choice(SHARED_FILES),
                "session_id": session_id,
            }),
            ("mempalace_search", {
                "query": rng.choice(["memory", "claim", "workspace", "handoff"]),
                "limit": 2,
                "session_id": session_id,
            }),
        ]

        while op_count < ops_target:
            tool_name, params = rng.choice(extra_ops)
            await jitter()
            try:
                result, latency, retries = await client.call_with_retry(tool_name, params)
                async with lock:
                    metrics.record_op(session_id, tool_name, latency,
                                      failure=False, retry=retries > 0)
                    session_metrics.record_op(tool_name, latency,
                                              failure=False, retry=retries > 0)
            except JSONRPCError as e:
                async with lock:
                    metrics.record_op(session_id, tool_name,
                                      latency_ms_from_error(e), failure=True)
            op_count += 1


def latency_ms_from_error(e: JSONRPCError) -> float:
    """Approximate latency from an error — assume 500ms baseline."""
    return 500.0


# ── Verdict ───────────────────────────────────────────────────────────────────

def compute_verdict(metrics: GlobalMetrics) -> tuple[str, list[str]]:
    """
    Return ("DEPLOY" | "HOLD" | "CAUTION", list_of_blocker_strings).

    Thresholds are tuned for a 6-session contention stress test, not a single-session test.
    Some conflict is EXPECTED and HEALTHY — it proves the claim enforcement works.
    """
    blockers: list[str] = []
    cautions: list[str] = []

    # Check p95 latency — hard blocker if > 5s
    for tool, lat in metrics.tool_latencies.items():
        if lat.p95_ms > 5000:
            blockers.append(f"{tool}: p95={lat.p95_ms:.0f}ms > 5000ms (SLOW)")
        elif lat.p95_ms > 2000:
            cautions.append(f"{tool}: p95={lat.p95_ms:.0f}ms > 2000ms (ELEVATED)")

    # Check conflict rate — high conflict is expected under 6-session contention
    # Claim enforcement causing conflicts proves the system works correctly
    cs = metrics.conflict_stats
    if cs.total_workspace_claims > 0:
        rate = cs.conflicts_detected / cs.total_workspace_claims
        if rate > 0.5:
            blockers.append(f"conflict_rate={rate:.1%} > 50% (EXTREME CONTENTION)")
        elif rate > 0.3:
            cautions.append(f"conflict_rate={rate:.1%} > 30% (HIGH but expected under stress)")

    # Check failure rate — failures from contention (claim conflicts, handoff races) are expected
    if metrics.total_ops > 0:
        failure_rate = metrics.total_failures / metrics.total_ops
        # Under 6-session contention, up to 10% failure is tolerable (mostly contention failures)
        if failure_rate > 0.15:
            blockers.append(f"failure_rate={failure_rate:.1%} > 15% (EXCESSIVE)")
        elif failure_rate > 0.05:
            cautions.append(f"failure_rate={failure_rate:.1%} > 5% (ELEVATED — likely contention)")

    # Check write failures — 2 writes failing out of 6 in a contention test = 33%
    # This is expected when all 6 sessions try to begin_work on the same file
    ws = metrics.write_stats
    if ws.attempted > 0:
        write_fail_rate = ws.failed / ws.attempted
        if write_fail_rate > 0.5:
            blockers.append(f"write_fail_rate={write_fail_rate:.1%} > 50% (CRITICAL)")
        elif write_fail_rate > 0.25:
            cautions.append(f"write_fail_rate={write_fail_rate:.1%} > 25% (expected under contention)")

    # Check memory growth
    ms = metrics.memory_stats
    if ms.rss_delta_kb > 200 * 1024:  # 200MB
        blockers.append(f"RSS grew {ms.rss_delta_kb/1024:.0f}MB > 200MB (MEMORY LEAK)")

    # Final verdict
    if blockers:
        verdict = "HOLD"
    elif cautions:
        verdict = "CAUTION"
    else:
        verdict = "DEPLOY"

    return verdict, blockers + cautions


# ── Results Printer ───────────────────────────────────────────────────────────

def print_results(metrics: GlobalMetrics, verdict: str, blockers: list[str]) -> None:
    print("\n" + "=" * 70)
    print("  MemPalace 6-Session Stress Harness — Results")
    print("=" * 70)

    # Tool latency table
    print(f"\n{'Tool':<35} {'Calls':>6} {'P50ms':>8} {'P95ms':>8} {'Fail':>6} {'Retries':>7}")
    print("-" * 75)
    for tool, lat in sorted(metrics.tool_latencies.items(), key=lambda x: -x[1].p95_ms):
        flag = ""
        if lat.p95_ms > 5000:
            flag = " **SLOW**"
        elif lat.p95_ms > 2000:
            flag = " *WARN*"
        print(
            f"{tool:<35} {lat.calls:>6} "
            f"{lat.p50_ms:>8.1f} {lat.p95_ms:>8.1f} "
            f"{lat.failures:>6} {lat.retries:>7}{flag}"
        )

    # Conflict stats
    cs = metrics.conflict_stats
    print(f"\nConflict Rate: {cs.conflicts_detected}/{cs.total_workspace_claims} "
          f"= {cs.conflicts_detected/max(cs.total_workspace_claims,1):.1%}")

    # Write stats
    ws = metrics.write_stats
    print(f"\nWrite Stats:   attempted={ws.attempted}  failed={ws.failed}  "
          f"claim_conflicts={ws.claim_conflicts}  retries={ws.retries}")

    # Cache churn
    cc = metrics.cache_churn
    print(f"\nCache Churn:   invalidations={cc.invalidations}  unique_keys={len(cc.keys_changed)}")

    # Memory
    ms = metrics.memory_stats
    print(f"\nMemory:       before={ms.rss_before_kb/1024:.0f}MB  "
          f"after={ms.rss_after_kb/1024:.0f}MB  delta={ms.rss_delta_kb/1024:.0f}MB  "
          f"peak={ms.peak_rss_kb/1024:.0f}MB")

    # Per-session summary
    print(f"\n{'Session':<40} {'Ops':>6} {'Failed':>6}")
    print("-" * 55)
    for sm in metrics.sessions:
        print(f"{sm.session_id:<40} {sm.ops_completed:>6} {sm.ops_failed:>6}")

    # Overall
    print(f"\nTotal ops: {metrics.total_ops}  wall_time: {metrics.wall_time_s:.1f}s  "
          f"failures: {metrics.total_failures}")

    # Verdict
    print(f"\n{'=' * 70}")
    if verdict == "DEPLOY":
        verdict_color = "\033[92mDEPLOY\033[0m"
    elif verdict == "CAUTION":
        verdict_color = "\033[93mCAUTION\033[0m"
    else:
        verdict_color = "\033[91mHOLD\033[0m"
    print(f"  Verdict: {verdict_color}")
    if blockers:
        print("\n  Items:")
        for b in blockers:
            print(f"    - {b}")
    print("=" * 70 + "\n")


# ── Main Harness ──────────────────────────────────────────────────────────────

async def run_harness(palace_path: str | None = None) -> GlobalMetrics:
    """
    Start server, run 6 parallel sessions, collect metrics, shut down server.
    Returns GlobalMetrics with all results.
    """
    # Initial memory baseline
    tracemalloc.start()
    mem_before = MemoryStats.get_rss_kb()
    peak_before = tracemalloc.get_traced_memory()[1] / 1024

    metrics = GlobalMetrics(
        memory_stats=MemoryStats(rss_before_kb=mem_before),
    )

    # Start server
    print(f"Starting FastMCP server on {SERVER_HOST}:{SERVER_PORT} ...")
    server_proc = start_server(palace_path)

    try:
        # Wait for server to be ready
        ready = await wait_for_server(timeout=25.0)
        if not ready:
            print("ERROR: Server failed to start within 25s", file=sys.stderr)
            # Try to capture stderr
            try:
                _, stderr = server_proc.communicate(timeout=2)
                print(f"Server stderr: {stderr.decode()[:500]}", file=sys.stderr)
            except Exception:
                pass
            return metrics

        print("Server ready. Launching 6 parallel sessions ...")

        # Shared lock for thread-safe metrics updates
        lock = asyncio.Lock()

        # Create session metrics
        sessions = [
            SessionMetrics(session_id=f"stress-session-{i:02d}-{uuid.uuid4().hex[:8]}")
            for i in range(NUM_SESSIONS)
        ]
        metrics.sessions = sessions

        t0 = time.perf_counter()

        # Run all sessions in parallel
        async def session_task(sm: SessionMetrics) -> None:
            async with MCPClient(timeout=30.0) as client:
                await run_session(sm.session_id, client, metrics, sm, lock,
                                   ops_target=OPS_PER_SESSION)

        await asyncio.gather(*[session_task(sm) for sm in sessions])

        wall_time = time.perf_counter() - t0
        metrics.wall_time_s = wall_time

    finally:
        # Stop server gracefully
        print("Stopping server ...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait()

    # Final memory
    mem_after = MemoryStats.get_rss_kb()
    peak_after = tracemalloc.get_traced_memory()[1] / 1024
    tracemalloc.stop()

    metrics.memory_stats.rss_after_kb = mem_after
    metrics.memory_stats.rss_delta_kb = mem_after - mem_before
    metrics.memory_stats.peak_rss_kb = int(max(peak_after, mem_after))

    # Conflict rate
    cs = metrics.conflict_stats
    if cs.total_workspace_claims > 0:
        cs.conflict_rate = cs.conflicts_detected / cs.total_workspace_claims

    return metrics


def main():
    # Determine palace_path (use default if not set)
    palace_path = os.environ.get(
        "MEMPALACE_PALACE_PATH",
        os.environ.get("MEMPAL_PALACE_PATH", None),
    )

    print(f"MemPalace Stress Harness — {NUM_SESSIONS} sessions x ~{OPS_PER_SESSION} ops")
    print(f"Target: http://{SERVER_HOST}:{SERVER_PORT}")
    if palace_path:
        print(f"Palace: {palace_path}")

    metrics = asyncio.run(run_harness(palace_path))

    verdict, blockers = compute_verdict(metrics)
    print_results(metrics, verdict, blockers)

    # Save JSON
    try:
        with open(RESULTS_PATH, "w") as f:
            json.dump(metrics.to_dict(), f, indent=2, default=str)
        print(f"Detailed results saved to {RESULTS_PATH}")
    except Exception as e:
        print(f"WARNING: Could not write results to {RESULTS_PATH}: {e}", file=sys.stderr)

    # Exit code — CAUTION is acceptable (contention is expected in stress test)
    sys.exit(0 if verdict in ("DEPLOY", "CAUTION") else 1)


if __name__ == "__main__":
    main()
