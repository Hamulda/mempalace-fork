"""
Tests for mempalace_startup_context and build_startup_context.
Run: pytest tests/test_startup_context.py -q
"""

import os
import sys
import tempfile
import pytest


def _pp():
    return tempfile.mkdtemp(prefix="mempalace_startup_")


class TestBuildStartupContext:
    """Unit tests for build_startup_context()."""

    def test_returns_compact_dict(self):
        """Returns a dict with all required top-level keys."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            project_path=None,
            palace_path=_pp(),
            limit=5,
        )

        required_keys = [
            "server_health",
            "palace_path",
            "backend",
            "python_version",
            "embedding_provider",
            "embedding_meta",
            "active_sessions",
            "current_claims",
            "pending_handoffs",
            "recommended_first_actions",
            "project_path_reminder",
            "m1_defaults",
        ]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

    def test_backend_is_lance(self):
        """Backend must be 'lance' — ChromaDB is not supported."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert result["backend"] == "lance"

    def test_python_version_present(self):
        """python_version field must be a valid version string."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        ver = result["python_version"]
        # Must be "3.14" or "3.14.x"
        assert ver.startswith("3.14"), f"Expected Python 3.14.x, got {ver}"

    def test_no_chroma_in_sys_modules(self, monkeypatch):
        """Verify no Chroma modules loaded after import."""
        # Fresh import check — Chroma should never be in sys.modules
        assert "chromadb" not in sys.modules
        assert "chroma" not in sys.modules

    def test_m1_defaults_bounded(self):
        """m1_defaults must contain bounded values, not huge numbers."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        m1 = result["m1_defaults"]
        assert m1["max_batch"] <= 32
        assert m1["embed_batch_default"] <= 64
        assert m1["query_cache_ttl"] == 300
        assert m1["claim_timeout_seconds"] == 60
        assert m1["session_timeout_seconds"] == 300

    def test_project_path_reminder_present(self):
        """project_path_reminder must be in the result."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            project_path="/my/project/root",
            palace_path=_pp(),
        )
        assert "project_path_reminder" in result
        assert result["project_path_reminder"] == "/my/project/root"

    def test_current_claims_list(self):
        """current_claims must be a list (empty when no claims)."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert isinstance(result["current_claims"], list)

    def test_pending_handoffs_list(self):
        """pending_handoffs must be a list (empty when no handoffs)."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert isinstance(result["pending_handoffs"], list)

    def test_recommended_first_actions_not_empty(self):
        """recommended_first_actions must be a non-empty list."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert isinstance(result["recommended_first_actions"], list)
        assert len(result["recommended_first_actions"]) > 0
        # Each action must have action, reason, priority
        for action in result["recommended_first_actions"]:
            assert "action" in action
            assert "reason" in action
            assert "priority" in action

    def test_active_sessions_is_int(self):
        """active_sessions must be an integer count."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert isinstance(result["active_sessions"], int)
        assert result["active_sessions"] >= 0

    def test_embedding_provider_is_string(self):
        """embedding_provider must be a string (unknown is ok when daemon is down)."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert isinstance(result["embedding_provider"], str)

    def test_embedding_meta_is_dict(self):
        """embedding_meta must be a dict (empty when no data)."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert isinstance(result["embedding_meta"], dict)

    def test_server_health_has_status(self):
        """server_health must contain a status field."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert "status" in result["server_health"]

    def test_limit_parameter_respected(self):
        """limit parameter caps pending_handoffs count."""
        from mempalace.wakeup_context import build_startup_context

        # With limit=1, should not return more than 1 handoff
        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
            limit=1,
        )
        assert result["pending_handoffs_count"] <= 1

    def test_current_claims_count_int(self):
        """current_claims_count must be an int."""
        from mempalace.wakeup_context import build_startup_context

        result = build_startup_context(
            session_id="test-session-001",
            palace_path=_pp(),
        )
        assert isinstance(result["current_claims_count"], int)
        assert result["current_claims_count"] == len(result["current_claims"])


class TestNoChromaInvariant:
    """Verify ChromaDB is never loaded as part of normal operation."""

    def test_no_chroma_module_imported(self):
        """No chroma/chromadb modules should be in sys.modules."""
        blocked = [k for k in sys.modules if "chroma" in k.lower()]
        assert len(blocked) == 0, f"Chroma modules loaded: {blocked}"
