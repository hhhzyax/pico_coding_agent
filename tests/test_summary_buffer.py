"""SummaryBuffer 和摘要条目测试。"""

from pico.summary_buffer import SummaryBuffer, SummaryEntry


class TestSummaryEntry:
    def test_entry_creation(self):
        entry = SummaryEntry(
            source_range=(3, 5),
            summary="读取 auth.py，发现 validate() 有空值检查缺陷",
            timestamp="2026-06-15T10:30:00Z",
        )
        assert entry.source_range == (3, 5)
        assert "auth.py" in entry.summary
        assert entry.timestamp == "2026-06-15T10:30:00Z"

    def test_entry_to_dict(self):
        entry = SummaryEntry(source_range=(1, 2), summary="test summary")
        d = entry.to_dict()
        assert d["source_range"] == [1, 2]
        assert d["summary"] == "test summary"
        assert "timestamp" in d

    def test_entry_from_dict(self):
        entry = SummaryEntry.from_dict({
            "source_range": [3, 7],
            "summary": "restored",
            "timestamp": "2026-06-15T10:30:00Z",
        })
        assert entry.source_range == (3, 7)
        assert entry.summary == "restored"


class TestSummaryBuffer:
    def test_empty_buffer(self):
        buffer = SummaryBuffer()
        assert len(buffer) == 0
        assert not buffer
        assert buffer.get_all() == []

    def test_append_and_retrieve(self):
        buffer = SummaryBuffer()
        buffer.append(0, 3, "首次工具调用摘要")
        buffer.append(4, 5, "第二次工具调用摘要")

        assert len(buffer) == 2
        assert bool(buffer)

        items = buffer.get_all()
        assert items[0].summary == "首次工具调用摘要"
        assert items[0].source_range == (0, 3)
        assert items[1].summary == "第二次工具调用摘要"

    def test_clear(self):
        buffer = SummaryBuffer()
        buffer.append(0, 1, "摘要一")
        buffer.append(2, 3, "摘要二")
        buffer.clear()
        assert len(buffer) == 0
        assert not buffer

    def test_get_since(self):
        buffer = SummaryBuffer()
        buffer.append(0, 2, "早期摘要")
        buffer.append(3, 5, "中期摘要")
        buffer.append(6, 8, "近期摘要")

        recent = buffer.get_since(2)  # 取出 source_range 起点 > 2 的
        assert len(recent) == 2
        assert recent[0].source_range == (3, 5)
        assert recent[1].source_range == (6, 8)

    def test_get_since_all(self):
        buffer = SummaryBuffer()
        buffer.append(1, 2, "唯一摘要")
        recent = buffer.get_since(-1)
        assert len(recent) == 1

    def test_summary_text(self):
        buffer = SummaryBuffer()
        assert "empty" in buffer.summary_text()

        buffer.append(2, 4, "读取 auth.py")
        text = buffer.summary_text()
        assert "auth.py" in text
        assert "[2-4]" in text

    def test_max_entries_limit(self):
        buffer = SummaryBuffer(max_entries=5)
        for i in range(10):
            buffer.append(i, i + 1, f"摘要 {i}")
        assert len(buffer) == 5
        # 应保留最新的 5 条
        assert buffer.get_all()[-1].summary == "摘要 9"
        assert buffer.get_all()[0].summary == "摘要 5"


class TestMemoryPipelineSummaryExtraction:
    def test_extract_summary_normal(self):
        from pico.memory_pipeline import MemoryPipeline
        text = "auth.py 包含 login() 和 validate()，validate() 在处理空 email 时会返回 None 而不是抛异常。"
        summary = MemoryPipeline._extract_summary(text)
        assert summary is not None
        assert "auth.py" in summary

    def test_extract_summary_too_short(self):
        from pico.memory_pipeline import MemoryPipeline
        text = "OK"  # 太短
        summary = MemoryPipeline._extract_summary(text)
        assert summary is None

    def test_extract_summary_empty(self):
        from pico.memory_pipeline import MemoryPipeline
        summary = MemoryPipeline._extract_summary("")
        assert summary is None

    def test_extract_summary_skips_tool_tags(self):
        from pico.memory_pipeline import MemoryPipeline
        # After removing <tool> tags, meaningful content should be extracted
        # skipping tool invocation lines like "read_file" and "src/main.py"
        text = "<tool>read_file\nsrc/main.py</tool>\n\nThe main.py file contains a critical null pointer exception in the validate function that needs immediate fix"
        summary = MemoryPipeline._extract_summary(text)
        assert summary is not None
        assert "null pointer" in summary
