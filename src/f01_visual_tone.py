"""F01 导演视觉基调提取。

输入：小说全文文本 → 调用 LLM + Skill 提示词 → 输出结构化视觉基调 JSON。
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

# ── Skill 文件路径 ────────────────────────────────────────
SKILL_PATH = _P(__file__).resolve().parent.parent / "skills" / "f01_visual_tone.md"


def load_skill_prompt() -> str:
    """加载 Skill 文件内容作为 system prompt。"""
    return SKILL_PATH.read_text(encoding="utf-8")


def extract_visual_tone(text: str, task_id: str | None = None) -> dict:
    """
    执行 F01：从小说全文中提取导演视觉基调。

    Args:
        text: 小说全文文本
        task_id: 任务标识（UUID）；若不传则自动生成

    Returns:
        结构化的视觉基调字典，包含 genre/visual_style/color_palette 等字段
    """
    if task_id is None:
        task_id = str(uuid.uuid4())

    client = get_llm_client()
    system_prompt = load_skill_prompt()

    # 构造用户消息：仅传入原始文本
    user_content = text

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    logger.info("F01 开始处理: task_id=%s, 文本长度=%d 字", task_id, len(text))

    raw_response = client.chat(
        messages=messages,
        temperature=0.7,
        max_tokens=4096,
    )

    # 解析 LLM 返回的 JSON
    result = json.loads(raw_response)

    # 强制覆盖 task_id（确保与上游一致）
    result["task_id"] = task_id

    logger.info("F01 处理完成: task_id=%s", task_id)
    return result


# ── CLI 入口（方便单独测试）──────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    sample_text = """《深夜的便利店》小说片段：

林默是这家便利店的值班店员，每天深夜独自守店。城市的高架桥在窗外延伸，路灯的暖黄光晕透过玻璃洒在货架上。他穿着褪色的深蓝工装，头发总是乱糟糟的。顾客很少，偶尔有加班的白领来买咖啡，或是流浪汉在门口徘徊。

店内白色荧光灯管嗡嗡作响，收银台旁的小台灯发出昏黄的光。窗外是深蓝色的夜空和稀疏的车流。林默靠在货架边，看着窗外出神。这时，一个穿红色风衣的女人推门进来，冷风卷着落叶跟了进来。
"""

    # 支持命令行参数传入文件路径
    if len(_sys.argv) > 1:
        filepath = _sys.argv[1]
        with open(filepath, encoding="utf-8") as f:
            sample_text = f.read()

    result = extract_visual_tone(sample_text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
