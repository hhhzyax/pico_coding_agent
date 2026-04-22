"""验证案例2：找到代码实现并讲解。

验证点：
1. 是否正确读取了 delegate_tool.py
2. 是否找到核心函数 tool_delegate
3. 是否讲解了父子 agent 的交互流程
4. 是否说明了深度限制机制
"""

import json
import re
import sys
from pathlib import Path


def extract_json_from_text(text):
    """从文本中提取 JSON 对象。"""
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return None


def verify_case2():
    """验证案例2的结果。"""
    runs_dir = Path(".pico/runs")
    if not runs_dir.exists():
        print("FAIL: No runs directory found")
        return False
    
    report_files = list(runs_dir.glob("*/report.json"))
    if not report_files:
        print("FAIL: No report.json found")
        return False
    
    latest_report = max(report_files, key=lambda p: p.stat().st_mtime)
    report = json.loads(latest_report.read_text(encoding="utf-8"))
    
    final_answer = report.get("final_answer", "")
    structured = extract_json_from_text(final_answer)
    
    if not structured:
        print("FAIL: Could not extract structured JSON from answer")
        print(f"Answer: {final_answer[:500]}")
        return False
    
    checks = []
    
    # 1. 检查 files_read
    files_read = structured.get("files_read", [])
    has_delegate_file = any("delegate" in f.lower() for f in files_read)
    checks.append(("files_read contains delegate_tool.py", has_delegate_file))
    
    # 2. 检查 core_function
    core_function = structured.get("core_function", "").lower()
    has_tool_delegate = "tool_delegate" in core_function or "delegate" in core_function
    checks.append(("core_function mentions tool_delegate", has_tool_delegate))
    
    # 3. 检查 interaction_flow
    interaction_flow = structured.get("interaction_flow", [])
    flow_text = " ".join(interaction_flow).lower()
    flow_keywords = ["parent", "child", "task", "summary", "return"]
    found_keywords = sum(1 for kw in flow_keywords if kw in flow_text)
    checks.append(("interaction_flow describes parent-child flow", found_keywords >= 3))
    
    # 4. 检查 depth_limit_mechanism
    depth_limit = structured.get("depth_limit_mechanism", "").lower()
    has_depth_check = "depth" in depth_limit and ("limit" in depth_limit or "check" in depth_limit)
    checks.append(("depth_limit_mechanism explains depth checking", has_depth_check))
    
    # 打印结果
    print("=" * 60)
    print("Case 2 Verification Results:")
    print("=" * 60)
    
    all_passed = True
    for check_name, passed in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {check_name}")
        if not passed:
            all_passed = False
    
    print("=" * 60)
    
    if all_passed:
        print("OVERALL: PASS")
        return True
    else:
        print("OVERALL: FAIL")
        print("\nExtracted JSON:")
        print(json.dumps(structured, indent=2, ensure_ascii=False))
        return False


if __name__ == "__main__":
    success = verify_case2()
    sys.exit(0 if success else 1)
