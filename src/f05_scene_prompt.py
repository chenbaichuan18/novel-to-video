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

from src.llm_client import get_llm_client

logger = logging.getLogger(__name__)

SKILL_PATH = _P(__file__).resolve().parent.parent / "skills" / "f05_scene_prompt.md"


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

    system_prompt = SKILL_PATH.read_text(encoding="utf-8")

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
    )

    result = json.loads(raw_response, strict=False)
    result["task_id"] = task_id
    result = _clean_result(result)

    logger.info("F05 处理完成: task_id=%s, scene=%s",
                task_id, result.get("scene_id"))
    return result


def generate_scene_prompts(f03_output: dict, f01_visual_tone: dict) -> dict:
    """
    为 F03 输出的所有场景批量生成定妆照提示词（主入口）。

    输入 F03 的完整输出（含 scenes.list）和 F01 的视觉基调，
    遍历每个场景调用 LLM 生成对应的定妆照提示词。

    Args:
        f03_output: F03 完整输出 JSON（包含 scenes.list 数组）
        f01_visual_tone: F01 完整输出中的 visual_tone 部分

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
    batch_task_id = f"batch-{uuid.uuid4()}"
    # 兼容两种格式：直接数组或包含 list 属性的对象
    scenes_data = f03_output.get("scenes", [])
    if isinstance(scenes_data, dict) and "list" in scenes_data:
        scenes_list = scenes_data["list"]
    else:
        scenes_list = scenes_data

    if not scenes_list:
        raise ValueError("f03_output 中没有找到 scenes.list，请检查输入格式")

    logger.info("F05 批量处理开始: batch_task_id=%s, 场景数量=%d",
                batch_task_id, len(scenes_list))

    results = []
    for i, scene in enumerate(scenes_list):
        scene_id = scene.get("id", f"unknown_{i}")
        scene_name = scene.get("name", "未知")
        logger.info("F05 批量 [%d/%d] 处理场景: %s (%s)",
                    i + 1, len(scenes_list), scene_id, scene_name)

        try:
            single_result = generate_scene_prompt(scene, f01_visual_tone)
            results.append(single_result)
        except Exception as e:
            logger.error("F05 批量 [%d/%d] 场景 %s 处理失败: %s",
                         i + 1, len(scenes_list), scene_id, e)
            results.append({
                "task_id": f"{batch_task_id}-{i}",
                "scene_id": scene_id,
                "final_prompt": "",
                "error": str(e),
            })

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



