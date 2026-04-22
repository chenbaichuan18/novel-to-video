"""F06 视频提示词生成 - 测试脚本"""

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


def main():
    from src.f06_video_prompt import run_f06_pipeline

    print("=" * 60)
    print("F06 测试：视频提示词生成（A 分段 + B 提示词）")
    print("=" * 60)

    # 从 fixtures 读输入
    inpath = os.path.join(PROJECT_ROOT, "tests", "fixtures", "f06_input.json")
    with open(inpath, "r", encoding="utf-8") as f:
        test_input = json.load(f)

    print(f"\n>>> 输入 (F06 格式): {inpath}")
    print(f"    task_id: {test_input['task_id']}")
    print(f"    原始文本长度: {len(test_input['original_text'])} 字")
    print(f"    人物数量: {len(test_input['characters'])}")
    print(f"    场景数量: {len(test_input['scenes'])}")

    # ── 阶段 A: 分段与绑定 ──
    t_start = time.time()
    result = run_f06_pipeline(
        original_text=test_input["original_text"],
        characters=test_input["characters"],
        scenes=test_input["scenes"],
        visual_tone=test_input["visual_tone"],
        task_id=test_input["task_id"],
    )
    elapsed = time.time() - t_start

    # 输出
    outpath = os.path.join(PROJECT_ROOT, "tests", "output", "f06_output.json")
    os.makedirs(os.path.dirname(outpath), exist_ok=True)
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 摘要
    stage_a = result.get("stage_a", {})
    stage_b = result.get("stage_b", {})

    print(f"\n{'='*60}")
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    print(f"F06 完成！耗时 {minutes}m {seconds}s")
    print(f"{'='*60}")
    print(f"\n>>> 阶段 A — 文本分段与实体绑定:")
    print(f"    总 segment 数: {stage_a.get('total_segments', 0)}")

    strategy = stage_a.get("segmentation_strategy", {})
    if strategy:
        print(f"    分段方法: {strategy.get('method', 'N/A')}")
        print(f"    平均长度: {strategy.get('avg_segment_length', 'N/A')} 字/段")

    res_stats = stage_a.get("resolution_statistics", {})
    if res_stats:
        print(f"    代词发现/消解: {res_stats.get('total_pronouns_found', 0)} / {res_stats.get('total_pronouns_resolved', 0)}")

    print(f"\n>>> 阶段 B — 视频提示词生成:")
    print(f"    总提示词数: {stage_b.get('total_video_prompts', 0)}")

    vps = stage_b.get("video_prompts", [])
    for i, vp in enumerate(vps):
        sid = vp.get("segment_id", "?")
        dur = vp.get("duration_seconds", "?")
        chars = ", ".join(vp.get("entity_bindings", {}).get("characters_present", []))
        scene = vp.get("entity_bindings", {}).get("scene_binding", {}).get("scene_name", "?")
        prompt_len = len(vp.get("final_video_prompt", ""))
        print(f"\n  [{i+1}] {sid} | {dur}s | 场景:{scene} | 角色:{chars} | 提示词:{prompt_len}字")
        # 打印前 120 字预览
        preview = vp.get("final_video_prompt", "")[:120]
        print(f"       {preview}...")

    print(f"\n>>> 完整结果已写入: {outpath}")
    return result


if __name__ == "__main__":
    main()
