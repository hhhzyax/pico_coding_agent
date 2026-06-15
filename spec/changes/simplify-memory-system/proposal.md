# 提案：记忆系统简化重构

## Why

当前 pico 记忆系统存在结构臃肿、提取机制低效、压缩策略粗糙的问题。经过分析（[memory-redesign.md](../../../memory-redesign.md)）和简化方案设计（[memory-simplified.md](../../../memory-simplified.md)），决定对记忆系统进行渐进式简化重构。

**核心问题**：

1. **记忆提取靠正则 pattern**：`promote_durable_memory()` 用关键词匹配提取记忆，覆盖率仅为 LLM 提取的约 1/20
2. **向量检索不准确**：关键词 token 交集检索，无语义相似度，中文分词未处理
3. **记忆更新在热路径同步执行**：每次工具调用后同步更新记忆，后续引入 LLM 会拖慢响应
4. **上下文压缩只有粗暴截断**：`_tail_clip()` 按字符数截断，不保留语义信息
5. **跨会话记忆断裂**：新会话完全不知道旧会话里发生了什么

**核心思路**：只做一件事——用 LLM 对历史对话做摘要，替代旧的工具调用结果来节省 token。不做分层体系（L1/L2/L3）、不做任务状态追踪（TaskState）、不做场景组织和用户画像——这些都留在未来计划。

## What Changes

### 新增模块

- **`pico/memory_pipeline.py`** — 异步记忆处理管道（MemoryPipeline）
- **`pico/summary_buffer.py`** — 摘要缓冲区管理
- **`pico/summary_compressor.py`** — LLM 驱动的摘要合并

### 修改模块

- **`pico/context_manager.py`** — 上下文组装时注入累计摘要；字符计数切换为 token 估算
- **`pico/runtime.py`** — `run_tool()` 末尾调用 `pipeline.notify()`
- **`pico/system_prompt.py`** — 系统提示词中加入回查原始记录的指引

### 被替代的组件

| 现有组件 | 简化版上线后 | 说明 |
|---------|-------------|------|
| `LayeredMemory.working` | 被替代 | 累计摘要已包含这些信息 |
| `LayeredMemory.episodic_notes` | 被替代 | 摘要缓冲区完全取代 |
| `LayeredMemory.file_summaries` | 被替代 | 文件内容摘要已在逐条工具摘要中 |
| `promote_durable_memory()` | 被替代 | LLM 摘要质量远超正则，后续可直接作为 topic 数据源 |

### 保留的组件

| 现有组件 | 处理方式 |
|---------|---------|
| `DurableMemoryStore` / topics | 保留，简化版给其提供更高质量的数据入口 |
| `LayeredMemory.durable_topics` | 保留，跨会话知识检索不变 |
| `session.json` / history | 保留，仍是 MemoryPipeline 的原材料和原始记录 |

## Impact

### 受影响的代码
- `pico/memory.py` — 移除 `working`、`episodic_notes`、`file_summaries`、`promote_durable_memory()`，保留 `DurableMemoryStore` 和 `durable_topics`
- `pico/context_manager.py` — 替换 `_tail_clip()` 为摘要注入；字符计数改为 token 估算
- `pico/runtime.py` — 移除同步记忆更新，接入 MemoryPipeline
- `pico/system_prompt.py` — 新增回查指引

### 用户影响
- Agent 响应延迟降低（记忆处理异步化，不阻塞热路径）
- 长期会话的上下文质量提升（语义摘要替代盲目截断）
- 跨轮对话的信息保留更完整

### API 变更
- `LayeredMemory` 的部分字段将被移除，但对外接口保持兼容
- `ContextManager.build()` 签名不变
- CLI 命令不变

### 需要的迁移
- [ ] 移除 `working`、`episodic_notes`、`file_summaries` 的读写逻辑
- [ ] 更新相关测试
- [ ] 无需数据库迁移（`session.json` 格式不变）

## 时间线评估

**预估工作量**：约 3-5 天（单一开发者）

- 核心实现：2-3 天
- 测试与验证：1 天
- 文档更新：0.5 天

## 风险

| 风险 | 缓解措施 |
|------|---------|
| LLM 摘要质量不稳定 | 摘要缓冲区保留 source_range，agent 可通过 read_file 回查原始记录；系统提示词中加入回查指引 |
| 后台线程异常崩溃 | 管道异常全部 catch，不影响主循环 |
| 摘要缓冲区膨胀 | 兜底触发机制防止长对话一直不压缩；轮次兜底 >= 20 轮 |
| 压缩后丢失关键信息 | 保留区策略：最近 25% 的历史完整保留；摘要与原始 history 永不共存 |
