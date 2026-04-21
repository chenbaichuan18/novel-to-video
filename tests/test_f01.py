"""F01 导演视觉基调提取 - 测试脚本"""

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

from src.f01_visual_tone import extract_visual_tone

FIXTURES_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "tests", "output")


def main():
    print("=" * 60)
    print("F01 测试：导演视觉基调提取")
    print("=" * 60)

    # 从 fixtures 读取输入（按 F01 输入格式: {task_id, text, user_settings}）
    inpath = os.path.join(FIXTURES_DIR, "f01_input.json")
    with open(inpath, "r", encoding="utf-8") as f:
        test_input = json.load(f)
    print(f"\n>>> 输入 (F01 格式): {inpath}")
    print(f"    task_id: {test_input['task_id']}")
    print(f"    文本长度: {len(test_input['text'])} 字")
    user_settings = test_input.get("user_settings", {})
    if user_settings:
        print(f"    用户设置: {json.dumps(user_settings, ensure_ascii=False)}")

    # 调用 F01
    result = extract_visual_tone(
        test_input["text"],
        user_settings=user_settings,
        task_id=test_input["task_id"]
    )

    # 写入输出文件
    outpath = os.path.join(OUTPUT_DIR, "f01_output.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n>>> 返回字段 ({len(result)} 个):")
    for key in result:
        val = result[key]
        if isinstance(val, dict):
            print(f"    {key}: {{...}}  ({len(val)} 子键)")
        elif isinstance(val, list):
            print(f"    {key}: [{len(val)} 项]")
        else:
            preview = str(val)[:60]
            print(f"    {key}: {preview}")

    print(f"\n>>> 完整结果已写入: {outpath}")
    return result


if __name__ == "__main__":
    main()
