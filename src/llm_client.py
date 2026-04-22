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
MAX_TOKEN_DOUBLING_LIMITS = 4  # max_tokens 最大翻倍次数


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
        调用聊天补全接口,返回助手的文本回复(已去除 markdown 代码块包裹)。

        支持 max_tokens 不足时自动翻倍重试。

        Args:
            messages: OpenAI 格式的消息列表 [{"role": "...", "content": "..."}]
            temperature: 温度参数
            max_tokens: 最大生成 token 数
            response_format: 如 {"type": "json_object"} 强制 JSON 输出
            timeout: 单次请求超时秒数
            max_retries: 超时重试次数

        Returns:
            助手回复文本(纯 JSON 字符串)
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # 自动翻倍机制
        doubling_count = 0
        current_max_tokens = max_tokens

        while True:
            payload: dict = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": current_max_tokens,
            }
            if response_format:
                payload["response_format"] = response_format

            logger.info("调用 LLM: model=%s, msg_count=%d, max_tokens=%d, timeout=%ds, retries=%d",
                        self.model, len(messages), current_max_tokens, timeout, max_retries)

            last_err = None
            response_text = None

            for attempt in range(max_retries + 1):
                try:
                    resp = requests.post(self.chat_url, headers=headers, json=payload, timeout=timeout)
                    resp.raise_for_status()

                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]

                    # 去除可能存在的 ```json ... ``` 包裹
                    response_text = _strip_markdown_code_block(content)

                    # 检查是否因 max_tokens 不足导致截断
                    if self._is_truncated_by_max_tokens(response_text, response_format):
                        if doubling_count < MAX_TOKEN_DOUBLING_LIMITS:
                            doubling_count += 1
                            current_max_tokens *= 2
                            logger.warning(
                                "检测到响应因 max_tokens 不足被截断,第 %d 次自动翻倍至 %d tokens 后重试...",
                                doubling_count, current_max_tokens
                            )
                            # 继续外层循环,使用翻倍后的 max_tokens 重新请求
                            break
                        else:
                            logger.error("已达到最大翻倍次数 %d,停止自动重试", MAX_TOKEN_DOUBLING_LIMITS)
                            return response_text  # 返回当前结果,即使可能不完整
                    else:
                        # 响应完整,直接返回
                        return response_text

                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    last_err = e
                    if attempt < max_retries:
                        wait = RETRY_DELAY * (attempt + 1)
                        logger.warning("LLM 请求失败(尝试 %d/%d): %s,%ds 后重试...",
                                       attempt + 1, max_retries + 1, e, wait)
                        time.sleep(wait)
                    else:
                        logger.error("LLM 请求最终失败(已重试 %d 次): %s", max_retries, e)

            # 如果跳出内层重试循环
            if last_err and doubling_count == 0:
                # 如果有错误且没有进行翻倍,抛出错误
                raise last_err
            # 如果翻倍了,继续外层循环重新请求

    def _is_truncated_by_max_tokens(self, text: str, response_format: dict | None = None) -> bool:
        """
        检测响应是否因 max_tokens 不足被截断。

        检测策略:
        1. 如果响应格式要求 JSON,尝试解析,如果 JSON 不完整则可能是截断
        2. 检查文本是否以未闭合的括号、引号等结束
        3. 检查是否有截断标记

        Args:
            text: 响应文本
            response_format: 请求时的响应格式配置

        Returns:
            True 表示可能是被截断
        """
        text = text.strip()

        # 如果要求 JSON 输出,检查 JSON 是否完整
        if response_format and response_format.get("type") == "json_object":
            try:
                json.loads(text)
                return False  # JSON 解析成功,说明完整
            except json.JSONDecodeError:
                # JSON 解析失败,可能被截断
                pass

        # 检查常见的截断标志
        # 1. 未闭合的引号
        quote_count = text.count('"') - text.count('\\"')  # 减去转义引号
        if quote_count % 2 != 0:
            return True

        # 2. 未闭合的大括号/方括号
        open_braces = text.count('{') + text.count('[')
        close_braces = text.count('}') + text.count(']')
        if open_braces > close_braces:
            return True

        # 3. 以省略号、断句等常见截断标记结尾
        truncation_markers = ['...', '。...', '......', '...)', '。.)']
        for marker in truncation_markers:
            if text.endswith(marker):
                return True

        # 4. 检查是否有 finish_reason 信息(某些 API 会返回)
        # 这里简化处理,仅基于内容判断

        return False


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
