"""Agent 运行时核心逻辑。

Pico 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import re
import textwrap
import uuid
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import memory as memorylib
from .context_manager import ContextManager
from .run_store import RunStore
from .task_state import TaskState
from . import tools as toolkit
from .workspace import IGNORED_PATH_NAMES, MAX_HISTORY, WorkspaceContext, clip, now


@dataclass
class PromptPrefix:
    """Prefix 除了文本本身，还带一小份元数据。"""
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str


class SessionStore:
    """会话存储，保存 history 和 memory。"""
    
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, session):
        path = self.root / f"{session['id']}.json"
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads((self.root / f"{session_id}.json").read_text(encoding="utf-8"))


class Pico:
    """Agent 运行时核心类。
    
    负责：
    1. 组装 prompt（prefix + memory + history + request）
    2. 调用模型获取响应
    3. 解析并执行工具调用
    4. 维护 session 状态
    5. 记录 trace 和 report
    """
    
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        depth=0,
        max_depth=1,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        
        # 初始化 session
        self.session = {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        
        # 初始化组件
        self.memory = memorylib.LayeredMemory(
            self.session["memory"],
            workspace_root=workspace.repo_root,
        )
        self.tools = self.build_tools()
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)

    def ask(self, user_message):
        """执行一次完整的 agent 回合。
        
        主循环：
        1. 组装 prompt
        2. 调用模型
        3. 如果是工具调用，执行工具并继续循环
        4. 如果是最终答案，返回结果
        """
        self.memory.set_task_summary(user_message)
        self.record({"role": "user", "content": user_message})
        
        tool_steps = 0
        while tool_steps < self.max_steps:
            # 组装 prompt
            prompt = self.prompt(user_message)
            
            # 调用模型
            raw = self.model_client.complete(prompt, self.max_new_tokens)
            kind, payload = self.parse(raw)
            
            if kind == "tool":
                # 执行工具
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                result = self.run_tool(name, args)
                self.record({
                    "role": "tool",
                    "name": name,
                    "args": args,
                    "content": result,
                })
                continue
            
            if kind == "final":
                # 返回最终答案
                final = payload.strip()
                self.record({"role": "assistant", "content": final})
                return final
        
        return "Stopped after reaching the step limit."

    def build_tools(self):
        """构建工具注册表。"""
        return toolkit.build_tool_registry(self)

    def build_prefix(self):
        """构建系统提示词（prefix）。"""
        # ... 省略具体实现
        pass

    def prompt(self, user_message):
        """组装完整 prompt。"""
        # ... 省略具体实现
        pass

    def parse(self, raw):
        """解析模型响应。"""
        # ... 省略具体实现
        pass

    def run_tool(self, name, args):
        """执行工具。"""
        tool = self.tools.get(name)
        if tool is None:
            return f"error: unknown tool '{name}'"
        return tool["run"](args)

    def record(self, item):
        """记录到 session history。"""
        self.session["history"].append(item)
        self.session_store.save(self.session)
