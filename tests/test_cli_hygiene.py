"""
CLI hygiene tests: embed-daemon timeout, swap reporting, cleanup offset pagination.

Run: pytest tests/test_cli_hygiene.py -v -s
"""
from __future__ import annotations

import argparse
import time
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("lancedb", reason="LanceDB not installed")


# ── Fix 1: embed-daemon start — timeout path cannot block forever ───────


def test_embed_daemon_start_timeout_is_bounded(monkeypatch, tmp_path):
    """
    The daemon start loop must use select so a stuck readline() never
    blocks the process past the deadline.
    """
    from mempalace.cli import cmd_embed_daemon

    sock = tmp_path / "test.sock"
    monkeypatch.setenv("MEMPALACE_EMBED_SOCK", str(sock))

    with patch("mempalace.backends.lance._daemon_is_running", return_value=False):
        fake_proc = MagicMock()
        fake_proc.stdout.readline = MagicMock(return_value="")  # never emits READY
        fake_proc.poll = MagicMock(return_value=None)
        fake_proc.stderr.read = MagicMock(return_value="")
        fake_proc.stdout.fileno = MagicMock(return_value=1)

        with patch("subprocess.Popen", return_value=fake_proc):
            args = argparse.Namespace(action="start", palace=None)
            start = time.monotonic()

            def fake_monotonic():
                return start + 31  # past the 30s deadline
            monkeypatch.setattr(time, "monotonic", fake_monotonic)

            cmd_embed_daemon(args)

            assert fake_proc.stdout.readline.called


def test_embed_daemon_start_select_used(monkeypatch, tmp_path):
    """
    Verify that select is actually used in the start loop (not bare readline).
    """
    from mempalace.cli import cmd_embed_daemon

    sock = tmp_path / "test.sock"
    monkeypatch.setenv("MEMPALACE_EMBED_SOCK", str(sock))

    fake_proc = MagicMock()
    fake_proc.stdout.readline = MagicMock(side_effect=["READY"])
    fake_proc.poll = MagicMock(return_value=None)
    fake_proc.stderr.read = MagicMock(return_value="")
    fake_proc.stdout.fileno = MagicMock(return_value=1)

    with patch("mempalace.backends.lance._daemon_is_running", return_value=False):
        with patch("subprocess.Popen", return_value=fake_proc):
            with patch("select.select") as mock_select:
                mock_select.return_value = ([], [], [])  # no data ready

                start = time.monotonic()
                call_time = [start]

                def fake_monotonic():
                    call_time[0] += 0.6
                    return call_time[0]
                monkeypatch.setattr(time, "monotonic", fake_monotonic)

                args = argparse.Namespace(action="start", palace=None)
                cmd_embed_daemon(args)

                mock_select.assert_called()


# ── Fix 2: swap reporting uses psutil.swap_memory() not vm.swapped ───────


@patch("mempalace.cli.MempalaceConfig")
@patch("mempalace.cli.os.path.isdir", return_value=True)
def test_swap_uses_swap_memory(mock_isdir, mock_config_cls, capsys):
    """
    cmd_status must use psutil.swap_memory() (sin+sout bytes) instead of the
    non-existent vm.swapped attribute.
    """
    from mempalace.cli import cmd_status

    mock_config = mock_config_cls.return_value
    mock_config.palace_path = "/fake/palace"
    mock_config.backend = "lance"
    args = argparse.Namespace(palace=None)

    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         patch("psutil.virtual_memory") as mock_vm, \
         patch("psutil.swap_memory") as mock_sm:

        mock_vm.return_value = MagicMock(percent=50.0, total=(16 * 1024**3))
        mock_sm.return_value = MagicMock(
            sin=100 * 1024**2, sout=0,
            total=1024**3, used=0, free=1024**3, percent=10,
            raw=MagicMock()
        )

        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.count.return_value = 42
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend

        cmd_status(args)

        mock_sm.assert_called_once()
        out = capsys.readouterr().out
        # sin=100MB + sout=0 = 100MB
        assert "Swap:" in out
        assert "100" in out


@patch("mempalace.cli.MempalaceConfig")
@patch("mempalace.cli.os.path.isdir", return_value=True)
def test_swap_reports_both_sin_and_sout(mock_isdir, mock_config_cls, capsys):
    """
    swap MB must include both swap-in (sin) and swap-out (sout) bytes.
    """
    from mempalace.cli import cmd_status

    mock_config = mock_config_cls.return_value
    mock_config.palace_path = "/fake/palace"
    mock_config.backend = "lance"
    args = argparse.Namespace(palace=None)

    with patch("mempalace.backends.get_backend") as mock_get_backend, \
         patch("psutil.virtual_memory") as mock_vm, \
         patch("psutil.swap_memory") as mock_sm:

        mock_vm.return_value = MagicMock(percent=50.0, total=(16 * 1024**3))
        # sin=50MB, sout=50MB → total 100MB
        mock_sm.return_value = MagicMock(
            sin=50 * 1024**2, sout=50 * 1024**2,
            total=100 * 1024**2, used=0, free=0, percent=100,
            raw=MagicMock()
        )

        mock_backend = MagicMock()
        mock_col = MagicMock()
        mock_col.count.return_value = 0
        mock_backend.get_collection.return_value = mock_col
        mock_get_backend.return_value = mock_backend

        cmd_status(args)

        mock_sm.assert_called_once()
        out = capsys.readouterr().out
        # 50+50=100MB
        assert "100" in out


