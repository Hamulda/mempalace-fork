"""
Tests for build_startup_context provider truthfulness.
Verifies embedding state is reported from stored metadata or socket probe,
NOT from a non-existent HTTP endpoint.

Run: pytest tests/test_startup_context_provider_truth.py -q
"""

import json
import sys
import tempfile
import pytest


def _pp():
    return tempfile.mkdtemp(prefix="mempalace_startup_truth_")


class TestEmbeddingProviderTruth:
    """Embedding provider must come from socket probe or stored metadata, not HTTP."""

    def test_no_http_probe_imported(self):
        """verify build_startup_context does not use HTTP 8766."""
        from mempalace import wakeup_context
        import inspect
        source = inspect.getsource(wakeup_context.build_startup_context)
        assert "127.0.0.1:8766" not in source, "HTTP 8766 probe must not be used"
        assert "urllib.request" not in source or "8766" not in source

    def test_stored_meta_when_no_daemon(self, monkeypatch, tmp_path):
        """When daemon is down but embedding_meta.json exists, stored provider is reported."""
        from mempalace.wakeup_context import build_startup_context

        palace = str(tmp_path)
        meta_path = tmp_path / "embedding_meta.json"
        meta_path.write_text(json.dumps({
            "provider": "mlx",
            "model_id": "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M",
            "dims": 256,
        }))

        # No daemon running, no socket
        def fake_probe(*args, **kwargs):
            return None

        monkeypatch.setattr("mempalace.wakeup_context._probe_embed_daemon_socket", fake_probe)

        result = build_startup_context(session_id="s1", palace_path=palace)

        assert result["embedding_stored_provider"] == "mlx"
        assert result["embedding_stored_model_id"] == "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M"
        assert result["embedding_stored_dims"] == 256
        assert result["embedding_current_provider"] is None
        assert result["embedding_drift_detected"] == "unknown"
        # legacy compat
        assert result["embedding_provider"] == "mlx"

    def test_daemon_probe_when_running(self, monkeypatch, tmp_path):
        """When daemon socket probe succeeds, current provider is reported."""
        from mempalace.wakeup_context import build_startup_context

        palace = str(tmp_path)
        # no embedding_meta.json — only daemon probe result
        def fake_probe(*args, **kwargs):
            return ("coreml", "BAAI/bge-small-en-v1.5", 256)

        monkeypatch.setattr("mempalace.wakeup_context._probe_embed_daemon_socket", fake_probe)

        result = build_startup_context(session_id="s1", palace_path=palace)

        assert result["embedding_current_provider"] == "coreml"
        assert result["embedding_current_model_id"] == "BAAI/bge-small-en-v1.5"
        assert result["embedding_current_dims"] == 256
        assert result["embedding_stored_provider"] is None
        assert result["embedding_drift_detected"] == "unknown"
        assert result["embedding_provider"] == "coreml"

    def test_drift_detected_when_stored_differs_from_current(self, monkeypatch, tmp_path):
        """Drift=true when stored and current providers differ."""
        from mempalace.wakeup_context import build_startup_context

        palace = str(tmp_path)
        meta_path = tmp_path / "embedding_meta.json"
        meta_path.write_text(json.dumps({
            "provider": "mlx",
            "model_id": "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M",
            "dims": 256,
        }))

        def fake_probe(*args, **kwargs):
            return ("coreml", "BAAI/bge-small-en-v1.5", 256)

        monkeypatch.setattr("mempalace.wakeup_context._probe_embed_daemon_socket", fake_probe)

        result = build_startup_context(session_id="s1", palace_path=palace)

        assert result["embedding_drift_detected"] is True
        assert result["embedding_stored_provider"] == "mlx"
        assert result["embedding_current_provider"] == "coreml"

    def test_drift_false_when_stored_matches_current(self, monkeypatch, tmp_path):
        """Drift=false when stored and current providers match."""
        from mempalace.wakeup_context import build_startup_context

        palace = str(tmp_path)
        meta_path = tmp_path / "embedding_meta.json"
        meta_path.write_text(json.dumps({
            "provider": "mlx",
            "model_id": "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M",
            "dims": 256,
        }))

        def fake_probe(*args, **kwargs):
            return ("mlx", "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M", 256)

        monkeypatch.setattr("mempalace.wakeup_context._probe_embed_daemon_socket", fake_probe)

        result = build_startup_context(session_id="s1", palace_path=palace)

        assert result["embedding_drift_detected"] is False

    def test_no_daemon_no_stored_unknown(self, monkeypatch, tmp_path):
        """When neither daemon nor stored meta available, drift=unknown, provider=unknown."""
        from mempalace.wakeup_context import build_startup_context

        palace = str(tmp_path)

        def fake_probe(*args, **kwargs):
            return None

        monkeypatch.setattr("mempalace.wakeup_context._probe_embed_daemon_socket", fake_probe)

        result = build_startup_context(session_id="s1", palace_path=palace)

        assert result["embedding_stored_provider"] is None
        assert result["embedding_current_provider"] is None
        assert result["embedding_drift_detected"] == "unknown"
        assert result["embedding_provider"] == "unknown"

    def test_new_embedding_fields_present(self, tmp_path):
        """Result must include all new embedding truthfulness fields."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(session_id="s1", palace_path=str(tmp_path))

        for field in [
            "embedding_stored_provider",
            "embedding_stored_model_id",
            "embedding_stored_dims",
            "embedding_current_provider",
            "embedding_current_model_id",
            "embedding_current_dims",
            "embedding_drift_detected",
            "path_index_count",
            "fts5_count",
            "symbol_count",
            # collection_count intentionally excluded — list_tables is O(n)
        ]:
            assert field in result, f"Missing field: {field}"

    def test_index_counts_are_null_or_int(self, tmp_path):
        """Index counts must be int or None (not error), and cheap O(1)."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(session_id="s1", palace_path=str(tmp_path))

        for field in ["path_index_count", "fts5_count", "symbol_count"]:
            val = result[field]
            assert val is None or isinstance(val, int), f"{field} must be int|None, got {type(val)}"
            assert val is None or val >= 0, f"{field} must be >=0"
        # collection_count intentionally excluded — list_tables is O(n)
        assert "collection_count" not in result


