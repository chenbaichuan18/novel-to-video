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
        max_tokens=4096,
    )

    # 解析 LLM 返回的 JSON
    result = json.loads(raw_response)

    # 强制覆盖 task_id 确保一致
    result["task_id"] = task_id

    logger.info("F02 处理完成: task_id=%s, 提取人物=%d 人",
                task_id, result.get("characters", {}).get("total", 0))
    return result


# ── CLI 入口（方便单独测试）──────────────────────────────
if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 默认读 fixtures 输入文件
    default_input = _PROJECT_ROOT / "tests" / "fixtures" / "f02_input.json"
    input_path = _sys.argv[1] if len(_sys.argv) > 1 else str(default_input)

    with open(input_path, encoding="utf-8") as f:
        test_data = json.load(f)

    result = extract_characters(test_data["text"], task_id=test_data.get("task_id"))

    # 写入 output 目录
    out_dir = _PROJECT_ROOT / "tests" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "f02_output.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=2))
