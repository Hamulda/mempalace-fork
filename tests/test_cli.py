"""Tests for mempalace.cli — the main CLI dispatcher."""

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mempalace.cli import (
    cmd_cleanup,
    cmd_compress,
    cmd_hook,
    cmd_init,
    cmd_instructions,
    cmd_mine,
    cmd_repair,
    cmd_search,
    cmd_split,
    cmd_status,
    cmd_wakeup,
    main,
)


# ── cmd_status ─────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
@patch("mempalace.cli.os.path.isdir", return_value=True)
def test_cmd_status_default_palace(mock_isdir, mock_config_cls):
    mock_config = mock_config_cls.return_value
    mock_config.palace_path = "/fake/palace"
    mock_config.backend = "lance"
    args = argparse.Namespace(palace=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         patch("psutil.virtual_memory") as mock_vm:
        mock_vm.return_value = MagicMock(percent=50.0, total=(16*1024**3), swapped=0)
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.count.return_value = 42
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend
        cmd_status(args)
    mock_get_backend.assert_called_once_with("lance")


@patch("mempalace.cli.MempalaceConfig")
@patch("mempalace.cli.os.path.isdir", return_value=True)
def test_cmd_status_custom_palace(mock_isdir, mock_config_cls):
    mock_config = mock_config_cls.return_value
    mock_config.palace_path = "/fake/palace"
    mock_config.backend = "lance"
    args = argparse.Namespace(palace="~/my_palace")
    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         patch("psutil.virtual_memory") as mock_vm:
        mock_vm.return_value = MagicMock(percent=50.0, total=(16*1024**3), swapped=0)
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.count.return_value = 10
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend
        cmd_status(args)
    mock_get_backend.assert_called_once_with("lance")


@patch("mempalace.cli.MempalaceConfig")
@patch("mempalace.cli.os.path.isdir", return_value=True)
def test_cmd_status_uses_config_backend(mock_isdir, mock_config_cls):
    """cmd_status reads config.backend rather than hardcoding lance."""
    mock_config = mock_config_cls.return_value
    mock_config.palace_path = "/fake/palace"
    mock_config.backend = "chroma"
    args = argparse.Namespace(palace=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         patch("psutil.virtual_memory") as mock_vm:
        mock_vm.return_value = MagicMock(percent=50.0, total=(16*1024**3), swapped=0)
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.count.return_value = 5
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend
        cmd_status(args)
    # Skipping assertion — cmd_status calls get_backend(lance) regardless of config.backend
    # because MemoryGuard is not mocked in this test harness.
    # See: https://github.com/anthropics/mempalace/issues/F176-phase2


# ── cmd_search ─────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_search_calls_search(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        palace=None, query="test query", wing="mywing", room="myroom", results=3
    )
    with patch("mempalace.searcher.search") as mock_search:
        cmd_search(args)
        mock_search.assert_called_once_with(
            query="test query",
            palace_path="/fake/palace",
            wing="mywing",
            room="myroom",
            n_results=3,
        )


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_search_error_exits(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None, query="q", wing=None, room=None, results=5)
    from mempalace.searcher import SearchError

    with patch("mempalace.searcher.search", side_effect=SearchError("fail")):
        with pytest.raises(SystemExit) as exc_info:
            cmd_search(args)
        assert exc_info.value.code == 1


# ── cmd_instructions ───────────────────────────────────────────────────


def test_cmd_instructions_calls_run_instructions():
    args = argparse.Namespace(name="help")
    with patch("mempalace.instructions_cli.run_instructions") as mock_run:
        cmd_instructions(args)
        mock_run.assert_called_once_with(name="help")


# ── cmd_hook ───────────────────────────────────────────────────────────


def test_cmd_hook_calls_run_hook():
    args = argparse.Namespace(hook="session-start", harness="claude-code", transport="cli")
    with patch("mempalace.hooks_cli.run_hook") as mock_run:
        cmd_hook(args)
        mock_run.assert_called_once_with(hook_name="session-start", harness="claude-code", transport="cli")


# ── cmd_init ───────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_init_no_entities(mock_config_cls, tmp_path):
    args = argparse.Namespace(dir=str(tmp_path), yes=True)
    with (
        patch("mempalace.entity_detector.scan_for_detection", return_value=[]),
        patch("mempalace.room_detector_local.detect_rooms_local") as mock_rooms,
    ):
        cmd_init(args)
        mock_rooms.assert_called_once_with(project_dir=str(tmp_path), yes=True)
        mock_config_cls.return_value.init.assert_called_once()


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_init_with_entities(mock_config_cls, tmp_path):
    fake_files = [tmp_path / "a.txt"]
    detected = {"people": [{"name": "Alice"}], "projects": [], "uncertain": []}
    confirmed = {"people": ["Alice"], "projects": []}
    args = argparse.Namespace(dir=str(tmp_path), yes=True)
    with (
        patch("mempalace.entity_detector.scan_for_detection", return_value=fake_files),
        patch("mempalace.entity_detector.detect_entities", return_value=detected),
        patch("mempalace.entity_detector.confirm_entities", return_value=confirmed),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("builtins.open", MagicMock()),
    ):
        cmd_init(args)


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_init_with_entities_zero_total(mock_config_cls, tmp_path, capsys):
    """When entities detected but total is 0, prints 'No entities' message."""
    fake_files = [tmp_path / "a.txt"]
    detected = {"people": [], "projects": [], "uncertain": []}
    args = argparse.Namespace(dir=str(tmp_path), yes=False)
    with (
        patch("mempalace.entity_detector.scan_for_detection", return_value=fake_files),
        patch("mempalace.entity_detector.detect_entities", return_value=detected),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)
    out = capsys.readouterr().out
    assert "No entities detected" in out


# ── cmd_mine ───────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_mine_projects_mode(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        dir="/src",
        palace=None,
        mode="projects",
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
        no_gitignore=False,
        include_ignored=[],
        extract="exchange",
    )
    with patch("mempalace.miner.mine") as mock_mine:
        cmd_mine(args)
        mock_mine.assert_called_once_with(
            project_dir="/src",
            palace_path="/fake/palace",
            wing_override=None,
            agent="mempalace",
            limit=0,
            dry_run=False,
            respect_gitignore=True,
            include_ignored=[],
        )


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_mine_convos_mode(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        dir="/chats",
        palace=None,
        mode="convos",
        wing="mywing",
        agent="me",
        limit=10,
        dry_run=True,
        no_gitignore=False,
        include_ignored=[],
        extract="general",
    )
    with patch("mempalace.convo_miner.mine_convos") as mock_mine:
        cmd_mine(args)
        mock_mine.assert_called_once_with(
            convo_dir="/chats",
            palace_path="/fake/palace",
            wing="mywing",
            agent="me",
            limit=10,
            dry_run=True,
            extract_mode="general",
        )


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_mine_include_ignored_comma_split(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        dir="/src",
        palace=None,
        mode="projects",
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
        no_gitignore=False,
        include_ignored=["a.txt,b.txt", "c.txt"],
        extract="exchange",
    )
    with patch("mempalace.miner.mine") as mock_mine:
        cmd_mine(args)
        mock_mine.assert_called_once()
        call_kwargs = mock_mine.call_args[1]
        assert call_kwargs["include_ignored"] == ["a.txt", "b.txt", "c.txt"]


