"""摘要缓冲区 — 存储工具调用后 agent 分析产生的逐条摘要。

设计原则：
- 零额外 LLM 成本：摘要来自 agent 自身的下一轮推理，不额外调用 LLM
- source_range 提供溯源能力：每条摘要记录覆盖的 history 索引范围
- 支持 append / flush / clear 三种基本操作
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def now_iso():
    return datetime.now().isoformat()


@dataclass
class SummaryEntry:
    """单条摘要条目。"""
    source_range: tuple[int, int]  # 覆盖 session["history"] 的 [start, end] 索引
    summary: str                    # 摘要文本
    timestamp: str = field(default_factory=now_iso)

    def to_dict(self):
        return {
            "source_range": list(self.source_range),
            "summary": self.summary,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            source_range=tuple(data["source_range"]),
            summary=data["summary"],
            timestamp=data.get("timestamp", now_iso()),
        )


class SummaryBuffer:
    """摘要缓冲区。

    存储 agent 工具调用后的逐条摘要，支持追加、清空和批量获取。
    缓冲区条目按时间顺序排列。
    """

    def __init__(self, max_entries: int = 200):
        self._entries: list[SummaryEntry] = []
        self.max_entries = max_entries

    def append(self, history_start: int, history_end: int, summary: str) -> SummaryEntry:
        """追加一条摘要。

        Args:
            history_start: 这条摘要覆盖的 history 起始索引
            history_end: 这条摘要覆盖的 history 结束索引
            summary: 摘要文本

        Returns:
            新创建的 SummaryEntry
        """
        entry = SummaryEntry(
            source_range=(history_start, history_end),
            summary=summary.strip(),
            timestamp=now_iso(),
        )
        self._entries.append(entry)
        # 防止无限膨胀
        if len(self._entries) > self.max_entries:
            self._entries = self._entries[-self.max_entries:]
        return entry

    def get_all(self) -> list[SummaryEntry]:
        """获取缓冲区中所有条目（按追加顺序）。"""
        return list(self._entries)

    def get_since(self, last_compression_index: int) -> list[SummaryEntry]:
        """获取自上次压缩以来的新条目。

        Args:
            last_compression_index: 上次压缩时处理的最后 history index

        Returns:
            所有 source_range 起始索引大于 last_compression_index 的条目
        """
        return [
            entry for entry in self._entries
            if entry.source_range[0] > last_compression_index
        ]

    def clear(self):
        """清空缓冲区。"""
        self._entries.clear()

    def __len__(self):
        return len(self._entries)

    def __bool__(self):
        return bool(self._entries)

    def summary_text(self) -> str:
        """以文本形式渲染缓冲区内容，用于快速查看。"""
        if not self._entries:
            return "(empty)"
        lines = []
        for entry in self._entries:
            lines.append(f"- [{entry.source_range[0]}-{entry.source_range[1]}] {entry.summary}")
        return "\n".join(lines)
