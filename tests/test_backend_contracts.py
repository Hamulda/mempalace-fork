"""
test_backend_contracts.py — Lance-canonical backend contract tests.

Verifies:
1. Lance is the canonical default everywhere
2. Chroma is legacy/migration-only (opt-in)
3. No accidental Chroma fallback on unknown backend values
4. Diagnostics always use Lance (Lance-only features: FTS5, symbol index)
5. Migration path preserved bidirectionally
"""

import pytest


class TestLanceCanonicalDefault:
    """Lance is always the canonical default — never Chroma."""

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

    def test_backend_choices_exports(self):
        """BACKEND_CHOICES must be exported and contain 'lance' and 'chroma'."""
        from mempalace.backends import BACKEND_CHOICES
        assert BACKEND_CHOICES == ("lance", "chroma")

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
        assert "lance" in error_msg or "chroma" in error_msg


class TestChromaLegacyOptIn:
    """Chroma is legacy/migration-only — opt-in, not default."""

    def test_get_backend_chroma_still_works(self):
        """get_backend('chroma') must still work for migration compatibility."""
        from mempalace.backends import get_backend
        b = get_backend("chroma")
        assert type(b).__name__ == "ChromaBackend"

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

    def test_env_unknown_backend_not_accepted(self, tmp_path):
        """MEMPALACE_BACKEND is passthrough; get_backend() validates and raises."""
        import os, json
        os.environ["MEMPALACE_BACKEND"] = "not_a_real_backend"
        try:
            cfg_dir = tmp_path / ".mempalace"
            cfg_dir.mkdir()
            cfg_file = cfg_dir / "config.json"
            cfg_file.write_text(json.dumps({}))
            from mempalace.config import MempalaceConfig
            cfg = MempalaceConfig(config_dir=str(cfg_dir))
            # config.py passes env var through without validation
            # validation happens in get_backend()
            from mempalace.backends import get_backend
            with pytest.raises(ValueError):
                get_backend(cfg.backend)
        finally:
            os.environ.pop("MEMPALACE_BACKEND", None)


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


class TestMigrationPathPreserved:
    """Bidirectional migration must continue to work."""

    def test_migrate_module_exports_both_functions(self):
        """migrate.py must export both migration functions."""
        from mempalace.migrate import migrate_chroma_to_lance, migrate_lance_to_chroma
        assert callable(migrate_chroma_to_lance)
        assert callable(migrate_lance_to_chroma)

    def test_chroma_backend_still_in_backends_init(self):
        """ChromaBackend and ChromaCollection must still be importable from backends."""
        from mempalace.backends import ChromaBackend, ChromaCollection
        assert ChromaBackend is not None
        assert ChromaCollection is not None
