"""异步记忆处理管道。

MemoryPipeline 是"生产者"，ContextManager 是"消费者"。
它在后台线程中处理记忆提取、摘要生成和压缩合并，不阻塞 agent 主循环。

设计原则：
- 热路径只做 O(1) 计数器累加
- LLM 调用在后台线程执行
- 阈值触发 + 轮次兜底，双重保障
"""

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .summary_buffer import SummaryBuffer, SummaryEntry
from .summary_compressor import compress as llm_compress


@dataclass
class PipelineConfig:
    """管道配置。

    触发机制：
    - token_threshold_ratio: 上下文 token 占用比例阈值（默认 75%）
    - round_fallback: 轮次兜底，距上次压缩超过此轮数时触发（默认 20）
    - idle_timeout_seconds: 空闲超时兜底（默认 120s）
    """
    token_threshold_ratio: float = 0.75
    round_fallback: int = 20
    idle_timeout_seconds: int = 120


class MemoryPipeline:
    """后台记忆处理管道。

    核心入口：
    - notify_agent_response(): agent 返回响应后调用（提取工具调用摘要）
    - check_and_compress(): 主循环中每次检查（判断是否需要压缩）
    """

    def __init__(self, agent, config=None):
        self.agent = agent
        self.config = config or PipelineConfig()

        # 摘要缓冲区
        self.buffer = SummaryBuffer()

        # 累计摘要状态
        self.cumulative_summary: str | None = None
        self.cumulative_source_range: tuple[int, int] | None = None

        # 压缩状态
        self.last_compression_history_index: int = -1
        self._rounds_since_last_compression: int = 0
        self._compression_in_progress: bool = False
        self._compression_lock = threading.Lock()

        # 空闲监控
        self._last_activity = time.monotonic()
        self._stop_event = threading.Event()
        self._idle_thread = None

    # ── 热路径（O(1)）──

    def notify_tool_executed(self):
        """工具执行后调用 — O(1)。"""
        self._rounds_since_last_compression += 1
        self._last_activity = time.monotonic()

    def notify_agent_response(self, response_text: str, history_start: int, history_end: int):
        """agent 返回响应后调用。

        从响应文本中提取对工具调用结果的分析内容作为摘要，
        存入缓冲区。不调用 LLM，零额外成本。

        Args:
            response_text: agent 的响应文本（assistant role content）
            history_start: 此次工具调用在 history 中的起始索引
            history_end: 此次工具调用在 history 中的结束索引
        """
        summary = self._extract_summary(response_text)
        if summary:
            self.buffer.append(history_start, history_end, summary)
        self._last_activity = time.monotonic()

    # ── 压缩触发 ──

    def check_and_compress(self, current_token_usage: int, total_budget: int):
        """主循环检查 — 超阈值则触发后台压缩。

        Args:
            current_token_usage: 当前估算的 token 用量
            total_budget: 总 token 预算
        """
        should_compress = False

        # 主触发：token 阈值
        if total_budget > 0 and current_token_usage >= total_budget * self.config.token_threshold_ratio:
            should_compress = True

        # 兜底触发：轮次
        if self._rounds_since_last_compression >= self.config.round_fallback:
            should_compress = True

        if should_compress and self.buffer:
            self._schedule_compression()

    # ── 后台压缩 ──

    def _schedule_compression(self):
        """安排一次后台压缩。非阻塞。"""
        with self._compression_lock:
            if self._compression_in_progress:
                return
            self._compression_in_progress = True

        thread = threading.Thread(target=self._run_compression, daemon=True)
        thread.start()

    def _run_compression(self):
        """后台执行摘要合并。

        将摘要缓冲区中的新条目与已有累计摘要合并。
        """
        try:
            new_items = self.buffer.get_all()
            if not new_items:
                return

            # 尝试使用 LLM 压缩（如果可用）
            new_summary, new_range = self._llm_compress(new_items)

            # 更新累计摘要
            self.cumulative_summary = new_summary
            self.cumulative_source_range = new_range

            # 更新压缩状态
            if new_items:
                last_item = new_items[-1]
                self.last_compression_history_index = last_item.source_range[1]

            self.buffer.clear()
            self._rounds_since_last_compression = 0
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            with self._compression_lock:
                self._compression_in_progress = False

    def _llm_compress(self, new_items: list[SummaryEntry]) -> tuple[str, tuple[int, int]]:
        """使用 LLM 合并摘要。"""
        model_client = getattr(self.agent, "model_client", None)
        return llm_compress(
            model_client,
            self.cumulative_summary,
            self.cumulative_source_range,
            new_items,
        )

    # ── 摘要提取 ──

    @staticmethod
    def _extract_summary(response_text: str) -> str | None:
        """从 agent 响应中提取对工具结果的分析摘要。"""
        text = str(response_text).strip()
        if not text:
            return None

        # 清理常见的包装标记
        for tag in ("<final>", "</final>", "<tool>", "</tool>"):
            text = text.replace(tag, "")

        text = text.strip()
        if not text:
            return None

        # 取第一个有实质意义的行（跳过工具调用语法行）
        lines = text.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("<"):
                continue
            # 跳过单行的工具名/路径（不太可能是自然语言摘要）
            words = line.split()
            if len(words) <= 1:
                # 单行且很短 → 可能是工具名或裸路径
                if len(line) < 40:
                    continue
            # 跳过纯路径行
            if line.count("/") >= 1 and len(line) < 40:
                continue
            first_line = line
            break
        else:
            first_line = text.split("\n")[0].strip()

        # 太短的内容通常不包含有价值的摘要
        if len(first_line) < 30:
            return None

        # 限制长度
        if len(first_line) > 200:
            first_line = first_line[:197] + "..."

        return first_line

    # ── 空闲监控 ──

    def start_idle_watcher(self):
        """启动空闲监控线程。"""
        if self._idle_thread is not None:
            return

        def _watch():
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=10)
                elapsed = time.monotonic() - self._last_activity
                if elapsed >= self.config.idle_timeout_seconds and self.buffer:
                    self._schedule_compression()

        self._idle_thread = threading.Thread(target=_watch, daemon=True)
        self._idle_thread.start()

    def stop_idle_watcher(self):
        """停止空闲监控线程。"""
        self._stop_event.set()
        if self._idle_thread is not None:
            self._idle_thread.join(timeout=5)
            self._idle_thread = None

    # ── 持久化 ──

    def save_records(self, session_id: str):
        """将摘要相关状态持久化到磁盘。

        存储位置: .pico/memory/records/{session_id}.jsonl
        """
        workspace_root = getattr(self.agent, "root", None)
        if workspace_root is None:
            return

        records_dir = Path(workspace_root) / ".pico" / "memory" / "records"
        records_dir.mkdir(parents=True, exist_ok=True)

        import json
        record_path = records_dir / f"{session_id}.jsonl"

        entries = []
        if self.cumulative_summary:
            entries.append({
                "type": "summary",
                "source_range": list(self.cumulative_source_range or (0, 0)),
                "content": self.cumulative_summary,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })

        # 追加写入
        with record_path.open("a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
