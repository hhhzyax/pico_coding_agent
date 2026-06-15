# 实施任务

## 阶段 1：基础设施建设

1. 创建 `pico/summary_buffer.py`，实现摘要缓冲区结构（source_range、summary、timestamp），支持 append/flush/clear 操作
2. 创建 `pico/memory_pipeline.py`，实现 MemoryPipeline 框架：后台线程调度、notify_agent_response()、check_and_compress() 入口
3. 迁移 `pico/context_manager.py` 的字符计数为 token 估算（基于字符数 × 经验比例 + API usage 校准）

## 阶段 2：阶段 0 — 零成本工具调用摘要

4. 在 `pico/runtime.py` 的 agent 响应处理中调用 `pipeline.notify_agent_response()`，从 assistant 消息中提取工具调用摘要写入缓冲区
5. 更新 `pico/context_manager.py` 的 `build()` 方法，支持注入摘要缓冲区内容（可选，仅在启用摘要模式时）

## 阶段 3：阶段 1 — LLM 累计摘要合并

6. 创建 `pico/summary_compressor.py`，实现 LLM 驱动的摘要合并：输入已有累计摘要 + 新缓冲区条目，输出合并后的累计摘要
7. 在 MemoryPipeline 中接入压缩触发逻辑：token 阈值 >= 75% 预算时触发后台压缩；轮次兜底 >= 20 轮
8. 在 `pico/context_manager.py` 中实现压缩后的上下文重组：删除被覆盖的旧条目，插入累计摘要，保留最近 25% 的完整 history

## 阶段 4：集成与清理

9. 移除 `pico/memory.py` 中 `working`、`episodic_notes`、`file_summaries`、`promote_durable_memory()` 及相关渲染逻辑
10. 在 `pico/system_prompt.py` 中加入回查原始记录的指引文本
11. 实现持久存储：将压缩产生的累计摘要写入 `.pico/memory/records/{session_id}.jsonl`

## 阶段 5：测试与验证

12. 编写 `SummaryBuffer` 单元测试：buffer append / flush / clear / source_range 正确性
13. 编写 `MemoryPipeline` 单元测试：notify 计数、阈值触发、后台压缩调度
14. 编写 `SummaryCompressor` 集成测试：LLM 摘要合并的质量和正确性
15. 编写 `ContextManager` 集成测试：摘要注入后的上下文格式、压缩边界正确性
16. 完整流程测试：模拟长对话，验证 token 阈值触发 → 后台压缩 → 上下文重组 → 持久存储全链路

---

**备注**：
- 每个阶段完成后可独立验证交付效果
- 阶段 2 不引入额外 LLM 调用（零成本），阶段 3 引入 LLM 调用（按需）
- 所有后台处理异常不影响主循环
- 保持 `session.json` 格式不变
