# 规范差异：记忆管道

本文件包含对 pico 记忆系统的新增与修改需求。

## ADDED 需求

### Requirement: 工具调用摘要提取
WHEN agent 完成一轮工具调用并返回响应,
系统 SHALL 从 assistant 消息中提取对工具调用结果的分析内容作为摘要,
并存入摘要缓冲区。

#### Scenario: 正常提取工具调用摘要
GIVEN agent 执行了 read_file 工具调用并收到 150 行代码的返回结果
AND agent 在下一轮响应中包含 "auth.py 包含 login() 和 validate()，validate() 未处理空值..."
WHEN 系统提取摘要
THEN 摘要缓冲区新增一条记录
AND 该记录包含 source_range 覆盖对应的工具调用消息索引
AND 该记录包含提取的摘要文本
AND 该记录包含 ISO 格式的时间戳
AND 不产生额外的 LLM 调用

#### Scenario: 无有效摘要内容
GIVEN agent 响应中不包含对工具结果的分析（仅含简短确认如 "ok"）
WHEN 系统尝试提取摘要
THEN 摘要缓冲区不新增记录
AND 不影响后续流程

---

### Requirement: 摘要缓冲区管理
系统 SHALL 维护一个有序的摘要缓冲区，记录每次工具调用后 agent 的分析摘要。

#### Scenario: 追加摘要条目
GIVEN 摘要缓冲区当前有 3 条条目
WHEN 新增一条摘要 {source_range: [7,8], summary: "运行 pytest，test_login 失败：AssertionError"}
THEN 缓冲区包含 4 条条目
AND 条目按追加顺序排列

#### Scenario: 缓冲区清空
GIVEN 摘要缓冲区有 5 条条目
AND 所有条目已被合并为累计摘要
WHEN 系统清空缓冲区
THEN 缓冲区条目数为 0

#### Scenario: 缓冲区持久化
GIVEN 摘要条目被添加到缓冲区
WHEN 条目写入完成
THEN 条目同时写入 `.pico/memory/records/{session_id}.jsonl`
AND 记录格式为 `{"type": "summary", "source_range": [...], "content": "...", "timestamp": "..."}`

---

### Requirement: 压缩触发
WHEN 上下文 token 占用达到总预算的 75%,
OR 距上次压缩已超过 20 轮对话,
系统 SHALL 在后台线程触发一次累计摘要合并。

#### Scenario: Token 阈值触发压缩
GIVEN 上下文预算为 100000 token
AND 当前上下文占用约 78000 token（78%）
WHEN 系统检测 token 使用率
THEN 触发后台压缩
AND 主循环不阻塞

#### Scenario: 轮次兜底触发压缩
GIVEN 上下文占用一直未超过 75%（如维持在 60%）
AND 距上次压缩已经过 21 轮对话
WHEN 系统检测压缩条件
THEN 触发后台压缩
AND 防止摘要缓冲区无限膨胀

#### Scenario: 未达触发条件
GIVEN 上下文占用为 40%
AND 距上次压缩仅 5 轮
WHEN 系统检测压缩条件
THEN 不触发压缩
AND 摘要缓冲区继续累积

---

### Requirement: 累计摘要合并
WHEN 压缩被触发,
系统 SHALL 使用一个 LLM 调用将已有累计摘要和摘要缓冲区中的新条目合并为一份新的累计摘要。

#### Scenario: 首次压缩（无已有累计摘要）
GIVEN 已有累计摘要为空
AND 摘要缓冲区有 5 条新条目
WHEN 系统执行 LLM 摘要合并
THEN LLM 收到：空累计摘要 + 5 条新条目
AND LLM 输出一份新的累计摘要
AND 摘要按时间顺序组织
AND 保留所有关键事实（修改文件、发现问题、做出决策）
AND 去重（同一事实只出现一次）

#### Scenario: 后续压缩（合并已有摘要）
GIVEN 已有累计摘要 v1 覆盖 source_range [0, 14]
AND 摘要缓冲区有 4 条新条目覆盖 source_range [15, 18]
WHEN 系统执行 LLM 摘要合并
THEN LLM 收到：累计摘要 v1 + 4 条新条目
AND LLM 输出累计摘要 v2
AND v2 的 source_range 扩大为 [0, 18]
AND v2 覆盖从对话开始到当前压缩点的全部历史

---

### Requirement: 压缩后上下文重组
WHEN 压缩完成,
系统 SHALL 从上下文中移除被覆盖的原始 history 条目,
并在保留区之前插入累计摘要。

