"""Test embedding error classification logic in lance.py.

Tests for:
- _daemon_is_running protocol shape checks
- _is_per_vector_embedding_error classification
- _embed_texts_resilient systemic vs per-vector behavior
"""
import json
import os
import socket
from unittest.mock import patch, MagicMock

import pytest

from mempalace.backends.lance import (
    _daemon_is_running,
    _embed_texts_resilient,
    _is_per_vector_embedding_error,
    EmbeddingDaemonError,
)
from mempalace.exceptions import MemoryPressureError


class TestDaemonIsRunningProtocol:
    """Protocol shape checks for _daemon_is_running daemon health probe."""

    def test_daemon_is_running_returns_true_for_valid_empty_response(self):
        """Valid empty request/response: {"embeddings": []} -> True."""
        response_data = {"embeddings": []}
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.side_effect = [
            len(json.dumps(response_data)).to_bytes(4, "big"),
            json.dumps(response_data).encode("utf-8"),
        ]

        with patch('socket.socket', return_value=mock_sock), \
             patch('mempalace.backends.lance.os.path.exists', return_value=True):
            result = _daemon_is_running()
            assert result is True
            mock_sock.sendall.assert_called_once()
            sent_data = mock_sock.sendall.call_args[0][0]
            sent_payload = json.loads(sent_data[4:].decode("utf-8"))
            assert sent_payload == {"texts": []}

    def test_daemon_is_running_returns_false_for_malformed_json(self):
        """Response that is not JSON -> False."""
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.side_effect = [
            len(b"not json").to_bytes(4, "big"),
            b"not json",
        ]

        with patch('socket.socket', return_value=mock_sock):
            result = _daemon_is_running()
            assert result is False

    def test_daemon_is_running_returns_false_for_dict_without_embeddings_key(self):
        """Response dict without "embeddings" key -> False."""
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.side_effect = [
            len(json.dumps({"error": None})).to_bytes(4, "big"),
            json.dumps({"error": None}).encode("utf-8"),
        ]

        with patch('socket.socket', return_value=mock_sock):
            result = _daemon_is_running()
            assert result is False

    def test_daemon_is_running_returns_false_for_embeddings_not_list(self):
        """Response where "embeddings" is not a list -> False."""
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.side_effect = [
            len(json.dumps({"embeddings": "string"})).to_bytes(4, "big"),
            json.dumps({"embeddings": "string"}).encode("utf-8"),
        ]

        with patch('socket.socket', return_value=mock_sock):
            result = _daemon_is_running()
            assert result is False

    def test_daemon_is_running_returns_false_for_nonempty_embeddings_on_empty_request(self):
        """Empty request receives non-empty embeddings -> False (protocol violation)."""
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.recv.side_effect = [
            len(json.dumps({"embeddings": [[0.1] * 384]})).to_bytes(4, "big"),
            json.dumps({"embeddings": [[0.1] * 384]}).encode("utf-8"),
        ]

        with patch('socket.socket', return_value=mock_sock):
            result = _daemon_is_running()
            assert result is False


class TestIsPerVectorEmbeddingError:
    """Unit tests for _is_per_vector_embedding_error classification."""

    def test_is_per_vector_embedding_error_degenerate(self):
        """Error with 'degenerate' in message -> True."""
        exc = RuntimeError("Embedding degenerate at position 2")
        assert _is_per_vector_embedding_error(exc) is True

    def test_is_per_vector_embedding_error_dimension(self):
        """Error with 'dimension' in message -> True."""
        exc = RuntimeError("Wrong dimension 384 vs expected 384")
        assert _is_per_vector_embedding_error(exc) is True

    def test_is_per_vector_embedding_error_nan(self):
        """Error with 'nan' in message -> True."""
        exc = RuntimeError("vector contains nan values")
        assert _is_per_vector_embedding_error(exc) is True

    def test_is_per_vector_embedding_error_socket_index(self):
        """Error with '_embed_via_socket[' in message -> True."""
        exc = RuntimeError("Embedding degenerate at _embed_via_socket[2]")
        assert _is_per_vector_embedding_error(exc) is True

    def test_is_per_vector_embedding_error_daemon_unavailable_excluded(self):
        """Error with 'daemon unavailable' -> False (systemic, not per-vector)."""
        exc = RuntimeError("Embedding daemon unavailable")
        assert _is_per_vector_embedding_error(exc) is False

    def test_is_per_vector_embedding_error_socket_excluded(self):
        """Error with 'socket' (but not '_embed_via_socket[') -> False."""
        exc = RuntimeError("socket connection refused")
        assert _is_per_vector_embedding_error(exc) is False

    def test_is_per_vector_embedding_error_timeout_excluded(self):
        """Error with 'timeout' -> False (systemic)."""
        exc = RuntimeError("socket timeout after 5s")
        assert _is_per_vector_embedding_error(exc) is False

    def test_is_per_vector_embedding_error_memory_pressure_excluded(self):
        """Error with 'memory pressure' -> False (systemic)."""
        exc = MemoryPressureError("memory pressure critical")
        assert _is_per_vector_embedding_error(exc) is False


