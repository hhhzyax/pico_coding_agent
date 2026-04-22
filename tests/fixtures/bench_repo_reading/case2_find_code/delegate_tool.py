"""子 Agent 委派工具实现。

这个模块实现了父子 agent 机制：
- 父 agent 通过 delegate 工具创建子 agent
- 子 agent 在独立的上下文中执行任务
- 子 agent 只返回摘要给父 agent
"""

import json
from pathlib import Path
from .runtime import Pico
from .workspace import WorkspaceContext
from .run_store import RunStore


def tool_delegate(parent_agent, args):
    """创建子 agent 执行子任务。
    
    这是实现父子 agent 的核心函数。子 agent 会：
    1. 继承父 agent 的工作区
    2. 使用全新的 session（空 history）
    3. 以只读模式运行（不能修改文件）
    4. 返回任务摘要给父 agent
    
    Args:
        parent_agent: 父 agent 实例
        args: {"task": "子任务描述", "max_steps": 3}
    
    Returns:
        子 agent 的执行结果摘要
    """
    task = args.get("task", "")
    max_steps = int(args.get("max_steps", 3))
    
    # 检查深度限制，防止无限递归
    if parent_agent.depth >= parent_agent.max_depth:
        return "error: delegate depth limit reached"
    
    # 创建子 agent
    child = Pico(
        model_client=parent_agent.model_client,
        workspace=parent_agent.workspace,
        session_store=parent_agent.session_store,
        approval_policy="auto",  # 子 agent 自动批准工具调用
        max_steps=max_steps,
        max_new_tokens=parent_agent.max_new_tokens,
        depth=parent_agent.depth + 1,  # 深度 +1
        max_depth=parent_agent.max_depth,
        read_only=True,  # 子 agent 只读，不能修改文件
    )
    
    # 执行子任务
    result = child.ask(task)
    
    # 返回摘要（只返回关键信息，不返回完整历史）
    summary = {
        "task": task,
        "result": result,
        "tool_steps": child.session["history"].count(lambda x: x["role"] == "tool"),
    }
    
    return json.dumps(summary, ensure_ascii=False, indent=2)


# 工具定义
DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}


# 使用示例
TOOL_EXAMPLES = {
    "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
}


def build_tool_registry(agent):
    """构建工具注册表，根据深度决定是否暴露 delegate 工具。
    
    子 agent 是刻意做成受限能力的：一旦深度耗尽，
    就连 delegate 这个工具都不再暴露给模型。
    """
    from functools import partial
    
    tools = {
        # ... 其他基础工具
    }
    
    # 只有未达到最大深度时才暴露 delegate 工具
    if agent.depth < agent.max_depth:
        tools["delegate"] = {
            **DELEGATE_TOOL_SPEC,
            "run": partial(tool_delegate, agent),
        }
    
    return tools


class SubagentExample:
    """父子 agent 使用示例。
    
    场景：父 agent 需要分析一个大型代码库
    
    父 agent: "帮我分析这个项目的架构"
        ↓
    调用 delegate 创建子 agent1: "分析 models.py 的类结构"
    调用 delegate 创建子 agent2: "分析 tools.py 的工具函数"
    调用 delegate 创建子 agent3: "分析 runtime.py 的核心逻辑"
        ↓
    收集三个子 agent 的摘要
        ↓
    父 agent 综合摘要，给出完整架构分析
    
    优势：
    1. 每个子 agent 有独立的上下文，不会互相干扰
    2. 父 agent 的 context 不会被大量代码细节填满
    3. 可以并行创建多个子 agent 分析不同文件
    4. 子 agent 只读，不会意外修改文件
    """
    
    def demo(self):
        """演示父子 agent 协作流程。"""
        # 父 agent 的 prompt
        parent_prompt = """
        分析项目架构。请使用子 agent 分别分析：
        1. models.py - 数据模型
        2. tools.py - 工具函数  
        3. runtime.py - 运行时逻辑
        """
        
        # 父 agent 会生成如下工具调用：
        tool_calls = [
            {"name": "delegate", "args": {"task": "分析 models.py 的类结构", "max_steps": 3}},
            {"name": "delegate", "args": {"task": "分析 tools.py 的工具函数", "max_steps": 3}},
            {"name": "delegate", "args": {"task": "分析 runtime.py 的核心逻辑", "max_steps": 3}},
        ]
        
        # 每个子 agent 独立执行，返回摘要
        summaries = [
            "models.py: 包含 OpenAICompatibleModelClient 等模型客户端",
            "tools.py: 包含 read_file, write_file, patch_file 等工具",
            "runtime.py: 包含 Pico 主类和 ask() 控制循环",
        ]
        
        # 父 agent 综合所有摘要，给出最终答案
        final_answer = """
        项目架构：
        - models: 模型客户端适配层
        - tools: 工具实现层  
        - runtime: Agent 运行时核心
        """
        
        return final_answer