#### Scenario: 上下文重组
GIVEN 压缩前上下文包含 history 条目 0-50
AND 压缩覆盖了条目 0-40（即 source_range 为 [0, 40]）
AND 保留区设为最近 25%（即条目 38-50 不被覆盖）
WHEN 系统重组上下文
THEN 条目 0-37 从 history 中删除
AND 累计摘要插入在保留区之前
AND 摘要与它覆盖的原始 history 永不共存于上下文中
AND 保留区（条目 38-50）完整保留不动

#### Scenario: 摘要索引定位
GIVEN 累计摘要以 system 消息格式注入上下文
AND 消息内容为 "[对话历史摘要]\n{累计摘要文本}"
WHEN agent 查看上下文
THEN agent 可识别该消息为历史摘要
AND 可通过 read_file 回查 `.pico/sessions/{session_id}.json` 中的 source_range 获取原始记录

---

### Requirement: 持久存储
WHEN 每次压缩完成,
系统 SHALL 将累计摘要持久化写入磁盘。

#### Scenario: 追加写入
GIVEN `.pico/memory/records/{session_id}.jsonl` 已存在
AND 已完成一次压缩，产生累计摘要 v2
WHEN 系统持久化摘要
THEN 在 JSONL 文件中追加一行记录
AND 该记录 source_range 覆盖范围相比上一次递增

#### Scenario: 磁盘记录不随上下文删除
GIVEN 上下文经过压缩，原始 history 条目已被移除
WHEN 系统检查磁盘持久记录
THEN 累计摘要的完整记录链在 `.pico/memory/records/` 中保留
AND source_range 提供溯源能力

---

## MODIFIED 需求

### Requirement: 上下文组装策略
**Previous**：ContextManager 使用基于字符数的固定 section 预算，history 超限时用 `_tail_clip()` 按字符截断。

WHEN ContextManager 组装一轮 prompt,
系统 SHALL 使用 token 估算（基于字符数 × 动态校准比例）替代字符计数进行预算控制,
AND 当 context 超限时，优先注入累计摘要并移除被覆盖的原始条目，而非盲目截断。

#### Scenario: Token 估算校准
GIVEN 初始 token_per_char_ratio 为 0.3
AND 一次 LLM 调用返回真实 input_tokens 为 15000
AND 发送的 prompt 字符数为 50000
WHEN 系统校准估算比例
THEN 新的 ratio = 0.7 × 0.3 + 0.3 × (15000/50000) = 0.30
AND 后续 token 估算更接近真实值

#### Scenario: Context 组装包含摘要
GIVEN 存在累计摘要 v2 覆盖 source_range [0, 28]
AND 用户发送新消息
WHEN ContextManager.build(user_message) 被调用
THEN history 中索引 0-27 的条目被移除
AND 累计摘要以 system 角色消息注入在保留 history 之前
AND 最近 history（保留区）完整保留

---

## REMOVED 需求

### Requirement: 正则 pattern 持久记忆提取（promote_durable_memory）
**移除原因**：LLM 摘要提取的质量和覆盖率远超正则匹配（约 20x）。正则 pattern 仅能匹配用户明确使用"记住"等触发词且 agent 输出符合特定格式的情况，覆盖极窄。

**迁移路径**：LLM 生成的累计摘要天然包含关键决策、发现和偏好。后续可通过 LLM 摘要自动按 topic 分类写入 DurableMemoryStore，替代手动正则提取。

### Requirement: 工具调用后同步记忆更新（update_memory_after_tool）
**移除原因**：同步更新阻塞 agent 热路径。改为 MemoryPipeline 后台异步处理。

**迁移路径**：`run_tool()` 末尾改为调用 `pipeline.notify()`（O(1) 计数器操作），实际记忆处理在后台线程执行。

### Requirement: 字符截断式压缩（_tail_clip）
**移除原因**：纯字符截断不保留语义，可能截断关键信息的中间位置。

**迁移路径**：改为累计摘要 + 保留区策略。摘要保留关键事实的语义，保留区保证近期完整上下文。

---

## 备注

- 系统提示词中新增回查指引：当对话历史摘要信息不足时，agent 可通过 read_file 读取会话文件中对应 source_range 的原始记录
- 未来计划（TaskState、向量检索、去重逻辑、跨会话记忆恢复等）不在本次变更范围内
- 所有后台处理异常不影响主循环
