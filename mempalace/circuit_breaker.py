import threading
import time
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class _State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class EmbedCircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = _State.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> _State:
        with self._lock:
            if self._state == _State.OPEN:
                if time.monotonic() - self._opened_at >= self._recovery_timeout:
                    self._state = _State.HALF_OPEN
                    logger.info("EmbedCircuitBreaker: OPEN → HALF_OPEN (testing daemon)")
            return self._state

    def record_success(self) -> None:
        with self._lock:
            if self._state in (_State.HALF_OPEN, _State.CLOSED):
                self._failures = 0
                if self._state == _State.HALF_OPEN:
                    self._state = _State.CLOSED
                    logger.info("EmbedCircuitBreaker: HALF_OPEN → CLOSED (daemon healthy)")

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == _State.HALF_OPEN:
                self._state = _State.OPEN
                self._opened_at = time.monotonic()
                logger.warning("EmbedCircuitBreaker: HALF_OPEN → OPEN (daemon still down)")
            elif self._failures >= self._threshold:
                self._state = _State.OPEN
                self._opened_at = time.monotonic()
                logger.warning("EmbedCircuitBreaker: CLOSED → OPEN (%d failures)", self._failures)

    def should_try_socket(self) -> bool:
        return self.state != _State.OPEN

    def status(self) -> dict:
        s = self.state
        return {
            "state": s.value,
            "failures": self._failures,
            "recovery_in": (
                max(0, self._recovery_timeout - (time.monotonic() - self._opened_at))
                if s == _State.OPEN else 0
            ),
        }


_embed_circuit = EmbedCircuitBreaker(failure_threshold=5, recovery_timeout=30.0)