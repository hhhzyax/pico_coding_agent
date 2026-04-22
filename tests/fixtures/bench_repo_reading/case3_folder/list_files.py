"""文件列表工具实现。

提供列出目录内容的功能。
"""

from pathlib import Path


def tool_list_files(agent, args):
    """列出工作区目录的内容。
    
    Args:
        agent: agent 实例
        args: {"path": "目录路径"}
    
    Returns:
        目录内容列表
    """
    path = agent.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    
    items = []
    for item in sorted(path.iterdir()):
        item_type = "dir" if item.is_dir() else "file"
        items.append(f"{item_type}: {item.name}")
    
    return "\n".join(items) if items else "(empty directory)"


# 工具定义
LIST_FILES_TOOL_SPEC = {
    "schema": {"path": "str='.'"},
    "risky": False,
    "description": "List files in the workspace.",
}


# 使用示例
TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
}
