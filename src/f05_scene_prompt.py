"""F05 场景提示词撰写。

输入：F03 输出（多场景列表）+ F01 视觉基调 → 遍历每个场景调用 LLM → 输出全部场景定妆照提示词。
"""

# ── 确保 import 可用 ──
from pathlib import Path as _P
import sys as _sys
_PROJECT_ROOT = _P(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PROJECT_ROOT))

import json
import logging
import uuid
import re

from functools import lru_cache

from src.llm_client import get_llm_client

logger = logging.getLogger(__name__)

SKILL_PATH = _P(__file__).resolve().parent.parent / "skills" / "f05_scene_prompt.md"


@lru_cache(maxsize=1)
def _load_skill_content() -> str:
    """加载 F05 Skill 文件内容（lru_cache 保证只读一次磁盘）"""
    return SKILL_PATH.read_text(encoding="utf-8")


def _clean_text(text: str) -> str:
    """清理文本中的转义控制字符，返回干净的纯文本。"""
    if not isinstance(text, str):
        return text
    text = text.replace("\n", " ").replace("\r", "")
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def _clean_result(result: dict) -> dict:
    """递归清理 result 中所有字符串字段的转义控制字符。"""
    if isinstance(result, dict):
        return {k: _clean_result(v) for k, v in result.items()}
    elif isinstance(result, list):
        return [_clean_result(item) for item in result]
    elif isinstance(result, str):
        return _clean_text(result)
    return result


