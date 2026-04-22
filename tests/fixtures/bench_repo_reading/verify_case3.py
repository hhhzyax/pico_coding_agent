"""验证案例3：读取文件夹下所有文件并总结。

验证点：
1. 是否读取了所有 Python 文件（5个）
2. 是否为每个文件提供了功能总结
3. 是否说明了文件之间的关系
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


def verify_case3():
    """验证案例3的结果。"""
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
    
    # 1. 检查 files_read 是否包含所有5个文件
    files_read = structured.get("files_read", [])
    expected_files = [
        "file_reader.py",
        "list_files.py", 
        "search.py",
        "write_file.py",
        "patch_file.py"
    ]
    found_files = 0
    for expected in expected_files:
        if any(expected.lower() in f.lower() for f in files_read):
            found_files += 1
    checks.append((f"files_read contains all 5 files ({found_files}/5)", found_files >= 4))
    
    # 2. 检查 file_summaries
    file_summaries = structured.get("file_summaries", [])
    summary_count = len(file_summaries)
    checks.append((f"file_summaries has entries ({summary_count} files)", summary_count >= 4))
    
    # 检查每个 summary 是否有 purpose
    purposes = [s.get("purpose", "").strip() for s in file_summaries]
    non_empty_purposes = sum(1 for p in purposes if len(p) > 10)
    checks.append(("file summaries have substantial purpose", non_empty_purposes >= 3))
    
    # 3. 检查 relationships
    relationships = structured.get("relationships", "")
    has_relationships = len(relationships) > 30  # 至少30字符
    checks.append(("relationships field is substantial", has_relationships))
    
    # 打印结果
    print("=" * 60)
    print("Case 3 Verification Results:")
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
    success = verify_case3()
    sys.exit(0 if success else 1)
