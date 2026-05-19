"""LLM 客户端封装——统一调用 DeepSeek OpenAI 兼容接口。"""

import json
import logging
import time

import requests

from src.config import LLM_BASE_URL, DEFAULT_MODEL_ID, SILICONFLOW_API_KEY

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300
MAX_RETRIES = 3
RETRY_DELAY = 5
MAX_TOKEN_DOUBLING_LIMITS = 4


class LLMClient:
    """基于 DeepSeek OpenAI 兼容接口的 LLM 客户端。"""

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

        默认强制 JSON 格式输出，支持 max_tokens 不足时自动翻倍重试。
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if response_format is None:
            response_format = {"type": "json_object"}

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

            logger.info("调用 LLM: model=%s, msg_count=%d, max_tokens=%d, timeout=%ds",
                        self.model, len(messages), current_max_tokens, timeout)

            last_err = None
            response_text = None
            json_retry_count = 0

            for attempt in range(max_retries + 1):
                try:
                    resp = requests.post(self.chat_url, headers=headers, json=payload, timeout=timeout)
                    resp.raise_for_status()

                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    response_text = _strip_markdown_code_block(content)

                    # JSON 模式下校验输出是否合法
                    if response_format and response_format.get("type") == "json_object":
                        # 先用容错提取函数尝试从响应中提取 JSON（处理 LLM 前后缀文字）
                        json_for_validate = _extract_json_str(response_text)
                        try:
                            parsed = json.loads(json_for_validate, strict=False)
                            if not isinstance(parsed, (dict, list)):
                                parsed = None
                            else:
                                # 提取成功 → 返回纯净的 JSON 字符串
                                response_text = json_for_validate
                        except Exception:
                            parsed = None

                        if parsed is None:
                            json_retry_count += 1
                            is_truncated = self._is_truncated_by_max_tokens(response_text)
                            if is_truncated and doubling_count < MAX_TOKEN_DOUBLING_LIMITS:
                                doubling_count += 1
                                current_max_tokens *= 2
                                logger.warning(
                                    "JSON 非法且检测到截断(第 %d 次), 翻倍 max_tokens 至 %d...",
                                    json_retry_count, current_max_tokens,
                                )
                                break

                            logger.warning("JSON 非法 (attempt %d/%d), len=%d, 重试...",
                                           json_retry_count, MAX_RETRIES, len(response_text))
                            if json_retry_count <= MAX_RETRIES:
                                continue
                            else:
                                logger.error("JSON 重试耗尽，返回原始内容")
                                return response_text

                    # 检查截断
                    if self._is_truncated_by_max_tokens(response_text):
                        if doubling_count < MAX_TOKEN_DOUBLING_LIMITS:
                            doubling_count += 1
                            current_max_tokens *= 2
                            logger.warning("响应截断, 第 %d 次翻倍至 %d tokens...",
                                           doubling_count, current_max_tokens)
                            break
                        else:
                            logger.error("已达最大翻倍次数 %d", MAX_TOKEN_DOUBLING_LIMITS)
                            return response_text
                    else:
                        return response_text

                except requests.exceptions.HTTPError as http_e:
                    status = http_e.response.status_code if http_e.response is not None else 0
                    if status == 429:
                        wait = min(30, RETRY_DELAY * (2 ** attempt)) * (attempt + 1)
                        logger.warning("429 速率限制, 等待 %.0fs 后重试 (%d/%d)...",
                                       wait, attempt + 1, max_retries + 1)
                        time.sleep(wait)
                        continue
                    raise

                except (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError) as e:
                    last_err = e
                    if attempt < max_retries:
                        wait = RETRY_DELAY * (attempt + 1)
                        logger.warning("请求失败 (%d/%d): %s, %ds后重试...",
                                       attempt + 1, max_retries + 1, e, wait)
                        time.sleep(wait)
                    else:
                        logger.error("请求最终失败 (已重试 %d 次): %s", max_retries, e)

            if last_err and doubling_count == 0:
                raise last_err

    def _is_truncated_by_max_tokens(self, text: str) -> bool:
        """检测响应是否因 max_tokens 不足被截断。"""
        text = text.strip()
        if not text:
            return False

        # 先尝试容错提取 JSON
        json_text = _extract_json_str(text)

        # 1. JSON 解析尝试（完整 JSON → 未截断）
        if json_text.startswith(("{", "[")):
            try:
                json.loads(json_text)
                return False
            except json.JSONDecodeError:
                pass

        # 2. 未闭合括号（明显截断）
        if text.count('{') > text.count('}') or text.count('[') > text.count(']'):
            return True

        # 3. 末尾省略号
        for marker in ('......', '...'):
            if text.endswith(marker):
                return True

        # 4. 值截断但括号闭合的情况：
        #    典型模式："key":  或 "key": "va
        #    LLM 输出被截断时，JSON 结构完整但最后字段的值不完整
        import re
        if json_text.startswith('{'):
            last_80 = text.rstrip()[-80:]
            # 冒号后直接换行/结尾（值完全缺失）
            if re.search(r':\s*$', last_80):
                return True
            # 冒号后是未闭合的字符串: "field": "xxx（无右引号结尾）
            # 排除正常结尾: "...", 或 "...}" 或 "...\n
            # 用 [^",}\]\s]* 确保引号后没有闭合结构才是截断
            if re.search(r':\s*"[^",}\]\s]*\Z', last_80):
                return True

        return False


def _strip_markdown_code_block(text: str) -> str:
    """移除 markdown 代码块标记。"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n", 1)
        if len(lines) == 2 and lines[1].endswith("```"):
            return lines[1][:-3].strip()
        first_newline = t.find("\n")
        if first_newline != -1 and t.endswith("```"):
            return t[first_newline + 1 : -3].strip()
        return t
    return t


def _extract_json_str(text: str) -> str:
    """
    从 LLM 响应文本中提取 JSON 字符串。
    
    LLM 常在 JSON 前后添加说明文字（如 "结果如下：" 或 "```json ... ```"），
    此函数尝试定位并返回最外层 JSON 对象/数组。
    
    策略（按优先级）：
      1. 整体已是合法 JSON → 直接返回
      2. 找到第一个 { 和最后一个 }（或 [ / ]）→ 截取中间部分
      3. 找到 ```json ... ``` 代码块 → 提取内容
    """
    text = text.strip()
    if not text:
        return text
    
    # 策略 1: 直接就是合法 JSON
    try:
        json.loads(text, strict=False)
        return text
    except json.JSONDecodeError:
        pass
    
    # 策略 2: 定位最外层 {} 或 []
    start = -1
    end = -1
    for i, ch in enumerate(text):
        if ch == '{' and start == -1:
            start = i
        elif ch == '[' and start == -1:
            start = i
    if start != -1:
        # 从末尾找匹配的闭合符
        open_ch = text[start]
        close_ch = '}' if open_ch == '{' else ']'
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"' and not in_string:
                in_string = True
                continue
            if ch == '"' and in_string:
                in_string = False
                continue
            if in_string:
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    
    if start != -1 and end > start:
        extracted = text[start:end]
        # 验证截取的部分是否为合法 JSON
        try:
            json.loads(extracted, strict=False)
            return extracted
        except json.JSONDecodeError:
            pass
    
    # 策略 3: 尝试提取 markdown 代码块中的内容
    import re
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        block_content = m.group(1).strip()
        try:
            json.loads(block_content, strict=False)
            return block_content
        except json.JSONDecodeError:
            pass
    
    # 全部策略失败，返回原文
    return text


# 全局默认实例
_default_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取或创建全局 LLM 客户端单例。"""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
