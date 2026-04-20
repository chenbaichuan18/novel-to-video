"""F02 人物元数据提取 - 测试脚本"""

import sys
import os
import json

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.chdir(PROJECT_ROOT)

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from src.f02_character_extract import extract_characters

FIXTURES_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "tests", "output")


def main():
    print("=" * 60)
    print("F02 测试：人物元数据提取")
    print("=" * 60)

    # 从 fixtures 读取输入（按 F02 输入格式: {task_id, text}）
    inpath = os.path.join(FIXTURES_DIR, "f02_input.json")
    with open(inpath, "r", encoding="utf-8") as f:
        test_input = json.load(f)
    print(f"\n>>> 输入 (F02 格式): {inpath}")
    print(f"    task_id: {test_input['task_id']}")
    print(f"    文本长度: {len(test_input['text'])} 字")

    # 调用 F02
    result = extract_characters(test_input["text"], task_id=test_input["task_id"])

    # 写入输出文件
    outpath = os.path.join(OUTPUT_DIR, "f02_output.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    total = result.get("characters", {}).get("total", 0)
    chars = result.get("characters", {}).get("list", [])
    print(f"\n>>> 提取人物总数: {total}")
    for ch in chars:
        traits = ", ".join(ch.get("key_traits", []))
        print(f"    [{ch['id']}] {ch['name']} ({ch['gender']}, {ch['age']}) - {ch['role_type']} | {traits}")

    summary = result.get("extraction_summary", {})
    print(f">>> 推断字段: {summary.get('inferred_fields', [])}")

    print(f"\n>>> 完整结果已写入: {outpath}")
    return result


if __name__ == "__main__":
    main()
