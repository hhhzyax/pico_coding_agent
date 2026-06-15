# 记忆系统架构文档

## 概述

本项目实现了一个**四层渐进式记忆管理系统**，用于在 LLM Agent 的上线文中管理、存储和检索记忆。系统核心设计理念是：**用最小的 Token 开销保留最大的信息价值**。

| 层级 | 名称 | 功能 | 触发时机 |
|------|------|------|----------|
| **L0** | 对话记录层（Raw Conversation） | 原始对话消息的持久化存储 | `agent_end` hook |
| **L1** | 原子记忆层（Atomic Memory） | 提取结构化记忆（人物、事件、指令） | 批量触发（阈值/空闲） |
| **L1.5** | 任务判定层（Task Judgment） | 判断任务边界和生命周期 | 工具调用累积后 |
| **L2** | 场景层（Scene Block / MMD） | 生成任务流程图和场景块 | L1 完成后定时触发 |
| **L3** | 画像层（Persona） | 生成用户画像和上下文压缩 | L2 完成后触发 |

系统由两个**正交子系统**组成：

1. **Memory Pipeline**（`src/core/` 和 `src/utils/pipeline-manager.ts`）：负责 L0→L1→L2→L3 的记忆提取管道
2. **Context Offload**（`src/offload/`）：负责工具调用上下文的压缩和卸载

两条线共享相同的磁盘目录结构，但各有自己的 LLM 调用和调度逻辑。

---

## 一、整体架构

```
┌────────────────────────────────────────────────────────────────────┐
│                           主 Agent 工作流                           │
│  (通过 OpenClaw 运行，受 Context Offload 压缩系统影响)               │
├────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  before_agent_start → recall(L1+L2+L3) → LLM → tool_calls →        │
│  after_tool_call → agent_end → capture(L0) → 下一轮循环             │
│                                                                     │
└────────────────────────────────────────────────────────────────────┘
                            │ 触发
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                    Memory Pipeline Manager                          │
│  (异步后台管道，独立 LLM 调用，不受上下文压缩影响)                   │
├────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  notifyConversation() → 触发 L1 → L1 完成 → 触发 L2 → L2 完成 → L3 │
│                                                                     │
└────────────────────────────────────────────────────────────────────┘
                            │ 读写
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                          存储层                                      │
├────────────────────────────────────────────────────────────────────┤
│  ~/.openclaw/memory-tdai/                                          │
│  ├── conversations/        ← L0 原始对话 (JSONL)                   │
│  ├── records/              ← L1 结构记忆 (JSONL)                   │
│  ├── scene_blocks/         ← L2 场景文件 (.md)                     │
│  ├── persona.md            ← L3 用户画像                           │
│  ├── store/                ← 向量数据库                             │
│  │   ├── vectors.db        ← 嵌入向量 (sqlite-vec / TCDVB)         │
│  │   └── fts.db            ← 全文索引 (FTS5 BM25)                  │
│  └── checkpoint.json       ← 管道进度持久化                         │
│                                                                     │
│  ~/.openclaw/context-offload/{agentName}/                           │
│  ├── refs/                 ← 工具调用原始结果 (.md)                 │
│  ├── mmds/                 ← 任务 Mermaid 流程图 (.mmd)            │
│  ├── offload-*.jsonl       ← 工具调用摘要记录                      │
│  └── state.json            ← 插件状态                              │
└────────────────────────────────────────────────────────────────────┘
```

---

## 二、核心源码结构