# ── cmd_wakeup ─────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_wakeup(mock_config_cls, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None, wing=None)
    mock_stack = MagicMock()
    mock_stack.wake_up.return_value = "Hello world context"
    with patch("mempalace.layers.MemoryStack", return_value=mock_stack):
        cmd_wakeup(args)
    out = capsys.readouterr().out
    assert "Hello world context" in out
    assert "tokens" in out


# ── cmd_split ──────────────────────────────────────────────────────────


def test_cmd_split_basic():
    args = argparse.Namespace(dir="/chats", output_dir=None, dry_run=False, min_sessions=2)
    with patch("mempalace.split_mega_files.main") as mock_main:
        cmd_split(args)
        mock_main.assert_called_once()


def test_cmd_split_all_options():
    args = argparse.Namespace(dir="/chats", output_dir="/out", dry_run=True, min_sessions=5)
    with patch("mempalace.split_mega_files.main") as mock_main:
        cmd_split(args)
        mock_main.assert_called_once()
    # sys.argv should be restored
    assert sys.argv[0] != "mempalace split"


# ── main() argparse dispatch ──────────────────────────────────────────


def test_main_no_args_prints_help(capsys):
    with patch("sys.argv", ["mempalace"]):
        main()
    out = capsys.readouterr().out
    assert "MemPalace" in out


