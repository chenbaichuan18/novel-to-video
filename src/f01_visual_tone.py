"""F01 导演视觉基调提取。

输入：小说全文文本 + 用户自定义设置 → 调用 LLM + Skill 提示词 → 输出结构化视觉基调 JSON。
用户可自定义：制作媒体、类型、年代、地点（直接填充）；其余字段由 LLM 从剧本分析提取。
"""

# ── 确保 import 可用（支持直接 python src/f01_xxx.py 运行）──
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

# ── medium 中文 → 枚举值映射 ─────────────────────────────
_MEDIUM_MAP = {
    "真人电影": "cinematic",
    "动画": "anime",
    "电视剧": "tv_series",
    "纪录片": "documentary",
}


def _resolve_medium(medium: str) -> str:
    """将用户输入的中文 medium 映射为英文枚举值，已是枚举值则直接返回。"""
    if not medium:
        return ""
    if medium.lower() in ("cinematic", "anime", "tv_series", "documentary"):
        return medium.lower()
    return _MEDIUM_MAP.get(medium, medium)

# ── Skill 文件路径 ────────────────────────────────────────
SKILL_PATH = _P(__file__).resolve().parent.parent / "skills" / "f01_visual_tone.md"


def load_skill_prompt() -> str:
    """加载 Skill 文件内容作为 system prompt。"""
    return SKILL_PATH.read_text(encoding="utf-8")


def extract_visual_tone(text: str, user_settings: dict | None = None, task_id: str | None = None) -> dict:
    """
    执行 F01：从小说全文中提取导演视觉基调。

    Args:
        text: 小说全文文本
        user_settings: 用户自定义的基础设置（可选），包含：
            - medium: 制作媒体类型，如 "cinematic" / "anime"
            - genre: 作品类型，如 "悬疑推理" / "仙侠" / "科幻"
            - era: 年代背景，如 "现代2020s" / "民国1930s"
            - location: 地点背景，如 "中国北方一线城市" / "架空仙侠世界"
            若不传或某字段为空，则由 LLM 自行推断。
        task_id: 任务标识（UUID）；若不传则自动生成

    Returns:
        结构化的视觉基调字典，包含 genre/visual_style/color_palette 等字段
    """
    if task_id is None:
        task_id = str(uuid.uuid4())
    if user_settings is None:
        user_settings = {}

    client = get_llm_client()
    system_prompt = load_skill_prompt()

    # 构造用户消息：传入原始文本 + 用户自定义设置
    user_content = json.dumps({
        "novel_text": text,
        "user_settings": {
            "medium": user_settings.get("medium", ""),
            "genre": user_settings.get("genre", ""),
            "era": user_settings.get("era", ""),
            "location": user_settings.get("location", ""),
        },
    }, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    logger.info("F01 开始处理: task_id=%s, 文本长度=%d 字, 用户设置=%s",
                task_id, len(text), bool(user_settings))

    raw_response = client.chat(
        messages=messages,
        temperature=0.7,
        max_tokens=8192,  # 增加以支持完整剧本的视觉基调提取
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

    # 用用户设置强制覆盖对应字段（确保用户输入不被 LLM 改写）
    if user_settings.get("medium"):
        result.setdefault("visual_style", {})["medium"] = _resolve_medium(user_settings["medium"])
    if user_settings.get("genre"):
        result.setdefault("genre", {})["primary"] = user_settings["genre"]
    if user_settings.get("era"):
        result.setdefault("era_setting", {})["era"] = user_settings["era"]
    if user_settings.get("location"):
        result.setdefault("world_setting", {})["geographic_context"] = user_settings["location"]

    # 强制覆盖 task_id（确保与上游一致）
    result["task_id"] = task_id

    logger.info("F01 处理完成: task_id=%s", task_id)
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="F01 导演视觉基调提取")
    parser.add_argument("novel_txt", help="原始小说文本文件路径")
    parser.add_argument("-o", "--output", default="f01_output.json", help="输出文件路径 (默认: f01_output.json)")
    parser.add_argument("--task-id", default=None, help="任务 ID (默认自动生成)")
    parser.add_argument("--medium", help="制作媒体类型 (如: 真人电影/动画/电视剧/纪录片)")
    parser.add_argument("--genre", help="作品类型 (如: 悬疑推理/仙侠/科幻)")
    parser.add_argument("--era", help="年代背景 (如: 现代2020s/民国1930s)")
    parser.add_argument("--location", help="地点背景 (如: 中国北方一线城市/架空仙侠世界)")
    parser.add_argument("--settings", help="用户设置 JSON 文件路径 (覆盖其他单个设置)")

    args = parser.parse_args()

    # 配置日志输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )

    print("=" * 60)
    print("F01 导演视觉基调提取")
    print("=" * 60)
    print(f"输入文件: {args.novel_txt}")
    print(f"输出文件: {args.output}")

    # 读取原始小说文本
    novel_txt_path = _P(args.novel_txt)
    text = novel_txt_path.read_text(encoding="utf-8")
    print(f"文本长度: {len(text)} 字")

    # 构建用户设置
    user_settings = {}
    if args.settings:
        with open(args.settings, "r", encoding="utf-8") as f:
            user_settings = json.load(f)
        print(f"使用设置文件: {args.settings}")
    else:
        if args.medium:
            user_settings["medium"] = args.medium
        if args.genre:
            user_settings["genre"] = args.genre
        if args.era:
            user_settings["era"] = args.era
        if args.location:
            user_settings["location"] = args.location

    if user_settings:
        print(f"用户设置: {json.dumps(user_settings, ensure_ascii=False)}")
    else:
        print("用户设置: 无（使用默认值）")

    # 运行提取
    print("\n开始调用 LLM...")
    result = extract_visual_tone(
        text=text,
        user_settings=user_settings if user_settings else None,
        task_id=args.task_id,
    )

    # 显示结果摘要
    print("\n" + "=" * 60)
    print("处理结果:")
    print("=" * 60)
    print(f"任务 ID: {result.get('task_id', 'N/A')}")
    print(f"类型: {result.get('genre', 'N/A')}")
    print(f"视觉风格: {result.get('visual_style', {}).get('style_name', 'N/A')}")
    if 'color_palette' in result:
        colors = result['color_palette'].get('dominant_colors', [])
        if colors:
            print(f"主色调: {', '.join(colors[:3])}")

    # 保存结果
    output_path = _P(args.output)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n结果已保存到: {output_path}")
    print("=" * 60)
