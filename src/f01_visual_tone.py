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
