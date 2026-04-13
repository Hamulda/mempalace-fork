import time
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mempalace.mcp_server import _MetaCache

def test_cache_hit():
    cache = _MetaCache(ttl=10.0)
    cache.set("wings", [{"wing": "test"}])
    result = cache.get("wings")
    assert result == [{"wing": "test"}]

def test_cache_miss_after_ttl():
    cache = _MetaCache(ttl=0.05)
    cache.set("key", "value")
    time.sleep(0.1)
    assert cache.get("key") is None

def test_invalidate():
    cache = _MetaCache(ttl=30.0)
    cache.set("wings", ["w"])
    cache.set("taxonomy", {"w": []})
    cache.invalidate()
    assert cache.get("wings") is None
    assert cache.get("taxonomy") is None

def test_invalidate_prefix():
    cache = _MetaCache(ttl=30.0)
    cache.set("rooms", ["r"])
    cache.set("rooms:wing1", ["r1"])
    cache.set("rooms:wing2", ["r2"])
    cache.invalidate("rooms:wing1")
    assert cache.get("rooms") == ["r"]
    assert cache.get("rooms:wing1") is None
    assert cache.get("rooms:wing2") == ["r2"]