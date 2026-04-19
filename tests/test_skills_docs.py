"""
test_skills_docs.py -- Tests for skills/ documentation and memory_protocol.
"""

from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).parent.parent / "mempalace" / "skills"


class TestAllSkillsExist:
    def test_all_skills_md_present(self):
        """All expected MD files exist in skills/."""
        expected = ["help.md", "init.md", "mine.md", "search.md", "status.md", "memory_protocol.md"]
        for name in expected:
            assert (SKILLS_DIR / name).exists(), f"Missing: {name}"


class TestHelpMdNewTools:
    def test_help_md_mentions_new_tools(self):
        """help.md mentions hybrid_search and kg_supersede."""
        content = (SKILLS_DIR / "help.md").read_text()
        assert "hybrid_search" in content, "help.md should mention hybrid_search"
        assert "kg_supersede" in content, "help.md should mention kg_supersede"


class TestMemoryProtocolSessionStart:
    def test_memory_protocol_has_session_start(self):
        """memory_protocol.md mentions mempalace_status (session start step)."""
        content = (SKILLS_DIR / "memory_protocol.md").read_text()
        assert "mempalace_status" in content, "memory_protocol.md should mention mempalace_status"


class TestSkillsDirectoryResourceLoads:
    def test_create_server_loads_skills(self, tmp_path):
        """create_server() loads without exception and skills directory is accessible."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))

        from mempalace.fastmcp_server import create_server
        from mempalace.settings import MemPalaceSettings

        test_settings = MemPalaceSettings(
            db_path=str(tmp_path / "palace"),
            db_backend="chroma",
        )

        # Should not raise
        server = create_server(settings=test_settings)
        assert server is not None

        # Skills directory must be readable and non-empty
        skills_path = Path(__file__).parent.parent / "mempalace" / "skills"
        assert skills_path.exists(), "skills/ directory must exist"
        md_files = list(skills_path.glob("*.md"))
        assert len(md_files) >= 5, "skills/ should have at least 5 .md files"

    @pytest.mark.asyncio
    async def test_skills_resource_registered(self, tmp_path):
        """FastMCP server has palace_skills DirectoryResource registered."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))

        from mempalace.fastmcp_server import create_server
        from mempalace.settings import MemPalaceSettings

        test_settings = MemPalaceSettings(
            db_path=str(tmp_path / "palace"),
            db_backend="chroma",
        )
        server = create_server(settings=test_settings)

        resources = await server.list_resources()
        resources_by_name = {r.name: r for r in resources}
        assert "palace_skills" in resources_by_name, (
            f"palace_skills resource not found. Available: {list(resources_by_name.keys())}"
        )

        skill_res = resources_by_name["palace_skills"]
        assert "skills" in str(skill_res.path), (
            f"palace_skills path does not point to skills/ dir: {skill_res.path}"
        )
