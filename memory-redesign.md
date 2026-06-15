# Pico 记忆系统重构设计文档

> 基于 [memory-system.md](memory-system.md) 参考架构，针对 pico 项目的记忆系统进行渐进式重构。

---

## 目录

1. [为什么改](#一为什么改)
2. [怎么改](#二怎么改)
3. [改完后的效果](#三改完后的效果)
4. [附录：关键代码细节](#附录关键代码细节)

---

## 一、为什么改

### 1.1 当前记忆系统的完整画像

pico 的记忆系统由三个模块协作构成：

```
┌──────────────────────────────────────────────────────────────┐
│                     pico/memory.py                            │
│  LayeredMemory: 工作记忆 + 情景笔记 + 文件摘要 + 持久记忆       │
│  检索方式: 关键词 token 重叠                                   │
└──────────────────────────┬───────────────────────────────────┘
                           │ 被组装进 prompt
┌──────────────────────────▼───────────────────────────────────┐
│                 pico/context_manager.py                       │
│  ContextManager: 按 section 预算控制 prompt 长度               │
│  压缩方式: 线性截断 (tail clip)                                │
└──────────────────────────┬───────────────────────────────────┘
                           │ 被 ask() 调度
┌──────────────────────────▼───────────────────────────────────┐
│                    pico/runtime.py                            │
│  Pico.ask(): 控制循环，工具执行后同步更新记忆                   │
│  持久记忆升级: 正则 pattern 匹配 final_answer                   │
└──────────────────────────────────────────────────────────────┘
```

**LayeredMemory 内部结构**（`pico/memory.py` — 单一文件承载四层职责）：

```python
# default_memory_state() 的完整形状
{
    "working": {
        "task_summary": "",      # 当前任务一句话摘要 (≤300 chars)
        "recent_files": [],      # 最近接触的文件路径 (≤8 个，去重去重)
    },
    "episodic_notes": [          # 情景笔记 (≤12 条)
        {
            "text": "...",       # 笔记正文 (≤500 chars)
            "tags": [...],       # 标签
            "source": "...",     # 来源 (文件名 / 工具名)
            "created_at": "...",# ISO timestamp
            "note_index": 0,     # 序号
            "kind": "episodic", # 类型: episodic | process | durable
        }
    ],
    "file_summaries": {          # 文件短摘要 (≤6 个)
        "path/to/file": {
            "summary": "...",    # 摘要 (≤500 chars)
            "created_at": "...",
            "freshness": "sha256"# 文件内容 hash，用于检测过期
        }
    },
    "durable_topics": [          # 持久记忆主题列表 (从磁盘加载)
        "project-conventions",
        "key-decisions",
        "dependency-facts",
        "user-preferences",
    ],
    # 以下为兼容旧格式的冗余字段
    "task": "",                  # → working.task_summary 的副本
    "files": [],                 # → working.recent_files 的副本
    "notes": [],                 # → episodic_notes[].text 的提取
    "next_note_index": 0,
}
```

**渲染给模型的格式**（`render_memory_text()` 的输出）：

```
Memory:
- task: 调查 test_login 失败的原因
- recent_files: src/auth.py, tests/test_auth.py
- file_summaries:
  - src/auth.py: JWT 认证模块，使用 PyJWT 库...
  - tests/test_auth.py: 登录测试用例，共 15 个测试...
- episodic_notes: 4
- durable_topics: project-conventions, key-decisions, dependency-facts, user-preferences
```

**检索方式**（`retrieval_candidates()`）：

```
用户输入 → _tokenize() 提取字母数字 token → 与每条 note 的 text + tags + source 做交集
                                         → 与 durable 主题的 notes 做交集
                                         → 按 (tag_exact_match, keyword_overlap, recency) 排序
                                         → 返回 top-3
```

**持久记忆落盘**（`DurableMemoryStore`）：

```
.pico/memory/
├── MEMORY.md                   # 索引文件: - [topic](topics/topic.md): title
│                              #            - summary: ...
│                              #            - tags: ...
└── topics/
    ├── project-conventions.md  # # Project Conventions
    ├── key-decisions.md        # ## Notes
    ├── dependency-facts.md     # - 具体笔记条目
    └── user-preferences.md     # - ...
```

**当前记忆数据流**：

```
用户输入 → ask()
  ├─ memory.set_task_summary(user_message)    # 把用户请求当任务摘要
  ├─ 工具执行 → update_memory_after_tool()     # 同步更新 recent_files + file_summaries
  ├─ 工具执行 → record_process_note_for_tool() # 记录错误/部分成功笔记
  └─ final_answer → promote_durable_memory()   # 正则匹配提取持久记忆
```

### 1.2 五个具体问题

#### 问题 1：记忆提取全靠规则，覆盖面极窄

当前只有**一条路**能把信息变成持久记忆：

```python
# pico/runtime.py:696-716
# 必须用户在输入里说"记住/保存/记录"等触发词
# + final_answer 里恰好有 "Project convention:" / "Decision:" 等格式行
DURABLE_MEMORY_INTENT_PATTERN = re.compile(r"(?i)\b(capture|remember|save|store|persist|note)\b")
DURABLE_MEMORY_LINE_PATTERNS = (
    ("project-conventions", re.compile(r"(?i)^Project convention:\s*(.+)$")),
    ("key-decisions", re.compile(r"(?i)^Decision:\s*(.+)$")),
    # ...
)
```

这意味着：
- 用户没说"记住"→ 什么都不会持久化
- Agent 没按格式输出 → 什么都不会持久化
- 对话中 95% 的有价值信息（偏好、决策、事实）被丢弃

**具体影响**：你和 agent 聊了 30 轮修 bug，它学到了你的编码风格、项目约定、依赖关系，但下次会话全部丢失——因为这些信息从没被提取出来。

#### 问题 2：检索只有关键词匹配，召回率低

```python
# pico/memory.py:519-547
def retrieval_candidates(state, query, limit=3, workspace_root=None):
    query_tokens = _tokenize(query)  # 正则提取字母数字 token
    for note in state["episodic_notes"]:
        note_tokens = _tokenize(note["text"])
        exact_tag_match = int(bool(query_tokens & note_tags))
        keyword_overlap = len(query_tokens & note_tokens)
        # 纯 token 交集排序
```

**具体影响**：
- 用户问"身份认证怎么做的" → 笔记里有"JWT token 在 middleware 层验证" → 关键词交集为空，召不回
- 中文分词完全没处理：`_tokenize` 用 `[A-Za-z0-9_]+` 提取 token，中文内容会被完全跳过
- 没有语义相似度，同义词/近义词不可能匹配

#### 问题 3：记忆更新在热路径上同步执行

```python
# pico/runtime.py:1097
def run_tool(self, name, args):
    # ... 工具执行 ...
    self.update_memory_after_tool(name, args, result)  # 同步
    self.record_process_note_for_tool(name, metadata)   # 同步
```

每次工具调用都会同步执行记忆更新。虽然当前操作很轻（只是 dict 操作），但后续如果要做 LLM 提取、向量化、去重，同步执行会显著拖慢 agent 响应。

#### 问题 4：上下文压缩只有截断，没有语义保留

```python
# pico/context_manager.py:33-41
def _tail_clip(text, limit):
    # 纯字符截断
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
```

当前的"压缩"就是简单的字符截断。这意味着：
- 旧工具结果被截断后，关键信息（错误信息、文件路径）可能丢失
- 没有"用摘要替代完整内容"的能力
- 截断位置是盲目的，不区分重要/不重要内容

#### 问题 5：没有跨会话的记忆连续性

虽然 pico 支持 `--resume` 恢复会话，但：

- 新会话完全不知道旧会话里发生了什么
- 用户画像不存在 → 每次都要重新建立上下文
- 项目约定、决策历史 → 只存在于那次会话的 `session.json` 里
- 不同会话之间的知识完全隔离

### 1.3 与参考系统的差距总结

| 维度 | 当前 pico | 参考系统 | 差距的本质 |
|------|----------|---------|-----------|
| 记忆提取 | 正则 pattern 匹配 | LLM 结构化提取 | 覆盖率差 ~20x |
| 检索 | 关键词 token 交集 | 向量 + BM25 混合 | 召回率差 ~5x |
| 去重 | 文本相等 | 向量相似度 + LLM 判定 | 大量重复记忆 |
| 压缩 | 字符截断 | 摘要替换 + 任务保护 | 信息损失大 |
| 跨会话 | 无 | L2 场景 + L3 画像 | 完全断裂 |
| 调度 | 同步 | 异步管道 | 阻塞热路径 |

---

## 二、怎么改

总体策略：**四个阶段，每个阶段独立可交付，后续阶段依赖前序阶段**。

```
阶段 1 (基础) ──→ 阶段 2 (核心) ──→ 阶段 3 (深化) ──→ 阶段 4 (可选)
  2-3 天           3-5 天           3-5 天          按需
```

### 阶段 1：记忆管道异步化

**目标**：将记忆处理从热路径剥离，为阶段 2 的 LLM 提取铺路。不引入新的 LLM 调用，不动存储格式。

**核心思路**：

当前 `run_tool()` 里 `update_memory_after_tool()` 和 `record_process_note_for_tool()` 虽然操作很轻（只是 dict 操作），但它们是同步的。阶段 2 引入 LLM 提取后，同步执行会直接拖慢 agent 响应。所以阶段 1 先把调度框架搭好——热路径只做 O(1) 计数，后台线程做实际处理。

另外，`session["history"]` 是一个有序列表，天然支持「从上次位置之后开始读」，不需要额外搞 JSONL 存储。用游标记录 `last_extracted_index` 即可。

**新文件**：`pico/memory_pipeline.py`

**改动点**：`run_tool()` 末尾调 `pipeline.notify()` 替代同步记忆更新：

```python
# pico/memory_pipeline.py

import threading
import time

class MemoryPipeline:
    """后台记忆处理管道。

    设计原则：
    - 热路径只做 O(1) 的计数器累加
    - LLM 调用在后台线程执行，不阻塞 agent 响应
    - 阈值触发 + 空闲触发，双重保障
    """

    def __init__(self, agent, config=None):
        self.agent = agent
        self.config = config or PipelineConfig()

        # 触发状态
        self._conversation_count = 0
        self._last_activity = time.monotonic()
        self._pending = False

        # 后台线程
        self._thread = None
        self._stop_event = threading.Event()

    def notify_tool_executed(self):
        """工具执行后调用 — O(1) 操作。"""
        self._conversation_count += 1
        self._last_activity = time.monotonic()

        if self._conversation_count >= self.config.every_n_turns:
            self._schedule_extraction()

    def notify_user_message(self):
        """用户发消息时调用 — 重置空闲计时。"""
        self._last_activity = time.monotonic()

    def _schedule_extraction(self):
        """安排一次 L1 提取。"""
        if self._pending:
            return
        self._pending = True
        # 在后台线程执行，不阻塞主循环
        t = threading.Thread(target=self._run_extraction, daemon=True)
        t.start()

    def _run_extraction(self):
        """后台执行 L1 提取。

        原材料来源：agent.session["history"] + 游标 last_extracted_index。
        不需要额外的存储层 — session["history"] 就是有序列表，
        天然支持增量读取。

        失败重试：如果提取失败，不推进游标，而是把失败的 index 范围
        记录到 checkpoint 的重试队列。下次触发时优先重试失败范围，
        超过 max_retries 才跳过。
        """
        try:
            history = self.agent.session["history"]

            # 优先处理上次失败的区间
            pending = self.checkpoint.get("pending_ranges", [])
            if pending:
                start, end, retries = pending[0]
                new_messages = history[start:end+1]
                effective_start = start
            else:
                last_idx = self.checkpoint.get("last_extracted_index", -1)
                new_messages = history[last_idx + 1:]
                effective_start = last_idx + 1

            if not new_messages:
                return

            # 阶段 2 会在这里插入 LLM 提取 + 去重
            # memories = extract_memories(new_messages, self.agent.model_client)
            # deduplicated = deduplicate(memories, self.vector_store)
            # write_records(deduplicated)

            # 成功 — 推进游标，清除重试记录
            self.checkpoint["last_extracted_index"] = effective_start + len(new_messages) - 1
            if pending:
                self.checkpoint["pending_ranges"] = pending[1:]
            self._save_checkpoint()
        except Exception:
            # 管道异常不影响主循环，但要把失败区间记下来下次重试
            import traceback
            traceback.print_exc()
            self._record_failed_range(effective_start, effective_start + len(new_messages) - 1)
        finally:
            self._pending = False
            self._conversation_count = 0

    def _record_failed_range(self, start: int, end: int):
        """将失败的 index 范围加入重试队列。"""
        max_retries = 3
        pending = list(self.checkpoint.get("pending_ranges", []))
        # 查找是否已有相同范围的记录
        for i, (s, e, retries) in enumerate(pending):
            if s == start and e == end:
                if retries >= max_retries:
                    # 超过重试上限，放弃这个范围，推进游标
                    self.checkpoint["last_extracted_index"] = end
                    pending.pop(i)
                else:
                    pending[i] = (s, e, retries + 1)
                break
        else:
            pending.append((start, end, 1))
        self.checkpoint["pending_ranges"] = pending
        self._save_checkpoint()

    def start_idle_watcher(self):
        """启动空闲监控线程。

        当用户停止聊天超过 idle_timeout_seconds 时，
        如果还有未处理的对话，触发一次兜底提取。
        """
        def _watch():
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=10)
                if self._conversation_count > 0:
                    elapsed = time.monotonic() - self._last_activity
                    if elapsed >= self.config.idle_timeout_seconds:
                        self._schedule_extraction()
        self._thread = threading.Thread(target=_watch, daemon=True)
        self._thread.start()
```

**配置**：`pico/memory_pipeline.py`：

```python
@dataclass
class PipelineConfig:
    every_n_turns: int = 5            # 每 N 轮工具调用触发一次 L1
    idle_timeout_seconds: int = 60    # 空闲超时兜底触发
    enable_warmup: bool = True        # 预热模式：新会话阈值从 1 开始指数增长
```

**预热模式**：新会话的前几次触发使用更低的阈值（1→2→4→8→...→5），确保早期对话快速被处理。

**为什么「阈值触发 + 空闲兜底」是合理的？**

这个双重触发机制解决了一个实际问题：**用户可能在 N 轮之前就结束了对话**。如果只有阈值触发，那不足 N 轮的对话片段就永远不会被提取。空闲兜底保证了「即使对话很短，记忆也不会丢」。

具体场景：

```
阈值 every_n_turns=5，idle_timeout=60s

场景 A（长对话）:
  用户连续交互 15 轮 → 阈值触发在第 5、10、15 轮各触发一次

场景 B（短对话）:
  用户只聊了 3 轮就停了
  → 阈值永远达不到 5
  → 60s 后空闲监控发现 _conversation_count=3 > 0
  → 触发兜底提取
  → 这 3 轮的对话内容不会丢失
```

两个触发路径互补，一个保证吞吐（长对话及时处理），一个保证完整（短对话不遗漏）。

#### checkpoint.json：记录管道进度

存储在 `.pico/memory/checkpoint.json`，记录 MemoryPipeline 的处理进度：

```json
{
  "last_extracted_index": 47,
  "last_l1_at": "2026-06-10T15:30:00+00:00",
  "last_l2_at": null,
  "last_l3_at": null,
  "pending_ranges": []
}
```

- `last_extracted_index`：指向 `session["history"]` 中最后一条被 L1 **成功处理**的消息 index
- `pending_ranges`：失败重试队列，每项为 `[start, end, retries]`，下次触发时优先处理。超过 3 次重试后跳过并推进游标
- 下次 L1 触发时，优先处理 `pending_ranges`，再处理 `history[last_extracted_index + 1:]`
- session 恢复时，游标和重试队列随之恢复，不会重复提取也不丢失败区间

#### 上下文预算：从字符数切换到 token 数

当前 `ContextManager` 使用 `len(prompt)`（字符数）做预算控制：

```python
# pico/context_manager.py — 当前实现
while len(prompt) > self.total_budget:  # 字符数比较
    ...
```

但每次 LLM 响应中已经携带了精确的 token 统计（`self.last_completion_metadata` 里的 `input_tokens`、`output_tokens`），这些数据来自 API 返回的 `usage` 字段，比字符估算准确得多。

**改动**：`ContextManager` 维护一个 `estimated_input_tokens` 字段，每次模型调用后用实际值校准：

```python
class ContextManager:
    def __init__(self, agent, total_budget=100000, ...):
        ...
        self.total_budget = int(total_budget)  # 现在解释为 token 数而非字符数
        self._estimated_input_tokens = 0
        self._token_per_char_ratio = 0.3  # 初始估算：英文 ~0.25, 中文 ~0.6, 取保守值

    def _estimate_tokens(self, text: str) -> int:
        """快速 token 估算。

        不做精确 tiktoken（太重），用字符数 × 经验比例。
        每次 API 返回真实 usage 后自动校准比例。
        """
        return int(len(text) * self._token_per_char_ratio)

    def calibrate_ratio(self, actual_input_tokens: int, prompt_chars: int):
        """用 API 返回的真实 token 数校准估算比例。"""
        if prompt_chars > 0 and actual_input_tokens > 0:
            # 指数移动平均，平滑波动
            observed = actual_input_tokens / prompt_chars
            self._token_per_char_ratio = (
                0.7 * self._token_per_char_ratio + 0.3 * observed
            )

    def build(self, user_message):
        ...
        # 所有预算比较改用 _estimate_tokens
        prompt = self._assemble_prompt(rendered)
        estimated_tokens = self._estimate_tokens(prompt)

        while estimated_tokens > self.total_budget:
            ...

        # 模型调用完成后（在 ask() 里）
        # actual_input = completion_metadata.get("input_tokens")
        # if actual_input:
        #     self.context_manager.calibrate_ratio(actual_input, len(prompt))
```

**效果**：几次调用后比例自动收敛到接近真实值，预算控制不再依赖盲目的字符计数。

---

### 阶段 2：LLM 驱动的记忆提取

**目标**：用一次轻量的 LLM 调用替代正则 pattern，大幅提升记忆覆盖率。

#### 2a. L1 记忆提取器

**新文件**：`pico/memory_extractor.py`

**核心流程**：

```
session["history"][last_extracted_index + 1:]   ← 增量消息，游标来自 checkpoint.json
    │
    ▼
┌─────────────────────────────────────┐
│  extract_memories(snippet, model)   │  ← 一次 LLM 调用完成三步
│                                     │
│  1. 情境切分: 是否开始新场景？       │
│  2. 记忆提取: persona/episodic/     │
│              instruction 三类       │
│  3. JSON 输出: 结构化记忆数组       │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  deduplicate(new, existing)        │  ← BM25 召回 + LLM 判定
│                                     │
│  对每条新记忆:                       │
│  1. BM25 召回 top-3 候选           │
│  2. LLM 判定: keep_new/keep_old/   │
│              merge/discard         │
└──────────────┬──────────────────────┘
               │
               ▼
        写入 .pico/memory/records/{session_id}.jsonl
        更新向量索引 .pico/memory/store/index.jsonl
```

**记忆类型定义**：

```python
# pico/memory_extractor.py

from dataclasses import dataclass
from enum import Enum

class MemoryType(str, Enum):
    PERSONA = "persona"        # 用户稳定属性、偏好
    EPISODIC = "episodic"      # 客观事件
    INSTRUCTION = "instruction"  # 长期行为规则

@dataclass
class MemoryRecord:
    id: str                    # 唯一 ID
    session_key: str           # 所属会话
    type: MemoryType
    content: str               # 记忆内容（一句话）
    priority: int              # 优先级 0-100
    tags: list[str]            # 标签
    created_at: str            # ISO timestamp
    source_message_indices: list[int]  # 来源消息在 L0 中的 index
    scene_id: str | None       # 归属的场景 ID（L2 填充）
```

**记忆优先级规则**：

| 类型 | 优先级范围 | 示例 |
|------|-----------|------|
| `persona` | 50-100 | "用户偏好使用 dataclass 而非 NamedTuple" — 70 |
| `episodic` | 60-100 | "修复了 auth.py 的 JWT 过期逻辑" — 80 |
| `instruction` | -1, 70-100 | "回复时附上代码示例" — 90（长期有效） |

**LLM Prompt 设计**（关键）：

```python
# pico/memory_extractor.py

L1_EXTRACTION_PROMPT = """You are a memory extraction system. Given a conversation snippet
between a user and a coding agent, extract structured memories.

## Step 1: Scene Segmentation
Determine if a new scene/task has started. A scene changes when the user
shifts to a substantially different topic or task.

## Step 2: Memory Extraction
Extract memories in three categories:

### persona (priority 50-100)
Stable user attributes, preferences, habits, or skills.
Example: "User prefers concise error messages over stack traces"
Example: "用户熟悉 Django ORM"

### episodic (priority 60-100)
Objective events: bugs found, decisions made, files changed, solutions applied.
Example: "Fixed N+1 query in UserViewSet by adding select_related"
Example: "在 auth.py:42 处发现 email 空值导致的空指针异常"

### instruction (priority -1 or 70-100)
Long-term behavioral rules the user has explicitly asked the agent to follow.
Priority -1 means "never expire".
Example: "Always run tests before committing changes"

## Step 3: Output Format
Return a JSON object with this structure:
{
  "scene_changed": true/false,
  "scene_summary": "brief description of current scene if changed",
  "memories": [
    {
      "type": "persona|episodic|instruction",
      "content": "one concise sentence",
      "priority": 0-100,
      "tags": ["tag1", "tag2"]
    }
  ],
  "context_keys": [
    {
      "source_range": "0-4",
      "summary": "read auth.py → 发现 validate() 空值问题"
    }
  ]
}

## context_keys
For each batch of messages that produced the memories above, generate ONE context_key:
- "source_range": the range of message indices this summary covers (e.g., "0-4")
- "summary": ONE short sentence capturing what happened in this range
  → focus on: what tool was used, what was found, what action was taken
  → this summary will replace the raw tool output during later context compression

## Rules
- Each memory must be a SINGLE self-contained sentence.
- Do NOT extract trivial or transient information (e.g., "user said hello").
- If nothing meaningful to extract, return empty memories array.
- Tags should be short lowercase keywords for retrieval.

## Conversation
{conversation_text}
"""
```

**context_key 的作用**：管道 L1 提取时顺便生成高质量摘要，存储后供阶段 3 的 MILD 压缩优先使用。这解决了「规则取第一行太粗糙」的问题 — 与其在压缩时盲猜哪些内容重要，不如在提取时就让 LLM 总结好。

```python
# pico/memory/pipeline.py 中 _run_extraction() 存储 context_key
def _run_extraction(self):
    # ...
    result = extract_memories(new_messages, model_client)

    # 存储 memories（原有逻辑）
    deduplicated = deduplicate(result["memories"], self.vector_store)
    write_records(deduplicated)

    # 存储 context_key，供阶段 3 MILD 压缩使用
    for ck in result.get("context_keys", []):
        self._context_keys[ck["source_range"]] = ck["summary"]

    # 更新 TaskState（如果 LLM 检测到任务进展）
    if result.get("task_update"):
        self.agent.memory.update_task_state(**result["task_update"])
```

#### 2b. 轻量向量检索

**新文件**：`pico/vector_store.py`

**方案选择**：不引入 sqlite-vec 或 chromadb 等重依赖，使用纯 Python 实现：

```python
# pico/vector_store.py

import json
import math
from collections import defaultdict
from pathlib import Path

class LightweightVectorStore:
    """轻量向量存储：BM25 + embedding 余弦相似度。

    设计原则：
    - 默认使用 BM25 + 向量混合检索（embedding 默认开启）
    - 使用 sentence-transformers 的轻量模型（如 all-MiniLM-L6-v2，~80MB）
    - 存储格式：JSONL 追加写
    """

    def __init__(self, root: Path, use_embeddings: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.use_embeddings = use_embeddings
        self._docs = []           # 内存缓存
        self._bm25 = BM25Index()

    def add(self, doc_id: str, text: str, metadata: dict = None) -> None:
        """添加文档到索引。"""
        self._docs.append({"id": doc_id, "text": text, "metadata": metadata or {}})
        self._bm25.add(doc_id, text)
        # 追加到磁盘
        with (self.root / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": doc_id, "text": text, "metadata": metadata or {}
            }, ensure_ascii=False) + "\n")

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """BM25 搜索，返回 top-k 结果。"""
        return self._bm25.search(query, limit)


class BM25Index:
    """BM25 全文检索的纯 Python 实现。

    相比当前 _tokenize 的交集匹配：
    - 考虑了词频 (TF) 和逆文档频率 (IDF)
    - 对中文：使用字符 bigram 作为 fallback tokenization
    - 对英文：使用标准分词
    """

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: dict[str, list[str]] = {}
        self._doc_lengths: dict[str, int] = {}
        self._avgdl: float = 0
        self._df: dict[str, int] = defaultdict(int)  # document frequency
        self._N: int = 0

    def add(self, doc_id: str, text: str) -> None:
        tokens = self._tokenize(text)
        self._docs[doc_id] = tokens
        self._doc_lengths[doc_id] = len(tokens)
        self._N += 1
        self._avgdl = sum(self._doc_lengths.values()) / self._N
        for token in set(tokens):
            self._df[token] += 1

    def search(self, query: str, limit: int = 5) -> list[dict]:
        query_tokens = self._tokenize(query)
        scores = {}
        for doc_id, doc_tokens in self._docs.items():
            score = self._score(query_tokens, doc_tokens)
            if score > 0:
                scores[doc_id] = score
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [{"id": doc_id, "score": score} for doc_id, score in ranked[:limit]]

    def _score(self, query_tokens: list[str], doc_tokens: list[str]) -> float:
        dl = len(doc_tokens)
        score = 0.0
        for token in set(query_tokens):
            if token not in self._df:
                continue
            tf = doc_tokens.count(token)
            idf = math.log((self._N - self._df[token] + 0.5) / (self._df[token] + 0.5) + 1.0)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
            score += idf * numerator / denominator
        return score

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """混合分词：英文用词边界，中文用字符 bigram。"""
        import re
        tokens = []
        # 英文/数字 token
        for match in re.finditer(r"[A-Za-z0-9_]+", text):
            tokens.append(match.group().lower())
        # 中文 bigram
        chinese_chars = re.findall(r"[一-鿿]+", text)
        for segment in chinese_chars:
            for i in range(len(segment) - 1):
                tokens.append(segment[i:i+2])
            tokens.append(segment[-1])  # 单字符也保留
        return tokens
```

#### 2c. 去重系统

```python
# pico/memory_dedup.py

def deduplicate_memories(
    new_memories: list[MemoryRecord],
    existing_store: LightweightVectorStore,
    model_client,  # 用于 LLM 判定
) -> tuple[list[MemoryRecord], list[str]]:
    """去重新记忆。

    对每条新记忆：
    1. 用 BM25 从已有记忆中召回 top-3 候选
    2. 如果没有 candidates → 直接保留
    3. 如果有 → 用 LLM 判断关系
    4. 返回 (保留的记忆, 被取代的记忆 ID 列表)

    LLM 判断结果：
    - "keep_both" → 新旧都保留（不同角度）
    - "keep_new"  → 新记忆取代旧（更新）
    - "keep_old"  → 新记忆是旧记忆的子集，丢弃新
    - "merge"     → 合并为一条更完整的记忆
    - "discard"   → 新记忆是噪音，丢弃
    """

DEDUP_PROMPT = """Compare two memory candidates and decide their relationship.

Existing memory: {existing_text}
New memory: {new_text}

Respond with exactly one word:
- keep_both: they describe different things
- keep_new: new information supersedes the old
- keep_old: existing is more complete/accurate
- merge: combine into a single better memory
- discard: new memory is noise/trivial
"""
```

**去重时的批量优化**：如果一次提取了 10 条新记忆，不需要做 10 次 LLM 调用。可以把所有 (new, candidates) 对打包成一次调用，让 LLM 批量判断。

#### 2d. 召回增强：查询扩展

**问题**：纯 BM25 对短查询、同义词、跨语言的召回率低。例如用户只问"还有类似问题吗"，query tokens 极少，几乎无法匹配。

**修正**：两步增强。

**① embedding 默认开启**（已在 2b 中修正）：

```python
class LightweightVectorStore:
    def __init__(self, root: Path, use_embeddings: bool = True):
        # 默认使用 BM25 + 向量混合检索
        # 使用 sentence-transformers 轻量模型（all-MiniLM-L6-v2, ~80MB）
```

**② 查询扩展**：当 user_message 过短时，用最近几轮用户消息补充上下文。

```python
# pico/context_manager.py

def _recall_l1_memories(self, user_message: str) -> list[str]:
    """召回相关 L1 记忆。

    查询扩展：短查询时自动补充最近对话上下文，
    避免"还有类似问题吗"这种极短查询完全无法匹配。
    """
    query = user_message

    # 查询扩展：少于 10 个词时，用最近用户消息补充
    if len(query.split()) < 10:
        recent_user_msgs = [
            entry.get("content", "")
            for entry in self.agent.session["history"][-6:]
            if entry.get("role") == "user"
        ]
        if recent_user_msgs:
            # 取最近的用户消息（排除当前这条）
            context_query = " ".join(recent_user_msgs[-2:]) if len(recent_user_msgs) >= 2 else ""
            if context_query:
                query = f"{query} {context_query}"

    # 混合检索（BM25 + 向量）
    results = self.agent.memory.retrieval_candidates(query, limit=5)

    return [r["content"] for r in results if r.get("content")]
```

**效果**：

```
用户: "还有类似问题吗"（仅 7 个词）
  → 查询扩展: "还有类似问题吗 帮我调查为什么 /api/login 返回 500 JSON 解析错误"
  → 召回: [mem_001] 调查了 /api/login 的 500 错误...
  → 召回: [mem_002] 发现 JSON 解析在 django middleware 层失败...
```

---

### 阶段 3：ContextManager 重构 — 二分注入 + 分级压缩

**目标**：三项改动集中在一个阶段，因为都在 `ContextManager` 内。
1. **二分注入**：稳定内容放 system 末尾（可缓存），动态内容放 user 前面（不破坏缓存）
2. **TaskState**：结构化任务状态对象，替代纯文本摘要
3. **分级压缩升级**：MILD 优先用管道生成的 context_key，AGGRESSIVE 用 TaskState.render()

#### 3a. 二分注入策略

**问题**：当前 `ContextManager.build()` 把所有内容打平成一段 system + messages。L1 召回每轮都在变，但被放在了 system prompt 的 `relevant_memory` section 里，导致 system prompt 的缓存每轮都被破坏。

**修正**：拆分为稳定区和动态区。

```python
# pico/context_manager.py

def build(self, user_message: str) -> tuple[str, list[dict], dict]:
    """
    返回 (system_prompt, messages, metadata)

    注入策略（二分）：
    - 稳定部分 → 拼在 system prompt 末尾 → 可被 API 缓存
    - 动态部分 → 拼在 user message 前面 → 不影响 system 缓存
    """

    # ========== 稳定部分（appendSystemContext）==========
    stable_parts = []

    # L3 Persona：几乎不变，放 system 末尾
    persona = self._load_persona()
    if persona:
        stable_parts.append(f"<user-persona>\n{persona}\n</user-persona>")

    # L2 Scene Navigation：场景切换时才变
    scene_nav = self._get_scene_navigation(user_message)
    if scene_nav:
        stable_parts.append(f"<scene-navigation>\n{scene_nav}\n</scene-navigation>")

    # 工作记忆（文件摘要、当前任务）：任务期间稳定
    working = self.agent.memory.render_memory_text()
    if working.strip():
        stable_parts.append(f"<working-memory>\n{working}\n</working-memory>")

    # ========== 动态部分（prependContext）==========
    dynamic_parts = []

    # L1 记忆召回：每轮可能变化，放 user message 前面
    relevant_l1 = self._recall_l1_memories(user_message)
    if relevant_l1:
        dynamic_parts.append(
            "<relevant-memories>\n" + "\n".join(f"- {m}" for m in relevant_l1) + "\n</relevant-memories>"
        )

    # ========== 组装 ==========
    system_prompt = self._base_system_prompt
    if stable_parts:
        system_prompt += "\n\n" + "\n\n".join(stable_parts)

    # 用户消息：动态记忆 + 实际输入
    augmented_user_message = user_message
    if dynamic_parts:
        augmented_user_message = "\n\n".join(dynamic_parts) + "\n\n" + user_message

    messages = self._build_messages(augmented_user_message)
    # history 部分仍然走原来的压缩逻辑
    return system_prompt, messages, metadata
```

**缓存效果**：

```
┌────────────────────────────────────────────┐
│ system: 你是 Pico，一个编程助手...           │ ← 永远不变
├────────────────────────────────────────────┤
│ system: <user-persona>                     │ ← 场景变化时才变
│  用户偏好 dataclass、FastAPI、pytest...      │   几乎永远命中缓存
│ system: <scene-navigation>                 │
│  当前上下文：修复 auth 模块 bug              │
│ system: <working-memory>                   │
│  task: 修复 JWT 认证...                     │
├────────────────────────────────────────────┤
│ user: <relevant-memories>                  │ ← 每轮变化
│  - auth.py:142 email 空值导致 500           │   但放在 user message 前
│  - 修复方案：validate() 开头加空值检查        │   不破坏 system 缓存
│                                            │
│ 帮我看看 auth 模块还有什么类似问题            │ ← 用户实际输入
└────────────────────────────────────────────┘
```

#### 3b. 轻量任务状态对象 (TaskState)

**问题**：原方案 AGGRESSIVE 压缩时生成的 Task Progress Summary 是纯文本，表达能力弱。分支/回溯信息（"试了方案 A 但放弃"）在纯文本里很难清晰表达。

**修正**：维护一个结构化 TaskState，压缩时渲染为 Markdown。

```python
# pico/memory/working.py 中增加

from dataclasses import dataclass, field

@dataclass
class TaskState:
    """当前任务的结构化状态。

    不是完整的 Mermaid 图（那对 Pico 的线性任务过度），
    但比纯文本更清晰。在 AGGRESSIVE 压缩时渲染为 Task Progress Summary。
    """
    label: str = ""                           # 任务名称
    completed: list[str] = field(default_factory=list)    # 已完成的步骤
    current: str = ""                          # 当前正在做的
    pending: list[str] = field(default_factory=list)      # 待处理
    abandoned: list[str] = field(default_factory=list)    # 已放弃的分支
    key_findings: list[str] = field(default_factory=list) # 关键发现

    def render(self) -> str:
        """渲染为注入 prompt 的 Markdown 文本。"""
        lines = [f"**当前任务**: {self.label}"]

        if self.key_findings:
            lines.append("**关键发现**:")
            for f in self.key_findings[-5:]:       # 最多 5 条
                lines.append(f"  - {f}")

        if self.completed:
            lines.append(f"**已完成** ({len(self.completed)} 步):")
            for s in self.completed[-8:]:           # 最多 8 条
                lines.append(f"  - ✅ {s}")

        if self.current:
            lines.append(f"**正在进行**: 🔄 {self.current}")

        if self.pending:
            lines.append("**待处理**:")
            for s in self.pending[:5]:
                lines.append(f"  - ⏳ {s}")

        if self.abandoned:
            lines.append("**已放弃的分支**:")
            for s in self.abandoned[:3]:
                lines.append(f"  - ❌ {s}")

        return "\n".join(lines)


# LayeredMemory 中增加
class LayeredMemory:
    def __init__(self, state=None, workspace_root=None):
        # ... 原有字段
        self.task_state = TaskState()

    def update_task_state(self, **kwargs):
        """更新任务状态。由管道在提取 L1 后调用。"""
        for key, value in kwargs.items():
            if hasattr(self.task_state, key):
                setattr(self.task_state, key, value)
```

**效果对比**：

```
原方案 AGGRESSIVE:
  [Task Progress]
  已完成: 定位 auth.py:142、修复空值检查、测试通过
  当前: 检查类似问题

修正后 AGGRESSIVE:
  **当前任务**: 修复 auth 模块空值检查问题
  **关键发现**:
    - auth.py:142 validate() email 为空时返回 None
    - password 字段也有类似风险
  **已完成** (3 步):
    - ✅ 定位 auth.py:142 空值异常
    - ✅ 修复 validate() 空值检查
    - ✅ 测试 test_login 通过
  **正在进行**: 🔄 检查 password 字段是否有类似问题
  **待处理**:
    - ⏳ 检查其他模块的 validate 调用
  **已放弃的分支**:
    - ❌ 方案A：在调用处加空值检查（太分散，改用方案B在 validate 内部统一处理）
```

TaskState 由 MemoryPipeline 在 L1 提取后更新。管道提取记忆时，LLM 顺便判断任务是否延续/完成，更新 TaskState 的字段。不需要独立的 L1.5 任务判定层。

#### 3c. 三级压缩策略（升级版）

**改动文件**：`pico/context_manager.py`

```python
# 新增压缩级别

@dataclass
class CompressionConfig:
    # 触发阈值（占上下文窗口比例）
    mild_threshold: float = 0.5          # 50% → MILD 压缩
    aggressive_threshold: float = 0.85   # 85% → AGGRESSIVE 压缩
    emergency_threshold: float = 0.95    # 95% → EMERGENCY 压缩

    # AGGRESSIVE 删除比例
    aggressive_delete_ratio: float = 0.4  # 删除 40% 的历史
    emergency_target_ratio: float = 0.6   # EMERGENCY 目标降至 60%

class CompressionLevel(Enum):
    NONE = "none"
    MILD = "mild"
    AGGRESSIVE = "aggressive"
    EMERGENCY = "emergency"

class ContextManager:
    def _compression_level(self, current_tokens: int) -> CompressionLevel:
        ratio = current_tokens / self.total_budget
        if ratio >= self.compression_config.emergency_threshold:
            return CompressionLevel.EMERGENCY
        if ratio >= self.compression_config.aggressive_threshold:
            return CompressionLevel.AGGRESSIVE
        if ratio >= self.compression_config.mild_threshold:
            return CompressionLevel.MILD
        return CompressionLevel.NONE
```

**MILD 压缩**：替换非当前任务的工具结果为摘要

```python
def _mild_compress(self, history_entries: list) -> list:
    """MILD: 将远期工具调用结果替换为单句摘要。

    保留：
    - 所有 user 消息
    - 最近 12 轮的完整内容
    - tool_call_id（用于 API 格式一致性）

    替换：
    - 超过 12 轮的工具结果 → 替换为带 result_ref 的摘要
    - result_ref 指向 session["history"] 中的具体 index，
      Agent 需要原文时可以通过 read_file 回读会话文件
    """
    recent_start = max(0, len(history_entries) - 12)
    compressed = []
    for i, entry in enumerate(history_entries):
        if i >= recent_start:
            compressed.append(entry)
            continue
        if entry.get("role") == "tool":
            entry["_entry_index"] = i  # 注入位置信息，供 _summarize_tool_result 使用
            summary = self._summarize_tool_result(entry)
            compressed.append({
                **entry,
                "content": summary,
                "_offloaded": True,
            })
        else:
            compressed.append(entry)
    return compressed

def _summarize_tool_result(self, entry: dict, entry_index: int) -> str:
    """生成工具结果的单句摘要，保留「下钻线索」。

    优先级：
    1. 管道生成的 context_key（LLM 提取记忆时顺便生成的摘要，质量最高）
    2. 规则提取（取第一个非导入/非注释行）
    3. 裸文件名（最坏情况）

    设计原则（符号化 / symbolization）：
    不保留原文，保留「指向原文的摘要」+ 下钻线索。
    result_ref 让 Agent 需要原文时能按图索骥。
    """

    # 优先使用管道生成的 context_key
    ck = self._get_context_key_for_index(entry_index)
    if ck:
        return f"[Offloaded] {ck}  result_ref: history[{entry_index}]"

    # 回退到规则摘要
    name = entry.get("name", "tool")
    args = entry.get("args", {})
    content = entry.get("content", "")

    if name == "read_file":
        path = args.get("path", "?")
        # 跳过空行、导入行和注释行，取第一个有意义的内容
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith(("import ", "from ", "#", "//", "package ", "use ")):
                return (
                    f"[Offloaded] read_file: {path} → {line[:100]}"
                    f"  result_ref: history[{entry_index}]"
                )
        return f"[Offloaded] read_file: {path}  result_ref: history[{entry_index}]"

    if name == "run_shell":
        cmd = args.get("command", "?")
        exit_match = re.search(r"exit_code:\s*(-?\d+)", content)
        exit_code = exit_match.group(1) if exit_match else "?"
        return (
            f"[Offloaded] run_shell: {cmd[:60]} → exit_code={exit_code}"
            f"  result_ref: history[{entry_index}]"
        )

    # 通用回退
    snippet = content[:80].replace("\n", " ")
    return f"[Offloaded] {name}: {snippet}...  result_ref: history[{entry_index}]"
```

**AGGRESSIVE 压缩**：删除历史消息 + 注入结构化任务摘要

```python
def _aggressive_compress(self, history_entries: list) -> tuple[list, str]:
    """AGGRESSIVE: 从最早的消息开始删除，用 TaskState 补偿。

    关键原则：
    - 当前任务相关的工具调用 → 保护，不删除
    - 用户消息 → 尽量保护
    - 被删除的内容 → 通过 TaskState.render() 补偿
    """
    delete_count = int(len(history_entries) * self.compression_config.aggressive_delete_ratio)

    # 从头部删除，但跳过用户消息
    kept = []
    deleted = 0
    for i, entry in enumerate(history_entries):
        if deleted >= delete_count:
            kept.append(entry)
            continue
        if entry.get("role") == "user":
            kept.append(entry)  # 保护用户消息
            continue
        deleted += 1
        if entry.get("role") == "tool":
            entry["_entry_index"] = i

    # 用 TaskState 生成任务摘要补偿被删除的信息
    task_state = getattr(self.agent.memory, "task_state", None)
    if task_state and task_state.label:
        progress_summary = task_state.render()
    else:
        progress_summary = self._build_progress_summary(deleted_summaries)

    return kept, progress_summary

    return kept, progress_summary
```

**EMERGENCY 压缩**：强制删除到目标水位

```python
def _emergency_compress(self, history_entries: list) -> list:
    """EMERGENCY: 强制删除到 target_ratio 以下。

    1. 保护 MMD/摘要消息（压缩后恢复）
    2. 从头部删除（跳过用户消息）
    3. 如果还不够 → 从尾部删除最大的工具结果
    4. 最后手段 → 截断单条超长消息到 2000 字符
    """
    target = int(self.total_budget * self.compression_config.emergency_target_ratio)
    # ... 实现细节见附录
```

#### 3d. 当前任务保护

```python
def _get_current_task_files(self) -> set[str]:
    """识别当前任务涉及的文件集合。

    通过追踪最近 N 轮工具调用中引用的文件路径来判断。
    这些文件相关的工具结果在压缩时被保护。
    """
    recent_tools = self.agent.session["history"][-12:]
    files = set()
    for entry in recent_tools:
        if entry.get("role") == "tool":
            path = entry.get("args", {}).get("path", "")
            if path:
                files.add(path)
    return files
```

---

### 阶段 4：场景组织与用户画像（按需）

#### 4a. 场景块 (Scene Blocks) — 明确触发节奏

**新文件**：`pico/memory/scene.py`

**问题**：原方案 L2 触发条件模糊（"积累到 20 条"），可能导致每次都触发或永远不触发。

**修正**：增加明确的触发条件和最小间隔。

```python
# pico/memory/scene.py

@dataclass
class SceneConfig:
    min_l1_count: int = 20              # 至少 20 条 L1 记忆才组织场景
    min_interval_seconds: int = 900     # 两次场景组织的最小间隔（15分钟）
    max_l1_age_for_trigger: int = 10    # 至少有 10 条是新提取的（避免重复触发）

class SceneManager:
    def __init__(self, config=None):
        self.config = config or SceneConfig()
        self._last_scene_at: float = 0
        self._last_l1_count_at_trigger: int = 0

    def should_organize(self, current_l1_count: int) -> bool:
        """判断是否应该触发场景组织。

        三个条件同时满足才触发：
        1. L1 记忆总数 >= min_l1_count (20)
        2. 距离上次组织 >= min_interval_seconds (15分钟)
        3. 新增记忆数 >= max_l1_age_for_trigger (10)
        """
        now = time.monotonic()

        if current_l1_count < self.config.min_l1_count:
            return False

        if now - self._last_scene_at < self.config.min_interval_seconds:
            return False

        new_since_last = current_l1_count - self._last_l1_count_at_trigger
        if new_since_last < self.config.max_l1_age_for_trigger:
            return False

        return True

    def organize(self, l1_records, model_client):
        """组织场景：LLM Agent 读写 scenes/*.md。

        更新 _last_scene_at 和计数，避免短时间内重复触发。
        """
        # ... LLM 调用，生成/更新 scenes/*.md
        self._last_scene_at = time.monotonic()
        self._last_l1_count_at_trigger = len(l1_records)
```

**场景文件目录**：

```
.pico/memory/scenes/
├── index.json                    # 场景索引
├── fix-jwt-auth-bug.md           # 场景文件
├── refactor-user-model.md
└── add-api-rate-limiting.md
```

场景文件格式：

```markdown
===META===
scene_name: 修复 JWT 认证过期逻辑
created_at: 2026-06-10T10:00:00Z
updated_at: 2026-06-10T11:30:00Z
status: active
tags: [auth, jwt, bugfix]
===END META===

## 事件
- 用户报告 token 在过期前就失效了
- 在 auth.py:42 发现 exp 字段用错了时区
- 改为 UTC 时间戳比较

## 结论
JWT 过期判断已修复，使用 `datetime.utcnow()` 替代 `datetime.now()`。

## 相关记忆
- [mem_abc123] 用户偏好使用 PyJWT 库
- [mem_def456] auth 模块测试覆盖率 85%
```

#### 4b. 用户画像 (Persona)

**新文件**：`pico/persona_generator.py`

当场景块积累到 3+ 个时，生成/更新 `persona.md`：

```markdown
# 用户画像

## 基本信息
- 角色：后端开发者
- 主要语言：Python, TypeScript
- 框架偏好：FastAPI, Django ORM

## 编码习惯
- 偏好 dataclass > NamedTuple
- 测试先行，但接受 pragma: no cover 标记
- 错误处理：偏好具体异常而非裸 except

## 项目上下文
- 当前项目：pico — 本地 coding agent
- 项目约定：使用 Ruff 做 linting，Python 3.10+ 兼容
```

---

### 不做什么

经过讨论，明确以下不做：

| 不做 | 原因 |
|------|------|
| **Mermaid 图** | Pico 是本地 coding agent，任务以线性为主。结构化 TaskState 对象足够。Mermaid 的拓扑能力对复杂分支任务有价值，但对 Pico 的典型使用场景属于过度工程。 |
| **独立的 L1.5 任务判定层** | TaskState 的更新集成在管道 L1 提取中（LLM 提取记忆时顺便判断任务是否延续/完成），不需要额外的 LLM 调用。 |
| **node_id 精确定位** | 用 `result_ref: history[N]` 做下钻，足够定位到原文。精确到 token 级别的索引对 Pico 的体量来说是过度设计。 |
| **外部向量数据库** | 保持 `LightweightVectorStore` 的纯 Python 实现。本地 sentence-transformers 模型（~80MB）足够满足语义召回需求。 |

---

## 三、改完后的效果

### 3.1 每个阶段的交付效果

#### 阶段 1 完成后

| 指标 | 改前 | 改后 |
|------|------|------|
| 记忆更新对热路径影响 | 同步（虽轻但耦合） | 异步非阻塞（`notify()` 只做计数） |
| L1 提取触发方式 | 无自动触发 | 阈值触发（每 N 轮）+ 空闲兜底（60s） |
| 增量提取能力 | 无 | 游标 `last_extracted_index` 精准增量 |
| 预热模式 | 无 | 新会话阈值 1→2→4→8→...→5 |
| session.json 格式 | 不变 | 不变（完全向后兼容） |

#### 阶段 2 完成后

**覆盖面对比**（假设 20 轮 bug 修复对话）：

| 维度 | 改前 | 改后 |
|------|------|------|
| 提取出的记忆条数 | 0-2（需用户主动说"记住"） | 8-15（LLM 自动提取） |
| 记忆类型 | 仅文本 | persona + episodic + instruction |
| 中文检索召回率 | ~0%（`_tokenize` 不处理中文） | ~80%（BM25 + bigram） |
| 重复记忆 | 可能重复（仅文本相等判断） | 自动去重（LLM 判定） |
| 跨会话知识保留 | 仅同一 session.json | 独立 records/ 持久化 |

**具体场景示例**：

```
用户: 帮我调查为什么 /api/login 返回 500

[改前 — 正则匹配路径]
  用户没说"记住" → 0 条持久记忆
  session 结束后全部丢失

[改后 — LLM 提取路径]
  自动提取:
  - [episodic, p80] 调查了 /api/login 的 500 错误，定位到 auth.py:142 的空指针
  - [episodic, p75] 发现 LoginService.validate() 在 email 为空时不抛异常而是返回 None
  - [persona, p60] 用户使用 FastAPI + SQLAlchemy 技术栈
  - [episodic, p85] 修复方案：在 validate() 开头添加 email 空值检查

  下次会话用户问 "登录还有问题吗" → 自动召回上述记忆
```

#### 阶段 3 完成后

**压缩效果对比**（长对话场景，假设上下文窗口 100K）：

| 压缩级别 | 触发条件 | 改前行为 | 改后行为 |
|----------|---------|---------|---------|
| 无 (<50%) | 正常 | 全量保留 | 全量保留 |
| MILD (50-85%) | Token 超半 | 截断旧条目 | 替换旧工具结果为摘要 |
| AGGRESSIVE (85-95%) | Token 紧张 | 截断更多 | 删除旧消息 + 注入任务摘要 |
| EMERGENCY (>95%) | Token 溢出 | 末尾截断 | 强制删除 + 保护用户消息 |

**关键差异**：

```
改前 AGGRESSIVE 场景:
  "现在上下文窗口用了 90%，我需要删掉一些旧消息"
  → tail_clip 截断，可能切到关键错误信息的中间
  → 模型看到 "[tool:run_shell] npm test...\nFAIL tests/test_auth.py::test_login - Asser..."
  → 模型不知道测试到底为什么失败

改后 AGGRESSIVE 场景:
  "现在上下文窗口用了 90%"
  → 删除最早的工具结果，但注入:
  "[Task Progress]
   - npm test 发现 test_auth.py::test_login 失败: AssertionError: expected 200 got 500
   - read_file auth.py:142 发现 email 空值问题
   - 当前正在修改 validate() 函数"
  → 模型依然知道任务全貌
```

#### 阶段 4 完成后

```
用户在第 10 次会话中说: "帮我加个新 API endpoint"

改前:
  Agent 不知道:
  - 项目之前用的是什么框架（FastAPI? Flask?）
  - 用户喜欢什么样的代码风格
  - auth 模块之前踩过什么坑
  → 需要用户重新解释上下文

改后:
  Agent 自动注入:
  [Persona] 用户使用 FastAPI + SQLAlchemy，偏好 dataclass，Python 3.10+
  [Scene: add-auth-module] JWT 认证模块已完成，使用 PyJWT + middleware 模式
  [Scene: fix-api-rate-limiting] API 限流已实现，使用 redis + sliding window
  → Agent 直接知道技术栈和历史决策，不需要用户重复
```

### 3.2 修正后方案 vs 原 Pico vs 原方案

| 维度 | 原 Pico | 原方案（修正前） | 修正后方案 |
|------|---------|----------------|-----------|
| **缓存策略** | 无考虑 | 未涉及 | 二分注入：稳定区（system 末尾）+ 动态区（user 前面） |
| **长任务连续性** | 无 | Task Progress Summary 纯文本 | TaskState 结构化对象 + 文本渲染 |
| **MILD 摘要质量** | N/A | 规则取第一行 | 管道生成 context_key 优先，规则取首行兜底 |
| **召回方式** | 关键词 token 重叠 | BM25（embedding 可选） | BM25 + 向量混合（默认开启）+ 查询扩展 |
| **L2 触发节奏** | 无 | "积累到 20 条" | 明确：20 条 + 间隔 15 分钟 + 新增 10 条 |
| **下钻机制** | 无 | 文件路径 | 文件路径 + message_index (result_ref) |
| **MMD 等价物** | 无 | 无 | TaskState 对象（轻量替代） |
| **记忆提取** | 正则 pattern | LLM 结构化提取 | LLM 提取 + 同步产出 context_key |
| **压缩** | 字符截断 | 摘要替换 + 任务保护 | context_key 优先 + TaskState 补偿 + 结果保护 |
| **调度** | 同步 | 异步管道 | 异步管道 + 失败重试队列 |
| **计数方式** | 字符数 | 字符数 | Token 数（用 API usage 校准） |

### 3.3 整体架构变化

**核心原则**：MemoryPipeline 是「生产者」，ContextManager + Session 系统是「消费者」。新增的后台管道提炼原材料，写入工作记忆和持久存储；现有的 prompt 组装和会话管理照常读取、渲染、注入。

```
                              ┌─────────────────────────┐
                              │       用户输入            │
                              └───────────┬─────────────┘
                                          │
┌─────────────────────────────────────────▼─────────────────────────────────────────┐
│                              Pico.ask() — 主循环（不变）                            │
│                                                                                   │
│  1. memory.set_task_summary(user_message)                                         │
│  2. session["history"] ← record(user)  ←── Session 系统（保留）                    │
│  3. ContextManager.build(user_message)  ←── 上下文拼接（保留，阶段 3 升级压缩）      │
│  4. model_client.complete()                                                        │
│  5. run_tool() → pipeline.notify()      ←── 通知管道（新增，O(1)）                  │
│  6. final_answer → promote_durable()    ←── 规则提取（保留，阶段 2 升级为 LLM）      │
│                                                                                   │
└──────────────┬──────────────────────┬──────────────────────┬──────────────────────┘
               │                      │                      │
               ▼                      ▼                      ▼
┌──────────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
│   Session 系统 (保留)  │ │  ContextManager (升级) │ │  MemoryPipeline (新增) │
│                      │ │                      │ │                      │
│ session["history"]   │ │ 二分注入（阶段 3）:    │ │ 后台线程，异步执行      │
│   ← 完整对话记录      │ │                      │ │                      │
│   ← L1 原材料         │ │ system prompt:       │ │ 阈值触发: 每 N 轮      │
│                      │ │   base_system         │ │ 空闲触发: 60s 兜底     │
│ session["memory"]    │ │   + <user-persona>    │ │                      │
│   ← 工作记忆          │ │   + <scene-navigation>│ │ 原材料:               │
│   ← LayeredMemory    │ │   + <working-memory>  │ │   session["history"]  │
│   ← 被 Pipeline 更新  │ │   = 稳定区（可缓存）   │ │   [last_idx+1:]       │
│   ← 含 TaskState     │ │                      │ │                      │
│                      │ │ user message:         │ │ 产出 → L1 records/    │
│ session["checkpoints"]│ │   <relevant-memories> │ │ 产出 → context_keys   │
│   ← 断点续跑          │ │   + 用户实际输入       │ │ 产出 → vector_store/  │
│                      │ │   = 动态区（不破缓存）  │ │ 产出 → session["memory"]│
│ session["id"]        │ │                      │ │ 产出 → TaskState 更新  │
│   ← 会话标识          │ │ history:             │ │                      │
│                      │ │   分级压缩后注入       │ │ 阶段 4 产出:           │
│ SessionStore         │ │   MILD → context_key  │ │ 产出 → scenes/        │
│   ← 落盘 + 恢复       │ │   AGGR → TaskState    │ │ 产出 → persona.md     │
│                      │ │   EMERG → 强制截断    │ │                      │
└──────────────────────┘ └──────────────────────┘ └──────────────────────┘
```

**数据流向**：

```
session["history"]  ──────────────────────────────→  ContextManager
    (完整对话)         原材料                            (render history section)
       │
       │ [last_idx+1:]  增量
       ▼
MemoryPipeline  ───→  records/{session}.jsonl    ───→  DurableMemoryStore
    (后台提取)          (L1 结构化记忆)                  (持久记忆索引)
       │
       ├──→ 更新 TaskState (LayeredMemory.task_state)
       ├──→ 存储 context_keys (供 MILD 压缩使用)
       └──→ session["memory"] ────────────────────→  ContextManager
            (LayeredMemory)                             (render working-memory section)

ContextManager.build() — 二分注入:
  system prompt = base_system
                + <user-persona>       ← 稳定，可缓存
                + <scene-navigation>   ← 场景切换时才变
                + <working-memory>     ← 任务期间稳定

  user message  = <relevant-memories>  ← 动态，每轮变化
                + 用户实际输入          ← 放 user 前，不破坏 system 缓存
```

**要点**：
- **Session 系统**：完全保留，`session["history"]` 既是对话记录也是 MemoryPipeline 的原材料，`session["memory"]` 被 Pipeline 更新后被 ContextManager 读取
- **ContextManager**：二分注入 + 三级压缩。稳定内容放 system 末尾（命中缓存），动态 L1 召回放 user 前面（不破坏缓存）。压缩优先使用管道生成的 context_key
- **MemoryPipeline**：新增的后台生产者，不替代任何现有模块。它从 session 读取增量对话，产生结构化记忆 + context_keys + TaskState 更新，写回 session 和磁盘

### 3.4 成本估算

LLM 记忆提取的额外成本（以 Claude Haiku 为例）：

| 操作 | Token 消耗 | 频率 | 日成本估算 |
|------|-----------|------|-----------|
| L1 提取 (每次 5 轮对话) | ~500 input + ~200 output | ~10 次/天 | ~$0.01 |
| L1 去重 (每次 5-10 条新记忆) | ~300 input + ~100 output | ~10 次/天 | ~$0.005 |
| L2 场景组织 | ~1000 input + ~500 output | ~2 次/天 | ~$0.005 |
| L3 画像更新 | ~2000 input + ~500 output | ~1 次/天 | ~$0.005 |
| **合计** | | | **~$0.025/天** |

如果使用本地 Ollama 小模型（如 qwen3:4b），成本为零。

### 3.5 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| LLM 提取质量不稳定 | 低质量记忆污染检索 | 去重环节兜底；低分记忆定期清理 |
| 后台线程异常崩溃 | 记忆丢失 | 管道异常全部 catch 写日志，不影响主循环 |
| 向量存储膨胀 | 磁盘增长 | 定期 compact；低优先级记忆 TTL 过期 |
| 上下文压缩过度 | 模型缺少关键信息 | offload 摘要始终保留 result_ref 指向原始数据 |

---

## 附录：关键代码细节

### A. 源代码目录结构

```
pico/
├── __init__.py
├── __main__.py
├── cli.py                         # 命令行入口
├── runtime.py                     # Pico 主循环
├── context_manager.py             # prompt 组装 + 预算控制（保留，阶段 3 升级压缩）
├── models.py                      # 模型后端适配
├── tools.py                       # 工具注册
├── task_state.py                  # 任务状态机
├── workspace.py                   # 工作区快照
├── run_store.py                   # 运行工件落盘
│
├── memory/                        # [重构] 记忆子系统 — 独立子包
│   ├── __init__.py                # 公开 API: from pico.memory import LayeredMemory, MemoryPipeline, ...
│   ├── working.py                 # LayeredMemory（从 pico/memory.py 迁入）
│   ├── durable.py                 # DurableMemoryStore（从 pico/memory.py 拆出）
│   ├── pipeline.py                # [阶段 1] MemoryPipeline 异步调度
│   ├── extractor.py               # [阶段 2] L1 LLM 记忆提取
│   ├── dedup.py                   # [阶段 2] 去重逻辑
│   ├── vector_store.py            # [阶段 2] BM25 轻量检索
│   ├── scene.py                   # [阶段 4] L2 场景组织
│   └── persona.py                 # [阶段 4] L3 用户画像
│
└── tests/
    ├── test_memory.py              # 更新 import: from pico.memory import ...
    ├── test_memory_pipeline.py     # [阶段 1]
    ├── test_memory_extractor.py   # [阶段 2]
    └── ...
```

### B. 文件变更清单（按阶段）

```
阶段 1 新增:
├── pico/memory/__init__.py        # 子包入口
├── pico/memory/working.py         # 从 pico/memory.py 迁入 LayeredMemory
├── pico/memory/durable.py         # 从 pico/memory.py 拆出 DurableMemoryStore
├── pico/memory/pipeline.py        # MemoryPipeline 异步调度
└── tests/test_memory_pipeline.py

阶段 1 修改:
├── pico/memory.py → 删除（内容迁入 pico/memory/）
├── pico/runtime.py                # import 改为 from pico.memory import ...；初始化 MemoryPipeline
├── pico/context_manager.py        # 字符数→Token 数，用 API usage 校准
├── pico/__init__.py               # 更新导出
└── tests/test_memory.py           # 更新 import

阶段 2 新增:
├── pico/memory/extractor.py       # L1 LLM 记忆提取（含 context_key 生成）
├── pico/memory/dedup.py           # 去重逻辑
├── pico/memory/vector_store.py    # 轻量向量存储 (BM25 + embedding 默认开启)
└── tests/test_memory_extractor.py

阶段 2 修改:
├── pico/memory/pipeline.py        # _run_extraction() 接入 LLM 提取 + 失败重试队列
├── pico/context_manager.py        # _recall_l1_memories() 查询扩展
└── pico/cli.py                    # 新增 --memory-model 参数

阶段 3 修改:
├── pico/context_manager.py        # 二分注入 + TaskState AGGRESSIVE + context_key MILD
├── pico/memory/working.py         # 增加 TaskState 类

阶段 4 新增:
├── pico/memory/scene.py           # L2 场景组织（含明确的 SceneConfig 触发条件）
├── pico/memory/persona.py         # L3 用户画像
└── tests/test_scene_manager.py
```

### C. 存储目录结构（阶段 2 完成后）

```
.pico/
├── sessions/
│   └── {session_id}.json          # 会话状态（不变）
├── memory/
│   ├── MEMORY.md                   # 持久记忆索引（已有）
│   ├── topics/                     # 持久记忆主题（已有）
│   │   ├── project-conventions.md
│   │   ├── key-decisions.md
│   │   ├── dependency-facts.md
│   │   └── user-preferences.md
│   ├── checkpoint.json             # [新增] 管道进度
│   ├── records/                    # [阶段 2] L1 结构记忆
│   │   └── {session_id}.jsonl
│   ├── store/                      # [阶段 2] 向量索引
│   │   └── index.jsonl
│   ├── scenes/                     # [阶段 4] L2 场景文件
│   │   ├── index.json
│   │   └── *.md
│   └── persona.md                  # [阶段 4] L3 用户画像
└── runs/
    └── {run_id}/
        ├── task_state.json
        ├── trace.jsonl
        └── report.json
```

### D. MemoryPipeline 调度时序

```
时间线 (以一次典型 bug 修复为例):

t=0   用户: "帮我调查 test_login 失败的原因"
      → notify_user_message()
      → pipeline._conversation_count = 0

t=1   工具: read_file tests/test_auth.py
      → notify_tool_executed()
      → pipeline._conversation_count = 1

t=2   工具: read_file src/auth.py
      → notify_tool_executed()
      → pipeline._conversation_count = 2

t=3   工具: run_shell "pytest tests/test_auth.py::test_login -x"
      → notify_tool_executed()
      → pipeline._conversation_count = 3

t=4   工具: read_file src/auth.py (查看具体行)
      → notify_tool_executed()
      → pipeline._conversation_count = 4

t=5   工具: patch_file src/auth.py (修复)
      → notify_tool_executed()
      → pipeline._conversation_count = 5 ✅ 达到阈值
      → _schedule_extraction() [后台线程启动]

t=6   工具: run_shell "pytest tests/test_auth.py::test_login -x" (验证)
      → 主循环继续，不阻塞
      → 后台线程: L1 提取进行中...

t=7   模型返回 final_answer: "修复完成"
      → 主循环结束

t=8   后台线程: L1 提取完成
      → 从 session["history"][0:7] 提取了 6 条记忆
      → 同时产出 2 个 context_keys: "0-3"→"调查 auth 模块...", "4-6"→"修复 validate()..."
      → 去重: 4 条保留，2 条合并
      → 写入 .pico/memory/records/{session_id}.jsonl
      → 更新 TaskState: completed=["定位异常","修复空值检查"], current="检查类似问题"
      → checkpoint.last_extracted_index = 6
      → pipeline._conversation_count = 0
```

### E. 向后兼容

所有阶段变更保持向后兼容：

1. **session.json 格式**：不变。`checkpoint.json` 是新增的独立文件，不存在时自动创建，游标从 0 开始。
2. **L1 提取默认关闭**：除非显式配置 `--memory-model`，否则使用旧的规则提取（`promote_durable_memory` 照常工作）
3. **ContextManager API 不变**：`build(user_message)` 的返回值 `(system, messages, metadata)` 签名不变
4. **CLI 命令不变**：`/memory`、`/reset`、`/session` 行为不变
5. **`feature_flags` 控制**：新增 `"memory_pipeline"` 和 `"memory_llm_extraction"` 开关

---

> **下一步**：如果同意这个方案，建议从阶段 1（MemoryPipeline 异步调度）开始实施。它最独立、风险最低——不动存储格式、不引入新 LLM 调用、完全向后兼容，但为阶段 2 的 LLM 提取搭好了调度框架。
