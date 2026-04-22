"""验证案例1：读取单个文件并总结。

验证点：
1. 是否正确读取了 runtime_summary.py
2. 总结是否包含主要类（Pico, SessionStore, PromptPrefix）
3. 总结是否包含核心方法（ask, build_tools, build_prefix 等）
4. 架构总结是否合理
"""

import json
import re
import sys
from pathlib import Path


def extract_json_from_text(text):
    """从文本中提取 JSON 对象。"""
    # 尝试找到 JSON 块
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return None


def verify_case1():
    """验证案例1的结果。"""
    # 读取 agent 的输出（从 report.json 或 task_state 中获取）
    runs_dir = Path(".pico/runs")
    if not runs_dir.exists():
        print("FAIL: No runs directory found")
        return False
    
    # 找到最新的 report.json
    report_files = list(runs_dir.glob("*/report.json"))
    if not report_files:
        print("FAIL: No report.json found")
        return False
    
    latest_report = max(report_files, key=lambda p: p.stat().st_mtime)
    report = json.loads(latest_report.read_text(encoding="utf-8"))
    
    final_answer = report.get("final_answer", "")
    
    # 提取结构化输出
    structured = extract_json_from_text(final_answer)
    
    if not structured:
        print("FAIL: Could not extract structured JSON from answer")
        print(f"Answer: {final_answer[:500]}")
        return False
    
    # 验证字段
    checks = []
    
    # 1. 检查 files_read
    files_read = structured.get("files_read", [])
    has_runtime_file = any("runtime" in f.lower() for f in files_read)
    checks.append(("files_read contains runtime file", has_runtime_file))
    
    # 2. 检查 main_classes
    main_classes = structured.get("main_classes", [])
    expected_classes = ["Pico", "SessionStore", "PromptPrefix"]
    found_classes = sum(1 for cls in expected_classes 
                       if any(cls.lower() in c.lower() for c in main_classes))
    checks.append(("main_classes contains expected classes", found_classes >= 2))
    
    # 3. 检查 key_methods
    key_methods = structured.get("key_methods", [])
    method_names = [m.get("name", "").lower() for m in key_methods]
    expected_methods = ["ask", "build_tools", "build_prefix", "run_tool"]
    found_methods = sum(1 for m in expected_methods if m in method_names)
    checks.append(("key_methods contains core methods", found_methods >= 3))
    
    # 4. 检查 architecture_summary
    arch_summary = structured.get("architecture_summary", "")
    has_architecture = len(arch_summary) > 50  # 至少50字符
    checks.append(("architecture_summary is substantial", has_architecture))
    
    # 打印结果
    print("=" * 60)
    print("Case 1 Verification Results:")
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
    success = verify_case1()
    sys.exit(0 if success else 1)