class TestEmbedTextsResilientSystemicVsPerVector:
    """Test _embed_texts_resilient distinguishes systemic from per-vector errors."""

    def setup_method(self):
        os.environ.pop('MEMPALACE_EMBED_FALLBACK', None)

    def teardown_method(self):
        os.environ.pop('MEMPALACE_EMBED_FALLBACK', None)

    def test_embed_texts_resilient_raises_on_embedding_daemon_error_fallback_disabled(self):
        """EmbeddingDaemonError with fallback disabled -> raises immediately."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'

        with patch('mempalace.backends.lance._embed_texts',
                   side_effect=EmbeddingDaemonError("daemon unavailable")):
            with pytest.raises(EmbeddingDaemonError):
                _embed_texts_resilient(["hello", "world"])

    def test_embed_texts_resilient_raises_on_socket_timeout_fallback_disabled(self):
        """Socket timeout with fallback disabled -> raises immediately."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'

        with patch('mempalace.backends.lance._embed_texts',
                   side_effect=RuntimeError("socket timeout")):
            with pytest.raises(RuntimeError):
                _embed_texts_resilient(["hello", "world"])

    def test_embed_texts_resilient_raises_on_memory_pressure_error(self):
        """MemoryPressureError -> raises regardless of fallback setting."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'

        with patch('mempalace.backends.lance._embed_texts',
                   side_effect=MemoryPressureError("memory critical")):
            with pytest.raises(MemoryPressureError):
                _embed_texts_resilient(["hello", "world"])

        os.environ['MEMPALACE_EMBED_FALLBACK'] = '1'

        with patch('mempalace.backends.lance._embed_texts',
                   side_effect=MemoryPressureError("memory critical")):
            with pytest.raises(MemoryPressureError):
                _embed_texts_resilient(["hello", "world"])

    def test_embed_texts_resilient_raises_on_malformed_json_error(self):
        """Malformed JSON error with fallback disabled -> raises."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'

        with patch('mempalace.backends.lance._embed_texts',
                   side_effect=RuntimeError("malformed json response")):
            with pytest.raises(RuntimeError):
                _embed_texts_resilient(["hello", "world"])

    def test_embed_texts_resilient_quarantines_on_degenerate_per_vector_error(self):
        """Per-vector degenerate error quarantines the affected chunk, mining continues."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'

        # _embed_texts raises with a chunk index in the error message
        with patch('mempalace.backends.lance._embed_texts',
                   side_effect=RuntimeError("Embedding degenerate at _embed_via_socket[2]")):
            texts = ["chunk0", "chunk1", "chunk2", "chunk3", "chunk4"]
            valid_texts, _valid_embs, failures, valid_orig_indices = _embed_texts_resilient(
                texts, context="test_degenerate"
            )

            # Chunk 2 should be quarantined, others should be valid
            assert valid_texts == []
            assert valid_orig_indices == []
            assert len(failures) == 1
            assert failures[0]["index"] == 2
            assert "degenerate" in failures[0]["reason"]

    def test_embed_texts_resilient_quarantines_all_chunks_on_unknown_degenerate(self):
        """Degenerate error without chunk index -> all chunks quarantined."""
        os.environ['MEMPALACE_EMBED_FALLBACK'] = '0'

        # Error without chunk index -> all chunks fail
        with patch('mempalace.backends.lance._embed_texts',
                   side_effect=RuntimeError("Embedding degenerate")):
            texts = ["chunk0", "chunk1", "chunk2"]
            valid_texts, _valid_embs, failures, valid_orig_indices = _embed_texts_resilient(
                texts, context="test_unknown_degenerate"
            )

            assert valid_texts == []
            assert valid_orig_indices == []
            # All 3 chunks quarantined
            assert len(failures) == 3
            for f in failures:
                assert f["index"] in [0, 1, 2]