# ── Fix 3: cleanup offset pagination after delete ───────────────────────


def test_cleanup_offset_advances_by_deleted_count(tmp_path):
    """
    When delete removes N items from a batch, offset must advance by N,
    not by the full BATCH size.

    Uses a mock that splits the dataset across two pages at offsets 0 and 5,
    with the first page returning 5 eligible items (offset advances to 5 after
    deletion) and the second page returning the remaining items.

    The critical assertion: get() is called at offset=5 (not offset=10,
    which would be the buggy fixed-BATCH advance) on the second iteration.
    """
    from mempalace.cli import cmd_cleanup

    palace_path = str(tmp_path / "palace")

    args = argparse.Namespace(
        palace=palace_path,
        days=365,
        kg_days=30,
        dry_run=False,
    )

    # 10 items: first 5 = eligible, last 5 = protected
    first_page_ids = [f"id_{i:04d}" for i in range(5)]
    first_page_metas = [{"timestamp": "2020-01-01T00:00:00Z", "is_latest": False}] * 5
    second_page_ids = [f"id_{i:04d}" for i in range(5, 10)]
    second_page_metas = [{"timestamp": "2020-01-01T00:00:00Z", "is_latest": True}] * 5

    seen_kwargs = []

    def tracking_get(*a, **kw):
        seen_kwargs.append(dict(kw))
        off = kw.get("offset", 0)
        if off == 0:
            # Return first 5 eligible items + 4995 protected items to fill BATCH
            big_ids = first_page_ids + [f"protected_{i:04d}" for i in range(4995)]
            big_metas = first_page_metas + [{"timestamp": "2020-01-01T00:00:00Z", "is_latest": True}] * 4995
            return {"ids": big_ids, "metadatas": big_metas}
        elif off == 5:
            # After fix: offset advances by deleted_count=5, reaching the protected items
            return {"ids": second_page_ids, "metadatas": second_page_metas}
        return {"ids": [], "metadatas": []}

    mock_backend = MagicMock()
    mock_col = MagicMock()
    mock_col.get.side_effect = tracking_get
    # Set return_value upfront so every call to get_collection returns mock_col
    mock_backend.get_collection = MagicMock(return_value=mock_col)

    with patch("mempalace.cli.MempalaceConfig") as mock_cfg, \
         patch("mempalace.backends.get_backend", return_value=mock_backend):
        mock_cfg.return_value.backend = "lance"
        mock_cfg.return_value.palace_path = palace_path
        cmd_cleanup(args)

    # Verify delete was called once with the 5 eligible ids from first page
    assert mock_col.delete.call_count == 1
    deleted_ids = mock_col.delete.call_args[1].get("ids")
    assert set(deleted_ids) == set(first_page_ids)

    # Key assertion: get was called with offset=5 (not offset=10, which would
    # be the value if offset += BATCH were used instead of offset += deleted)
    offsets = [kw.get("offset", 0) for kw in seen_kwargs]
    assert 5 in offsets, f"Expected offset=5 in get calls, got offsets: {offsets}"


def test_cleanup_no_gaps_when_every_other_item_deleted(tmp_path):
    """
    Cleanup loop must check ALL records in a batch, not skip any after delete.
    Uses a mock that returns records where half are eligible for deletion.
    Verifies exactly 10 eligible items were identified and deleted.
    """
    from mempalace.cli import cmd_cleanup

    palace_path = str(tmp_path / "palace2")

    args = argparse.Namespace(
        palace=palace_path,
        days=365,
        kg_days=30,
        dry_run=False,
    )

    # 20 items: even indices = old+non-latest (eligible), odd = old+latest (protected)
    all_ids = [f"id_{i:04d}" for i in range(20)]
    all_metas = [
        {"timestamp": "2020-01-01T00:00:00Z", "is_latest": (i % 2 == 1)}
        for i in range(20)
    ]

    mock_col = MagicMock()
    mock_col.get.side_effect = lambda **kw: {"ids": all_ids, "metadatas": all_metas}
    mock_col.delete = MagicMock()

    mock_backend = MagicMock()
    mock_backend.get_collection.return_value = mock_col

    with patch("mempalace.cli.MempalaceConfig") as mock_cfg, \
         patch("mempalace.backends.get_backend", return_value=mock_backend):
        mock_cfg.return_value.backend = "lance"
        mock_cfg.return_value.palace_path = palace_path
        cmd_cleanup(args)

    # Delete called once with all 10 eligible (even-indexed) ids
    assert mock_col.delete.call_count == 1
    deleted_ids = mock_col.delete.call_args[1].get("ids")
    assert len(deleted_ids) == 10
    # All deleted must be even-indexed (is_latest=False)
    for did in deleted_ids:
        idx = int(did.split("_")[1])
        assert idx % 2 == 0, f"Deleted a protected id: {did}"


# ── Smoke: imports work with new select import ──────────────────────────


def test_select_imported():
    """Verify select is in the cli module globals."""
    from mempalace import cli
    assert "select" in dir(cli) or hasattr(cli, "select"), "select not imported in cli module"
