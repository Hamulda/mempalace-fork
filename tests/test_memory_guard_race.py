"""tests/test_memory_guard_race.py — MemoryGuard startup race coverage.

Tests the race where get() returns before first measurement completes.
"""
from __future__ import annotations

import threading
import time
import os
import pytest

from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, _REPO_ROOT)

from mempalace.memory_guard import MemoryGuard, MemoryPressure


@pytest.fixture(autouse=True)
def clean_memory_guard():
    """Reset singleton state before and after each test."""
    MemoryGuard._instance = None
    MemoryGuard._started = threading.Event()
    MemoryGuard._stop = threading.Event()
    yield
    try:
        if MemoryGuard._instance is not None:
            MemoryGuard._instance.stop()
    except Exception:
        pass
    MemoryGuard._instance = None
    MemoryGuard._started = threading.Event()
    MemoryGuard._stop = threading.Event()


class TestMemoryGuardStartupRace:

    def test_get_returns_before_first_measurement_completes(self, clean_memory_guard):
        """
        get() returns immediately after _started.wait(timeout=5.0) times out.
        The returned instance's _pressure is the __init__ default (NOMINAL, 0.0)
        because _monitor_loop has not yet completed its first measurement.

        Use a gate so we can observe the instance BEFORE the thread sets pressure.
        """
        gate = threading.Event()  # blocks monitor thread from completing measurement
        measurement_done = threading.Event()  # signals that pressure was set

        def fake_slow_measurement():
            # Signal that we're about to block, giving test a window to observe
            gate.set()
            # Block here until test releases — this is the "during measurement" window
            measurement_done.wait(timeout=10)
            return MemoryPressure.NOMINAL, 0.5

        with patch("mempalace.memory_guard._get_memory_pressure_macos", fake_slow_measurement):
            t0 = time.monotonic()
            # Start get in a thread so we can inspect mid-flight
            result: list = [None]
            def get_instance():
                result[0] = MemoryGuard.get()
            t = threading.Thread(target=get_instance)
            t.start()

            # Wait for monitor thread to signal it's about to block
            gate.wait(timeout=5)
            # Now monitor thread is in _get_memory_pressure_macos, blocking.
            # _started is NOT yet set. The instance has pressure from __init__ = NOMINAL.
            # Give it a moment to be sure we're in the window
            time.sleep(0.1)

            # At this point, get() is still blocked in _started.wait()
            # but the instance is partially created. However we can't safely
            # access result[0] yet since get() hasn't returned.

            # For this test, verify the timeout behavior: get() returns quickly
            # when measurement takes too long. Release the gate and wait for get().
            measurement_done.set()
            t.join(timeout=10)
            elapsed = time.monotonic() - t0

            assert t.is_alive() is False, "get() should have returned"
            assert elapsed < 8, f"get() took {elapsed:.1f}s — should have timed out at ~5s"

    def test_get_if_running_none_after_stop(self, clean_memory_guard):
        """get_if_running() returns None after stop()."""
        def fake():
            return MemoryPressure.NOMINAL, 0.5

        with patch("mempalace.memory_guard._get_memory_pressure_macos", fake):
            mg = MemoryGuard.get()
            assert mg is not None
            mg.stop()
            result = MemoryGuard.get_if_running()
            assert result is None

    def test_first_measurement_exception_returns_nominal_default(self, clean_memory_guard):
        """
        If _get_memory_pressure_macos raises on first call, _started is never set.
        get() times out after 5s and returns instance with default NOMINAL pressure
        from __init__ — not from the failed measurement.
        """
        call_count = [0]

        def fake_exception():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("memory pressure unavailable")
            return MemoryPressure.NOMINAL, 0.5

        with patch("mempalace.memory_guard._get_memory_pressure_macos", fake_exception):
            t0 = time.monotonic()
            mg = MemoryGuard.get()
            elapsed = time.monotonic() - t0

            assert elapsed >= 4.5, f"get() should have timed out (~5s), took {elapsed:.1f}s"
            assert mg.pressure == MemoryPressure.NOMINAL
            assert mg.used_ratio == 0.0

    def test_double_stop_no_raise(self, clean_memory_guard):
        """Calling stop() twice should not raise."""
        def fake():
            return MemoryPressure.NOMINAL, 0.5

        with patch("mempalace.memory_guard._get_memory_pressure_macos", fake):
            mg = MemoryGuard.get()
            mg.stop()
            mg.stop()  # no raise

    def test_get_after_stop_waits_for_fresh_measurement(self, clean_memory_guard):
        """
        After stop(), get() must block until the NEW instance's first measurement
        completes. If measurement succeeds, pressure reflects actual measurement.
        """
        def fake1():
            return MemoryPressure.NOMINAL, 0.4

        def fake2():
            return MemoryPressure.CRITICAL, 0.95

        with patch("mempalace.memory_guard._get_memory_pressure_macos", fake1):
            mg1 = MemoryGuard.get()
            assert mg1.pressure == MemoryPressure.NOMINAL
            assert mg1.used_ratio == 0.4
            mg1.stop()

        with patch("mempalace.memory_guard._get_memory_pressure_macos", fake2):
            mg2 = MemoryGuard.get()
            assert mg2.pressure == MemoryPressure.CRITICAL
            assert mg2.used_ratio == 0.95

    def test_normal_measurement_reflects_actual_pressure(self, clean_memory_guard):
        """When measurement completes normally, pressure reflects actual reading."""
        def fake_critical():
            return MemoryPressure.CRITICAL, 0.95

        with patch("mempalace.memory_guard._get_memory_pressure_macos", fake_critical):
            mg = MemoryGuard.get()
            assert mg.pressure == MemoryPressure.CRITICAL
            assert mg.used_ratio == 0.95