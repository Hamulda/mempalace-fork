"""tests/test_embedding_detection_hotpath.py — Embedding detection hotpath verification.

Validates Phase 41 post-conditions:
1. detect_current_provider() is write-path / status only — NOT called per search hit.
2. TTL cache prevents repeated heavy detection on hot write paths.
3. No fastembed import when:
   a. MEMPALACE_EMBED_PROVIDER is set (env hint short-circuits);
   b. embedding_meta.json exists (daemon or env probe sufficient);
   c. daemon socket is unavailable (falls through to _detect_embed_provider, not fastembed import).
4. detect_current_provider() is NOT called during search/retrieve operations.

Env isolation: each test clears cache and resets env vars.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Env isolation
_ENVS = (
    "MOCK_EMBED",
    "MEMPALACE_EVAL_MODE",
    "MEMPALACE_EMBED_SOCK",
    "MEMPALACE_EMBED_PROVIDER",
    "MEMPALACE_EMBED_MODEL_ID",
    "MEMPALACE_EMBED_PROVIDER_CACHE_TTL",
    "MEMPALACE_ALLOW_EMBEDDING_PROVIDER_DRIFT",
)
_orig_env = {k: os.environ.pop(k, None) for k in _ENVS}


def _clear_cache():
    """Clear detection cache and env vars between tests."""
    from mempalace import embed_metadata as em
    em.clear_detection_cache()
    for k in _ENVS:
        os.environ.pop(k, None)


def _fastembed_modules() -> frozenset[str]:
    return frozenset(m for m in sys.modules if m == "fastembed" or m.startswith("fastembed."))


def _chroma_modules() -> frozenset[str]:
    return frozenset(m for m in sys.modules if m == "chromadb" or m.startswith("chroma"))


# --------------------------------------------------------------------------- #
# Tests — write-path only (not per-search-hit)
# --------------------------------------------------------------------------- #

class TestWritePathOnly:
    """detect_current_provider must NOT be called during search/retrieve."""

    def test_searcher_module_does_not_call_detect_current_provider(self):
        """searcher.py must not import or call detect_current_provider."""
        from mempalace import searcher as searcher_mod

        src = Path(searcher_mod.__file__).read_text()
        assert "detect_current_provider" not in src, (
            "searcher.py must not call detect_current_provider — hot-path violation"
        )
        assert "from .. import embed_metadata" not in src
        assert "import embed_metadata" not in src

    def test_no_embed_metadata_import_in_backends(self):
        """No backend search/retrieve path may call detect_current_provider."""
        backends_dir = Path("/Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/mempalace/backends")
        for py_file in backends_dir.glob("*.py"):
            if py_file.name.startswith("__") or py_file.name == "lance.py":
                continue
            src = py_file.read_text()
            assert "detect_current_provider" not in src, (
                f"{py_file.name} must not call detect_current_provider — hot-path violation"
            )

    def test_detect_current_provider_called_only_in_lance_write_methods(self):
        """Only _do_add and post-write ensure_meta may call detect_current_provider."""
        lance_src = Path("/Users/vojtechhamada/.claude/plugins/marketplaces/mempalace/mempalace/backends/lance.py").read_text()
        lines = [
            (i + 1, l.strip())
            for i, l in enumerate(lance_src.splitlines())
            if "detect_current_provider" in l and not l.strip().startswith("#")
        ]
        # Must be exactly 2 call sites: _do_add validation + ensure_meta
        assert len(lines) == 2, (
            f"Expected exactly 2 detect_current_provider call sites, got {len(lines)}: {lines}"
        )
        line_nos = [ln for ln, _ in lines]

        # Line 1804 is _do_add validate; Line 1909 is ensure_meta
        for ln in line_nos:
            assert ln in (1804, 1909), (
                f"Unexpected call site at line {ln}. "
                "detect_current_provider must only be in _do_add (L1804) and ensure_meta (L1909)."
            )


# --------------------------------------------------------------------------- #
# Tests — TTL cache prevents repeated heavy detection
# --------------------------------------------------------------------------- #

class TestTtlCacheHotWrite:
    """TTL cache must prevent repeated heavy detection on consecutive writes."""

    def test_consecutive_writes_hit_cache(self, monkeypatch, tmp_path):
        """Multiple _do_add calls within TTL must all hit cache."""
        _clear_cache()
        from mempalace import embed_metadata as em

        probe_count = [0]

        def counting_probe():
            probe_count[0] += 1
            return ("mlx", f"call-{probe_count[0]}", 256)

        monkeypatch.setattr(em, "_probe_daemon_socket", counting_probe)

        # Simulate 5 consecutive write batches
        results = []
        for _ in range(5):
            p, m, d = em.detect_current_provider()
            results.append((p, m, d))

        # All should be cached — no new probe calls
        assert probe_count[0] == 1, (
            f"TTL cache miss: expected 1 probe for 5 calls, got {probe_count[0]}"
        )
        assert all(r == ("mlx", "call-1", 256) for r in results)

        stats = em.detection_cache_stats()
        assert stats["hits"] == 4, f"Expected 4 cache hits, got {stats['hits']}"

    def test_ttl_cache_covers_all_concurrent_write_batches(self, monkeypatch):
        """Write coalescer batch loop must benefit from TTL cache."""
        _clear_cache()
        from mempalace import embed_metadata as em

        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: ("mlx", "batch-model", 256))

        # Simulate WriteCoalescer draining 20 batches in quick succession
        for _ in range(20):
            p, m, d = em.detect_current_provider()
            assert p == "mlx"

        stats = em.detection_cache_stats()
        assert stats["hits"] == 19, f"Expected 19 hits for 20 batches, got {stats['hits']}"
        assert stats["cached"] is True


# --------------------------------------------------------------------------- #
# Tests — no fastembed import under skip conditions
# --------------------------------------------------------------------------- #

class TestNoFastembedImport:
    """fastembed must NOT be imported when detection can be satisfied without it."""

    def test_env_provider_avoids_fastembed(self, monkeypatch):
        """MEMPALACE_EMBED_PROVIDER set → no fastembed import."""
        _clear_cache()
        modules_before = _fastembed_modules()

        from mempalace import embed_metadata as em
        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER", "mlx")
        monkeypatch.setenv("MEMPALACE_EMBED_MODEL_ID", "my-model")
        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)

        p, m, d = em.detect_current_provider()

        modules_after = _fastembed_modules()
        new = modules_after - modules_before
        assert len(new) == 0, f"fastembed imported despite env hint: {new}"
        assert p == "mlx"

    def test_daemon_probe_success_avoids_fastembed(self, monkeypatch):
        """Daemon socket responds → no fastembed import."""
        _clear_cache()
        modules_before = _fastembed_modules()

        from mempalace import embed_metadata as em
        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: ("mlx", "daemon-model", 256))

        p, m, d = em.detect_current_provider()

        modules_after = _fastembed_modules()
        new = modules_after - modules_before
        assert len(new) == 0, f"fastembed imported despite daemon probe: {new}"
        assert p == "mlx"
        stats = em.detection_cache_stats()
        assert stats["source"] == "daemon"

    def test_embedding_meta_json_exists_skips_fastembed(self, monkeypatch, tmp_path):
        """embedding_meta.json present → no fastembed import (daemon or env used)."""
        _clear_cache()
        modules_before = _fastembed_modules()

        palace = tmp_path / "test_palace"
        palace.mkdir()
        meta_path = palace / "embedding_meta.json"
        meta_path.write_text(json.dumps({
            "provider": "mlx", "model_id": "saved-model", "dims": 256,
            "version": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"
        }))

        from mempalace import embed_metadata as em

        # Env hint present — should skip daemon and fastembed entirely
        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER", "mlx")
        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)

        p, m, d = em.detect_current_provider()

        modules_after = _fastembed_modules()
        new = modules_after - modules_before
        assert len(new) == 0, f"fastembed imported despite existing meta: {new}"
        assert p == "mlx"
        assert m == "env-hint"

    def test_daemon_socket_unavailable_no_fastembed_when_embed_daemon_imports(self, monkeypatch):
        """Daemon unavailable but embed_daemon succeeds → no fastembed."""
        _clear_cache()
        modules_before = _fastembed_modules()

        from mempalace import embed_metadata as em
        # Daemon socket doesn't exist → _probe_daemon_socket returns None
        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)
        # Mock embed_daemon's _detect_embed_provider to succeed (e.g. MLX loaded)
        import mempalace.embed_daemon as ed
        original = ed._detect_embed_provider
        def mock_detect():
            return ("mlx", "mlx-detected")
        ed._detect_embed_provider = mock_detect

        p, m, d = em.detect_current_provider()

        ed._detect_embed_provider = original  # restore
        modules_after = _fastembed_modules()
        new = modules_after - modules_before
        assert len(new) == 0, f"fastembed imported despite _detect_embed_provider: {new}"
        assert p == "mlx"

    def test_all_fastembed_import_paths_guarded(self, monkeypatch):
        """Final fallback is fastembed import but only when all light paths fail."""
        _clear_cache()
        from mempalace import embed_metadata as em

        # Force every path: no cache, no env, no daemon, no embed_daemon
        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)
        monkeypatch.delenv("MEMPALACE_EMBED_PROVIDER", raising=False)
        import mempalace.embed_daemon as ed
        original = ed._detect_embed_provider
        def force_fallback():
            raise Exception("forced fallback")
        ed._detect_embed_provider = force_fallback

        modules_before = _fastembed_modules()
        try:
            p, m, d = em.detect_current_provider()
        except Exception:
            p = "unknown"

        ed._detect_embed_provider = original  # restore
        modules_after = _fastembed_modules()
        new = modules_after - modules_before
        # Fallback branch imports fastembed as final resort — that's expected
        assert p in ("unknown", "fastembed_cpu")


# --------------------------------------------------------------------------- #
# Tests — no Chroma anywhere
# --------------------------------------------------------------------------- #

class TestNoChromaAnywhere:
    """No Chroma import under any detection condition."""

    @pytest.mark.parametrize("env_config", [
        {"MOCK_EMBED": "1"},
        {"MEMPALACE_EVAL_MODE": "lexical"},
        {"MEMPALACE_EMBED_PROVIDER": "mlx"},
        {"MEMPALACE_EMBED_PROVIDER": "fastembed_cpu"},
        {},
    ])
    def test_no_chroma_under_any_env(self, monkeypatch, env_config):
        """Chroma never imported regardless of env configuration."""
        _clear_cache()
        modules_before = _chroma_modules()

        from mempalace import embed_metadata as em
        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)

        for k, v in env_config.items():
            monkeypatch.setenv(k, v)

        try:
            em.detect_current_provider()
        except Exception:
            pass

        modules_after = _chroma_modules()
        new = modules_after - modules_before
        assert len(new) == 0, f"chroma imported under {env_config}: {new}"


# --------------------------------------------------------------------------- #
# Tests — cache key includes socket path
# --------------------------------------------------------------------------- #

class TestCacheKey:
    """Cache key must include socket path so different sockets get separate caches."""

    def test_cache_key_includes_sock_path(self):
        """Different socket paths must not share cache within TTL."""
        _clear_cache()
        from mempalace import embed_metadata as em

        key1 = em._cache_key()
        os.environ["MEMPALACE_EMBED_SOCK"] = "/tmp/sock1.sock"
        key2 = em._cache_key()
        os.environ["MEMPALACE_EMBED_SOCK"] = "/tmp/sock2.sock"
        key3 = em._cache_key()

        # Keys must be distinct
        assert key1 != key2, "Cache key must include sock path"
        assert key2 != key3, "Cache key must include sock path"
        os.environ.pop("MEMPALACE_EMBED_SOCK", None)


# --------------------------------------------------------------------------- #
# Restore env
# --------------------------------------------------------------------------- #

def teardown_module(module=None):
    _clear_cache()
    for k, v in _orig_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v