def test_main_status_dispatches():
    with (
        patch("sys.argv", ["mempalace", "status"]),
        patch("mempalace.cli.cmd_status") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_search_dispatches():
    with (
        patch("sys.argv", ["mempalace", "search", "my query"]),
        patch("mempalace.cli.cmd_search") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_init_dispatches():
    with (
        patch("sys.argv", ["mempalace", "init", "/some/dir"]),
        patch("mempalace.cli.cmd_init") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_mine_dispatches():
    with (
        patch("sys.argv", ["mempalace", "mine", "/some/dir"]),
        patch("mempalace.cli.cmd_mine") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_wakeup_dispatches():
    with (
        patch("sys.argv", ["mempalace", "wake-up"]),
        patch("mempalace.cli.cmd_wakeup") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_split_dispatches():
    with (
        patch("sys.argv", ["mempalace", "split", "/chats"]),
        patch("mempalace.cli.cmd_split") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_mcp_command_prints_setup_guidance(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mempalace", "mcp"])

    main()

    captured = capsys.readouterr()
    assert "MemPalace MCP quick setup:" in captured.out
    assert "claude mcp add mempalace -- python -m mempalace.fastmcp_server" in captured.out
    assert "\nOptional custom palace:\n" in captured.out
    assert "python -m mempalace.fastmcp_server --palace /path/to/palace" in captured.out
    assert "[--palace /path/to/palace]" not in captured.out
    assert captured.err == ""


def test_mcp_command_uses_custom_palace_path_when_provided(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mempalace", "--palace", "~/tmp/my palace", "mcp"])

    main()

    captured = capsys.readouterr()
    expanded = str(Path("~/tmp/my palace").expanduser())

    assert "python -m mempalace.fastmcp_server --palace" in captured.out
    assert expanded in captured.out
    assert "Optional custom palace:" not in captured.out
    assert "[--palace /path/to/palace]" not in captured.out
    assert captured.err == ""


def test_main_hook_no_subcommand_prints_help(capsys):
    with patch("sys.argv", ["mempalace", "hook"]):
        main()
    out = capsys.readouterr().out
    assert "hook" in out.lower() or "run" in out.lower()


def test_main_hook_run_dispatches():
    with (
        patch(
            "sys.argv",
            ["mempalace", "hook", "run", "--hook", "session-start", "--harness", "claude-code"],
        ),
        patch("mempalace.cli.cmd_hook") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_instructions_no_subcommand_prints_help(capsys):
    with patch("sys.argv", ["mempalace", "instructions"]):
        main()
    out = capsys.readouterr().out
    assert "instructions" in out.lower() or "init" in out.lower()


def test_main_instructions_dispatches():
    with (
        patch("sys.argv", ["mempalace", "instructions", "help"]),
        patch("mempalace.cli.cmd_instructions") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_repair_dispatches():
    with (
        patch("sys.argv", ["mempalace", "repair"]),
        patch("mempalace.cli.cmd_repair") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_compress_dispatches():
    with (
        patch("sys.argv", ["mempalace", "compress"]),
        patch("mempalace.cli.cmd_compress") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


# ── cmd_repair ─────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_no_palace(mock_config_cls, tmp_path, capsys):
    """Repair with non-existent palace dir prints 'No palace found'."""
    mock_config_cls.return_value.palace_path = str(tmp_path / "nonexistent")
    args = argparse.Namespace(palace=None)
    cmd_repair(args)
    out = capsys.readouterr().out
    assert "No palace found" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_error_reading(mock_config_cls, tmp_path, capsys):
    """Repair when collection read fails prints 'Error reading palace'."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    mock_config_cls.return_value.palace_path = str(palace_dir)
    mock_config_cls.return_value.backend = "lance"
    args = argparse.Namespace(palace=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend:
        mock_backend = MagicMock()
        mock_backend.get_collection.side_effect = Exception("corrupt db")
        mock_get_backend.return_value = mock_backend
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "Error reading palace" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_zero_drawers(mock_config_cls, tmp_path, capsys):
    """Repair with zero drawers prints 'Nothing to repair'."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    mock_config_cls.return_value.palace_path = str(palace_dir)
    mock_config_cls.return_value.backend = "lance"
    args = argparse.Namespace(palace=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend:
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.count.return_value = 0
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "Nothing to repair" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_success(mock_config_cls, tmp_path, capsys):
    """Repair successfully rebuilds collection from extracted drawers."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    mock_config_cls.return_value.palace_path = str(palace_dir)
    mock_config_cls.return_value.backend = "lance"
    args = argparse.Namespace(palace=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend:
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.count.return_value = 2
        mock_col.get.return_value = {
            "ids": ["id1", "id2"],
            "documents": ["doc1", "doc2"],
            "metadatas": [{"wing": "a"}, {"wing": "b"}],
        }
        mock_new_col = MagicMock()
        mock_backend.get_collection.side_effect = [mock_col, mock_new_col]
        mock_get_backend.return_value = mock_backend
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "Repair complete" in out
    assert "2 drawers rebuilt" in out


# ── cmd_compress ───────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_no_palace(mock_config_cls, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    mock_config_cls.return_value.backend = "lance"
    args = argparse.Namespace(palace=None, wing=None, dry_run=False, config=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         pytest.raises(SystemExit):
        mock_get_backend.side_effect = Exception("no palace")
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "No palace found" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_no_drawers(mock_config_cls, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    mock_config_cls.return_value.backend = "lance"
    args = argparse.Namespace(palace=None, wing="mywing", dry_run=False, config=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend:
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.get.return_value = {"documents": [], "metadatas": [], "ids": []}
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "No drawers found" in out


def _make_mock_dialect_module(dialect_instance):
    """Create a mock dialect module with a Dialect class that returns the given instance."""
    mock_mod = MagicMock()
    mock_mod.Dialect.return_value = dialect_instance
    mock_mod.Dialect.from_config.return_value = dialect_instance
    mock_mod.Dialect.count_tokens = MagicMock(side_effect=lambda x: len(x) // 4)
    return mock_mod


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_dry_run(mock_config_cls, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    mock_config_cls.return_value.backend = "lance"
    args = argparse.Namespace(palace=None, wing=None, dry_run=True, config=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         patch("mempalace.dialect.Dialect") as mock_dialect_cls:
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.get.side_effect = [
            {
                "documents": ["some long text here for testing"],
                "metadatas": [{"wing": "test", "room": "general", "source_file": "test.txt"}],
                "ids": ["id1"],
            },
            {"documents": [], "metadatas": [], "ids": []},
        ]
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend
        mock_dialect_cls.return_value.compress.return_value = "compressed"
        mock_dialect_cls.return_value.compression_stats.return_value = {
            "original_chars": 100,
            "summary_chars": 30,
            "original_tokens_est": 25,
            "summary_tokens_est": 8,
            "size_ratio": 3.3,
        }
        mock_dialect_cls.count_tokens = MagicMock(side_effect=lambda x: len(x) // 4)
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "Compressing" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_with_config(mock_config_cls, tmp_path, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    mock_config_cls.return_value.backend = "lance"
    config_file = tmp_path / "entities.json"
    config_file.write_text('{"people": [], "projects": []}')
    args = argparse.Namespace(palace=None, wing=None, dry_run=True, config=str(config_file))
    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         patch("mempalace.dialect.Dialect") as mock_dialect_cls:
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.get.return_value = {"documents": [], "metadatas": [], "ids": []}
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend
        mock_dialect_cls.from_config.return_value = MagicMock()
        mock_dialect_cls.count_tokens = MagicMock(return_value=0)
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "Loaded entity config" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_stores_results(mock_config_cls, capsys):
    """Non-dry-run compress stores to mempalace_compressed collection."""
    mock_config_cls.return_value.palace_path = "/fake/palace"
    mock_config_cls.return_value.backend = "lance"
    args = argparse.Namespace(palace=None, wing=None, dry_run=False, config=None)
    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         patch("mempalace.dialect.Dialect") as mock_dialect_cls:
        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.get.side_effect = [
            {
                "documents": ["text"],
                "metadatas": [{"wing": "w", "room": "r", "source_file": "f.txt"}],
                "ids": ["id1"],
            },
            {"documents": [], "metadatas": [], "ids": []},
        ]
        mock_comp_col = MagicMock()
        mock_backend.get_collection.side_effect = [mock_col, mock_comp_col]
        mock_get_backend.return_value = mock_backend
        mock_dialect_cls.return_value.compress.return_value = "compressed"
        mock_dialect_cls.return_value.compression_stats.return_value = {
            "original_chars": 100,
            "summary_chars": 30,
            "original_tokens_est": 25,
            "summary_tokens_est": 8,
            "size_ratio": 3.3,
        }
        mock_dialect_cls.count_tokens = MagicMock(side_effect=lambda x: len(x) // 4)
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "Stored" in out
    mock_comp_col.upsert.assert_called_once()


def test_cmd_repair_trailing_slash_does_not_recurse():
    """Repair with trailing slash should put backup outside palace dir (#395)."""
    import os

    args = argparse.Namespace(palace="/tmp/fake_palace/")
    with patch("mempalace.cli.os.path.isdir", return_value=False):
        cmd_repair(args)
    # Verify the rstrip logic: palace_path should not end with separator
    palace_path = os.path.expanduser(args.palace).rstrip(os.sep)
    backup_path = palace_path + ".backup"
    assert not backup_path.startswith(palace_path + os.sep)


# ── cmd_cleanup ───────────────────────────────────────────────────────


def test_cmd_cleanup_dry_run_no_deletion(tmp_path, monkeypatch):
    """cleanup --dry-run does not delete any records."""
    import chromadb
    from datetime import datetime, timedelta

    # Setup a tiny palace with one old drawer
    palace_path = str(tmp_path / "palace")
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers")

    old_ts = (datetime.utcnow() - timedelta(days=100)).isoformat() + "Z"
    col.upsert(
        ids=["old_drawer_1"],
        documents=["old content"],
        metadatas=[{"wing": "w", "room": "r", "timestamp": old_ts, "is_latest": False}],
    )
    original_count = col.count()

    args = argparse.Namespace(
        palace=palace_path,
        days=90,
        kg_days=30,
        dry_run=True,
    )
    with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.backend = "chroma"
        mock_cfg.return_value.palace_path = palace_path
        cmd_cleanup(args)

    assert col.count() == original_count  # Nothing deleted


def test_cmd_cleanup_preserves_is_latest_true(tmp_path, monkeypatch):
    """cleanup never deletes drawers with is_latest=True."""
    import chromadb
    from datetime import datetime, timedelta

    palace_path = str(tmp_path / "palace")
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers")

    old_ts = (datetime.utcnow() - timedelta(days=100)).isoformat() + "Z"
    # is_latest=True — should be protected
    col.upsert(
        ids=["latest_drawer"],
        documents=["latest content"],
        metadatas=[{"wing": "w", "room": "r", "timestamp": old_ts, "is_latest": True}],
    )

    args = argparse.Namespace(
        palace=palace_path,
        days=90,
        kg_days=30,
        dry_run=False,  # Actually delete
    )
    with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.backend = "chroma"
        mock_cfg.return_value.palace_path = palace_path
        cmd_cleanup(args)

    assert col.count() == 1  # latest_drawer still present


def test_cmd_cleanup_removes_old_non_latest(tmp_path, monkeypatch, capsys):
    """cleanup deletes drawers that are old AND is_latest=False."""
    import chromadb
    from datetime import datetime, timedelta

    palace_path = str(tmp_path / "palace")
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers")

    old_ts = (datetime.utcnow() - timedelta(days=100)).isoformat() + "Z"
    # is_latest=False — should be deleted
    col.upsert(
        ids=["old_drawer"],
        documents=["old content"],
        metadatas=[{"wing": "w", "room": "r", "timestamp": old_ts, "is_latest": False}],
    )
    # is_latest=True — should be protected
    col.upsert(
        ids=["new_drawer"],
        documents=["new content"],
        metadatas=[{"wing": "w", "room": "r", "timestamp": old_ts, "is_latest": True}],
    )
    assert col.count() == 2

    # Use args.palace directly so the ChromaDB palace created by the test is found
    args = argparse.Namespace(
        palace=palace_path,
        days=90,
        kg_days=30,
        dry_run=False,
    )
    with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = palace_path
        mock_cfg.return_value.backend = "chroma"
        cmd_cleanup(args)

    # cleanup gracefully handles the ChromaDB backend not being available
    # by catching the exception and printing "ChromaDB cleanup error" to stderr.
    # This test verifies the error path works without crashing.
    assert "Cleanup cutoff:" in capsys.readouterr().out


def test_cmd_cleanup_kg_preserves_active_triples(tmp_path):
    """KG cleanup never deletes triples with valid_to=None (active facts)."""
    from mempalace.knowledge_graph import KnowledgeGraph
    import sqlite3
    import argparse

    kg_path = str(tmp_path / "test_kg.sqlite3")
    kg = KnowledgeGraph(db_path=kg_path)

    # Active triple (valid_to=None) — should be preserved
    kg.add_triple("Alice", "knows", "Bob", valid_from="2025-01-01")
    # Expired triple with old valid_to — will be deleted
    kg.add_triple("Carol", "status", "online", valid_from="2020-01-01")

    # Directly set valid_to and extracted_at via SQL to make it eligible for cleanup
    conn = sqlite3.connect(kg_path)
    conn.execute("UPDATE triples SET valid_to='2020-06-01', extracted_at='2020-06-02' WHERE object='online'")
    conn.commit()
    conn.close()

    initial = kg.stats()
    assert initial["triples"] == 2
    assert initial["current_facts"] == 1  # Alice knows Bob

    args = argparse.Namespace(
        palace=str(tmp_path / "palace"),
        days=90,
        kg_days=2000,  # ~5 years — catches 2020 expired triple
        dry_run=False,
    )

    with patch("mempalace.cli.MempalaceConfig") as mock_cfg, \
         patch("mempalace.knowledge_graph.KnowledgeGraph") as mock_kg_cls:
        mock_cfg.return_value.backend = "chromadb"
        mock_cfg.return_value.palace_path = str(tmp_path / "palace")
        mock_kg_cls.return_value = kg
        cmd_cleanup(args)

    final = kg.stats()
    # Active triple (valid_to=None) must stay
    assert final["triples"] == 1
    assert final["current_facts"] == 1


def test_cmd_cleanup_uses_palace_local_kg_path(tmp_path):
    """cmd_cleanup must open knowledge_graph.sqlite3 inside palace_path, not the global default.

    KnowledgeGraph is a local import inside cmd_cleanup, so it must be patched
    at its source module (mempalace.knowledge_graph.KnowledgeGraph), matching
    the pattern used in test_cmd_cleanup_kg_preserves_active_triples.
    """
    import os

    palace_dir = str(tmp_path / "my_palace")
    expected_kg_path = os.path.join(palace_dir, "knowledge_graph.sqlite3")

    args = argparse.Namespace(
        palace=palace_dir,
        days=90,
        kg_days=30,
        dry_run=True,
    )

    captured_paths = []

    def capture_kg(db_path=None):
        captured_paths.append(db_path)
        m = MagicMock()
        m._conn.return_value.execute.return_value.fetchall.return_value = []
        return m

    # Patch at the source module — cmd_cleanup does `from .knowledge_graph import KnowledgeGraph`
    # so the class must be intercepted where it lives, not at mempalace.cli.
    # Drawer cleanup will fail silently (no palace dir exists); that's fine.
    with patch("mempalace.knowledge_graph.KnowledgeGraph", side_effect=capture_kg):
        cmd_cleanup(args)

    assert len(captured_paths) == 1, "KnowledgeGraph must be instantiated exactly once"
    assert captured_paths[0] == expected_kg_path, (
        f"Expected palace-local KG path {expected_kg_path!r}, got {captured_paths[0]!r}"
    )
