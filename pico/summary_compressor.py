"""LLM 驱动的摘要合并器。

提供独立的 LLM 摘要压缩功能，供 MemoryPipeline 使用。
核心能力：将多份摘要合并为一份紧凑的累计摘要。
"""

from .summary_buffer import SummaryEntry

COMPRESSION_PROMPT = """你是一个对话摘要生成器。你会收到：
1. 【已有摘要】一份覆盖之前对话历史的累计摘要（可能为空）
2. 【新进展】最近几轮工具调用的逐条摘要

请生成一份合并后的累计摘要，要求：
- 按时间顺序组织
- 保留所有关键事实：修改了哪些文件、发现了什么问题、做了什么决策
- 去重：同一事实只出现一次
- 控制在一段话内，不超过 300 字

【已有摘要】
{previous_summary}

【新进展】
{new_items_text}

请输出合并后的累计摘要（纯文本，不需要标记）："""


def build_compression_prompt(
    previous_summary: str | None,
    new_items: list[SummaryEntry],
) -> str:
    """构建压缩 prompt。

    Args:
        previous_summary: 已有的累计摘要，为空时表示首次压缩
        new_items: 摘要缓冲区中的新条目

    Returns:
        发送给 LLM 的完整 prompt 文本
    """
    previous = previous_summary or "（无，这是首次压缩）"

    new_items_text = "\n".join(
        f"- [{entry.source_range[0]}-{entry.source_range[1]}] {entry.summary}"
        for entry in new_items
    )

    return COMPRESSION_PROMPT.format(
        previous_summary=previous,
        new_items_text=new_items_text,
    )


def call_llm_compress(model_client, prompt: str, max_tokens: int = 400) -> str:
    """调用 LLM 执行摘要合并。

    Args:
        model_client: LLM 客户端（需要有 complete 方法）
        prompt: 压缩 prompt
        max_tokens: 最大输出 token 数

    Returns:
        合并后的摘要文本，已清理格式标记
    """
    messages = [{"role": "user", "content": prompt}]

    try:
        result = model_client.complete(messages, max_new_tokens=max_tokens)
    except TypeError:
        result = model_client.complete(messages)

    return _clean_llm_output(str(result))


def template_merge(
    previous_summary: str | None,
    new_items: list[SummaryEntry],
) -> tuple[str, tuple[int, int]]:
    """无 LLM 时的模板拼接回退。

    Args:
        previous_summary: 已有的累计摘要
        new_items: 新摘要条目列表

    Returns:
        (合并后的摘要文本, 新的 source_range)
    """
    if previous_summary:
        base = previous_summary
    else:
        base = ""

    if not new_items:
        return base, (0, 0)

    new_text = "；".join(entry.summary for entry in new_items)

    if base:
        merged = f"{base}；{new_text}"
    else:
        merged = new_text

    # 计算新的 source_range
    if new_items:
        new_range = (new_items[0].source_range[0], new_items[-1].source_range[1])
    else:
        new_range = (0, 0)

    return merged, new_range


def compress(
    model_client,
    previous_summary: str | None,
    previous_source_range: tuple[int, int] | None,
    new_items: list[SummaryEntry],
) -> tuple[str, tuple[int, int]]:
    """执行摘要压缩（优先 LLM，失败时回退到模板）。

    Args:
        model_client: LLM 客户端
        previous_summary: 已有的累计摘要
        previous_source_range: 已有累计摘要覆盖的 history 范围
        new_items: 新摘要条目

    Returns:
        (合并后的累计摘要, 新的 source_range)
    """
    if not new_items:
        if previous_summary:
            return previous_summary, previous_source_range or (0, 0)
        return "", (0, 0)

    # 计算新的 source_range
    if previous_source_range:
        new_start = previous_source_range[0]
    else:
        new_start = new_items[0].source_range[0]
    new_end = max(item.source_range[1] for item in new_items)
    new_range = (new_start, new_end)

    if model_client is None:
        merged, _ = template_merge(previous_summary, new_items)
        return merged, new_range

    try:
        prompt = build_compression_prompt(previous_summary, new_items)
        merged = call_llm_compress(model_client, prompt)
        if merged:
            return merged, new_range
    except Exception:
        pass

    # LLM 失败回退
    merged, _ = template_merge(previous_summary, new_items)
    return merged, new_range


def _clean_llm_output(text: str) -> str:
    """清理 LLM 输出中的格式标记。"""
    text = text.strip()
    for prefix in ("```markdown", "```plain", "```text", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text
