"""LLM 客户端封装——统一调用硅基流动 OpenAI 兼容接口。"""

import json
import logging
import time

import requests

from src.config import (
    LLM_BASE_URL,
    DEFAULT_MODEL_ID,
    SILICONFLOW_API_KEY,
)

logger = logging.getLogger(__name__)

# 默认超时和重试配置
DEFAULT_TIMEOUT = 300  # 5 分钟
MAX_RETRIES = 3
RETRY_DELAY = 5  # 首次重试等待秒数


class LLMClient:
    """基于 SiliconFlow OpenAI 兼容接口的 LLM 客户端。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or SILICONFLOW_API_KEY
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL_ID
        self.chat_url = f"{self.base_url}/chat/completions"

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> str:
        """
        调用聊天补全接口，返回助手的文本回复（已去除 markdown 代码块包裹）。

        Args:
            messages: OpenAI 格式的消息列表 [{"role": "...", "content": "..."}]
            temperature: 温度参数
            max_tokens: 最大生成 token 数
            response_format: 如 {"type": "json_object"} 强制 JSON 输出
            timeout: 单次请求超时秒数
            max_retries: 超时重试次数

        Returns:
            助手回复文本（纯 JSON 字符串）
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        logger.info("调用 LLM: model=%s, msg_count=%d, timeout=%ds, retries=%d",
                     self.model, len(messages), timeout, max_retries)

        last_err = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(self.chat_url, headers=headers, json=payload, timeout=timeout)
                resp.raise_for_status()

                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                # 去除可能存在的 ```json ... ``` 包裹
                text = _strip_markdown_code_block(content)
                return text

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_err = e
                if attempt < max_retries:
                    wait = RETRY_DELAY * (attempt + 1)
                    logger.warning("LLM 请求失败(尝试 %d/%d): %s，%ds 后重试...",
                                   attempt + 1, max_retries + 1, e, wait)
                    time.sleep(wait)
                else:
                    logger.error("LLM 请求最终失败(已重试 %d 次): %s", max_retries, e)

        raise last_err


def _strip_markdown_code_block(text: str) -> str:
    """移除 markdown 代码块标记，保留内部纯文本。"""
    t = text.strip()
    if t.startswith("```"):
        # 移除第一行 (```json 或 ```)
        lines = t.split("\n", 1)
        if len(lines) == 2 and lines[1].endswith("```"):
            return lines[1][:-3].strip()
        # 多行代码块
        first_newline = t.find("\n")
        if first_newline != -1 and t.endswith("```"):
            return t[first_newline + 1 : -3].strip()
        return t
    return t


# 全局默认实例
_default_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取或创建全局 LLM 客户端单例。"""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
