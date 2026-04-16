"""
Tests for MemoryGuard memory pressure monitoring.

Run: pytest tests/test_memory_guard.py -v -s
"""

import time
import pytest
from unittest import mock

from mempalace.memory_guard import (
    MemoryGuard,
    MemoryPressure,
    _get_memory_pressure_macos,
)


class TestMemoryPressure:
    def test_nominal_is_known(self):
        """MemoryPressure has nominal value."""
        assert MemoryPressure.NOMINAL.value == "nominal"

    def test_get_memory_pressure_returns_tuple(self):
        """_get_memory_pressure_macos returns (pressure, ratio)."""
        pressure, ratio = _get_memory_pressure_macos()
        assert isinstance(pressure, MemoryPressure)
        assert 0.0 <= ratio <= 1.0

    def test_guard_singleton(self):
        """MemoryGuard is a singleton."""
        guard1 = MemoryGuard.get()
        guard2 = MemoryGuard.get()
        assert guard1 is guard2

    def test_nominal_allows_writes(self):
        """Při nominal pressure should_pause_writes je False."""
        guard = MemoryGuard.get()
        guard._pressure = MemoryPressure.NOMINAL
        assert guard.should_pause_writes() is False
        assert guard.should_throttle() is False

    def test_critical_pressure_blocks_writes(self):
        """Při critical pressure should_pause_writes je True."""
        guard = MemoryGuard.get()
        guard._pressure = MemoryPressure.CRITICAL
        assert guard.should_pause_writes() is True

    def test_warn_pressure_throttles(self):
        """Při warn pressure should_throttle je True."""
        guard = MemoryGuard.get()
        guard._pressure = MemoryPressure.WARN
        assert guard.should_throttle() is True
        assert guard.should_pause_writes() is False

    def test_wait_for_nominal_returns_true_when_nominal(self):
        """wait_for_nominal vrací True pokud pressure hned nominal."""
        guard = MemoryGuard.get()
        guard._pressure = MemoryPressure.NOMINAL
        result = guard.wait_for_nominal(timeout=1.0)
        assert result is True

    def test_wait_for_nominal_times_out(self):
        """wait_for_nominal vrací False po timeoutu."""
        guard = MemoryGuard.get()
        guard._pressure = MemoryPressure.CRITICAL

        start = time.monotonic()
        result = guard.wait_for_nominal(timeout=0.5)
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed >= 0.5

    def test_pressure_parsing_warn(self):
        """_get_memory_pressure_macos správně parsuje warn z memory_pressure output."""
        mock_output = """
            System-wide memory free percentage: 25%
            Memory pressure: FILEIO_WARNNING
        """

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(stdout=mock_output, returncode=0)
            pressure, ratio = _get_memory_pressure_macos()

            assert pressure == MemoryPressure.WARN
            assert ratio == 0.75

    def test_pressure_parsing_critical(self):
        """_get_memory_pressure_macos správně parsuje critical."""
        mock_output = """
            System-wide memory free percentage: 10%
            Memory pressure: MEMORY_PRESSURE_CRITICAL
        """

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(stdout=mock_output, returncode=0)
            pressure, ratio = _get_memory_pressure_macos()

            assert pressure == MemoryPressure.CRITICAL
            assert ratio == 0.90

    def test_psutil_fallback(self):
        """Pokud memory_pressure CLI selže, použije se psutil fallback."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with mock.patch("psutil.virtual_memory") as mock_vm:
                mock_vm.return_value = mock.Mock(percent=85.0)
                pressure, ratio = _get_memory_pressure_macos()

                assert pressure == MemoryPressure.WARN
                assert ratio == 0.85

    def test_get_blocks_until_first_measurement(self):
        """
        get() vrací až po prvním reálném měření — ne implicitní NOMINAL.
        Tento test opravuje startup blind spot kde druhý thread mohl dostat
        instanci s _pressure=NOMINAL před _monitor_loop nastavil _started.
        """
        # Reset singleton — začínáme s čistým stavem
        MemoryGuard._instance = None
        MemoryGuard._started.clear()

        with mock.patch(
            "mempalace.memory_guard._get_memory_pressure_macos",
            return_value=(MemoryPressure.WARN, 0.75),
        ):
            guard = MemoryGuard.get()
            # Po return z get() musí být known state (ne implicitní NOMINAL)
            assert guard.pressure == MemoryPressure.WARN
            assert guard.used_ratio == 0.75
            # _started musí být set — další volání get() neblokuje
            assert MemoryGuard._started.is_set()

        # Úklid — obnov singleton pro ostatní testy
        MemoryGuard._instance = None
        MemoryGuard._started.clear()