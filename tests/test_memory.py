"""简化版记忆系统的测试。

工作记忆（working、episodic_notes、file_summaries）已由
MemoryPipeline / SummaryBuffer 管理，LayeredMemory 现在仅管理持久记忆。
"""

import pytest
from pico.memory import (
    LayeredMemory,
    DurableMemoryStore,
    default_memory_state,
    render_memory_text,
    retrieval_candidates,
    canonicalize_path,
    file_freshness,
    _tokenize,
)


class TestDefaultState:
    def test_default_memory_state_is_minimal(self):
        state = default_memory_state()
        assert "durable_topics" in state
        assert state["durable_topics"] == []


class TestDurableMemoryStore:
    def test_empty_store_returns_empty_topics(self, tmp_path):
        store = DurableMemoryStore(tmp_path / ".pico" / "memory")
        assert store.topic_slugs() == []
        assert store.load_index() == []

    def test_promote_and_retrieve(self, tmp_path):
        store = DurableMemoryStore(tmp_path / ".pico" / "memory")
        promoted, _ = store.promote([("key-decisions", "Use pytest for testing")])
        assert "key-decisions: Use pytest for testing" in promoted
        assert store.topic_slugs() == ["key-decisions"]

        candidates = store.retrieval_candidates("pytest testing", limit=3)
        assert len(candidates) >= 1

    def test_promote_deduplicates(self, tmp_path):
        store = DurableMemoryStore(tmp_path / ".pico" / "memory")
        store.promote([("user-preferences", "Prefers tab indentation")])
        promoted, superseded = store.promote([("user-preferences", "Prefers tab indentation")])
        assert promoted == []
        assert superseded == []

    def test_subject_key_replacement(self, tmp_path):
        store = DurableMemoryStore(tmp_path / ".pico" / "memory")
        # _subject_key 使用 "is" 和 "uses" 等模式匹配
        store.promote([("user-preferences", "The framework is FastAPI")])
        promoted, superseded = store.promote([("user-preferences", "The framework is FastAPI and more")])
        assert len(superseded) >= 1


class TestLayeredMemory:
    def test_simplified_memory_has_durable_only(self):
        memory = LayeredMemory()
        snapshot = memory.to_dict()
        assert "durable_topics" in snapshot
        # 旧字段不应出现
        assert "working" not in snapshot
        assert "episodic_notes" not in snapshot
        assert "file_summaries" not in snapshot
        assert "task" not in snapshot
        assert "files" not in snapshot
        assert "notes" not in snapshot

    def test_render_memory_text(self):
        memory = LayeredMemory()
        text = memory.render_memory_text()
        assert "Memory:" in text
        assert "durable_topics" in text

    def test_promote_durable_works(self, tmp_path):
        memory = LayeredMemory(workspace_root=tmp_path)
        promoted, superseded = memory.promote_durable([
            ("key-decisions", "Use PostgreSQL as primary database")
        ])
        assert len(promoted) >= 1
        snapshot = memory.to_dict()
        assert "key-decisions" in snapshot["durable_topics"]

    def test_retrieval_from_durable(self, tmp_path):
        memory = LayeredMemory(workspace_root=tmp_path)
        memory.promote_durable([("key-decisions", "Use async/await for all I/O")])
        candidates = memory.retrieval_candidates("async await I/O", limit=3)
        assert len(candidates) >= 1

    def test_retrieval_view(self, tmp_path):
        memory = LayeredMemory(workspace_root=tmp_path)
        memory.promote_durable([("user-preferences", "CI uses GitHub Actions on push")])
        view = memory.retrieval_view("GitHub Actions CI")
        assert "GitHub Actions" in view


class TestRenderMemoryText:
    def test_renders_empty_state(self):
        state = default_memory_state()
        text = render_memory_text(state)
        assert "durable_topics: -" in text

    def test_renders_with_topics(self):
        state = {"durable_topics": ["key-decisions", "user-preferences"]}
        text = render_memory_text(state)
        assert "key-decisions" in text
        assert "user-preferences" in text

    def test_handles_invalid_state(self):
        assert "invalid state" in render_memory_text(123)


class TestUtilityFunctions:
    def test_canonicalize_path(self, tmp_path):
        file_path = tmp_path / "src" / "app.py"
        file_path.parent.mkdir(exist_ok=True)
        file_path.write_text("print('hello')")
        result = canonicalize_path(file_path, tmp_path)
        assert result == "src/app.py"

    def test_file_freshness(self, tmp_path):
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello")
        f1 = file_freshness(file_path, tmp_path)
        file_path.write_text("world")
        f2 = file_freshness(file_path, tmp_path)
        assert f1 is not None
        assert f2 is not None
        assert f1 != f2

    def test_tokenize(self):
        tokens = _tokenize("Hello World 42 test_abc")
        assert "hello" in tokens
        assert "world" in tokens
        assert "42" in tokens
        assert "test_abc" in tokens
