"""Unit tests for convo_miner pure functions (no chromadb needed)."""

from unittest.mock import MagicMock

from mempalace.convo_miner import (
    chunk_exchanges,
    detect_convo_room,
    mine_convos,
    scan_convos,
)


class TestChunkExchanges:
    def test_exchange_chunking(self):
        content = (
            "> What is memory?\n"
            "Memory is persistence of information over time.\n\n"
            "> Why does it matter?\n"
            "It enables continuity across sessions and conversations.\n\n"
            "> How do we build it?\n"
            "With structured storage and retrieval mechanisms.\n"
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 2
        assert all("content" in c and "chunk_index" in c for c in chunks)

    def test_paragraph_fallback(self):
        """Content without '>' lines falls back to paragraph chunking."""
        content = (
            "This is a long paragraph about memory systems. " * 10 + "\n\n"
            "This is another paragraph about storage. " * 10 + "\n\n"
            "And a third paragraph about retrieval. " * 10
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 2

    def test_paragraph_line_group_fallback(self):
        """Long content with no paragraph breaks chunks by line groups."""
        lines = [f"Line {i}: some content that is meaningful" for i in range(60)]
        content = "\n".join(lines)
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1

    def test_empty_content(self):
        chunks = chunk_exchanges("")
        assert chunks == []

    def test_short_content_skipped(self):
        chunks = chunk_exchanges("> hi\nbye")
        # Too short to produce chunks (below MIN_CHUNK_SIZE)
        assert isinstance(chunks, list)


class TestDetectConvoRoom:
    def test_technical_room(self):
        content = "Let me debug this python function and fix the code error in the api"
        assert detect_convo_room(content) == "technical"

    def test_planning_room(self):
        content = "We need to plan the roadmap for the next sprint and set milestone deadlines"
        assert detect_convo_room(content) == "planning"

    def test_architecture_room(self):
        content = "The architecture uses a service layer with component interface and module design"
        assert detect_convo_room(content) == "architecture"

    def test_decisions_room(self):
        content = "We decided to switch and migrated to the new framework after we chose it"
        assert detect_convo_room(content) == "decisions"

    def test_general_fallback(self):
        content = "Hello, how are you doing today? The weather is nice."
        assert detect_convo_room(content) == "general"


class TestScanConvos:
    def test_scan_finds_txt_and_md(self, tmp_path):
        (tmp_path / "chat.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "notes.md").write_text("world", encoding="utf-8")
        (tmp_path / "image.png").write_bytes(b"fake")
        files = scan_convos(str(tmp_path))
        extensions = {f.suffix for f in files}
        assert ".txt" in extensions
        assert ".md" in extensions
        assert ".png" not in extensions

    def test_scan_skips_git_dir(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config.txt").write_text("git stuff", encoding="utf-8")
        (tmp_path / "chat.txt").write_text("hello", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        assert len(files) == 1

    def test_scan_skips_meta_json(self, tmp_path):
        (tmp_path / "chat.meta.json").write_text("{}", encoding="utf-8")
        (tmp_path / "chat.json").write_text("{}", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]
        assert "chat.json" in names
        assert "chat.meta.json" not in names

    def test_scan_empty_dir(self, tmp_path):
        files = scan_convos(str(tmp_path))
        assert files == []


class TestConvoMinerBatch:
    """Verify mine_convos() batch write includes source_mtime provenance."""

    def test_source_mtime_in_convo_metadata(self, tmp_path, monkeypatch):
        """Each chunk's metadata must carry source_mtime matching the file's mtime."""
        convo_file = tmp_path / "chat.txt"
        convo_file.write_text(
            "> How does memory work?\n"
            "Memory stores information for later retrieval.\n\n"
            "> Why is persistence important?\n"
            "Without persistence, information is lost between sessions.\n\n"
            "> What is the best storage format?\n"
            "Structured formats with metadata enable rich retrieval.\n",
            encoding="utf-8",
        )
        expected_mtime = convo_file.stat().st_mtime

        upsert_calls = []

        def mock_upsert(documents, ids, metadatas):
            upsert_calls.append(list(metadatas))

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": [], "metadatas": []}
        mock_col.upsert = mock_upsert

        mock_backend = MagicMock()
        mock_backend.get_collection.return_value = mock_col

        monkeypatch.setattr("mempalace.convo_miner.get_backend", lambda _: mock_backend)

        mock_cfg = MagicMock()
        mock_cfg.backend = "lance"
        mock_cfg.collection_name = "mempalace_drawers"
        monkeypatch.setattr("mempalace.convo_miner.MempalaceConfig", lambda: mock_cfg)

        mine_convos(str(tmp_path), palace_path=str(tmp_path / "palace"))

        assert len(upsert_calls) == 1, f"Expected 1 upsert, got {len(upsert_calls)}"
        metas = upsert_calls[0]
        assert len(metas) > 0
        for m in metas:
            assert "source_mtime" in m, f"source_mtime missing from convo batch metadata: {m}"
            assert abs(m["source_mtime"] - expected_mtime) < 1

    def test_one_upsert_per_convo_file(self, tmp_path, monkeypatch):
        """Two conversation files must produce exactly 2 upsert calls."""
        for name in ("a.txt", "b.txt"):
            # 3+ "> " lines → exchange chunking; content long enough to pass MIN_CHUNK_SIZE
            (tmp_path / name).write_text(
                "> How does persistent memory work in AI systems?\n"
                "Persistent memory stores information across sessions so that context is retained.\n\n"
                "> Why is provenance tracking important for memory entries?\n"
                "Provenance tracking allows the system to know where each memory came from and when.\n\n"
                "> What metadata should accompany each stored chunk?\n"
                "Each chunk needs source_file, source_mtime, wing, room, and timestamp fields.\n",
                encoding="utf-8",
            )

        upsert_calls = []

        def mock_upsert(documents, ids, metadatas):
            upsert_calls.append(len(documents))

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": [], "metadatas": []}
        mock_col.upsert = mock_upsert

        mock_backend = MagicMock()
        mock_backend.get_collection.return_value = mock_col

        monkeypatch.setattr("mempalace.convo_miner.get_backend", lambda _: mock_backend)

        mock_cfg = MagicMock()
        mock_cfg.backend = "lance"
        mock_cfg.collection_name = "mempalace_drawers"
        monkeypatch.setattr("mempalace.convo_miner.MempalaceConfig", lambda: mock_cfg)

        mine_convos(str(tmp_path), palace_path=str(tmp_path / "palace"))

        assert len(upsert_calls) == 2, f"Expected 2 upserts (1 per file), got {len(upsert_calls)}"
