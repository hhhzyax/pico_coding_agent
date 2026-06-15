# Pico 记忆系统简化设计文档

## 一、当前问题

根据 `memory-redesign.md` 的分析，现有记忆系统存在以下核心问题：

1. **记忆提取靠正则 pattern**：`promote_durable_memory()` 用关键词匹配提取记忆，覆盖率为 LLM 提取的约 1/20
2. **向量检索不准确**：关键词 token 交集检索，无语义相似度，中文分词未处理
3. **记忆更新在热路径同步执行**：每次工具调用后同步更新记忆，后续引入 LLM 调用会明显拖慢 agent 响应
4. **上下文压缩只有粗暴截断**：`_tail_clip()` 按字符数截断，不保留语义信息
5. **跨会话记忆断裂**：新会话完全不知道旧会话里发生了什么

## 二、核心设计

### 2.1 总体思路

**只做一件事：用 LLM 对历史对话做摘要，替代旧的工具调用结果来节省 token。**

不做分层体系（L1/L2/L3）、不做任务状态追踪（TaskState）、不做场景组织和用户画像——这些都留在未来计划。

### 2.2 两阶段信息压缩

```
阶段 0（零成本）：单条工具调用摘要
  -> 利用 agent 自身的下次推理作为摘要，不需要额外 LLM 调用
  -> 摘要自然产生于 agent 处理工具结果时的思考过程

阶段 1（按需调用）：累计摘要合并
  -> 当上下文 token 超过阈值时，用一个 LLM 调用：
      1. 将缓冲区中的逐条摘要合并为一份累计摘要
      2. 累计摘要覆盖"对话开始到压缩点之前"的全部历史
  -> 压缩区的原始 history 从上下文删除，仅保留累计摘要
```

### 2.3 阶段 0：单条工具调用摘要（零额外成本）

**原理**：agent 执行工具调用并收到结果后，下一轮回复中必然包含对结果的分析和理解——这个过程本身就是一份摘要。

```
工具调用: read_file src/auth.py -> 返回 150 行代码
agent 下轮思考: "auth.py 包含 login() 和 validate()，validate() 未处理空值..."
```

**做法**：
- 将 agent 每条 assistant 消息中紧跟工具调用的那部分推理提取出来，作为对应工具调用结果的摘要
- 存入摘要缓冲区 `_summary_buffer`
- **不额外调用 LLM**，不产生额外延迟和 token 成本

**缓冲区条目结构**：

```python
_summary_buffer = [
    {
        "source_range": [3, 5],   # 覆盖 session["history"] 的索引 3~5
        "summary": "读取 auth.py，发现 validate() 有空值检查缺陷",
        "timestamp": "2026-06-15T10:30:00Z"
    },
    {
        "source_range": [6, 7],
        "summary": "运行 pytest，test_login 失败：AssertionError",
        "timestamp": "2026-06-15T10:30:15Z"
    },
    ...
]
```

**关于找回原始信息**：

摘要条目在上下文中**只展示文本，不携带索引引用说明**。agent 需要查看原始细节时，可通过 `source_range` 字段回查原始记录。在系统提示词中一次性声明：

> 当对话历史摘要中的信息不足以判断时，你可以使用 read_file 工具读取会话文件（`.pico/sessions/{session_id}.json`）中对应 `source_range` 索引范围的原始历史记录。

### 2.4 压缩触发时机

采用 **token 阈值触发 + 轮次兜底**：

| 触发条件 | 阈值 | 说明 |
|---------|------|------|
| **主触发** | 上下文占用 >= 总预算的 75% | token 超线时触发压缩 |
| **兜底触发** | 距上次压缩 >= 20 轮 | 防止长对话一直不超线导致摘要缓冲区膨胀 |

触发后在后台线程异步执行，不阻塞 agent 主循环。

### 2.5 阶段 1：累计摘要合并

当触发条件满足时，用一个 LLM 调用完成：

```
输入:
  - 已有的累计摘要（如果是首次压缩则为空）
  - 摘要缓冲区中的新条目（从上次压缩后到当前）

输出:
  - 一份合并后的新累计摘要
  - 覆盖从对话开始到当前压缩点的全部历史
  - source_range 扩大为新范围
```

**prompt 示例**：

```
你是一个对话摘要生成器。你会收到：
1. 【已有摘要】一份覆盖之前对话历史的累计摘要（可能为空）
2. 【新进展】最近几轮工具调用的逐条摘要

请生成一份合并后的累计摘要，要求：
- 按时间顺序组织
- 保留所有关键事实：修改了哪些文件、发现了什么问题、做了什么决策
- 去重：同一事实只出现一次
- 控制在一段话内，不超过 300 字
```

### 2.6 上下文组装策略

压缩完成后，上下文变为：

```
[累计摘要]  +  [最近 N 轮完整 history]

|              -> 完整保留，不压缩
|                 保证 agent 有足够的近期上下文
-> 替代被删除的旧历史
   与保留区不重叠
```

**关键原则：摘要和它所覆盖的原始 history 永不共存于上下文中。** 压缩时，被压缩的条目从 context 的 history 列表中硬删除，摘要插入在相同位置。不存在重叠冲突。

**保留区大小**：最近 25% 的上下文预算。即压缩时保留最新约 1/4 的 history 不触动。

### 2.7 存储

持久记忆存储在 `.pico/memory/records/{session_id}.jsonl`：

