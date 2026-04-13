import time
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mempalace.circuit_breaker import EmbedCircuitBreaker, _State

def test_opens_after_threshold():
    cb = EmbedCircuitBreaker(failure_threshold=3, recovery_timeout=0.1)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == _State.OPEN
    assert not cb.should_try_socket()

def test_half_open_after_timeout():
    cb = EmbedCircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
    cb.record_failure()
    time.sleep(0.1)
    assert cb.state == _State.HALF_OPEN
    assert cb.should_try_socket()

def test_closes_after_success_in_half_open():
    cb = EmbedCircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
    cb.record_failure()
    time.sleep(0.1)
    # First access triggers OPEN -> HALF_OPEN transition
    _ = cb.state
    cb.record_success()
    # Second access confirms HALF_OPEN -> CLOSED transition
    assert cb.state == _State.CLOSED