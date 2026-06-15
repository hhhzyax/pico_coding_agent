"""摘要压缩器测试。"""

from pico.summary_buffer import SummaryEntry
from pico.summary_compressor import (
    build_compression_prompt,
    template_merge,
    compress,
    _clean_llm_output,
)


class TestBuildCompressionPrompt:
    def test_first_compression_prompt(self):
        items = [
            SummaryEntry(source_range=(0, 2), summary="读取 auth.py，发现空值问题"),
            SummaryEntry(source_range=(3, 4), summary="运行 pytest，test_login 失败"),
        ]
        prompt = build_compression_prompt(None, items)
        assert "首次压缩" in prompt or "无" in prompt
        assert "auth.py" in prompt
        assert "test_login" in prompt
        assert "[0-2]" in prompt

    def test_subsequent_compression_prompt(self):
        items = [SummaryEntry(source_range=(5, 7), summary="修复了 validate() 函数")]
        prompt = build_compression_prompt("之前修复了空值检查", items)
        assert "之前修复了空值检查" in prompt
        assert "validate()" in prompt


class TestTemplateMerge:
    def test_merge_without_previous(self):
        items = [
            SummaryEntry(source_range=(0, 2), summary="读取 auth.py"),
            SummaryEntry(source_range=(3, 4), summary="测试失败"),
        ]
        merged, new_range = template_merge(None, items)
        assert "auth.py" in merged
        assert "测试失败" in merged
        assert new_range == (0, 4)

    def test_merge_with_previous(self):
        items = [SummaryEntry(source_range=(5, 6), summary="修复完成")]
        merged, new_range = template_merge("已读取 auth.py；测试失败", items)
        assert merged.startswith("已读取 auth.py；测试失败")
        assert "修复完成" in merged
        assert new_range == (5, 6)

    def test_merge_empty_items(self):
        merged, new_range = template_merge("已有摘要", [])
        assert merged == "已有摘要"
        assert new_range == (0, 0)
        assert new_range == (0, 0)


class TestCompress:
    def test_compress_without_client(self):
        items = [
            SummaryEntry(source_range=(0, 1), summary="调查了问题"),
            SummaryEntry(source_range=(2, 3), summary="找到了根因"),
        ]
        merged, new_range = compress(None, None, None, items)
        assert "调查了问题" in merged
        assert "找到了根因" in merged
        assert new_range == (0, 3)

    def test_compress_with_previous(self):
        items = [SummaryEntry(source_range=(5, 6), summary="新发现")]
        merged, new_range = compress(
            None, "之前的摘要内容", (0, 4), items
        )
        # 无 LLM 时使用模板合并
        assert "之前的摘要内容" in merged
        assert "新发现" in merged
        assert new_range == (0, 6)


class TestCleanLLMOutput:
    def test_clean_markdown_wrapper(self):
        assert _clean_llm_output("```\nhello\n```") == "hello"

    def test_clean_text_prefix(self):
        assert _clean_llm_output("```text\nworld\n```") == "world"

    def test_clean_no_wrapper(self):
        assert _clean_llm_output("plain text") == "plain text"

    def test_clean_whitespace(self):
        assert _clean_llm_output("  trimmed  ") == "trimmed"
