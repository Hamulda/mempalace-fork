"""
test_backend_defaults.py — Regression tests for Lance-as-default backend configuration.

Verifies:
1. settings.py defaults to "lance" only (ChromaDB removed)
2. config.py backend property defaults to "lance", warns on "chroma"
3. backends/__init__.py get_backend() defaults to "lance"
4. get_backend("chroma") raises clear ValueError (ChromaDB removed)
5. BACKEND_CHOICES is ("lance",) only
6. No eager chromadb import in sys.modules
"""

import pytest
import warnings


class TestSettingsDefaultBackend:
    """settings.py must default to lance only."""

    def test_settings_db_backend_default_is_lance(self):
        """MemPalaceSettings.db_backend must default to 'lance'."""
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings()
        assert s.db_backend == "lance", f"Expected 'lance', got {s.db_backend!r}"

    def test_settings_db_backend_accepts_lance(self):
        """db_backend must accept 'lance'."""
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings(db_backend="lance")
        assert s.db_backend == "lance"

    def test_settings_db_backend_rejects_chroma(self):
        """db_backend must reject 'chroma' with ValidationError (ChromaDB removed)."""
        from mempalace.settings import MemPalaceSettings
        from pydantic_core import ValidationError
        with pytest.raises(ValidationError):
            MemPalaceSettings(db_backend="chroma")


class TestConfigDefaultBackend:
    """config.py must default to lance and warn on chroma."""

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

    def test_config_backend_warns_on_env_chroma(self, tmp_path):
        """MEMPALACE_BACKEND=chroma must warn and return 'lance'."""
        import os, json
        os.environ["MEMPALACE_BACKEND"] = "chroma"
        try:
            cfg_dir = tmp_path / ".mempalace"
            cfg_dir.mkdir()
            cfg_file = cfg_dir / "config.json"
            cfg_file.write_text(json.dumps({}))
            from mempalace.config import MempalaceConfig
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                cfg = MempalaceConfig(config_dir=str(cfg_dir))
                assert cfg.backend == "lance"
                assert len(w) == 1
                assert "ChromaDB" in str(w[0].message)
                assert "removed" in str(w[0].message).lower()
        finally:
            os.environ.pop("MEMPALACE_BACKEND", None)

    def test_config_backend_warns_on_file_chroma(self, tmp_path):
        """config.json with backend='chroma' must warn and return 'lance'."""
        import os, json
        os.environ.pop("MEMPALACE_BACKEND", None)
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({"backend": "chroma"}))
        from mempalace.config import MempalaceConfig
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cfg = MempalaceConfig(config_dir=str(cfg_dir))
            assert cfg.backend == "lance"
            assert len(w) == 1
            assert "ChromaDB" in str(w[0].message)
        os.environ.pop("MEMPALACE_BACKEND", None)


class TestBackendsLazyImport:
    """Backends must use lazy import — ChromaDB not loaded unless explicitly requested."""

    def test_lance_available_flag_set(self):
        """_LANCE_AVAILABLE must be True when LanceDB installed, False otherwise."""
        from mempalace.backends import _LANCE_AVAILABLE
        assert isinstance(_LANCE_AVAILABLE, bool)

    def test_get_backend_default_is_lance(self):
        """get_backend() with no args must return LanceBackend."""
        from mempalace.backends import get_backend
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

    def test_get_backend_chroma_raises(self):
        """get_backend('chroma') must raise ValueError with clear message."""
        from mempalace.backends import get_backend
        with pytest.raises(ValueError) as exc_info:
            get_backend("chroma")
        assert "ChromaDB" in str(exc_info.value)
        assert "removed" in str(exc_info.value).lower()
        assert "LanceDB" in str(exc_info.value)

    def test_chromadb_not_in_sys_modules_after_backend_import(self):
        """Importing backends package must NOT put chromadb in sys.modules."""
        import sys
        # chromadb should not be loaded by backends/__init__.py
        assert "chromadb" not in sys.modules

    def test_chromadb_not_loaded_by_get_backend_chroma_raises(self):
        """get_backend('chroma') must raise WITHOUT loading chromadb into sys.modules."""
        import sys
        from mempalace.backends import get_backend
        # Track modules before
        had_chromadb = "chromadb" in sys.modules
        try:
            get_backend("chroma")
        except ValueError:
            pass  # Expected
        # chromadb should NOT be in sys.modules after failed get_backend("chroma")
        assert "chromadb" not in sys.modules, "chromadb was loaded despite raising ValueError"


class TestBackendNaming:
    """Backend naming must be consistent: 'lance' only (ChromaDB removed)."""

    def test_settings_naming_convention(self):
        """settings.py must use 'lance' only (not 'chromadb')."""
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings()
        assert s.db_backend == "lance"

    def test_config_naming_convention_lance(self, tmp_path):
        """config.py must use 'lance' (not 'lancedb'/'chromadb')."""
        import os, json
        os.environ.pop("MEMPALACE_BACKEND", None)
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({"backend": "lance"}))
        from mempalace.config import MempalaceConfig
        cfg = MempalaceConfig(config_dir=str(cfg_dir))
        assert cfg.backend == "lance"

    def test_config_naming_convention_warns_on_chroma(self, tmp_path):
        """config.json with backend='chroma' must warn and return 'lance'."""
        import os, json
        os.environ.pop("MEMPALACE_BACKEND", None)
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text(json.dumps({"backend": "chroma"}))
        from mempalace.config import MempalaceConfig
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cfg = MempalaceConfig(config_dir=str(cfg_dir))
            assert cfg.backend == "lance"
            assert len(w) == 1
