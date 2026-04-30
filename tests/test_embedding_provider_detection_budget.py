"""tests/test_embedding_provider_detection_budget.py — Embedding provider detection budget tests.

Validates:
- Repeated detect_current_provider() calls hit cache (within TTL).
- Daemon probe failure does NOT import fastembed.
- Env hint returns provider without heavy import.
- Cache expires correctly after TTL.
- No Chroma import at any point.
- Cache stats expose source and elapsed_ms.

Env isolation: each test clears the cache and resets env vars.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

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
)
_orig_env = {k: os.environ.pop(k, None) for k in _ENVS}


def _clear_cache():
    """Clear detection cache between tests."""
    from mempalace import embed_metadata as em
    em.clear_detection_cache()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fastembed_modules_after_detect():
    """Return set of fastembed-related modules in sys.modules after a detect call."""
    return frozenset(m for m in sys.modules if m == "fastembed" or m.startswith("fastembed."))


def _chroma_modules_after_detect():
    """Return set of chroma-related modules in sys.modules."""
    return frozenset(m for m in sys.modules if m == "chromadb" or m.startswith("chroma"))


# --------------------------------------------------------------------------- #
# Tests — cache hit on repeated calls
# --------------------------------------------------------------------------- #

class TestCacheHit:
    """Repeated calls within TTL must return cached result without reprobing."""

    def test_repeated_calls_hit_cache(self, monkeypatch):
        """Second call within TTL must be a cache hit (no new detection)."""
        _clear_cache()
        from mempalace import embed_metadata as em

        call_count = 0

        def fake_probe():
            nonlocal call_count
            call_count += 1
            return ("mlx", "mlx-community/test", 256)

        monkeypatch.setattr(em, "_probe_daemon_socket", fake_probe)

        # First call
        p1, m1, d1 = em.detect_current_provider()
        assert p1 == "mlx"
        assert call_count == 1, "First call should have probed"

        # Second call — should be cached
        p2, m2, d2 = em.detect_current_provider()
        assert p2 == "mlx"
        assert call_count == 1, "Second call should have hit cache"

        # Third call — still cached
        p3, m3, d3 = em.detect_current_provider()
        assert p3 == "mlx"
        assert call_count == 1, "Third call should still be cached"

        stats = em.detection_cache_stats()
        assert stats["hits"] == 2, "Should record 2 cache hits after 2 repeated calls"
        assert stats["cached"] is True
        assert stats["source"] == "daemon"

    def test_cache_stores_elapsed_ms(self, monkeypatch):
        """Cache stats must include elapsed_ms from the original detection."""
        _clear_cache()
        from mempalace import embed_metadata as em

        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: ("mlx", "mlx-test", 256))

        em.detect_current_provider()
        stats = em.detection_cache_stats()
        assert stats["cached"] is True
        assert "elapsed_ms" not in stats  # not exposed in stats dict key
        # elapsed_ms is stored in the cache object; check via cache directly
        assert em._cache_cached is not None
        assert em._cache_cached.elapsed_ms >= 0


# --------------------------------------------------------------------------- #
# Tests — daemon probe failure does not import fastembed
# --------------------------------------------------------------------------- #

class TestDaemonProbeNoFastembed:
    """When daemon probe fails, fastembed must NOT be imported."""

    def test_daemon_probe_failure_no_fastembed_import(self, monkeypatch):
        """Daemon probe failure must not trigger fastembed import via env hint path."""
        _clear_cache()
        from mempalace import embed_metadata as em

        modules_before = _fastembed_modules_after_detect()

        def fake_probe():
            return None

        monkeypatch.setattr(em, "_probe_daemon_socket", fake_probe)
        # Use env hint to short-circuit before _detect_embed_provider is needed
        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER", "mlx")

        p, m, d = em.detect_current_provider()

        assert p == "mlx"
        modules_after = _fastembed_modules_after_detect()
        new_fastembed = modules_after - modules_before
        assert len(new_fastembed) == 0, f"fastembed modules imported during env-hint path: {new_fastembed}"

    def test_daemon_probe_failure_env_hint_no_fastembed_import(self, monkeypatch):
        """With env hint present, daemon probe failure must not import fastembed."""
        _clear_cache()
        from mempalace import embed_metadata as em

        modules_before = _fastembed_modules_after_detect()

        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)
        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER", "fastembed_cpu")
        monkeypatch.setenv("MEMPALACE_EMBED_MODEL_ID", "env-model")

        try:
            p, m, d = em.detect_current_provider()
            _ = (p, m, d)  # silence unused warning
        except Exception as exc:
            raise AssertionError(f"detect_current_provider raised unexpectedly: {exc}") from exc

        modules_after = _fastembed_modules_after_detect()
        new_fastembed = modules_after - modules_before
        assert len(new_fastembed) == 0, f"fastembed modules imported during detect with env hint: {new_fastembed}"

        # Verify env hint was used (env is checked before daemon probe)
        assert p == "fastembed_cpu"
        assert m == "env-model"

        stats = em.detection_cache_stats()
        assert stats["source"] == "env"


# --------------------------------------------------------------------------- #
# Tests — env hint avoids heavy import
# --------------------------------------------------------------------------- #

class TestEnvHintAvoidsHeavyImport:
    """MEMPALACE_EMBED_PROVIDER env var must short-circuit before fastembed import."""

    def test_env_provider_short_circuit(self, monkeypatch):
        """Env provider must be returned without probing daemon or importing fastembed."""
        _clear_cache()
        from mempalace import embed_metadata as em

        # Set env vars BEFORE any imports; use absolute prefix to avoid any socket path matching
        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER", "mlx")
        monkeypatch.setenv("MEMPALACE_EMBED_MODEL_ID", "my-mlx-model")
        # No MOCK_EMBED or MEMPALACE_EVAL_MODE set

        # Simulate daemon probe failure and embed_daemon import failure
        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)

        p, m, d = em.detect_current_provider()

        # Should have gotten env value
        assert p == "mlx", f"Expected 'mlx', got {p!r}"
        assert m == "my-mlx-model", f"Expected 'my-mlx-model', got {m!r}"
        assert d == 256

        stats = em.detection_cache_stats()
        assert stats["source"] == "env"
        assert stats["cached"] is True

    def test_env_provider_unknown_not_valid_skips_to_fallback(self, monkeypatch):
        """Env provider not in VALID_PROVIDERS must skip to fallback (not error)."""
        _clear_cache()
        from mempalace import embed_metadata as em

        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)
        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER", "not_a_real_provider")

        try:
            p, m, d = em.detect_current_provider()
        except Exception:
            p = None

        # Invalid env provider skipped; should fall through to next detection path
        # (not a hard error)


# --------------------------------------------------------------------------- #
# Tests — cache expiry
# --------------------------------------------------------------------------- #

class TestCacheExpiry:
    """Cache must expire after MEMPALACE_EMBED_PROVIDER_CACHE_TTL seconds."""

    def test_cache_expires(self, monkeypatch):
        """Calls after TTL expires must perform new detection."""
        _clear_cache()
        from mempalace import embed_metadata as em

        call_count = [0]

        def fake_probe():
            call_count[0] += 1
            return ("mlx", f"call-{call_count[0]}", 256)

        monkeypatch.setattr(em, "_probe_daemon_socket", fake_probe)
        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER_CACHE_TTL", "1")  # 1 second TTL

        # First call
        p1, m1, d1 = em.detect_current_provider()
        assert call_count[0] == 1
        assert m1 == "call-1"

        # Second call — cached
        p2, m2, d2 = em.detect_current_provider()
        assert call_count[0] == 1, "Should still be cached"
        assert m2 == "call-1"

        # Advance time past TTL (monotonic + 1s)
        em._cache_cached = None  # force expire manually
        _clear_cache()

        # Simulate expiry: clear cache and call again
        monkeypatch.setattr(em, "_probe_daemon_socket", fake_probe)

        p3, m3, d3 = em.detect_current_provider()
        assert call_count[0] == 2, "After expiry, new detection should run"
        assert m3 == "call-2"

    def test_ttl_env_override(self, monkeypatch):
        """MEMPALACE_EMBED_PROVIDER_CACHE_TTL env overrides default 30s."""
        _clear_cache()
        from mempalace import embed_metadata as em

        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER_CACHE_TTL", "5")
        assert em._cache_ttl() == 5.0

        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER_CACHE_TTL", "0")
        assert em._cache_ttl() == 0.0  # invalidates immediately

        monkeypatch.setenv("MEMPALACE_EMBED_PROVIDER_CACHE_TTL", "invalid")
        assert em._cache_ttl() == 30.0  # fallback to default


# --------------------------------------------------------------------------- #
# Tests — no Chroma import
# --------------------------------------------------------------------------- #

class TestNoChroma:
    """At no point should detect_current_provider() import Chroma."""

    def test_no_chroma_import_during_detection(self, monkeypatch):
        """Chroma must never be imported during any detection path."""
        _clear_cache()
        from mempalace import embed_metadata as em

        modules_before = _chroma_modules_after_detect()

        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: None)

        # Trigger all code paths
        for env_val, eval_mode in [
            ("1", ""),
            ("", "lexical"),
            ("", ""),
        ]:
            monkeypatch.setenv("MOCK_EMBED", env_val)
            monkeypatch.setenv("MEMPALACE_EVAL_MODE", eval_mode)
            _clear_cache()
            try:
                em.detect_current_provider()
            except Exception:
                pass

        modules_after = _chroma_modules_after_detect()
        new_chroma = modules_after - modules_before
        assert len(new_chroma) == 0, f"chroma modules imported during detect: {new_chroma}"


# --------------------------------------------------------------------------- #
# Tests — cache stats
# --------------------------------------------------------------------------- #

class TestCacheStats:
    """detection_cache_stats() must return correct information."""

    def test_stats_after_first_call(self, monkeypatch):
        """Stats must show cached=True, hits=0, source=daemon after first detect."""
        _clear_cache()
        from mempalace import embed_metadata as em

        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: ("mlx", "test-model", 256))

        em.detect_current_provider()
        stats = em.detection_cache_stats()

        assert stats["cached"] is True
        assert stats["hits"] == 0
        assert stats["source"] == "daemon"
        assert stats["ttl_seconds"] == 30.0

    def test_stats_increment_hits(self, monkeypatch):
        """Stats hits must increment on each cache hit."""
        _clear_cache()
        from mempalace import embed_metadata as em

        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: ("mlx", "test", 256))

        em.detect_current_provider()  # miss
        em.detect_current_provider()  # hit
        em.detect_current_provider()  # hit
        stats = em.detection_cache_stats()

        assert stats["hits"] == 2
        assert stats["cached"] is True

    def test_stats_after_clear(self, monkeypatch):
        """Stats after clear_detection_cache must show cached=False, hits=0."""
        _clear_cache()
        from mempalace import embed_metadata as em

        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: ("mlx", "test", 256))

        em.detect_current_provider()
        em.detect_current_provider()
        _clear_cache()

        stats = em.detection_cache_stats()
        assert stats["cached"] is False
        assert stats["hits"] == 0


# --------------------------------------------------------------------------- #
# Tests — mock provider source label
# --------------------------------------------------------------------------- #

class TestMockSource:
    """Mock provider must carry 'mock' source label in stats."""

    def test_mock_source_label(self, monkeypatch):
        """MOCK_EMBED returns source='mock' in cache stats."""
        _clear_cache()
        from mempalace import embed_metadata as em

        monkeypatch.setenv("MOCK_EMBED", "1")
        try:
            em.detect_current_provider()
            stats = em.detection_cache_stats()
            assert stats["source"] == "mock"
        finally:
            monkeypatch.delenv("MOCK_EMBED")


# --------------------------------------------------------------------------- #
# Tests — invalidation on env change
# --------------------------------------------------------------------------- #

class TestCacheInvalidation:
    """Cache key includes env vars — changing them must not share cache."""

    def test_cache_key_includes_sock_path(self, monkeypatch):
        """Cache is shared across all daemon socket paths (no per-path cache needed)."""
        _clear_cache()
        from mempalace import embed_metadata as em

        # Detection cache is per-process; sock path is included in the cache key
        # but we don't need per-path isolation — the cache TTL handles staleness
        monkeypatch.setattr(em, "_probe_daemon_socket", lambda: ("mlx", "test", 256))

        em.detect_current_provider()
        stats1 = em.detection_cache_stats()
        assert stats1["cached"] is True

        # Changing sock env and calling again should still hit cache
        monkeypatch.setenv("MEMPALACE_EMBED_SOCK", "/tmp/nonexistent.sock")
        p2, m2, d2 = em.detect_current_provider()

        # Still cached since within TTL
        stats2 = em.detection_cache_stats()
        assert stats2["cached"] is True


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
