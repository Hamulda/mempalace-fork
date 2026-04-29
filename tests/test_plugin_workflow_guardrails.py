"""
tests/test_plugin_workflow_guardrails.py

Static truth-alignment checks for plugin workflow documentation.
No network, no daemon required.

Checks:
- workflow docs mention project_path requirement.
- workflow docs mention file_context project_path/allowed roots security.
- workflow docs mention one shared HTTP server.
- workflow docs do not mention Chroma support.
- workflow docs do not mention Python <3.14 as canonical.
- workflow docs mention M1 bounded defaults (limit, no large exports during coding).
- pre-edit checklist section exists in codebase-rag-workflow.md.
"""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).parent.parent
PLUGIN_ROOT = REPO_ROOT / ".claude-plugin"


class TestWorkflowDocsProjectPath:
    """Workflow docs must guide Claude Code to always pass project_path."""

    @classmethod
    def workflow_doc(cls):
        return (PLUGIN_ROOT / "skills" / "mempalace" / "codebase-rag-workflow.md").read_text()

    def test_project_context_mentioned(self):
        """codebase-rag-workflow.md mentions mempalace_project_context."""
        assert "mempalace_project_context" in self.workflow_doc()

    def test_project_path_in_search_rules(self):
        """Search rules mention project_path is required for repo-scoped work."""
        text = self.workflow_doc()
        assert re.search(r"project_path", text), (
            "codebase-rag-workflow.md must mention project_path in search rules"
        )

    def test_search_code_requires_project_path(self):
        """mempalace_search_code docs state project_path is required."""
        help_md = (PLUGIN_ROOT / "commands" / "help.md").read_text()
        assert re.search(r"project_path.*search_code|search_code.*project_path", help_md), (
            "help.md must mention project_path for mempalace_search_code"
        )


class TestFileContextSecurityDocs:
    """file_context security model must be documented in workflow docs."""

    @classmethod
    def workflow_doc(cls):
        return (PLUGIN_ROOT / "skills" / "mempalace" / "codebase-rag-workflow.md").read_text()

    def test_file_context_project_path_or_allowed_roots(self):
        """file_context usage requires project_path or allowed_roots documented."""
        text = self.workflow_doc()
        assert re.search(r"project_path.*file_context|file_context.*project_path", text), (
            "codebase-rag-workflow.md must document file_context requires project_path or allowed_roots"
        )

    def test_file_context_security_section(self):
        """A file_context security section exists and explains defaults."""
        text = self.workflow_doc()
        assert "file_context" in text and ("security" in text.lower() or "allowed_roots" in text), (
            "codebase-rag-workflow.md must document file_context security model"
        )


class TestSharedHttpServer:
    """Workflow docs must describe the single shared HTTP server architecture."""

    def test_shared_server_in_workflow_doc(self):
        """codebase-rag-workflow.md mentions shared MemPalace server."""
        text = (PLUGIN_ROOT / "skills" / "mempalace" / "codebase-rag-workflow.md").read_text()
        assert "shared" in text.lower() and "server" in text.lower(), (
            "codebase-rag-workflow.md must document shared server architecture"
        )

    def test_no_chroma_as_supported_backend(self):
        """Workflow docs must not claim Chroma is a supported backend."""
        for path in (
            PLUGIN_ROOT / "skills" / "mempalace" / "codebase-rag-workflow.md",
            PLUGIN_ROOT / "commands" / "help.md",
            PLUGIN_ROOT / "commands" / "status.md",
            PLUGIN_ROOT / "commands" / "mine.md",
            PLUGIN_ROOT / "commands" / "init.md",
        ):
            text = path.read_text().lower()
            assert "chroma" not in text, f"{path.name} mentions Chroma — LanceDB only"

    def test_no_chroma_as_backend_in_doctor(self):
        """doctor.md may reference Chroma only in diagnostic checks, not as supported."""
        doc = (PLUGIN_ROOT / "commands" / "doctor.md").read_text()
        # Allowed references (diagnostic output / symptom descriptions):
        #   - "Chroma NOT Imported" / "Chroma not loaded" / "chroma: clean" — check output
        #   - "Backend shows `chroma` | ... | Only LanceDB is supported — Chroma is not supported" — table row
        # Not allowed: recommendations, suggestions, or "switch to chroma"
        import re
        lines = doc.split('\n')
        problematic = []
        for line in lines:
            stripped = line.strip()
            # Skip frontmatter description
            if stripped.startswith('description:'):
                continue
            # Skip diagnostic output lines (not imported/not loaded/chroma clean/warning)
            if re.search(r'not\s*imported|not\s*loaded|chroma.*clean|chroma.*warning', stripped, re.I):
                continue
            # Skip markdown table rows that say Chroma is "not supported"
            if '|' in stripped and 'not supported' in stripped.lower():
                continue
            if 'chroma' in stripped.lower():
                problematic.append(stripped)
        assert not problematic, (
            f"doctor.md has non-diagnostic Chroma references: {problematic}"
        )


