"""文件读取工具实现。

提供按行范围读取文件的功能，支持大文件的分段读取。
"""

from pathlib import Path


def tool_read_file(agent, args):
    """读取 UTF-8 文件的指定行范围。
    
    Args:
        agent: agent 实例（用于获取工作区根目录）
        args: {"path": "文件路径", "start": 1, "end": 200}
    
    Returns:
        格式化后的文件内容，包含行号
    """
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    
    # 读取文件
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total_lines = len(lines)
    
    # 格式化输出（带行号）
    body = "\n".join(
        f"{number:>4}: {line}" 
        for number, line in enumerate(lines[start - 1:end], start=start)
    )
    
    # 如果有更多内容，添加提示
    has_more = end < total_lines
    more_hint = ""
    if has_more:
        more_hint = f"\n# ... ({total_lines - end} more lines, use start={end+1} to continue)"
    
    return f"# {path.relative_to(agent.root)} (lines {start}-{min(end, total_lines)} of {total_lines})\n{body}{more_hint}"


# 工具定义
READ_FILE_TOOL_SPEC = {
    "schema": {"path": "str", "start": "int=1", "end": "int=200"},
    "risky": False,
    "description": "Read a UTF-8 file by line range.",
}


# 使用示例
TOOL_EXAMPLES = {
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
}