```
src/
├── core/                             # 核心记忆算法（Host-neutral）
│   ├── types.ts                      # 抽象接口定义
│   ├── tdai-core.ts                  # 统一入口 facade
│   ├── conversation/
│   │   └── l0-recorder.ts            # L0 对话记录器
│   ├── hooks/
│   │   ├── auto-recall.ts            # 记忆召回 hook
│   │   └── auto-capture.ts           # 记忆捕获 hook
│   ├── record/
│   │   ├── l1-extractor.ts           # L1 记忆提取器
│   │   ├── l1-dedup.ts              # L1 去重
│   │   ├── l1-writer.ts             # L1 写入器
│   │   └── l1-reader.ts             # L1 读取器
│   ├── scene/
│   │   ├── scene-extractor.ts        # L2 场景提取器
│   │   ├── scene-index.ts           # 场景索引
│   │   ├── scene-format.ts          # 场景格式
│   │   └── scene-navigation.ts      # 场景导航
│   ├── persona/
│   │   ├── persona-generator.ts      # L3 画像生成器
│   │   └── persona-trigger.ts        # L3 触发逻辑
│   ├── profile/
│   │   └── profile-sync.ts           # 画像同步
│   ├── store/                        # 存储抽象层
│   │   ├── types.ts                  # 存储接口
│   │   ├── sqlite.ts                 # SQLite 实现
│   │   ├── embedding.ts             # 嵌入服务
│   │   ├── bm25-client.ts           # BM25 客户端
│   │   └── tcvdb.ts                 # 向量数据库
│   ├── tools/
│   │   ├── memory-search.ts          # 主 Agent 记忆搜索工具
│   │   └── conversation-search.ts   # 主 Agent 对话搜索工具
│   └── prompts/                      # LLM prompts
│       ├── l1-extraction.ts          # L1 提取 prompt
│       ├── l1-dedup.ts              # L1 去重 prompt
│       ├── scene-extraction.ts       # L2 场景 prompt
│       └── persona-generation.ts     # L3 画像 prompt
│
├── offload/                          # 上下文卸载系统
│   ├── types.ts                      # 类型和配置
│   ├── index.ts                      # 模块入口
│   ├── storage.ts                    # 文件 I/O 层
│   ├── state-manager.ts             # 状态管理
│   ├── l3-helpers.ts                # L3 压缩助手函数
│   ├── l3-token-counter.ts          # Token 计数器
│   ├── mmd-injector.ts              # MMD 注入器
│   ├── mmd-meta.ts                  # MMD 元数据解析
│   ├── reclaimer.ts                  # 数据回收器
│   ├── session-registry.ts          # 会话注册表
│   ├── context-token-tracker.ts     # Token 追踪器
│   ├── fast-token-estimate.ts       # 快速 Token 估算
│   ├── backend-client.ts            # 后端 API 客户端
│   ├── opik-tracer.ts              # Opik 追踪
│   ├── hooks/
│   │   ├── after-tool-call.ts       # 工具调用后 hook
│   │   ├── before-agent-start.ts    # Agent 启动前 hook
│   │   ├── before-prompt-build.ts   # Prompt 构建前 hook
│   │   ├── llm-input-l3.ts         # L3 上下文压缩 hook
│   │   └── llm-output.ts           # LLM 输出 hook
│   ├── local-llm/                    # 本地 LLM 调用
│   │   ├── llm-caller.ts
│   │   ├── parsers/                  # 输出解析器
│   │   └── prompts/                  # 各层的 prompt
│   │       ├── l1-prompt.ts
│   │       ├── l15-prompt.ts
│   │       └── l2-prompt.ts
│   └── pipelines/
│       └── l2-mermaid.ts            # L2 MMD 管道
│
├── utils/
│   ├── pipeline-factory.ts           # 管道工厂（创建所有组件）
│   ├── pipeline-manager.ts           # 管道管理器（调度逻辑）
│   ├── checkpoint.ts                 # 检查点管理器
│   ├── clean-context-runner.ts       # 干净上下文运行器
│   ├── sanitize.ts                   # 清洗工具
│   └── serial-queue.ts              # 串行队列
│
├── adapters/
│   ├── openclaw/                     # OpenClaw 适配器
│   ├── standalone/                   # 独立运行适配器
│   └── index.ts
│
└── config.ts                         # 配置类型
```

---

## 三、各层详解

