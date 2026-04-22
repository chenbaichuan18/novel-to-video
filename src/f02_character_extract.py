"""F02 人物元数据提取。

输入：小说全文文本 + task_id → 调用 LLM + Skill 提示词 → 输出结构化人物元数据 JSON。
"""

# ── 确保 import 可用（支持直接 python src/f02_xxx.py 运行）──
from pathlib import Path as _P
import sys as _sys
_PROJECT_ROOT = _P(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_PROJECT_ROOT))

import json
import logging
import uuid

from src.llm_client import get_llm_client

logger = logging.getLogger(__name__)

# ── Skill 文件路径 ────────────────────────────────────────
SKILL_PATH = _P(__file__).resolve().parent.parent / "skills" / "f02_character_extract.md"


def extract_characters(text: str, task_id: str = None) -> dict:
    """
    从小说文本中提取人物元数据。

    Args:
        text: 小说全文文本
        task_id: 任务 ID（可选，自动生成）

    Returns:
        结构化的 F02 输出 JSON (dict)
    """
    if task_id is None:
        task_id = str(uuid.uuid4())

    # 读取 Skill 提示词
    system_prompt = SKILL_PATH.read_text(encoding="utf-8")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]

    logger.info("F02 开始处理: task_id=%s, 文本长度=%d 字", task_id, len(text))

    client = get_llm_client()
    raw_response = client.chat(
        messages=messages,
        temperature=0.7,
        max_tokens=8192,  # 增加以支持完整剧本的人物提取
    )

    # 解析 LLM 返回的 JSON
    logger.info(f"LLM 原始响应长度: {len(raw_response)} 字")
    # 尝试清理响应（去除可能的 markdown 代码块标记）
    cleaned_response = raw_response.strip()
    if cleaned_response.startswith("```json"):
        cleaned_response = cleaned_response[7:]
    if cleaned_response.startswith("```"):
        cleaned_response = cleaned_response[3:]
    if cleaned_response.endswith("```"):
        cleaned_response = cleaned_response[:-3]
    cleaned_response = cleaned_response.strip()

    try:
        result = json.loads(cleaned_response)
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}")
        logger.error(f"原始响应前500字: {raw_response[:500]}")
        logger.error(f"清理后响应前500字: {cleaned_response[:500]}")
        logger.error(f"响应后200字: {raw_response[-200:]}")
        raise

    # 强制覆盖 task_id 确保一致
    result["task_id"] = task_id

    logger.info("F02 处理完成: task_id=%s, 提取人物=%d 人",
                task_id, result.get("characters", {}).get("total", 0))
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="F02 人物元数据提取")
    parser.add_argument("novel_txt", help="原始小说文本文件路径")
    parser.add_argument("-o", "--output", default="f02_output.json", help="输出文件路径 (默认: f02_output.json)")
    parser.add_argument("--task-id", default=None, help="任务 ID (默认自动生成)")

    args = parser.parse_args()

    # 配置日志输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )

    print("=" * 60)
    print("F02 人物元数据提取")
    print("=" * 60)
    print(f"输入文件: {args.novel_txt}")
    print(f"输出文件: {args.output}")

    # 读取原始小说文本
    novel_txt_path = _P(args.novel_txt)
    text = novel_txt_path.read_text(encoding="utf-8")
    print(f"文本长度: {len(text)} 字")

    # 运行提取
    print("\n开始调用 LLM...")
    result = extract_characters(
        text=text,
        task_id=args.task_id,
    )

    # 显示结果摘要
    print("\n" + "=" * 60)
    print("处理结果:")
    print("=" * 60)
    print(f"任务 ID: {result.get('task_id', 'N/A')}")

    characters_data = result.get("characters", {})
    total = characters_data.get("total", 0)
    char_list = characters_data.get("list", [])
    print(f"提取人物数: {total} 人")

    if char_list:
        print("\n人物列表:")
        for idx, char in enumerate(char_list, 1):
            name = char.get("name", "未知")
            gender = char.get("gender", "未知")
            age = char.get("age", "未知")
            print(f"  [{idx}] {name} ({gender}, {age})")

    # 保存结果
    output_path = _P(args.output)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n结果已保存到: {output_path}")
    print("=" * 60)


