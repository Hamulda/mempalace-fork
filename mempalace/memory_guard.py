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

    def __init__(self, check_interval: float = 10.0):
        self._pressure = MemoryPressure.NOMINAL
        self._used_ratio = 0.0
        self._interval = check_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True
        )
        self._thread.start()
        self._started.wait(timeout=5.0)  # wait for first measurement before returning
        logger.info("MemoryGuard started (check interval: %ss)", check_interval)

    @classmethod
    def get(cls) -> "MemoryGuard":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
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