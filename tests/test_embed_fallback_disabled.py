"""Test for daemon-only mining mode (MEMPALACE_EMBED_FALLBACK)."""
import pytest
import os

from mempalace.backends.lance import _embed_fallback_enabled


class TestEmbedFallbackEnabled:
    """Test _embed_fallback_enabled() behavior."""

    def setup_method(self):
        # Clean env before each test
        os.environ.pop('MEMPALACE_EMBED_FALLBACK', None)

    def test_default_is_true(self):
        """Default (not set) should enable fallback."""
        assert _embed_fallback_enabled() is True

    def test_0_disables(self):
        """'0' should disable fallback."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'
        assert _embed_fallback_enabled() is False

    def test_false_disables(self):
        """'false' should disable fallback."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = 'false'
        assert _embed_fallback_enabled() is False

    def test_no_disables(self):
        """'no' should disable fallback."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = 'no'
        assert _embed_fallback_enabled() is False

    def test_off_disables(self):
        """'off' should disable fallback."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = 'off'
        assert _embed_fallback_enabled() is False

    def test_1_enables(self):
        """'1' should enable fallback."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '1'
        assert _embed_fallback_enabled() is True

    def test_true_enables(self):
        """'true' should enable fallback (same as '1')."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = 'true'
        assert _embed_fallback_enabled() is True

    def test_explicit_1_overrides_mining_default(self):
        """Explicit MEMPALACE_EMBED_FALLBACK=1 overrides mining's default of 0."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '1'
        assert _embed_fallback_enabled() is True
