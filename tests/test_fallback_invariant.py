"""Test fallback invariant: MEMPALACE_EMBED_FALLBACK=0 must hard-block fallback paths."""
import pytest
import os
from unittest.mock import patch, MagicMock

from mempalace.backends.lance import (
    _embed_fallback_enabled,
    _embed_texts,
    _embed_texts_fallback,
    EmbeddingDaemonError,
)


class TestFallbackInvariant:
    """Verify that MEMPALACE_EMBED_FALLBACK=0 enforces hard blocks."""

    def setup_method(self):
        os.environ.pop('MEMPALACE_EMBED_FALLBACK', None)

    def teardown_method(self):
        os.environ.pop('MEMPALACE_EMBED_FALLBACK', None)

    def test_socket_failure_with_fallback_disabled_raises(self):
        """Socket failure + MEMPALACE_EMBED_FALLBACK=0 -> raises, fallback NOT called."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'
        assert _embed_fallback_enabled() is False

        with patch('mempalace.backends.lance._start_daemon_if_needed', return_value=True), \
             patch('mempalace.backends.lance._embed_via_socket',
                   side_effect=RuntimeError("mock socket failure")), \
             patch('mempalace.backends.lance._embed_texts_fallback') as fb:
            with pytest.raises(EmbeddingDaemonError):
                _embed_texts(["hello"])
            assert fb.call_count == 0, "fallback must not be called when disabled"

    def test_socket_failure_with_fallback_disabled_no_using_fallback_log(self, caplog):
        """Socket failure + MEMPALACE_EMBED_FALLBACK=0 -> logs must NOT contain 'using fallback'."""
        import logging
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'

        with patch('mempalace.backends.lance._start_daemon_if_needed', return_value=True), \
             patch('mempalace.backends.lance._embed_via_socket',
                   side_effect=RuntimeError("mock socket failure")), \
             patch('mempalace.backends.lance._embed_texts_fallback'):
            try:
                _embed_texts(["hello"])
            except EmbeddingDaemonError:
                pass

        for record in caplog.records:
            assert "using fallback" not in record.message.lower(), \
                f"log should not say 'using fallback' when disabled: {record.message}"

    def test_socket_failure_with_fallback_enabled_calls_fallback(self):
        """Socket failure + MEMPALACE_EMBED_FALLBACK=1 -> fallback is called."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '1'
        assert _embed_fallback_enabled() is True

        mock_fb = MagicMock(return_value=[[0.1] * 256])
        with patch('mempalace.backends.lance._start_daemon_if_needed', return_value=True), \
             patch('mempalace.backends.lance._embed_via_socket',
                   side_effect=RuntimeError("mock socket failure")), \
             patch('mempalace.backends.lance._embed_texts_fallback', mock_fb):
            result = _embed_texts(["hello"])
            assert mock_fb.call_count == 1, "fallback must be called when enabled"

    def test_direct_fallback_with_env_0_raises(self):
        """Direct _embed_texts_fallback() with env=0 -> raises RuntimeError."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'
        assert _embed_fallback_enabled() is False

        with pytest.raises(RuntimeError, match="in-process fallback is disabled"):
            _embed_texts_fallback(["hello"])

    def test_direct_fallback_with_env_1_proceeds(self):
        """Direct _embed_texts_fallback() with env=1 -> guard passes (model may not load in test env)."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '1'
        assert _embed_fallback_enabled() is True

        with patch('mempalace.backends.lance._embed_fallback_enabled', return_value=True):
            try:
                _embed_texts_fallback(["hello"])
            except RuntimeError as e:
                assert "disabled" not in str(e).lower(), \
                    f"should not raise disabled guard when env=1: {e}"
            except Exception:
                pass  # model not found, memory pressure — acceptable in test env


class TestFallbackEnabledGuard:
    """Test that _embed_fallback_enabled() correctly reports state."""

    def setup_method(self):
        os.environ.pop('MEMPALACE_EMBED_FALLBACK', None)

    def teardown_method(self):
        os.environ.pop('MEMPALACE_EMBED_FALLBACK', None)

    def test_default_is_enabled(self):
        os.environ.pop('MEMPALACE_EMBED_FALLBACK', None)
        assert _embed_fallback_enabled() is True

    def test_0_disables(self):
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'
        assert _embed_fallback_enabled() is False

    def test_1_enables(self):
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '1'
        assert _embed_fallback_enabled() is True

    def test_false_disables(self):
        os.environ['MEMPALACE_EMBED_FALLBACK'] = 'false'
        assert _embed_fallback_enabled() is False

    def test_true_enables(self):
        os.environ['MEMPALACE_EMBED_FALLBACK'] = 'true'
        assert _embed_fallback_enabled() is True
