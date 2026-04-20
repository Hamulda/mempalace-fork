import os
import json
import tempfile
from mempalace.config import MempalaceConfig


def test_default_config():
    cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
    assert "palace" in cfg.palace_path
    assert cfg.collection_name == "mempalace_drawers"


def test_config_from_file():
    tmpdir = tempfile.mkdtemp()
    with open(os.path.join(tmpdir, "config.json"), "w") as f:
        json.dump({"palace_path": "/custom/palace"}, f)
    cfg = MempalaceConfig(config_dir=tmpdir)
    assert cfg.palace_path == "/custom/palace"


def test_env_override():
    os.environ["MEMPALACE_PALACE_PATH"] = "/env/palace"
    cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
    assert cfg.palace_path == "/env/palace"
    del os.environ["MEMPALACE_PALACE_PATH"]


def test_init():
    tmpdir = tempfile.mkdtemp()
    cfg = MempalaceConfig(config_dir=tmpdir)
    cfg.init()
    assert os.path.exists(os.path.join(tmpdir, "config.json"))


# ── backend ─────────────────────────────────────────────────────────────


def test_backend_defaults_to_lance():
    """Canonical storage is Lance — backend defaults to 'lance', not 'chroma'."""
    cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
    assert cfg.backend == "lance"


def test_backend_reads_file_lance(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"backend": "lance"}))
    assert cfg.backend == "lance"


def test_backend_reads_file_chroma(tmp_path):
    """Legacy Chroma backend is still readable when explicitly configured."""
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"backend": "chroma"}))
    # Re-init to re-read after writing
    cfg2 = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg2.backend == "chroma"


def test_backend_env_overrides_file(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"backend": "chroma"}))
    os.environ["MEMPALACE_BACKEND"] = "lance"
    try:
        assert cfg.backend == "lance"
    finally:
        del os.environ["MEMPALACE_BACKEND"]


def test_backend_unknown_value_defaults_to_lance(tmp_path):
    """If config has garbage value, treat 'lance' as the safe default."""
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"backend": "garbage"}))
    assert cfg.backend == "lance"


# ── settings / factory consistency ─────────────────────────────────────


def test_settings_palace_path_agrees_with_env():
    """settings.palace_path resolves from the same env vars as MempalaceConfig."""
    os.environ["MEMPALACE_PALACE_PATH"] = "/test/env/palace"
    try:
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings()
        assert s.palace_path == "/test/env/palace"
        assert s.db_path == "/test/env/palace"  # synced after validator
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]


def test_settings_db_path_default_syncs_to_palace_path():
    """When db_path is empty (default), it should be synced to palace_path."""
    from mempalace.settings import MemPalaceSettings
    s = MemPalaceSettings()
    assert s.db_path == s.palace_path


def test_settings_db_path_explicit_override_is_respected():
    """MEMPALACE_DB_PATH override should NOT be overwritten to palace_path."""
    os.environ["MEMPALACE_DB_PATH"] = "/custom/db"
    os.environ["MEMPALACE_PALACE_PATH"] = "/custom/palace"
    try:
        from mempalace.settings import MemPalaceSettings
        s = MemPalaceSettings()
        # Explicit db_path should be kept as-is
        assert s.db_path == "/custom/db"
        # palace_path is still the canonical palace location
        assert s.palace_path == "/custom/palace"
    finally:
        del os.environ["MEMPALACE_DB_PATH"]
        del os.environ["MEMPALACE_PALACE_PATH"]


def test_factory_palace_path_is_settings_palace_path(tmp_path):
    """create_server should use settings.palace_path as canonical source."""
    os.environ["MEMPALACE_PALACE_PATH"] = str(tmp_path / "test_palace")
    try:
        from mempalace.settings import MemPalaceSettings
        from mempalace.config import MempalaceConfig

        settings = MemPalaceSettings()
        # Simulate what factory does now (derived from settings.palace_path)
        factory_config = MempalaceConfig(config_dir=str(tmp_path / "test_palace"))

        assert settings.palace_path == str(tmp_path / "test_palace")
        assert factory_config.palace_path == str(tmp_path / "test_palace")
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]


def test_factory_config_dir_from_palace_path_not_db_path(tmp_path):
    """config_dir should derive from palace_path, not db_path (which may have compat override)."""
    os.environ["MEMPALACE_PALACE_PATH"] = str(tmp_path / "palace")
    os.environ["MEMPALACE_DB_PATH"] = str(tmp_path / "other_db")  # compat override
    try:
        from mempalace.settings import MemPalaceSettings

        settings = MemPalaceSettings()
        # palace_path is canonical; db_path is compat override
        assert settings.palace_path == str(tmp_path / "palace")
        assert settings.db_path == str(tmp_path / "other_db")

        # config_dir derives from palace_path, NOT db_path
        from mempalace.config import MempalaceConfig
        config = MempalaceConfig(config_dir=str(tmp_path / "palace"))
        assert config.palace_path == str(tmp_path / "palace")
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]
        del os.environ["MEMPALACE_DB_PATH"]


def test_session_registry_uses_palace_path(tmp_path):
    """SessionRegistry should be initialized with the same palace_path that config uses."""
    os.environ["MEMPALACE_PALACE_PATH"] = str(tmp_path / "palace")
    try:
        from mempalace.settings import MemPalaceSettings
        from mempalace.session_registry import SessionRegistry
        from mempalace.config import MempalaceConfig

        settings = MemPalaceSettings()
        config = MempalaceConfig(config_dir=str(tmp_path / "palace"))

        # Both resolve to same path
        assert settings.palace_path == config.palace_path

        # SessionRegistry uses palace_path
        registry = SessionRegistry(settings.palace_path)
        assert registry._db_path == str(tmp_path / "palace" / "sessions.sqlite3")
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]