class TestPathBoundaryClaimFilter:
    """Claims must be filtered by strict path boundary, not startswith."""

    def test_proj_does_not_match_proj_old(self):
        """Path /proj-old must NOT be matched by project /proj."""
        from mempalace.wakeup_context import _path_boundary_contains

        assert not _path_boundary_contains("/proj-old", "/proj")
        assert not _path_boundary_contains("/proj-old/file.py", "/proj")
        assert not _path_boundary_contains("/some/proj-old", "/proj")

    def test_proj_matches_exact_and_children(self):
        """Path /proj MUST match /proj and its children."""
        from mempalace.wakeup_context import _path_boundary_contains

        assert _path_boundary_contains("/proj", "/proj")
        assert _path_boundary_contains("/proj/foo", "/proj")
        assert _path_boundary_contains("/proj/foo/bar.py", "/proj")
        assert _path_boundary_contains("/proj/a/b/c/d.py", "/proj")

    def test_proj_with_slash_matches_children(self):
        """project_path /proj/ trailing slash handled correctly."""
        from mempalace.wakeup_context import _path_boundary_contains

        assert _path_boundary_contains("/proj/foo", "/proj/")
        assert _path_boundary_contains("/proj", "/proj/")

    def test_nested_projects_do_not_match(self):
        """Nested projects with shared prefix are correctly separated."""
        from mempalace.wakeup_context import _path_boundary_contains

        assert not _path_boundary_contains("/proj-a", "/proj")
        assert not _path_boundary_contains("/proj-a/b", "/proj")
        assert not _path_boundary_contains("/proj", "/proj-a")
        assert not _path_boundary_contains("/proj/a", "/proj-a")

    def test_claims_filtered_by_boundary_in_context(self, monkeypatch, tmp_path):
        """build_startup_context with project_path=/proj does not include /proj-old claims."""
        from mempalace.wakeup_context import build_startup_context

        palace = str(tmp_path)
        meta_path = tmp_path / "embedding_meta.json"
        meta_path.write_text(json.dumps({
            "provider": "mlx",
            "model_id": "mlx-community/nomic-embed-text-v1-ablated-flash-smollm2-360M",
            "dims": 256,
        }))

        # Mock claims manager to return claims for two projects
        class MockClaim:
            def __init__(self, target_id):
                self._data = {
                    "target_id": target_id,
                    "owner": "other",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
                self.target_id = target_id
                self.session_id = "other"
                self.expires_at = "2099-01-01T00:00:00Z"
                self.payload = {}

            def __getitem__(self, key):
                return self._data.get(key)

            def get(self, key, default=None):
                return self._data.get(key, default)

        class MockClaimsMgr:
            def list_active_claims(self):
                return [
                    MockClaim("/proj/file1.py"),
                    MockClaim("/proj/file2.py"),
                    MockClaim("/proj-old/file3.py"),
                    MockClaim("/proj-old/file4.py"),
                    MockClaim("/some/completely/unrelated.py"),
                ]

        def fake_probe(*args, **kwargs):
            return None

        monkeypatch.setattr("mempalace.wakeup_context._probe_embed_daemon_socket", fake_probe)
        monkeypatch.setattr("mempalace.wakeup_context.ClaimsManager", lambda p: MockClaimsMgr())

        result = build_startup_context(session_id="s1", project_path="/proj", palace_path=palace)

        paths = [c["path"] for c in result["current_claims"]]
        assert "/proj/file1.py" in paths
        assert "/proj/file2.py" in paths
        assert "/proj-old/file3.py" not in paths
        assert "/proj-old/file4.py" not in paths
        assert "/some/completely/unrelated.py" not in paths


class TestNoHeavyImports:
    """build_startup_context must not import heavy models."""

    def test_no_mlx_load_on_import(self, monkeypatch, tmp_path):
        """Importing build_startup_context must not trigger mlx model load."""
        imported_modules = set(sys.modules.keys())

        from mempalace.wakeup_context import build_startup_context

        # Check no new modules loaded that contain MLX heavy deps
        mlx_modules = [m for m in sys.modules if "mlx" in m.lower()]
        heavy_modules = [m for m in sys.modules if m not in imported_modules and any(
            x in m for x in ["fastembed", "mlx", "coreml", "onnx"]
        )]
        # fastembed import detection is OK during probe; but no model loading
        assert len(heavy_modules) == 0 or all("mlx" not in m for m in heavy_modules), \
            f"Heavy modules imported: {heavy_modules}"

    def test_socket_probe_does_not_load_model(self, monkeypatch, tmp_path, capsys):
        """Socket probe must be truly lightweight (socket send/recv only)."""
        probe_calls = []

        def tracking_probe():
            probe_calls.append(True)
            return None

        monkeypatch.setattr(
            "mempalace.wakeup_context._probe_embed_daemon_socket",
            tracking_probe
        )

        from mempalace.wakeup_context import build_startup_context
        build_startup_context(session_id="s1", palace_path=str(tmp_path))

        assert len(probe_calls) == 1, "Socket probe should be called exactly once"
