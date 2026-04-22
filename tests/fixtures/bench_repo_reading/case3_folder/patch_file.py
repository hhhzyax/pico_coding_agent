"""文件补丁工具实现。

提供替换文件中指定文本块的功能。
"""

from pathlib import Path


def tool_patch_file(agent, args):
    """替换文件中的指定文本块。
    
    Args:
        agent: agent 实例
        args: {"path": "文件路径", "old_text": "旧文本", "new_text": "新文本"}
    
    Returns:
        操作结果
    """
    path = agent.path(args["path"])
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")
    
    # 读取文件
    content = path.read_text(encoding="utf-8")
    
    # 检查旧文本是否存在
    if old_text not in content:
        return f"error: old_text not found in {path.relative_to(agent.root)}"
    
    # 替换文本
    new_content = content.replace(old_text, new_text, 1)
    
    # 写回文件
    path.write_text(new_content, encoding="utf-8")
    
    return f"patched: {path.relative_to(agent.root)}"


# 工具定义
PATCH_FILE_TOOL_SPEC = {
    "schema": {"path": "str", "old_text": "str", "new_text": "str"},
    "risky": True,
    "description": "Replace one exact text block in a file.",
}


# 使用示例
TOOL_EXAMPLES = {
    "patch_file": '<tool name="patch_file" path="hello.py"><old_text>print("hello")</old_text><new_text>print("world")</new_text></tool>',
}