class TestPython314Target:
    """Plugin docs must not claim Python <3.14 as canonical."""

    def test_readme_no_old_python_versions(self):
        """README must not claim Python 3.10-3.13 as canonical."""
        readme = (PLUGIN_ROOT / "README.md").read_text()
        bad = []
        for v in ["3.10", "3.11", "3.12", "3.13"]:
            if re.search(rf"Python\s+3\.{v}\b", readme):
                bad.append(v)
        assert not bad, f"README claims canonical Python {bad} — should target 3.14"

    def test_preconditions_section_targets_314(self):
        """Preconditions section mentions Python 3.14 target."""
        readme = (PLUGIN_ROOT / "README.md").read_text()
        assert re.search(r"3\.14", readme), "README should mention Python 3.14 target"


class TestM1BoundedDefaults:
    """Workflow docs must mention M1 8GB bounded defaults."""

    @classmethod
    def workflow_doc(cls):
        return (PLUGIN_ROOT / "skills" / "mempalace" / "codebase-rag-workflow.md").read_text()

    def test_m1_limit_bounded(self):
        """M1 rules mention bounded limit (5-20) on searches."""
        text = self.workflow_doc()
        assert re.search(r"limit.*\d+|bounded.*limit", text), (
            "codebase-rag-workflow.md must mention bounded limit for M1"
        )

    def test_m1_no_large_exports_during_coding(self):
        """M1 rules warn against large exports during active coding."""
        text = self.workflow_doc()
        assert re.search(r"large.*export|export.*large|mining.*active|active.*mining", text), (
            "codebase-rag-workflow.md must warn against large exports during coding on M1"
        )


class TestPreEditChecklist:
    """codebase-rag-workflow.md must have a pre-edit checklist section."""

    @classmethod
    def workflow_doc(cls):
        return (PLUGIN_ROOT / "skills" / "mempalace" / "codebase-rag-workflow.md").read_text()

    def test_checklist_section_exists(self):
        """Pre-edit checklist section exists."""
        text = self.workflow_doc()
        assert re.search(r"pre.?edit.*checklist|checklist.*pre.?edit", text, re.I), (
            "codebase-rag-workflow.md must have a 'Pre-Edit Checklist' section"
        )

    def test_checklist_mentions_begin_work(self):
        """Checklist mentions mempalace_begin_work as first editing step."""
        text = self.workflow_doc()
        assert "mempalace_begin_work" in text, (
            "Pre-edit checklist must mention mempalace_begin_work"
        )

    def test_checklist_mentions_finish_work(self):
        """Checklist mentions mempalace_finish_work as final step."""
        text = self.workflow_doc()
        assert "mempalace_finish_work" in text, (
            "Pre-edit checklist must mention mempalace_finish_work"
        )

    def test_checklist_mentions_claim_prepare_edit(self):
        """Checklist mentions mempalace_prepare_edit before editing."""
        text = self.workflow_doc()
        assert "mempalace_prepare_edit" in text, (
            "Pre-edit checklist must mention mempalace_prepare_edit"
        )

    def test_checklist_mentions_tests(self):
        """Checklist mentions running tests before finishing."""
        text = self.workflow_doc()
        assert re.search(r"test|run", text), (
            "Pre-edit checklist must mention running tests"
        )