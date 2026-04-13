"""Tests for mempalace.hooks.pre_compact."""

import inspect


def test_pre_compact_default_port():
    """Default MEMPALACE_URL must point to port 8765, not 8766."""
    from mempalace.hooks import pre_compact
    src = inspect.getsource(pre_compact)
    assert "8765" in src, "Default MCP URL must use port 8765"
    assert "8766" not in src, "Default MCP URL must not use port 8766"
