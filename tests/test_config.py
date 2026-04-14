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
