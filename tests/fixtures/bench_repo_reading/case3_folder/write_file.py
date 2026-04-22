"""文件写入工具实现。

提供写入文本文件的功能。
"""

from pathlib import Path


def tool_write_file(agent, args):
    """写入文本文件。
    
    Args:
        agent: agent 实例
        args: {"path": "文件路径", "content": "文件内容"}
    
    Returns:
        操作结果
    """
    path = agent.path(args["path"])
    content = args.get("content", "")
    
    # 确保父目录存在
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # 写入文件
    path.write_text(content, encoding="utf-8")
    
    return f"written: {path.relative_to(agent.root)}"


# 工具定义
WRITE_FILE_TOOL_SPEC = {
    "schema": {"path": "str", "content": "str"},
    "risky": True,
    "description": "Write a text file.",
}


# 使用示例
TOOL_EXAMPLES = {
    "write_file": '<tool name="write_file" path="hello.py"><content>print("hello")</content></tool>',
}
