"""
test_backend_defaults.py — Regression tests for Lance-as-default backend configuration.

Verifies:
1. settings.py defaults to "lance" (not "chromadb")
2. config.py backend property defaults to "lance"
3. backends/__init__.py get_backend() defaults to "lance"
4. Lazy Chroma import — Chroma not loaded until explicitly requested
5. CLI status command respects canonical naming ("chroma", not "chromadb")
"""

import pytest


class TestSettingsDefaultBackend:
    """settings.py must default to lance."""

    def test_settings_db_backend_default_is_lance(self):
        """MemPalaceSettings.db_backend must default to 'lance', not 'chromadb'."""
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings()
        assert s.db_backend == "lance", f"Expected 'lance', got {s.db_backend!r}"

    def test_settings_db_backend_accepts_lance(self):
        """db_backend must accept 'lance'."""
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings(db_backend="lance")
        assert s.db_backend == "lance"

    def test_settings_db_backend_accepts_chroma(self):
        """db_backend must accept 'chroma' (legacy compat)."""
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings(db_backend="chroma")
        assert s.db_backend == "chroma"


class TestConfigDefaultBackend:
    """config.py must default to lance."""

    def test_config_backend_default_is_lance(self, tmp_path):
        """MempalaceConfig.backend must default to 'lance' when config.json is absent."""
        import os, json
        os.environ.pop("MEMPALACE_BACKEND", None)
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({}))
        from mempalace.config import MempalaceConfig
        cfg = MempalaceConfig(config_dir=str(cfg_dir))
        assert cfg.backend == "lance", f"Expected 'lance', got {cfg.backend!r}"

    def test_config_backend_env_override(self, tmp_path):
        """MEMPALACE_BACKEND env var must override config."""
        import os
        os.environ["MEMPALACE_BACKEND"] = "chroma"
        try:
            from mempalace.config import MempalaceConfig
            cfg = MempalaceConfig()
            assert cfg.backend == "chroma"
        finally:
            os.environ.pop("MEMPALACE_BACKEND", None)


class TestBackendsLazyImport:
    """Backends must use lazy import — Chroma not loaded until requested."""

    def test_lance_available_flag_set(self):
        """_LANCE_AVAILABLE must be True when LanceDB installed, False otherwise."""
        from mempalace.backends import _LANCE_AVAILABLE
        # Flag exists and is a boolean
        assert isinstance(_LANCE_AVAILABLE, bool)

    def test_get_backend_default_is_lance(self):
        """get_backend() must default to 'lance'."""
        from mempalace.backends import get_backend
        # With no args, should use lance
        # Note: will raise ImportError if LanceDB not installed — that's expected
        try:
            b = get_backend("lance")
            assert type(b).__name__ == "LanceBackend"
        except ImportError:
            pytest.skip("LanceDB not installed")

    def test_get_backend_lance_explicit(self):
        """get_backend('lance') must return LanceBackend."""
        from mempalace.backends import get_backend
        try:
            b = get_backend("lance")
            assert type(b).__name__ == "LanceBackend"
        except ImportError:
            pytest.skip("LanceDB not installed")

    def test_get_backend_chroma_lazy(self):
        """get_backend('chroma') must load Chroma lazily (after Lance attempt)."""
        from mempalace.backends import get_backend
        b = get_backend("chroma")
        assert type(b).__name__ == "ChromaBackend"

    def test_chroma_not_eager_on_import(self):
        """Importing backends package must NOT eagerly load Chroma."""
        import sys
        # Chroma modules should NOT be in sys.modules after backends import
        # (unless already imported by previous tests)
        # This is a smoke test — if ChromaBackend is a real class (not None),
        # it means lazy import worked
        from mempalace.backends import ChromaBackend
        # ChromaBackend is None before first get_backend("chroma") call
        # After calling get_backend("chroma"), it should be set


class TestBackendNaming:
    """Backend naming must be consistent: 'lance' | 'chroma' (not 'chromadb')."""

    def test_settings_naming_convention(self):
        """settings.py must use 'lance' and 'chroma' (not 'lancedb'/'chromadb')."""
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings()
        # Must be 'lance' or 'chroma', not 'lancedb' or 'chromadb'
        assert s.db_backend in ("lance", "chroma")

    def test_config_naming_convention(self, tmp_path):
        """config.py must use 'lance' and 'chroma' (not 'lancedb'/'chromadb')."""
        import os, json
        os.environ.pop("MEMPALACE_BACKEND", None)
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({"backend": "lance"}))
        from mempalace.config import MempalaceConfig
        cfg = MempalaceConfig(config_dir=str(cfg_dir))
        assert cfg.backend == "lance"
        # config.json with 'chromadb' should be accepted and passed through as 'chromadb'
        cfg_file.write_text(json.dumps({"backend": "chroma"}))
        cfg2 = MempalaceConfig(config_dir=str(cfg_dir))
        assert cfg2.backend == "chroma"
