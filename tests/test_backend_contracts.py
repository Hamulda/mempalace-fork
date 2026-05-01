"""
test_backend_contracts.py — Lance-canonical backend contract tests.

Verifies:
1. Lance is the canonical default everywhere
2. ChromaDB is removed — get_backend("chroma") raises ValueError
3. BACKEND_CHOICES == ("lance",)
4. No chromadb eager loading in sys.modules
5. Diagnostics always use Lance (Lance-only features: FTS5, symbol index)
"""

import pytest
import warnings


class TestLanceCanonicalDefault:
    """Lance is always the canonical default."""

    def test_config_backend_default_is_lance(self, tmp_path):
        """MempalaceConfig.backend must default to 'lance' when config.json absent."""
        import os, json
        os.environ.pop("MEMPALACE_BACKEND", None)
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({}))
        from mempalace.config import MempalaceConfig
        cfg = MempalaceConfig(config_dir=str(cfg_dir))
        assert cfg.backend == "lance"

    def test_settings_db_backend_default_is_lance(self):
        """MemPalaceSettings.db_backend must default to 'lance'."""
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings()
        assert s.db_backend == "lance"

    def test_get_backend_default_is_lance(self):
        """get_backend() with no args must return LanceBackend."""
        from mempalace.backends import get_backend
        try:
            b = get_backend()
            assert type(b).__name__ == "LanceBackend"
        except ImportError:
            pytest.skip("LanceDB not installed")

    def test_backend_choices_is_lance_only(self):
        """BACKEND_CHOICES must be ('lance',) — ChromaDB removed."""
        from mempalace.backends import BACKEND_CHOICES
        assert BACKEND_CHOICES == ("lance",)

    def test_backend_type_exports(self):
        """BackendType must be exported as a Literal type."""
        from mempalace.backends import BackendType
        assert BackendType is not None

    def test_backend_choices_in_value_error(self):
        """ValueError for unknown backend must list allowed choices."""
        from mempalace.backends import get_backend
        with pytest.raises(ValueError) as exc_info:
            get_backend("not_a_backend")
        error_msg = str(exc_info.value)
        assert "lance" in error_msg


class TestChromaRemoved:
    """ChromaDB has been removed — LanceDB is the only supported backend."""

    def test_get_backend_chroma_raises_valueerror(self):
        """get_backend('chroma') must raise ValueError with clear removal message."""
        from mempalace.backends import get_backend
        with pytest.raises(ValueError) as exc_info:
            get_backend("chroma")
        msg = str(exc_info.value)
        assert "ChromaDB" in msg
        assert "removed" in msg.lower()
        assert "LanceDB" in msg

    def test_chroma_not_default_in_any_config_path(self):
        """Chroma must never be the default in any config path."""
        import os
        os.environ.pop("MEMPALACE_BACKEND", None)
        os.environ.pop("MEMPALACE_DB_BACKEND", None)
        from mempalace.config import MempalaceConfig
        from mempalace.settings import MemPalaceSettings
        cfg = MempalaceConfig()
        settings = MemPalaceSettings()
        assert cfg.backend == "lance"
        assert settings.db_backend == "lance"

    def test_chromadb_not_imported_on_backend_package_import(self):
        """Importing mempalace.backends must NOT load chromadb into sys.modules."""
        import sys
        # Check exact module key or prefix+dot (not substring — PIL.GimpGradientFile matches 'chroma')
        assert "chromadb" not in sys.modules and not any(k.startswith("chromadb.") for k in sys.modules), \
            "chromadb was loaded into sys.modules"

    def test_chromadb_not_imported_on_get_backend_chroma_call(self):
        """get_backend('chroma') must raise WITHOUT loading chromadb into sys.modules."""
        import sys
        from mempalace.backends import get_backend
        before = "chromadb" in sys.modules or any(k.startswith("chromadb.") for k in sys.modules)
        try:
            get_backend("chroma")
        except ValueError:
            pass  # Expected
        after = "chromadb" in sys.modules or any(k.startswith("chromadb.") for k in sys.modules)
        assert not after, "chromadb was loaded into sys.modules despite ValueError"


class TestNoAccidentalChromaFallback:
    """Unknown backend values must raise, not silently fall back to Chroma."""

    def test_get_backend_unknown_raises_valueerror(self):
        """get_backend('unknown') must raise ValueError, not silently use Chroma."""
        from mempalace.backends import get_backend
        with pytest.raises(ValueError) as exc_info:
            get_backend("unknown")
        assert "unknown" in str(exc_info.value).lower() or "lance" in str(exc_info.value).lower()

    def test_config_unknown_backend_resolves_to_lance(self, tmp_path):
        """config with unknown backend value falls back to 'lance' (canonical default)."""
        import os, json
        os.environ.pop("MEMPALACE_BACKEND", None)
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({"backend": "unknown_backend"}))
        from mempalace.config import MempalaceConfig
        cfg = MempalaceConfig(config_dir=str(cfg_dir))
        assert cfg.backend == "lance"

    def test_env_unknown_backend_rejected_by_get_backend(self, tmp_path):
        """get_backend() must raise ValueError when called with unknown backend."""
        import os, json
        os.environ.pop("MEMPALACE_BACKEND", None)
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({"backend": "not_a_real_backend"}))
        from mempalace.backends import get_backend
        # get_backend should raise because "not_a_real_backend" is not in BACKEND_CHOICES
        with pytest.raises(ValueError):
            get_backend("not_a_real_backend")


class TestDiagnosticsAlwaysLance:
    """Diagnostics always use Lance — FTS5 and symbol index are Lance-only features."""

    def test_validate_keyword_index_hardcodes_lance(self):
        """validate_keyword_index() must hardcode 'lance' — not read from config.backend."""
        import inspect
        from mempalace.diagnostics import validate_keyword_index
        source = inspect.getsource(validate_keyword_index)
        assert 'get_backend("lance")' in source
        assert 'get_backend(cfg.backend)' not in source
        assert 'get_backend(backend_type)' not in source

    def test_rebuild_keyword_index_hardcodes_lance(self):
        """rebuild_keyword_index() must hardcode 'lance' — not read from config.backend."""
        import inspect
        from mempalace.diagnostics import rebuild_keyword_index
        source = inspect.getsource(rebuild_keyword_index)
        assert 'get_backend("lance")' in source
        assert 'get_backend(cfg.backend)' not in source
        assert 'get_backend(backend_type)' not in source


class TestMigrationRemoved:
    """Migration functions are deprecated pending migrate.py cleanup in later phase.

    migrate.py still has migrate_chroma_to_lance / migrate_lance_to_chroma.
    These tests are placeholders — they verify the INTENT that migration is removed,
    but are deferred until migrate.py is cleaned up in a later phase.
    """

    def test_migrate_deprecated_note(self):
        """Deferred: migrate.py cleanup pending later phase."""
        pytest.skip("migrate.py cleanup deferred to Phase 3+")
