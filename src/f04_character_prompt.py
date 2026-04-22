"""F04 人物提示词撰写。

输入：F02 输出（多角色列表）+ F01 视觉基调 → 遍历每个角色调用 LLM → 输出全部定妆照提示词。
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

SKILL_PATH = _P(__file__).resolve().parent.parent / "skills" / "f04_character_prompt.md"


def _clean_text(text: str) -> str:
    """清理文本中的转义控制字符，返回干净的纯文本。"""
    if not isinstance(text, str):
        return text
    # 将 \n \r \t 等转义序列替换为空格（保留可读性）
    text = text.replace("\n", " ").replace("\r", "")
    # 合并多余空格
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


def generate_character_prompt(character: dict, visual_tone: dict, task_id: str = None) -> dict:
    """
    为单个角色生成定妆照提示词。

    Args:
        character: F02 输出的单个角色对象 (characters.list[i])
        visual_tone: F01 输出的完整视觉基调对象
        task_id: 任务 ID（可选，自动生成）

    Returns:
        {task_id, character_id, final_prompt}
    """
    if task_id is None:
        task_id = str(uuid.uuid4())

    system_prompt = SKILL_PATH.read_text(encoding="utf-8")

    user_content = json.dumps({
        "task_id": task_id,
        "character": character,
        "visual_tone": visual_tone,
    }, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    logger.info("F04 开始处理: task_id=%s, character=%s (%s)",
                task_id, character.get("id"), character.get("name"))

    client = get_llm_client()
    raw_response = client.chat(
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
    )

    result = json.loads(raw_response, strict=False)
    result["task_id"] = task_id
    result = _clean_result(result)

    logger.info("F04 处理完成: task_id=%s, character=%s",
                task_id, result.get("character_id"))
    return result


def generate_character_prompts(f02_output: dict, f01_visual_tone: dict) -> dict:
    """
    为 F02 输出的所有角色批量生成定妆照提示词（主入口）。

    输入 F02 的完整输出（含 characters.list）和 F01 的视觉基调，
    遍历每个角色调用 LLM 生成对应的定妆照提示词。

    Args:
        f02_output: F02 完整输出 JSON（包含 characters.list 数组）
        f01_visual_tone: F01 完整输出中的 visual_tone 部分

    Returns:
        {
            "task_id": "batch-UUID",
            "total_characters": int,
            "results": [
                { "task_id", "character_id", "final_prompt" },
                ...
            ],
            "metadata": {
                "generated_at": "ISO8601",
                "source_task_id": f02_output.task_id,
                "visual_tone_version": ...
            }
        }
    """
    batch_task_id = f"batch-{uuid.uuid4()}"
    # 兼容两种格式：直接数组或包含 list 属性的对象
    characters_data = f02_output.get("characters", [])
    if isinstance(characters_data, dict) and "list" in characters_data:
        characters_list = characters_data["list"]
    else:
        characters_list = characters_data

    if not characters_list:
        raise ValueError("f02_output 中没有找到 characters.list，请检查输入格式")

    logger.info("F04 批量处理开始: batch_task_id=%s, 角色数量=%d",
                batch_task_id, len(characters_list))

    results = []
    for i, char in enumerate(characters_list):
        char_id = char.get("id", f"unknown_{i}")
        char_name = char.get("name", "未知")
        logger.info("F04 批量 [%d/%d] 处理角色: %s (%s)",
                    i + 1, len(characters_list), char_id, char_name)

        try:
            single_result = generate_character_prompt(char, f01_visual_tone)
            results.append(single_result)
        except Exception as e:
            logger.error("F04 批量 [%d/%d] 角色 %s 处理失败: %s",
                         i + 1, len(characters_list), char_id, e)
            results.append({
                "task_id": f"{batch_task_id}-{i}",
                "character_id": char_id,
                "final_prompt": "",
                "error": str(e),
            })

    output = {
        "task_id": batch_task_id,
        "total_characters": len(characters_list),
        "successful_count": sum(1 for r in results if "error" not in r),
        "failed_count": sum(1 for r in results if "error" in r),
        "results": results,
        "metadata": {
            "generated_at": __import__("datetime").datetime.now().isoformat(),
            "source_task_id": f02_output.get("task_id"),
        },
    }

    logger.info("F04 批量处理完成: batch_task_id=%s, 成功=%d, 失败=%d",
                 batch_task_id, output["successful_count"], output["failed_count"])
    return output


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="F04 人物提示词撰写（批量模式）")
    parser.add_argument("f02_output", help="F02 角色提取输出 JSON 文件路径")
    parser.add_argument("f01_output", help="F01 视觉基调输出 JSON 文件路径")
    parser.add_argument("-o", "--output", default="f04_output.json", help="输出文件路径 (默认: f04_output.json)")

    args = parser.parse_args()

    # 配置日志输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )

    import time
    start_time = time.time()

    print("=" * 60)
    print("F04 人物提示词撰写（批量模式）")
    print("=" * 60)
    print(f"F02 输入: {args.f02_output}")
    print(f"F01 输入: {args.f01_output}")
    print(f"输出文件: {args.output}")
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 读取 f02 输出（角色）
    with open(args.f02_output, "r", encoding="utf-8") as f:
        f02_data = json.load(f)

    characters_data = f02_data.get("characters", [])
    if isinstance(characters_data, dict) and "list" in characters_data:
        characters_list = characters_data["list"]
    else:
        characters_list = characters_data
    print(f"角色数量: {len(characters_list)}")

    if characters_list:
        print("\n角色列表:")
        for idx, char in enumerate(characters_list, 1):
            name = char.get("name", "未知")
            gender = char.get("gender", "未知")
            age = char.get("age", "未知")
            print(f"  [{idx}] {name} ({gender}, {age})")

    # 读取 f01 输出（视觉基调）
    with open(args.f01_output, "r", encoding="utf-8") as f:
        f01_data = json.load(f)

    visual_style = f01_data.get("visual_style", {})
    print(f"\n视觉基调: {visual_style.get('style_name', '未知')}")

    # 运行批量生成
    print("\n开始批量生成提示词...")
    result = generate_character_prompts(f02_data, f01_data)

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
            char_id = res.get("character_id", "N/A")
            status = "✓" if "error" not in res else "✗"
            if "error" not in res:
                prompt_text = res.get("final_prompt", "")
                # 截取前 100 个字符用于显示
                display_text = prompt_text[:100] if len(prompt_text) > 100 else prompt_text
                print(f"  [{idx}] {char_id} {status} - {display_text}...")
            else:
                error = res.get("error", "未知错误")
                print(f"  [{idx}] {char_id} {status} - 错误: {error}")

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

