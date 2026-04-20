"""全局配置，从 .env 文件加载。"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 加载项目根目录的 .env
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ── LLM 配置 ──────────────────────────────────────────────
LLM_PROVIDER = _env("LLM_PROVIDER", "siliconflow")
UNIFIED_PLATFORM = _env("UNIFIED_PLATFORM", "siliconflow")

if UNIFIED_PLATFORM == "siliconflow":
    SILICONFLOW_API_KEY = _env("SILICONFLOW_API_KEY")
    DEFAULT_MODEL_ID = _env("DEFAULT_MODEL_ID", "Qwen/Qwen3-8B")
    # 硅基流动 OpenAI 兼容端点
    LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
else:
    raise ValueError(f"不支持的平台: {UNIFIED_PLATFORM}")
