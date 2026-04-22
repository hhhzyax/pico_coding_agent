"""代码搜索工具实现。

提供在工作区搜索代码的功能。
"""

import shutil
import subprocess
from pathlib import Path


def tool_search(agent, args):
    """在工作区搜索代码。
    
    优先使用 ripgrep (rg)，如果不存在则使用简单回退方案。
    
    Args:
        agent: agent 实例
        args: {"pattern": "搜索模式", "path": "搜索路径"}
    
    Returns:
        搜索结果
    """
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    
    path = agent.path(args.get("path", "."))
    
    # 优先使用 rg
    if shutil.which("rg"):
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=agent.root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"
    
    # 回退方案：简单文本搜索
    matches = []
    for file_path in path.rglob("*.py"):
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(content.splitlines(), 1):
                if pattern in line:
                    matches.append(f"{file_path}:{i}:{line.strip()}")
                    if len(matches) >= 200:
                        break
        except Exception:
            continue
        if len(matches) >= 200:
            break
    
    return "\n".join(matches) if matches else "(no matches)"


# 工具定义
SEARCH_TOOL_SPEC = {
    "schema": {"pattern": "str", "path": "str='.'"},
    "risky": False,
    "description": "Search the workspace with rg or a simple fallback.",
}


# 使用示例
TOOL_EXAMPLES = {
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
}
