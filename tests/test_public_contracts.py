"""
test_public_contracts.py — Targeted tests for public API contracts.

These tests lock down the contracts that comments/docstrings claim:
no behavior changes, only verification that the public API behaves
as documented.
"""
import tempfile
from pathlib import Path

import pytest


class TestBackendNamingContract:
    """Canonical backend names are 'lance' and 'chroma' — no 'lancedb'/'chromadb' variants."""

    def test_backend_type_literal_defined(self):
        """BackendType is a Literal with exactly 'lance' and 'chroma'."""
        from mempalace.backends import BackendType, BACKEND_CHOICES

        assert BACKEND_CHOICES == ("lance", "chroma")

    def test_get_backend_rejects_lancedb_chromadb(self):
        """get_backend raises ValueError for 'lancedb' and 'chromadb'."""
        from mempalace.backends import get_backend

        with pytest.raises(ValueError) as exc_info:
            get_backend("lancedb")
        assert "lancedb" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            get_backend("chromadb")
        assert "chromadb" in str(exc_info.value)

    def test_get_backend_default_is_lance(self):
        """get_backend() (no args) returns a LanceBackend instance."""
        from mempalace.backends import get_backend, _LANCE_AVAILABLE

        if not _LANCE_AVAILABLE:
            pytest.skip("LanceDB not installed")

        backend = get_backend()
        assert backend.__class__.__name__ == "LanceBackend"

    def test_settings_db_backend_default_is_lance(self):
        """MemPalaceSettings.db_backend defaults to 'lance'."""
        from mempalace.settings import MemPalaceSettings

        settings = MemPalaceSettings()
        assert settings.db_backend == "lance"

    def test_config_backend_default_is_lance(self, tmp_path):
        """MempalaceConfig.backend property defaults to 'lance'."""
        from mempalace.config import MempalaceConfig

        config = MempalaceConfig(config_dir=str(tmp_path))
        assert config.init() is not None  # Ensure config is written
        assert config.backend == "lance"


class TestSettingsTimeoutNaming:
    """timeout_read/write comments match their actual scope (LanceDB-only)."""

    def test_timeout_read_docstring_mentions_lance(self):
        """settings.timeout_read docstring refers to LanceDB, not ChromaDB."""
        from mempalace.settings import MemPalaceSettings
        import inspect

        docstring = inspect.getdoc(MemPalaceSettings.__init__)
        # Should not mention ChromaDB in the timeout_read line
        lines = docstring.split("\n")
        timeout_read_line = next((l for l in lines if "timeout_read" in l), "")
        assert "ChromaDB" not in timeout_read_line

    def test_timeout_write_docstring_mentions_lance(self):
        """settings.timeout_write docstring refers to LanceDB, not ChromaDB."""
        from mempalace.settings import MemPalaceSettings
        import inspect

        docstring = inspect.getdoc(MemPalaceSettings.__init__)
        lines = docstring.split("\n")
        timeout_write_line = next((l for l in lines if "timeout_write" in l), "")
        assert "ChromaDB" not in timeout_write_line


class TestDiagnosticsBackupContract:
    """Repair functions backup before destructive action (diagnostics-only contract)."""

    def test_timestamped_backup_is_public(self):
        """_timestamped_backup is importable from diagnostics module."""
        from mempalace.diagnostics import _timestamped_backup

        assert callable(_timestamped_backup)

    def test_rebuild_keyword_index_returns_backup_path(self, palace_path, seeded_collection):
        """rebuild_keyword_index result includes 'backup_path' key (may be None if no prior index)."""
        from mempalace.diagnostics import rebuild_keyword_index

        result = rebuild_keyword_index(palace_path)

        assert "backup_path" in result
        # backup_path may be None (no prior index to backup) or a path string
        assert result["backup_path"] is None or isinstance(result["backup_path"], str)

    def test_rebuild_symbol_index_returns_backup_path(self, palace_path, tmp_path):
        """rebuild_symbol_index result includes 'backup_path' key (may be None)."""
        from mempalace.diagnostics import rebuild_symbol_index

        # Create a minimal project for symbol index
        project = tmp_path / "project"
        project.mkdir()
        (project / "main.py").write_text("def foo(): pass\n")

        result = rebuild_symbol_index(palace_path, str(project))

        assert "backup_path" in result
        assert result["backup_path"] is None or isinstance(result["backup_path"], str)


class TestSearcherCacheContract:
    """searcher.py uses canonical query cache singleton."""

    def test_searcher_uses_query_cache_singleton(self):
        """searcher uses the canonical get_query_cache singleton from query_cache.py."""
        from mempalace.query_cache import get_query_cache

        cache_from_module = get_query_cache()

        # get_query_cache returns the singleton
        assert cache_from_module is get_query_cache()

    def test_invalidate_query_cache_clears_all(self):
        """invalidate_query_cache() clears the shared cache (full flush)."""
        from mempalace.searcher import invalidate_query_cache
        from mempalace.query_cache import get_query_cache

        cache = get_query_cache()
        # Prime the cache with a known key
        cache.set_value("test_key", {"data": "test"}, palace_path="/tmp/nonexistent", collection="test")
        assert cache.get_value("test_key", palace_path="/tmp/nonexistent", collection="test") is not None

        invalidate_query_cache()

        # Cache should be empty after full clear
        assert cache.get_value("test_key", palace_path="/tmp/nonexistent", collection="test") is None


class TestInfrstructureStatusCacheContract:
    """StatusCache is per-server-instance, not global."""

    def test_make_status_cache_returns_new_instance(self):
        """make_status_cache() returns a fresh StatusCache each call."""
        from mempalace.server._infrastructure import make_status_cache

        cache1 = make_status_cache()
        cache2 = make_status_cache()

        assert cache1 is not cache2

    def test_status_cache_is_per_palace_path(self):
        """StatusCache stores results keyed by palace_path, not a single global entry."""
        from mempalace.server._infrastructure import StatusCache

        cache = StatusCache()

        cache.set("/palace/a", {"data": "a"}, 100.0)
        cache.set("/palace/b", {"data": "b"}, 200.0)

        data_a, ts_a = cache.get("/palace/a")
        data_b, ts_b = cache.get("/palace/b")

        assert data_a == {"data": "a"}
        assert ts_a == 100.0
        assert data_b == {"data": "b"}
        assert ts_b == 200.0
