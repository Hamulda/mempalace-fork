"""
tests/test_readme_private_truth.py

Static truth-alignment checks for the main README.
No network, no daemon required.

Checks:
- README mentions Python 3.14 only.
- README mentions LanceDB only.
- README does not mention ChromaDB as a supported backend.
- README does not mention Python 3.9+ minimum.
- README does not recommend stdio for Claude Code.
- README mentions shared HTTP server at 127.0.0.1:8765/mcp.
- README points to .claude-plugin README for hook registration truth.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


class TestReadmePrivateTruth:
    _readme = None

    @classmethod
    def readme(cls):
        if cls._readme is None:
            cls._readme = (REPO_ROOT / "README.md").read_text()
        return cls._readme

    def test_python_314_mentioned(self):
        """README must reference Python 3.14 as the runtime version."""
        text = self.readme()
        assert "3.14" in text, "README must mention Python 3.14"

    def test_lancedb_only(self):
        """README must state LanceDB is the only supported backend."""
        text = self.readme().lower()
        assert "lancedb" in text, "README must mention LanceDB"
        # Should not have any Chroma as primary/alternative backend claim
        assert "chroma" not in text or "removed" in text, (
            "README must not claim ChromaDB as a supported backend"
        )

    def test_no_chroma_as_backend(self):
        """README must not mention ChromaDB as a supported storage backend."""
        text = self.readme().lower()
        # Allow in the private fork truth section (which explains removal)
        # But no claim that it's a working backend
        lines = text.split("\n")
        for line in lines:
            if "chroma" in line and "removed" not in line and "private fork" not in line.lower():
                # Chroma mentioned outside of "removed" context — likely a claim
                assert False, f"README mentions Chroma as a working backend: {line.strip()}"

    def test_no_python_39_minimum(self):
        """README must not claim Python 3.9+ as the minimum version."""
        text = self.readme()
        assert not re.search(r"Python\s+3\.9\+", text), (
            "README claims Python 3.9+ minimum — should be 3.14"
        )

    def test_no_stdio_recommended_for_claude_code(self):
        """README must not recommend stdio as the canonical MCP transport for Claude Code."""
        text = self.readme()
        # stdio is fallback/dev-only; HTTP is canonical
        assert not re.search(r"stdio.*recommended|recommended.*stdio", text, re.I), (
            "README recommends stdio for Claude Code — HTTP is canonical"
        )

    def test_shared_http_server_127_0_0_1_8765(self):
        """README must document the shared HTTP server at 127.0.0.1:8765/mcp."""
        text = self.readme()
        assert "127.0.0.1:8765" in text, (
            "README must mention shared HTTP server at 127.0.0.1:8765"
        )
        assert "/mcp" in text, "README must mention /mcp endpoint"

    def test_hooks_registration_pointed_to_plugin_readme(self):
        """README must direct users to .claude-plugin/README.md for hook registration truth."""
        text = self.readme()
        assert ".claude-plugin/README.md" in text, (
            "README must reference .claude-plugin/README.md for hook registration"
        )