---

### 3.1 L0：对话记录层（Raw Conversation）

**目的**：持久化保存原始对话消息，为更上层的记忆提取提供原材料。

**触发时机**：每轮对话结束时（`agent_end` hook）

**核心文件**：
- [auto-capture.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/hooks/auto-capture.ts) — 捕获 hook
- [l0-recorder.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/conversation/l0-recorder.ts) — 记录器

**数据流**：

```
agent_end → performAutoCapture() → recordConversation() → conversations/YYYY-MM-DD.jsonl
                                                              ↓
         过滤用户/助手消息 → 清洗（去注入标签） → 写入 JSONL
```

**存储格式**（[l0-recorder.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/conversation/l0-recorder.ts#L43-L53)）：

```typescript
interface L0MessageRecord {
  sessionKey: string;      // 所属会话
  sessionId: string;       // 会话子 ID
  recordedAt: string;      // 记录时间（ISO）
  id: string;              // 消息唯一 ID
  role: "user" | "assistant";  // 角色
  content: string;         // 消息内容
  timestamp: number;       // epoch ms
}
```

**关键设计**：

- **增量捕获**：使用 `originalUserMessageCount`（位置游标）+ `afterTimestamp`（时间游标）双重保护，防止重复捕获（[l0-recorder.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/conversation/l0-recorder.ts#L109-L131)）
- **原子操作**：通过 `captureAtomically()` 文件锁保证并发安全
- **过滤规则**：短消息、纯命令消息、只含代码块的消息会被过滤掉

---

### 3.2 L1：原子记忆层（Atomic Memory）

**目的**：从 L0 对话中提取结构化的核心记忆，去除琐碎内容。

**触发时机**：由 `MemoryPipelineManager` 调度，有两种触发路径（[pipeline-manager.ts](file:///d:/code/TencentDB-Agent-Memory/src/utils/pipeline-manager.ts#L145-L155)）：

| 路径 | 条件 | 说明 |
|------|------|------|
| A. 阈值触发 | `conversation_count >= effectiveThreshold` | 累积够一定轮数后立即触发 |
| B. 空闲触发 | 会话空闲超过 `l1IdleTimeoutSeconds`（默认 60s） | 用户停止聊天后兜底触发 |

**预热模式**（Warm-up）：新会话的触发阈值从 1 开始指数增长：1→2→4→8→...→`everyNConversations`（默认 5），确保早期对话快速处理。

**核心文件**：
- [l1-extractor.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/record/l1-extractor.ts) — 提取器
- [l1-dedup.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/record/l1-dedup.ts) — 去重
- [l1-extraction.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/prompts/l1-extraction.ts) — LLM prompt

**LLM 提取逻辑**（一次 LLM 调用完成三步）（[l1-extraction.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/prompts/l1-extraction.ts#L12-L14)）：

```
1. 情境切分（Scene Segmentation）：判断是否切分新场景
2. 记忆提取（Memory Extraction）：提取三类记忆
3. JSON 输出：结构化的记忆数组
```

**记忆类型**（[l1-extraction.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/prompts/l1-extraction.ts#L56-L78)）：

| 类型 | 含义 | 示例 | 优先级范围 |
|------|------|------|-----------|
| `persona` | 用户稳定属性、偏好 | "用户喜欢用 Python 写后端" | 50-100 |
| `episodic` | 客观事件 | "用户在 2024-01-15 修复了登录 bug" | 60-100 |
| `instruction` | 长期行为规则 | "用户要求 AI 回答时附上代码示例" | -1, 70-100 |

**存储格式**（[l1-writer.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/record/l1-writer.ts)）：

```typescript
interface MemoryRecord {
  id: string;              // 唯一 ID
  sessionKey: string;      // 所属会话
  type: MemoryType;        // persona | episodic | instruction
  content: string;         // 记忆内容
  priority: number;        // 优先级 0-100
  tags: string[];          // 标签
  createdAt: number;       // epoch ms
  updatedAt: number;       // epoch ms
  sourceMessageIds: string[];  // 来源消息 ID
  conflictInfo?: string;   // 冲突检测信息
}
```

**去重机制**（[l1-dedup.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/record/l1-dedup.ts)）：

- 使用向量相似度（cosine）召回候选记录
- LLM 判断是"保留新旧"、"保留旧"、"保留新"、"合并"、"丢弃"中的哪种
- 支持批量去重以减少 LLM 调用次数

---

### 3.3 L1.5：任务判定层（Task Judgment）

> 注意：L1.5 是 **Context Offload 子系统** 的一部分，与 Memory Pipeline 的 L0-L3 是**两条独立线**。

**目的**：判断当前对话中的工具调用属于什么类型的任务，确定任务边界。

**核心文件**：
- [l15-prompt.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/local-llm/prompts/l15-prompt.ts) — LLM prompt
- [before-agent-start.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/hooks/before-agent-start.ts) — 触发 hook

**判定逻辑**（[l15-prompt.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/local-llm/prompts/l15-prompt.ts#L9-L18)）：

通过 LLM 交叉分析三个输入源：
1. **近期对话消息** — 提取用户最新诉求
2. **当前 MMD 流程图** — 评估当前任务基线
3. **历史 MMD 文件列表** — 判断是否延续旧任务

**输出**（[l15-prompt.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/local-llm/prompts/l15-prompt.ts#L33-L42)）：

```typescript
interface TaskJudgment {
  taskCompleted: boolean;       // 当前任务是否完成
  isLongTask: boolean;          // 是否是需要多步操作的复杂任务
  isContinuation: boolean;      // 是否延续历史任务
  continuationMmdFile?: string; // 延续的 MMD 文件名
  newTaskLabel?: string;        // 新任务标签
}
```

**边界记录**（[types.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/types.ts#L98-L108)）：

```typescript
interface L15Boundary {
  startIndex: number;           // 从此 entry 开始
  result: "long" | "short" | "pending";
  targetMmd: string | null;     // 归属的 MMD 文件
}
```

---

### 3.4 L2：场景层（Scene Block / MMD）

**目的**：
- **Memory Pipeline 线**：将 L1 记忆组织成结构化的场景块文件（Markdown）
- **Context Offload 线**：生成任务 Mermaid 流程图（.mmd），提供高密度任务摘要

#### 3.4.1 Memory Pipeline 线（Scene Blocks）

**核心文件**：
- [scene-extractor.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/scene/scene-extractor.ts) — 场景提取器
- [scene-index.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/scene/scene-index.ts) — 场景索引
- [scene-navigation.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/scene/scene-navigation.ts) — 场景导航

**调度逻辑**：

```
L1 完成 → 延迟 delayAfterL1Seconds（默认 90s）→ L2 触发
                                              ↓
                          每隔 maxIntervalSeconds（默认 3600s）→ L2 再次触发
                          但受 minIntervalSeconds（默认 900s）下限约束
                          会话冷却超过 sessionActiveWindowHours（默认 24h）则停止
```

**执行流程**（[scene-extractor.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/scene/scene-extractor.ts#L25-L35)）：

```
1. 备份场景文件
2. 加载场景索引
3. 构建提取 prompt（含新记忆 + 现有场景上下文）
4. 调用 LLM Agent（工具启用，沙箱到 scene_blocks/ 目录）
5. LLM Agent 自主读写 .md 场景文件
6. 同步场景索引
7. 生成场景导航
```

**场景文件格式**（Markdown + 自定义元数据头）：

```markdown
===META===
scene_name: 用户在修复登录页面的bug
created_at: 2024-01-15T10:00:00Z
updated_at: 2024-01-15T11:30:00Z
status: active
===END META===

## 事件

- 用户发现登录页面有空指针异常
- 查看 auth.js 第 42 行发现 email 未做空值检查
- 已添加空值检查逻辑

## 结论

登录页面 bug 已修复，修复方案是在调用 validateEmail 前检查 email 参数。
```

#### 3.4.2 Context Offload 线（MMD 流程图）

**核心文件**：
- [l2-mermaid.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/pipelines/l2-mermaid.ts) — MMD 生成管道
- [l2-prompt.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/local-llm/prompts/l2-prompt.ts) — MMD 生成的 LLM prompt
- [mmd-injector.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/mmd-injector.ts) — MMD 注入器

**MMD 文件结构**（[l2-prompt.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/local-llm/prompts/l2-prompt.ts#L27-L30)）：

```mermaid
%%{ "taskGoal": "修复登录页面的bug", "progress": "60", "createdTime": "2024-01-15T10:00:00Z", "updatedTime": "2024-01-15T11:30:00Z" }%%

flowchart TD
    001-N1["定位问题: 查看错误日志<br/>status: done<br/>summary: 发现空指针异常在auth.js第42行<br/>Timestamp: 2024-01-15T10:05:00Z"]
    001-N2["分析原因: 检查用户输入<br/>status: done<br/>summary: 发现当email为空时会触发<br/>Timestamp: 2024-01-15T10:15:00Z"]
    001-N3["编写修复: 添加空值检查<br/>status: doing<br/>summary: 正在实现验证逻辑<br/>Timestamp: 2024-01-15T11:30:00Z"]
    
    001-N1 --> 001-N2 --> 001-N3
```

**触发条件**（两种）：

| 条件 | 说明 |
|------|------|
| A. null 计数阈值 | `offload.jsonl` 中 `node_id=null` 的条目数 >= `l2NullThreshold`（默认 4） |
| B. 超时触发 | 距离上次 L2 超过 `l2TimeoutSeconds`（默认 300s） |

**节点映射**：L2 为每个工具调用分配 `node_id`（如 `001-N3`），并回写到 `offload.jsonl`。

---

### 3.5 L3：画像层（Persona）

**目的**：生成用户画像，提供全局性的用户特征摘要。

**核心文件**：
- [persona-generator.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/persona/persona-generator.ts) — 画像生成器

**触发条件**：L2 完成且 L3 pending 标志被设置（全局互斥，并发度=1）

**执行流程**（[persona-generator.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/persona/persona-generator.ts#L55-L75)）：

```
1. 读取现有 persona.md
2. 加载场景索引，找出变化场景
3. 读取变化场景的完整内容
4. 构建 persona 生成 prompt
5. 调用 LLM Agent（工具启用，声明沙箱到 scene_blocks/）
6. LLM Agent 自主读取场景文件 → 生成/更新 persona.md
```

**画像格式**（`persona.md`）：

```markdown
# 用户画像

## 基本信息
- 职业：后端开发者
- 技术栈：TypeScript, Node.js

## 偏好
- 喜欢简洁的代码风格
- 优先使用原生 API 而非第三方库

## 行为模式
- 倾向于先阅读代码再修改
- 习惯在修改前运行测试

## 重要事件
- 2024-01-15：修复了登录页面的空指针异常
```

---

### 3.6 Context Offload：上下文压缩系统

**目的**：在主 Agent 的上下文中管理 Token 使用，防止上下文窗口被工具调用结果撑爆。

**核心文件**：
- [after-tool-call.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/hooks/after-tool-call.ts) — 工具调用后处理
- [llm-input-l3.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/hooks/llm-input-l3.ts) — L3 上下文压缩
- [before-prompt-build.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/hooks/before-prompt-build.ts) — Prompt 构建前压缩
- [l3-helpers.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/l3-helpers.ts) — 压缩辅助函数

#### 3.6.1 三级压缩策略

**阈值配置**（[types.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/types.ts#L170-L201)）：

| 级别 | 触发阈值（占上下文比例） | 目标 | 默认值 |
|------|-------------------------|------|--------|
| MILD | `mildOffloadRatio` | 替换非当前任务工具结果为摘要 | 50% |
| AGGRESSIVE | `aggressiveCompressRatio` | 删除历史消息 + MMD 注入补偿 | 85% |
| EMERGENCY | `emergencyCompressRatio` | 强制删除到目标水位 | 95% |

**压缩流程**（[llm-input-l3.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/hooks/llm-input-l3.ts#L230-L320)）：

```
Token 使用率检测
    │
    ├── < mildThreshold → 不压缩
    │
    ├── >= aggressiveThreshold → AGGRESSIVE:
    │    1. 计算删除量：从最早消息开始删除
    │    2. 保护当前任务的节点
    │    3. 被删除的消息通过 MMD 历史注入补偿
    │    4. 如果被用户消息阻塞 → 标记 forceEmergencyNext
    │
    ├── >= mildThreshold → MILD:
    │    1. 扫描消息（按 replaceability score 排序）
    │    2. 替换非当前任务的工具结果为摘要
    │    3. 保留 tool_call_id 和 result_ref
    │
    └── >= emergencyThreshold → EMERGENCY:
         1. 保护 MMD 消息（提取出来，压缩后恢复）
         2. 从头部删除（跳过用户消息）
         3. 头部阻塞时从尾部删除最大消息
         4. 最后手段：截断超大消息内容到 2000 字符
```

#### 3.6.2 当前任务保护机制

**获取当前任务节点**（[l3-helpers.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/l3-helpers.ts#L306-L320)）：

```typescript
async function getCurrentTaskNodeIds(stateManager): Promise<Set<string>> {
  const activeMmdFile = stateManager.getActiveMmdFile();  // 当前活跃 MMD
  const mmdContent = await readMmd(stateManager.ctx, activeMmdFile);
  // 提取所有 node_id (如 001-N1, 001-N2)
  const nodePattern = /\b(\d+-N\d+|N\d+)\b/g;
  // ... 返回当前任务的所有节点 ID
}
```

**保护逻辑**：属于当前活跃 MMD 的所有节点在压缩时被跳过。

#### 3.6.3 工具调用结果处理

**after_tool_call hook**（[after-tool-call.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/hooks/after-tool-call.ts)）：

```
工具执行完成
    │
    ├── 1. 收集 ToolPair → pendingToolPairs 缓冲区
    │
    ├── 2. 写入 refs/{timestamp}.md（完整原始结果）
    │
    ├── 3. 检查 Token 压力
    │     ├── 充足 → 完整保留在上下文
    │     ├── 紧张 → 替换为摘要（含 result_ref）
    │     └── 极紧 → 删除（通过 MMD 注入补偿）
    │
    └── 4. 如果 L1.5 已确定边界，更新 MMD 注入
```

**摘要格式**（[l3-helpers.ts](file:///d:/code/TencentDB-Agent-Memory/src/offload/l3-helpers.ts#L224-L250)）：

```text
[Offloaded Tool Result | node: 001-N3]
Summary: 文件读取成功，包含 package.json 内容...
result_ref: refs/2024-01-15T10-30-00-000Z.md (read this file for full tool call and raw result)
```

---

## 四、记忆召回（Recall）

**触发时机**：每轮对话开始前（`before_agent_start` hook）

**核心文件**：[auto-recall.ts](file:///d:/code/TencentDB-Agent-Memory/src/core/hooks/auto-recall.ts)

**召回策略**（可配置）：

| 策略 | 检索方式 | 适用场景 |
|------|----------|----------|
| `keyword` | FTS5 BM25 全文搜索 | 精确关键词匹配 |
| `embedding` | 向量余弦相似度 | 语义相似度搜索 |
| `hybrid` | 两者合并 + RRF 排序 | 综合效果最优（默认） |

**召回内容**：

```
before_agent_start
    │
    ├── 1. L1 记忆搜索（与用户消息相关）
    │     → prependContext（动态，注入到用户消息前）
    │
    ├── 2. L3 画像读取
    │     → appendSystemContext（稳定，注入到系统消息后）
    │
    ├── 3. L2 场景导航生成
    │     → appendSystemContext（稳定，注入到系统消息后）
    │
    └── 4. 记忆工具使用指南
          → appendSystemContext（教 Agent 如何主动检索）
```

**调用限制**：每轮对话中 `tdai_memory_search` + `tdai_conversation_search` 合计最多调用 3 次。

---

## 五、工具调用处理流程

### 5.1 工具栏调用（OpenClaw Hooks）

```
用户输入
    │
    ├── before_agent_start:
    │     - L1.5 任务判定（新任务/继续/闲聊）
    │     - MMD 注入
    │     - L0 过滤心跳消息
    │
    ├── before_prompt_build:
    │     - Token 压力检测
    │     - 三级压缩（MILD/AGGRESSIVE/EMERGENCY）
    │     - MMD 注入（活跃任务 + 历史任务）
    │
    ├── LLM 响应 → tool_calls 执行
    │
    ├── after_tool_call:
    │     - 收集 ToolPair
    │     - 写入 refs/ + offload.jsonl
    │     - Token 检测（如果超标，即时压缩）
    │     - MMD 注入（如果 L2 已就绪）
    │
    └── agent_end:
          - 触发 L0 记录（auto-capture）
          - 通知 Memory Pipeline Manager
```

### 5.2 Memory Pipeline 后台处理

```
MemoryPipelineManager.notifyConversation()
    │
    ├── 累加 conversation_count
    │
    ├── 达到阈值 → 立即触发 L1:
    │     ├── 读取 L0 增量消息
    │     ├── LLM 提取结构记忆
    │     ├── 去重检测
    │     └── 写入 records/ + 向量库
    │
    ├── L1 完成 → 安排 L2:
    │     ├── 延迟 delayAfterL1Seconds
    │     ├── 读取 L1 新记忆
    │     ├── LLM Agent 更新场景文件
    │     └── 同步场景索引
    │
    └── L2 完成 → 触发 L3:
          ├── 全局互斥（同一时间只运行一个 L3）
          ├── 读取变化场景
          ├── LLM Agent 生成/更新 persona.md
          └── 更新检查点
```

---

## 六、配置参数

### 6.1 Memory Pipeline 配置

```typescript
interface PipelineConfig {
  everyNConversations: number;        // L1 触发阈值（默认 5）
  enableWarmup: boolean;              // 预热模式（默认 true）
  
  l1: {
    idleTimeoutSeconds: number;       // 空闲超时（默认 60s）
  };
  
  l2: {
    delayAfterL1Seconds: number;      // L1 后延迟（默认 90s）
    minIntervalSeconds: number;       // 最小间隔（默认 900s / 15min）
    maxIntervalSeconds: number;       // 最大间隔（默认 3600s / 1h）
    sessionActiveWindowHours: number; // 会话活跃窗口（默认 24h）
  };
}
```

### 6.2 Context Offload 配置

```typescript
interface PluginConfig {
  // LLM 配置
  model?: string;                     // 模型 ID
  temperature?: number;               // 温度（默认 0.2）
  
  // 触发阈值
  forceTriggerThreshold?: number;     // L1.5 触发阈值（默认 4）
  l2NullThreshold?: number;           // L2 null 数量阈值（默认 4）
  l2TimeoutSeconds?: number;          // L2 超时（默认 300s）
  
  // 上下文压缩
  defaultContextWindow?: number;      // 默认上下文窗口（默认 200000）
  mildOffloadRatio?: number;          // MILD 触发比例（默认 0.5）
  aggressiveCompressRatio?: number;   // AGGRESSIVE 触发比例（默认 0.85）
  aggressiveDeleteRatio?: number;     // AGGRESSIVE 删除比例（默认 0.4）
  emergencyCompressRatio?: number;    // EMERGENCY 触发比例（默认 0.95）
  emergencyTargetRatio?: number;      // EMERGENCY 目标比例（默认 0.6）
  
  // Token 计数
  l3TokenCountMode?: "tiktoken" | "heuristic";  // 计数模式（默认 tiktoken）
  l3TiktokenEncoding?: string;        // tiktoken 编码（默认 cl100k_base）
  
  // MMD
  mmdMaxTokenRatio?: number;          // MMD 最大 Token 比例（默认 0.2）
}
```

---

## 七、数据流完整示例

```
用户: "帮我修复登录页面的 bug"
    │
    ├── before_agent_start:
    │     - recall: 搜索相关记忆（无）
    │     - L1.5 判定: isLongTask=true, newTaskLabel="fix-login-bug"
    │
    ├── before_prompt_build:
    │     - Token 检测: 低 → 不压缩
    │     - 创建 001-fix-login-bug.mmd → 设为 active
    │
    ├── Agent: "好的，我来查看代码"
    │     read_file({ path: "/src/auth.js" })
    │
    ├── after_tool_call:
    │     - 写入 refs/2024-01-15T10-00-00-000Z.md（完整文件内容）
    │     - 写入 offload.jsonl（摘要）
    │     - Token 检测: 充足 → 完整保留
    │
    ├── [更多工具调用...]
    │     search_files, edit_file, run_command
    │
    ├── after_tool_call（积累 4 个 ToolPair）:
    │     - 触发 L1.5: 任务未完成，继续
    │
    ├── agent_end:
    │     - L0 记录: conversations/2024-01-15.jsonl
    │     - 通知 Pipeline Manager
    │
    │     [后台异步处理]
    │     - L1 提取: 提取记忆（约 5 条）
    │     - 延迟 90s → L2 提取:
    │         LLM Agent 更新场景文件
    │     - L2 完成 → L3 更新画像
    │
    ├── 用户: "注册页面也有同样的问题"
    │
    ├── L1.5 判定: taskCompleted=false, isContinuation=true
    │     → 继续使用 001-fix-login-bug.mmd
    │
    ├── after_tool_call（Token 达到 60%）:
    │     - MILD 压缩: 替换最早的工具结果为摘要
    │
    └── [继续迭代...]
```

---

## 八、关键设计原则

| 原则 | 具体表现 |
|------|----------|
| **渐进式披露** | 从完整结果 → 摘要 + 引用 → MMD 流程图 → 按需读取 |
| **当前任务保护** | 属于当前活跃 MMD 的节点在压缩时被跳过 |
| **MMD 永不被删** | EMERGENCY 压缩也会提前保护并恢复 MMD 消息 |
| **独立管道** | Memory Pipeline 使用独立 LLM 调用，不受上下文压缩影响 |
| **可追溯性** | 无论怎么压缩，都能通过 `result_ref` / `node_id` 找回原始数据 |
| **增量捕获** | 位置游标 + 时间游标双重保护，防止重复捕获 |
| **安全第一** | OpenClaw 模式使用 `CleanContextRunner` 执行 L2/L3，避免压缩干扰 |

## 九、注意事项

1. **两条独立的记忆线**：Memory Pipeline（L0-L3 场景+画像）和 Context Offload（L1.5-L2 MMD+L3 压缩）是**正交**的，不要混淆。
2. **存放路径不同**：Memory Pipeline 数据在 `~/.openclaw/memory-tdai/`；Context Offload 数据在 `~/.openclaw/context-offload/`。
3. **LLM 调用隔离**：Context Offload 使用 `local-llm/` 调用路线；Memory Pipeline 使用 `adapters/` 适配器路线。两者可以从不同的模型/API 获取服务。
4. **AGGRESSIVE 压缩的特殊性**：AGGRESSIVE 可以在一定程度上删除当前任务的工具调用结果，但会通过 MMD 历史注入来保留任务结构摘要。