```jsonl
{"type": "summary", "source_range": [0, 14], "content": "累计摘要文本...", "timestamp": "..."}
{"type": "summary", "source_range": [0, 28], "content": "更新后的累计摘要...", "timestamp": "..."}
```

- 每次压缩产生一条记录，`source_range` 覆盖范围递增
- 这是持久记忆，不随上下文压缩而被删除
- `source_range` 提供溯源能力，agent 可通过 read_file 读取对应范围的原始历史

### 2.8 压缩时序

以一次典型的 bug 修复为例：

```
t=0    用户: "修复 test_login 失败"
       -> 上下文占比 5%

t=1-5  工具调用: read_file x2, run pytest, patch, run pytest
       -> 上下文占比 35%
       -> 摘要缓冲区积累 5 条

t=6-12 更多工具调用...
       -> 上下文占比 78% <- 触发压缩

       -> 后台线程: LLM 合并缓冲区所有摘要 -> 产生累计摘要 v1
       -> 上下文变为: [累计摘要 v1] + [最近 4 轮完整]
       -> 上下文占比降至 40%

t=13-25 继续工作...
       -> 上下文占比 76% <- 再次触发

       -> 后台线程: LLM(累计摘要 v1 + 新缓冲区) -> 累计摘要 v2
       -> 上下文变为: [累计摘要 v2] + [最近 4 轮完整]
```

---

## 三、与现有系统的关系

| 现有组件 | 简化版上线后 | 说明 |
|---------|-------------|------|
| LayeredMemory.working | 被替代 | 累计摘要已包含这些信息 |
| LayeredMemory.episodic_notes | 被替代 | 摘要缓冲区完全取代 |
| LayeredMemory.file_summaries | 被替代 | 文件内容摘要已在逐条工具摘要中 |
| promote_durable_memory() | 被替代 | LLM 摘要质量远超正则，后续可直接作为 topic 数据源 |
| DurableMemoryStore / topics | 保留 | 跨会话持久记忆，简化版给它提供更高质量的数据入口 |
| LayeredMemory.durable_topics | 保留 | 跨会话知识检索不变 |
| session.json / history | 保留 | 仍是 MemoryPipeline 的原材料和原始记录 |

---

## 四、实现步骤

### 需要新增/修改的文件

```
新增:
+-- pico/memory_pipeline.py     # 异步记忆处理管道
+-- pico/summary_buffer.py      # 摘要缓冲区管理
+-- pico/summary_compressor.py  # LLM 驱动的摘要合并

修改:
+-- pico/context_manager.py     # 上下文组装时注入累计摘要；字符计数切换为 token 估算
+-- pico/runtime.py             # run_tool() 末尾调用 pipeline.notify()
+-- pico/system_prompt.py       # 系统提示词中加入回查原始记录的指引
```

### MemoryPipeline 核心结构

```python
class MemoryPipeline:
    def __init__(self, agent):
        self.agent = agent
        self.buffer = SummaryBuffer()       # 摘要缓冲区
        self.cumulative_summary = None      # 当前累计摘要
        self.last_compression_idx = 0       # 上次压缩时处理的最后 history index

    def notify_agent_response(self, response_text, history_start, history_end):
        # agent 返回响应后调用 -- 从响应中提取工具调用摘要
        summary = self._extract_summary(response_text)
        if summary:
            self.buffer.append(history_start, history_end, summary)

    def check_and_compress(self, current_token_usage, budget):
        # 主循环中每次检查 -- 如果超阈值则触发后台压缩
        if current_token_usage > budget * 0.75:
            thread = Thread(target=self._run_compression, daemon=True)
            thread.start()

    def _run_compression(self):
        # 后台执行 LLM 压缩
        new_summary = llm_compress(
            previous=self.cumulative_summary,
            new_items=self.buffer.get_all()
        )
        self.cumulative_summary = new_summary
        self.buffer.clear()
```

### ContextManager 改动

```python
class ContextManager:
    def build(self, user_message):
        ...
        # 注入累计摘要：
        if pipeline.cumulative_summary:
            # 找到压缩边界，删除被覆盖的旧条目
            history = self._remove_compressed_entries(
                history, pipeline.last_compression_idx
            )
            # 在保留区之前插入摘要
            history.insert(0, {
                "role": "system",
                "content": f"[对话历史摘要]\n{pipeline.cumulative_summary}"
            })
        ...
```

### 系统提示词增量

当对话历史摘要（标记为[对话历史摘要]的内容）中引用信息不足以判断时，你可以使用 read_file 工具读取 .pico/sessions/{session_id}.json，查看 source_range 对应索引范围的原始工具调用记录。

---

## 五、未来计划

以下功能不在当前简化版范围内，留待后续阶段实现：

| 功能 | 说明 |
|------|------|
| TaskState | 追踪当前任务、已完成子任务、待处理事项 |
| L1 结构化记忆 | 将摘要条目按类型（文件变更、错误修复、决策记录）分类 |
| L2 场景组织 | 按开发场景（bug修复、功能开发、重构）组织记忆 |
| L3 用户画像 | 提取用户偏好、编码风格、常用技术栈 |
| 向量检索 | 为摘要条目生成 embedding，支持跨会话语义检索 |
| 去重逻辑 | 独立的向量相似度去重 + LLM 判定（当前依靠压缩 LLM prompt 要求自然去重） |
| 跨会话记忆恢复 | 新会话启动时自动加载历史会话的累计摘要 |
| 摘要到topic自动归入 | LLM 摘要自动按 topic 分类写入 DurableMemoryStore |
