"""F05 场景提示词撰写 - 测试脚本"""

import sys
import os
import json
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.chdir(PROJECT_ROOT)

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from src.f05_scene_prompt import generate_scene_prompts

FIXTURES_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "tests", "output")


def main():
    print("=" * 60)
    print("F05 测试：场景提示词撰写（批量模式）")
    print("=" * 60)

    # 用法: python tests/test_f05.py [f05_input.json]
    # 默认读取 tests/fixtures/f05_input.json
    input_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(FIXTURES_DIR, "f05_input.json")

    with open(input_path, "r", encoding="utf-8") as f:
        test_data = json.load(f)

    scenes_list = test_data.get("scenes", {}).get("list", [])
    visual_tone = test_data.get("visual_tone", {})

    print(f"\n>>> 输入: {input_path}")
    print(f"    task_id: {test_data.get('task_id')}")
    print(f"    场景数量: {len(scenes_list)}")
    for s in scenes_list:
        print(f"      - [{s['id']}] {s['name']} ({s['scene_type']}/{s['location_type']})")
    print(f"    视觉基调: {visual_tone.get('genre', {}).get('primary')}")

    start_time = time.time()
    result = generate_scene_prompts(test_data, visual_tone)
    elapsed = time.time() - start_time

    outpath = os.path.join(OUTPUT_DIR, "f05_output.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n>>> 批量结果:")
    print(f"    total_scenes: {result['total_scenes']}")
    print(f"    successful_count: {result.get('successful_count')}")
    print(f"    failed_count: {result.get('failed_count', 0)}")

    for r in result.get("results", []):
        prompt = r.get("final_prompt", "")
        sid = r.get("scene_id", "?")
        status = "OK" if "error" not in r else f"FAIL({r.get('error', '')})"
        print(f"      {status} {sid}: {len(prompt)} 字")

    print(f"\n>>> 完整结果已写入: {outpath}")
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    print(f">>> 耗时: {minutes}m {seconds}s")
    return result


if __name__ == "__main__":
    main()