def generate_scene_prompt(scene: dict, visual_tone: dict, task_id: str = None) -> dict:
    """
    为单个场景生成定妆照提示词。

    Args:
        scene: F03 输出的单个场景对象 (scenes.list[i])
        visual_tone: F01 输出的完整视觉基调对象
        task_id: 任务 ID（可选，自动生成）

    Returns:
        {task_id, scene_id, final_prompt}
    """
    if task_id is None:
        task_id = str(uuid.uuid4())

    system_prompt = _load_skill_content()

    user_content = json.dumps({
        "task_id": task_id,
        "scene": scene,
        "visual_tone": visual_tone,
    }, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    logger.info("F05 开始处理: task_id=%s, scene=%s (%s)",
                task_id, scene.get("id"), scene.get("name"))

    client = get_llm_client()
    raw_response = client.chat(
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
        enable_thinking=True,  # 开启思考模式，提高提示词生成质量
    )

    result = json.loads(raw_response, strict=False)
    result["task_id"] = task_id
    result = _clean_result(result)

    logger.info("F05 处理完成: task_id=%s, scene=%s",
                task_id, result.get("scene_id"))
    return result


def generate_scene_prompts(f03_output: dict, f01_visual_tone: dict, max_workers: int = 5) -> dict:
    """
    为 F03 输出的所有场景批量生成定妆照提示词（主入口，并发版本）。

    输入 F03 的完整输出（含 scenes.list）和 F01 的视觉基调，
    并发调用 LLM 生成对应的定妆照提示词。

    Args:
        f03_output: F03 完整输出 JSON（包含 scenes.list 数组）
        f01_visual_tone: F01 完整输出中的 visual_tone 部分
        max_workers: 最大并发线程数（默认 5）

    Returns:
        {
            "task_id": "batch-UUID",
            "total_scenes": int,
            "results": [
                { "task_id", "scene_id", "final_prompt" },
                ...
            ],
            "metadata": { ... }
        }
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    batch_task_id = f"batch-{uuid.uuid4()}"
    # 兼容两种格式：直接数组或包含 list 属性的对象
    scenes_data = f03_output.get("scenes", [])
    if isinstance(scenes_data, dict) and "list" in scenes_data:
        scenes_list = scenes_data["list"]
    else:
        scenes_list = scenes_data

    if not scenes_list:
        raise ValueError("f03_output 中没有找到 scenes.list，请检查输入格式")

    total = len(scenes_list)
    logger.info("F05 批量处理开始: batch_task_id=%s, 场景数量=%d, 并发数=%d",
                batch_task_id, total, max_workers)

    def _process_one(args: tuple) -> tuple:
        i, scene = args
        scene_id = scene.get("id", f"unknown_{i}")
        scene_name = scene.get("name", "未知")
        logger.info("F05 批量 [%d/%d] 处理场景: %s (%s)", i + 1, total, scene_id, scene_name)
        try:
            single_result = generate_scene_prompt(scene, f01_visual_tone)
            return i, single_result
        except Exception as e:
            logger.error("F05 批量 [%d/%d] 场景 %s 处理失败: %s", i + 1, total, scene_id, e)
            return i, {
                "task_id": f"{batch_task_id}-{i}",
                "scene_id": scene_id,
                "final_prompt": "",
                "error": str(e),
            }

    indexed_results: list[tuple] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_one, (i, scene)): i for i, scene in enumerate(scenes_list)}
        for future in as_completed(futures):
            indexed_results.append(future.result())

    indexed_results.sort(key=lambda x: x[0])
    results = [r for _, r in indexed_results]

    output = {
        "task_id": batch_task_id,
        "total_scenes": len(scenes_list),
        "successful_count": sum(1 for r in results if "error" not in r),
        "failed_count": sum(1 for r in results if "error" in r),
        "results": results,
        "metadata": {
            "generated_at": __import__("datetime").datetime.now().isoformat(),
            "source_task_id": f03_output.get("task_id"),
        },
    }

    logger.info("F05 批量处理完成: batch_task_id=%s, 成功=%d, 失败=%d",
                 batch_task_id, output["successful_count"], output["failed_count"])
    return output


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="F05 场景提示词撰写（批量模式）")
    parser.add_argument("f03_output", help="F03 场景提取输出 JSON 文件路径")
    parser.add_argument("f01_output", help="F01 视觉基调输出 JSON 文件路径")
    parser.add_argument("-o", "--output", default="f05_output.json", help="输出文件路径 (默认: f05_output.json)")

    args = parser.parse_args()

    # 配置日志输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )

    import time
    start_time = time.time()

    print("=" * 60)
    print("F05 场景提示词撰写（批量模式）")
    print("=" * 60)
    print(f"F03 输入: {args.f03_output}")
    print(f"F01 输入: {args.f01_output}")
    print(f"输出文件: {args.output}")
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 读取 f03 输出（场景）
    with open(args.f03_output, "r", encoding="utf-8") as f:
        f03_data = json.load(f)

    scenes_data = f03_data.get("scenes", [])
    if isinstance(scenes_data, dict) and "list" in scenes_data:
        scenes_list = scenes_data["list"]
    else:
        scenes_list = scenes_data
    print(f"场景数量: {len(scenes_list)}")

    if scenes_list:
        print("\n场景列表:")
        for idx, scene in enumerate(scenes_list, 1):
            name = scene.get("name", "未知")
            location_type = scene.get("location_type", "未知")
            time_period = scene.get("time_period", "未知")
            print(f"  [{idx}] {name} ({location_type}, {time_period})")

    # 读取 f01 输出（视觉基调）
    with open(args.f01_output, "r", encoding="utf-8") as f:
        f01_data = json.load(f)

    visual_style = f01_data.get("visual_style", {})
    print(f"\n视觉基调: {visual_style.get('style_name', '未知')}")

    # 运行批量生成
    print("\n开始批量生成提示词...")
    result = generate_scene_prompts(f03_data, f01_data)

    # 显示结果摘要
    print("\n" + "=" * 60)
    print("处理结果:")
    print("=" * 60)
    print(f"批次 ID: {result.get('batch_task_id', 'N/A')}")
    print(f"成功: {result.get('successful_count', 0)} 个")
    print(f"失败: {result.get('failed_count', 0)} 个")

    if result.get("results"):
        print("\n生成的提示词:")
        for idx, res in enumerate(result["results"], 1):
            scene_id = res.get("scene_id", "N/A")
            status = "✓" if "error" not in res else "✗"
            if "error" not in res:
                prompt_text = res.get("final_prompt", "")
                # 截取前 100 个字符用于显示
                display_text = prompt_text[:100] if len(prompt_text) > 100 else prompt_text
                print(f"  [{idx}] {scene_id} {status} - {display_text}...")
            else:
                error = res.get("error", "未知错误")
                print(f"  [{idx}] {scene_id} {status} - 错误: {error}")

    # 保存结果
    output_path = _P(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    elapsed_time = time.time() - start_time
    print(f"\n结果已保存到: {output_path}")
    print(f"总耗时: {elapsed_time:.2f} 秒 ({elapsed_time/60:.2f} 分钟)")
    print("=" * 60)



