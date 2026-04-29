"""
tests/test_plugin_docs_truth.py

Static truth-alignment checks for the Claude Code plugin documentation.
No network, no daemon required.

Checks:
- .mcp.json URL is localhost 8765.
- README says hooks require manual registration.
- README does not mention Chroma support.
- README does not mention Python 3.10/3.11/3.12/3.13 as canonical.
- README does not mention stdio MCP server as canonical.
- README mentions one shared HTTP server.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PLUGIN_ROOT = REPO_ROOT / ".claude-plugin"


class TestMcpConfig:
    def test_mcp_json_url_is_localhost_8765(self):
        """URL must be http://127.0.0.1:8765/mcp."""
        path = PLUGIN_ROOT / ".mcp.json"
        data = json.loads(path.read_text())
        cfg = data.get("mempalace", {})
        assert cfg.get("url") == "http://127.0.0.1:8765/mcp", (
            f"Expected http://127.0.0.1:8765/mcp, got {cfg.get('url')}"
        )
        assert cfg.get("transport") == "http"


class TestReadmeTruth:
    _readme = None

    @classmethod
    def readme(cls):
        if cls._readme is None:
            cls._readme = (PLUGIN_ROOT / "README.md").read_text()
        return cls._readme

    def test_no_chroma_mention(self):
        """README must not reference ChromaDB or chroma as a supported backend."""
        text = self.readme().lower()
        assert "chroma" not in text, "README mentions Chroma — LanceDB is the only supported backend"

    def test_no_python_3_10_through_3_13_canonical(self):
        """
        README must not claim Python 3.10-3.13 as canonical Python versions.
        The package targets Python 3.14 (per pyproject.toml classifiers).
        """
        text = self.readme()
        bad_versions = []
        for v in ["3.10", "3.11", "3.12", "3.13"]:
            if re.search(rf"Python\s+3\.{v}\b", text):
                bad_versions.append(v)
        assert not bad_versions, f"README claims canonical Python {bad_versions} — should not"

    def test_no_stdio_as_canonical(self):
        """
        README must not claim stdio as the canonical MCP transport.
        HTTP (streamable-http on port 8765) is canonical; stdio is fallback/dev-only.
        """
        text = self.readme()
        # Check for "stdio" used as primary recommended transport
        assert not re.search(r"stdio.*canonical|canonical.*stdio", text, re.I), (
            "README calls stdio canonical — HTTP is the canonical multi-session transport"
        )

    def test_one_shared_http_server(self):
        """README must document one shared HTTP server architecture."""
        text = self.readme()
        assert "shared" in text.lower() and "server" in text.lower(), (
            "README must mention shared server architecture"
        )

    def test_hooks_manual_registration_required(self):
        """README must state hooks require manual registration in settings.json."""
        text = self.readme().lower()
        assert "manual registration" in text or "requires manual" in text, (
            "README must explicitly state hooks require manual registration"
        )

    