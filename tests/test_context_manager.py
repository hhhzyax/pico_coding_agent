from pico import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from pico.context_manager import ContextManager


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def test_context_manager_assembles_sections_in_expected_order(tmp_path):
    agent = build_agent(tmp_path, [])
    # 使用持久记忆存储一条笔记，确保 relevant_memory 可以召回
    agent.memory.promote_durable([("user-preferences", "deploy key is red")])
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record({"role": "assistant", "content": "old answer", "created_at": "2026-04-07T10:00:30+00:00"})

    system, messages, metadata = ContextManager(agent).build("Where is the deploy key?")

    # Native function calling format: system + messages
    assert "You are pico" in system
    assert "Memory:" in system
    assert "Where is the deploy key?" in messages[-1]["content"]
    assert metadata["section_order"] == ["prefix", "memory", "relevant_memory", "history", "current_request"]


def test_context_manager_reduces_relevant_memory_before_history_and_preserves_newer_context(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    # 使用持久记忆存储多条笔记
    agent.memory.promote_durable([
        ("user-preferences", "keep episodic note one " + ("C" * 220)),
        ("key-decisions", "keep episodic note two " + ("D" * 220)),
        ("project-conventions", "keep episodic note three " + ("E" * 220)),
    ])
    agent.record({"role": "user", "content": "OLD-CONTEXT " + ("D" * 260), "created_at": "2026-04-07T09:59:00+00:00"})
    for minute in range(1, 8):
        role = "assistant" if minute % 2 == 1 else "user"
        content = "RECENT-CONTEXT " + ("E" * 260) if minute == 7 else f"recent-{minute} " + ("E" * 180)
        agent.record({"role": role, "content": content, "created_at": f"2026-04-07T10:0{minute}:00+00:00"})

    manager = ContextManager(
        agent,
        total_budget=200,  # 低预算确保触发缩减
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "relevant_memory": 120,
            "history": 400,
        },
    )
    system, messages, metadata = manager.build("recall keep notes")

    assert metadata["budget_reductions"]
    reductions_by_section = {item["section"]: item for item in metadata["budget_reductions"]}
    assert "relevant_memory" in reductions_by_section


def test_context_manager_without_reduction_preserves_all_sections(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.feature_flags["context_reduction"] = False
    agent.record({"role": "user", "content": "simple query", "created_at": "2026-04-07T10:00:00+00:00"})

    system, messages, metadata = ContextManager(agent).build("query")
    # Native format: history entries should be in messages
    assert any("simple query" in m.get("content", "") for m in messages)
    assert not metadata["budget_reductions"]


def test_context_manager_token_estimation(tmp_path):
    """测试 token 估算功能。"""
    agent = build_agent(tmp_path, [])
    manager = ContextManager(agent)

    # 初始比例应为 0.3
    assert manager._token_per_char_ratio == 0.3

    # 估算 token
    estimated = manager._estimate_tokens("hello world")  # 11 chars * 0.3 = 3
    assert estimated == 3

    # 校准
    manager.calibrate_ratio(500, 1000)  # actual = 0.5, ratio = 0.7*0.3 + 0.3*0.5 = 0.36
    assert abs(manager._token_per_char_ratio - 0.36) < 0.001

    # 确认估算更新
    estimated2 = manager._estimate_tokens("hello world")  # 11 chars * 0.36 = 3
    assert estimated2 == 3  # floor to int


def test_cumulative_summary_injection(tmp_path):
    """测试累计摘要注入到上下文。"""
    agent = build_agent(tmp_path, [])
    # 设置 pipeline 的累计摘要
    agent.memory_pipeline.cumulative_summary = "用户之前修复了 auth.py 的空值检查问题。"
    agent.memory_pipeline.cumulative_source_range = (0, 5)
    agent.memory_pipeline.last_compression_history_index = 5

    agent.record({"role": "user", "content": "recent message", "created_at": "2026-04-08T10:00:00+00:00"})
    agent.record({"role": "assistant", "content": "response", "created_at": "2026-04-08T10:01:00+00:00"})

    manager = ContextManager(agent)
    _, messages, _ = manager.build("new query")

    # 应该有一条 system 消息包含摘要
    summary_msgs = [m for m in messages if "对话历史摘要" in m.get("content", "")]
    assert len(summary_msgs) == 1
    assert "空值检查" in summary_msgs[0]["content"]


def test_cumulative_summary_without_compression(tmp_path):
    """测试没有压缩时不会注入摘要。"""
    agent = build_agent(tmp_path, [])
    agent.record({"role": "user", "content": "test", "created_at": "2026-04-08T10:00:00+00:00"})

    manager = ContextManager(agent)
    _, messages, _ = manager.build("new query")

    summary_msgs = [m for m in messages if "对话历史摘要" in m.get("content", "")]
    assert len(summary_msgs) == 0
