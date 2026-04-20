"""
Memory pressure monitor for Apple Silicon M1 8GB.
Monitoruje paměťový tlak a adaptivně omejuje operace.
"""
import subprocess
import threading
import time
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class MemoryPressure(Enum):
    NOMINAL = "nominal"   # <70% RAM – vše normálně
    WARN    = "warn"      # 70-85% – ztlum batch mining
    CRITICAL = "critical" # >85% – pausuj zápisy, jen čtení


def _get_memory_pressure_macos() -> tuple[MemoryPressure, float]:
    """
    Čte memory pressure přes 'memory_pressure' CLI tool (macOS).
    Vrátí (pressure_level, used_ratio).
    """
    try:
        result = subprocess.run(
            ["memory_pressure"],
            capture_output=True, text=True, timeout=2
        )
        output = result.stdout.lower()

        # Parsuj "System-wide memory free percentage: X%"
        for line in output.splitlines():
            if "free percentage" in line:
                pct_str = line.split(":")[-1].strip().rstrip("%")
                free_pct = float(pct_str)
                used_ratio = 1.0 - (free_pct / 100.0)

                if "critical" in output:
                    return MemoryPressure.CRITICAL, used_ratio
                elif "warn" in output or used_ratio > 0.85:
                    return MemoryPressure.WARN, used_ratio
                else:
                    return MemoryPressure.NOMINAL, used_ratio
    except Exception:
        pass

    # Fallback: psutil
    try:
        import psutil
        vm = psutil.virtual_memory()
        ratio = vm.percent / 100.0
        if ratio > 0.90:
            return MemoryPressure.CRITICAL, ratio
        elif ratio > 0.80:
            return MemoryPressure.WARN, ratio
        else:
            return MemoryPressure.NOMINAL, ratio
    except ImportError:
        return MemoryPressure.NOMINAL, 0.0


class MemoryGuard:
    """
    Singleton který monitoruje memory pressure v pozadí.
    Komponenty (daemon, MCP server) se ho ptají před těžkými operacemi.
    """

    _instance = None
    _lock = threading.Lock()
    _started = threading.Event()
    # Class-level _stop ensures all instances share the same stop signal.
    # Using instance-level _stop caused old monitor threads to miss the stop
    # signal when a new instance was created via get() after stop().
    _stop = threading.Event()

    def __init__(self, check_interval: float = 10.0):
        self._pressure = MemoryPressure.NOMINAL
        self._used_ratio = 0.0
        self._interval = check_interval
        # Bind _monitor_loop to self while the thread is alive so the correct
        # pressure/used_ratio are updated regardless of how many restarts occur.
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        # Wait for first measurement before returning so callers always see
        # a fully initialized instance (not just the NOMINAL default).
        self._started.wait(timeout=5.0)
        logger.info("MemoryGuard started (check interval: %ss)", check_interval)

    @classmethod
    def get(cls) -> "MemoryGuard":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            # Hold the lock until first measurement completes so callers
            # always see a fully initialized instance (not just NOMINAL default).
            cls._started.wait(timeout=5.0)
        return cls._instance

    @property
    def pressure(self) -> MemoryPressure:
        return self._pressure

    @property
    def used_ratio(self) -> float:
        return self._used_ratio

    def should_pause_writes(self) -> bool:
        return self._pressure == MemoryPressure.CRITICAL

    def should_throttle(self) -> bool:
        return self._pressure in (MemoryPressure.WARN, MemoryPressure.CRITICAL)

    def wait_for_nominal(self, timeout: float = 30.0) -> bool:
        """
        Blokuje dokud není memory pressure nominal nebo timeout.
        Vrátí True pokud nominal, False pokud timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._pressure == MemoryPressure.NOMINAL:
                return True
            time.sleep(1.0)
        return False

    def _monitor_loop(self):
        # První měření hned — nečekej na interval
        pressure, ratio = _get_memory_pressure_macos()
        self._pressure = pressure
        self._used_ratio = ratio
        self._started.set()

        while not self._stop.wait(self._interval):
            pressure, ratio = _get_memory_pressure_macos()

            if pressure != self._pressure:
                logger.warning(
                    "Memory pressure changed: %s → %s (%.1f%% used)",
                    self._pressure.value, pressure.value, ratio * 100
                )

            self._pressure = pressure
            self._used_ratio = ratio

    def stop(self) -> None:
        """
        Stop the background monitor thread and reset singleton state.

        After stop() a subsequent get() will block until the new instance's
        first measurement completes (fresh-start semantics).
        """
        self._stop.set()
        with type(self)._lock:
            type(self)._instance = None
            # Reset _started so the next get() blocks until first measurement.
            # Without this, a subsequent get() after stop() returns immediately
            # (since _started was already set by the previous instance) without
            # waiting for the new instance's first measurement.
            type(self)._started.clear()
            # Create a fresh _stop event so that when the next get() creates
            # a new instance, its _monitor_loop blocks on a clean event (not
            # one that is already set by this stop() call).
            type(self)._stop = threading.Event()
        # Use self._thread directly — no lock needed for join target resolution.
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("MemoryGuard stopped")