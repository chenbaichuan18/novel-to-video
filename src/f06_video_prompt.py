"""F06 视频生成提示词——两阶段：A(分段与绑定) + B(视频提示词)"""

import json
import re
import sys
import os
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from src.llm_client import LLMClient

logger = logging.getLogger(__name__)


def _clean_text(text: str) -> str:
    """清理文本中的转义控制字符，返回干净的纯文本。"""
    if not isinstance(text, str):
        return text
    text = text.replace("\n", " ").replace("\r", "")
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


from functools import lru_cache


@lru_cache(maxsize=1)
def _load_skill_content() -> str:
    """加载完整的 F06 Skill 文件内容（lru_cache 保证只读一次磁盘）"""
    skill_path = PROJECT_ROOT / "skills" / "f06_video_prompt.md"
    with open(skill_path, "r", encoding="utf-8") as f:
        return f.read()


def _load_system_prompt_a() -> str:
    """加载 F06-A 的 System Prompt（# 第一阶段 到 # 第一阶段 User Message 模板之前）"""
    content = _load_skill_content()
    # 取 "# 第一阶段" 到 "# 第一阶段 User Message 模板" 之间的内容
    match = re.search(
        r"# 第一阶段 \(F06-A\).*?(?=# 第一阶段 User Message 模板)",
        content, re.DOTALL
    )
    if match:
        return match.group(0).strip()
    # fallback: 取到 "---" 或 "# 第二阶段" 为止
    parts = re.split(r"\n---\n", content)
    return parts[0].strip()


def _load_prompt_a_template() -> str:
    """加载 F06-A 的 User Message 模板"""
    content = _load_skill_content()
    match = re.search(
        r"# 第一阶段 User Message 模板.*?\n(.*?)\n---\n",
        content, re.DOTALL
    )
    if match:
        return match.group(1).strip()
    raise FileNotFoundError("skills/f06_video_prompt.md 中未找到 '# 第一阶段 User Message 模板' 部分")


def _load_system_prompt_b() -> str:
    """加载 F06-B 的 System Prompt（角色+任务部分）"""
    content = _load_skill_content()
    match = re.search(
        r"# 第二阶段.*?## 角色\s*\n(.*?)\n## 任务\s*\n(.*?)(?=\n## )",
        content, re.DOTALL
    )
    if match:
        return f"## 角色\n{match.group(1).strip()}\n\n## 任务\n{match.group(2).strip()}"
    raise FileNotFoundError("skills/f06_video_prompt.md 中未找到 F06-B System Prompt（角色+任务）部分")


def _load_prompt_b_template() -> str:
    """加载 F06-B 的 Skill 模板（含占位符）"""
    content = _load_skill_content()
    parts = content.split("# 第二阶段")
    if len(parts) > 1:
        return ("# 第二阶段" + parts[1]).strip()
    raise FileNotFoundError("skills/f06_video_prompt.md 中未找到 '# 第二阶段' 部分")


def _build_f06a_prompt(
    original_text: str,
    characters: list[dict],
    scenes: list[dict],
    visual_tone: dict,
) -> str:
    """构建 F06-A 的 User Message（从 skill 模板加载 + 变量替换）"""
    # 构建角色摘要
    char_lines = []
    for ch in characters:
        aliases_str = "、".join(ch.get("aliases", [])) or "(无)"
        char_lines.append(f"- {ch['id']}: {ch['name']} | 别名:{aliases_str} | 性别:{ch.get('gender','?')}")

    # 构建场景摘要
    scene_lines = []
    for sc in scenes:
        aliases_str = "、".join(sc.get("aliases", [])) or "(无)"
        scene_lines.append(f"- {sc['id']}: {sc['name']} | 别名:{aliases_str}")

    # 从 skill 文件加载 A 阶段模板
    template = _load_prompt_a_template()

    # 变量替换
    return (
        template
        .replace("{{ORIGINAL_TEXT}}", original_text)
        .replace("{{CHAR_LIST}}", "\n".join(char_lines))
        .replace("{{SCENE_LIST}}", "\n".join(scene_lines))
    )


def segment_and_bind(
    original_text: str,
    characters: list[dict],
    scenes: list[dict],
    visual_tone: dict,
    task_id: str | None = None,
) -> dict[str, Any]:
    """F06-A: 文本分段与实体绑定（使用精简提示词，不强制 JSON 格式）"""
    from src.config import DEFAULT_MODEL_ID

    task_id = task_id or str(uuid.uuid4())
    client = LLMClient(model=DEFAULT_MODEL_ID)

    user_message = _build_f06a_prompt(
        original_text=original_text,
        characters=characters,
        scenes=scenes,
        visual_tone=visual_tone,
    )

    messages = [
        {"role": "system", "content": _load_system_prompt_a()},
        {"role": "user", "content": user_message},
    ]

    # 不用 response_format 强制 JSON，改用自然语言输出再提取
    raw_response = client.chat(
        messages=messages,
        temperature=0.5,
        max_tokens=8000,
        timeout=900,  # F06 数据量大，超时时间增加到 15 分钟
        max_retries=1,  # 减少重试次数，避免浪费时间
        enable_thinking=True,  # 开启思考模式，提高分段与绑定的准确性
    )

    logger.info("F06-A 原始响应长度: %d 字符", len(raw_response))

    # 从文本中提取 JSON（处理 ```json ... ``` 包裹）
    json_str = _extract_json_from_text(raw_response)

    result: dict[str, Any] = _safe_parse_json(raw_response)
    if result is None:
        raise ValueError("F06-A JSON 解析失败且无法修复")
    result["task_id"] = task_id
    result = _clean_result(result)
    logger.info("F06-A 解析后 keys=%s, total_segments=%s",
                 list(result.keys()), result.get("total_segments", 0))

    result["task_id"] = task_id

    # A 阶段后处理：规则修正 LLM 分段不合理的情况
    result = _post_process_segments(result)
    logger.info("F06-A 后处理后 total_segments=%s", result.get("total_segments", 0))

    return result


def _post_process_segments(result: dict) -> dict:
    """A 阶段后处理：基于规则自动合并不合理的相邻 segment。

    修正的问题：
    1. 同一场景内相邻两段都是纯描写（无对话无新人物），合并为 1 段
    2. 同一场景内相邻两段都是对话/反应且人物完全相同，时长都 <=10s，合并
    3. 重新编号 segment id
    """
    segments: list[dict] = result.get("segments", [])
    if not segments:
        return result

    def _is_pure_description(seg: dict) -> bool:
        """判断该段是否为纯描写段（无对话标志，内容类型为 description_merge 或旁白）"""
        reason = seg.get("split_reason", "")
        text = seg.get("text_original", "") + seg.get("text_resolved", "")
        has_dialogue = '"' in text or '"' in text or '"' in text or '「' in text
        return reason == "description_merge" or (not has_dialogue and reason != "scene_switch")

    def _can_merge(a: dict, b: dict) -> bool:
        """判断相邻两段是否可以合并：
        - 同一 scene_id（包括 scene_unknown 且 scene_name 相同）
        - 合并后时长 <= 15s
        - 条件 A：两段均为纯描写，人物集合相同或 b 没有新增主动角色
        - 条件 B：两段均 <=10s 且人物集合完全相同且同一动作链（无场景切换）
        """
        # 场景必须相同
        if a.get("scene_id") != b.get("scene_id"):
            return False
        if a.get("scene_id") == "scene_unknown" and a.get("scene_name") != b.get("scene_name"):
            return False
        # b 不能以 scene_switch 开头（说明有地点切换）
        if b.get("split_reason") == "scene_switch":
            return False
        # 合并后时长检查
        dur_a = a.get("duration_estimate", 10)
        dur_b = b.get("duration_estimate", 10)
        if dur_a + dur_b > 15:
            return False
        # 人物集合
        chars_a = set(a.get("characters_present", []))
        chars_b = set(b.get("characters_present", []))
        # 条件 A：两段都是纯描写
        if _is_pure_description(a) and _is_pure_description(b):
            return True
        # 条件 B：时长都短，人物完全一致，同一动作链
        if dur_a <= 10 and dur_b <= 10 and chars_a == chars_b:
            return True
        return False

    def _merge_two(a: dict, b: dict) -> dict:
        """合并两段为一段"""
        merged_text_orig = (a.get("text_original", "") + " " + b.get("text_original", "")).strip()
        merged_text_res = (a.get("text_resolved", "") + " " + b.get("text_resolved", "")).strip()
        merged_chars = list(dict.fromkeys(
            a.get("characters_present", []) + b.get("characters_present", [])
        ))
        merged_dur = min(a.get("duration_estimate", 10) + b.get("duration_estimate", 10), 15)
        reason = a.get("split_reason", "description_merge")
        if reason != "description_merge":
            reason = "description_merge"
        return {
            "id": a["id"],
            "sequence_order": a.get("sequence_order", 1),
            "text_original": merged_text_orig,
            "text_resolved": merged_text_res,
            "characters_present": merged_chars,
            "scene_id": a.get("scene_id", ""),
            "scene_name": a.get("scene_name", ""),
            "duration_estimate": merged_dur,
            "split_reason": reason,
        }

    # 迭代合并：每轮扫描一次，发生合并则重新扫描
    changed = True
    while changed:
        changed = False
        new_segs: list[dict] = []
        i = 0
        while i < len(segments):
            if i + 1 < len(segments) and _can_merge(segments[i], segments[i + 1]):
                merged = _merge_two(segments[i], segments[i + 1])
                logger.info(
                    "F06-A 后处理合并: %s + %s → %s (dur=%ds)",
                    segments[i]["id"], segments[i + 1]["id"], merged["id"], merged["duration_estimate"]
                )
                new_segs.append(merged)
                i += 2
                changed = True
            else:
                new_segs.append(segments[i])
                i += 1
        segments = new_segs

    # 重新编号
    for idx, seg in enumerate(segments, 1):
        seg["id"] = f"seg_{idx}"
        seg["sequence_order"] = idx

    result["segments"] = segments
    result["total_segments"] = len(segments)
    return result


def _extract_json_from_text(text: str) -> str:
    """从 LLM 文本响应中提取 JSON 内容"""
    # 尝试 1: 找 ```json ... ``` 代码块
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # 尝试 2: 找 { ... } 对象
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]

    # 兜底：返回原文
    return text.strip()


def _fix_truncated_json(json_str: str) -> str:
    """尝试修复被截断的 JSON 字符串。

    常见场景：LLM 输出因 max_tokens 不足或网络中断导致字符串未闭合。
    策略：补全未闭合的引号、补全缺失的 } / ]、移除最后一个不完整的值。
    """
    fixed = json_str.strip()

    # 检查是否确实看起来像截断的（末尾没有正常闭合）
    if fixed.endswith("}") or fixed.endswith("]"):
        return json_str  # 已经是完整结构，无需修复

    # 策略：逐层修复
    # 1. 首先处理未闭合的字符串
    in_string = False
    quote_char = None
    last_complete_pos = -1
    i = 0
    while i < len(fixed):
        ch = fixed[i]
        if not in_string and ch in ('"', "'"):
            in_string = True
            quote_char = ch
        elif in_string and ch == quote_char:
            # 检查是否是转义引号
            backslash_count = 0
            j = i - 1
            while j >= 0 and fixed[j] == '\\':
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 0:
                in_string = False
                quote_char = None
                last_complete_pos = i
        elif not in_string and ch in (",", "}", "]"):
            last_complete_pos = i
        i += 1

    if in_string:
        # 字符串未闭合，截断到最后一个完整位置
        if last_complete_pos >= 0:
            fixed = fixed[:last_complete_pos + 1]
            # 移除尾部逗号
            fixed = fixed.rstrip(",")
        else:
            # 没有找到完整位置，返回空字典
            return "{}"

    # 2. 补全闭合的括号
    open_braces = fixed.count("{") - fixed.count("}")
    open_brackets = fixed.count("[") - fixed.count("]")

    fixed = fixed.rstrip(",")
    fixed += "]" * open_brackets + "}" * open_braces

    return fixed


def _safe_parse_json(raw_response: str) -> dict | None:
    """安全解析 LLM 返回的 JSON，带截断修复重试。"""
    import json as _json

    json_str = _extract_json_from_text(raw_response)

    # 第一次尝试：直接解析
    try:
        result = _json.loads(json_str, strict=False)
        return result
    except _json.JSONDecodeError as e:
        logger.debug("JSON 解析失败: %s", e)
        logger.debug("原始响应前 300 字: %s", raw_response[:300])

    # 第二次尝试：修复截断后解析
    try:
        fixed = _fix_truncated_json(json_str)
        logger.info("JSON 解析失败，尝试截断修复... 原始长度=%d, 修复后长度=%d",
                     len(json_str), len(fixed))
        logger.debug("修复后前 300 字: %s", fixed[:300])
        result = _json.loads(fixed, strict=False)
        logger.info("截断修复成功")
        return result
    except _json.JSONDecodeError as e2:
        logger.warning("截断修复也失败: %s", e2)
        logger.warning("原始响应长度=%d", len(raw_response))
        logger.warning("原始响应前 500 字: %s", raw_response[:500])
        logger.warning("原始响应后 200 字: %s", raw_response[-200:] if len(raw_response) > 200 else raw_response)
        logger.warning("修复后前 500 字: %s", fixed[:500])
        logger.warning("修复后后 200 字: %s", fixed[-200:] if len(fixed) > 200 else fixed)

    return None



def _build_f06b_prompt(segment: dict, visual_tone: str, char_map: dict[str, str] | None = None) -> str:
    """构建 F06-B 的提示词（从 skill 模板加载 + 变量替换）"""
    # 提取 segment 关键信息
    seg_id = segment.get("id", "seg_?")
    text_resolved = segment.get("text_resolved", segment.get("text_original", ""))
    chars_raw = segment.get("characters_present", [])
    scene_name = segment.get("scene_name", "")
    scene_id = segment.get("scene_id", "")
    duration = segment.get("duration_estimate", 10)

    # 将 char_id 转换为 "角色名(char_id)" 格式
    if char_map:
        chars_formatted = [f"{char_map.get(c, c)}({c})" for c in chars_raw]
    else:
        chars_formatted = list(chars_raw)
    char_info = ", ".join(chars_formatted) if chars_formatted else "(无)"

    # 场景也格式化为 "场景名(scene_id)" 格式，与角色保持一致
    if scene_name and scene_id:
        scene_info = f"{scene_name}({scene_id})"
    elif scene_name:
        scene_info = scene_name
    else:
        scene_info = "(未知场景)"

    # 从 skill 文件加载 B 阶段模板
    template = _load_prompt_b_template()

    # 变量替换
    return (
        template
        .replace("{{SEG_ID}}", seg_id)
        .replace("{{SCENE_NAME}}", scene_name)
        .replace("{{SCENE_ID}}", scene_id)
        .replace("{{SCENE_INFO}}", scene_info)
        .replace("{{CHAR_INFO}}", char_info)
        .replace("{{DURATION}}", str(duration))
        .replace("{{TEXT_RESOLVED}}", text_resolved)
        .replace("{{VISUAL_TONE}}", visual_tone)
        .replace("{{CHARS_FORMATTED}}", json.dumps(chars_formatted, ensure_ascii=False))
    )



def generate_video_prompts(
    segments: list[dict],
    visual_tone: dict,
    characters: list[dict] | None = None,
    task_id: str | None = None,
    max_workers: int = 5,
) -> dict[str, Any]:
    """F06-B: 视频提示词生成（并发版本，max_workers 控制并发数）"""
    from src.config import DEFAULT_MODEL_ID
    from concurrent.futures import ThreadPoolExecutor, as_completed

    task_id = task_id or str(uuid.uuid4())

    # 构建 char_id -> name 映射（用于 "角色名(char_id)" 格式）
    char_map: dict[str, str] = {}
    if characters:
        for ch in characters:
            if isinstance(ch, dict) and ch.get("id") and ch.get("name"):
                char_map[ch["id"]] = ch["name"]
    logger.info("角色映射: %s", char_map)

    # 将 visual_tone 压缩为摘要字符串（避免输入过大）
    vt_genre = visual_tone.get("genre", {})
    vt_style = visual_tone.get("visual_style", {})
    vt_color = visual_tone.get("color_palette", {})
    vt_lighting = visual_tone.get("lighting_philosophy", "")
    vt_atmosphere = visual_tone.get("atmosphere", {})

    tone_summary = (
        f"类型:{vt_genre.get('primary','')}/{vt_genre.get('secondary','')} | "
        f"导演风格:{vt_style.get('director_style','')} | "
        f"摄影特点:{vt_style.get('cinematography','')} | "
        f"参考作:{', '.join(vt_style.get('reference_works',[]))} | "
        f"主色调:{', '.join(vt_color.get('dominant_colors',[]))} 点缀色:{vt_color.get('accent_color','')} | "
        f"光线哲学:{vt_lighting} | "
        f"氛围:{vt_atmosphere.get('overall_mood','')}"
    )

    total = len(segments)
    # 预加载 system prompt（只读一次磁盘，所有线程共享）
    system_prompt_b = _load_system_prompt_b()

    def _process_one(args: tuple[int, dict]) -> tuple[int, dict]:
        """处理单个 segment，返回 (原始索引, 结果)，失败时最多重试 2 次"""
        i, seg = args
        # 每个线程独立创建 LLMClient（requests.Session 非线程安全）
        _client = LLMClient(model=DEFAULT_MODEL_ID)
        user_message = _build_f06b_prompt(seg, tone_summary, char_map=char_map)
        # 重试时在 system prompt 末尾加上强提示，并降低 temperature
        retry_system = system_prompt_b + "\n\n⚠️ 注意：你必须只返回合法 JSON，不要输出任何 JSON 以外的文字、说明或代码块标记。"
        attempts = [
            (system_prompt_b, 0.7),   # 第 1 次：正常
            (retry_system, 0.3),       # 第 2 次：强提示 + 低温
            (retry_system, 0.0),       # 第 3 次：强提示 + 零温（最确定性）
        ]
        last_exc: Exception = Exception("未知错误")
        logger.info("F06-B 处理 segment %d/%d: %s", i + 1, total, seg.get("id"))
        for attempt_idx, (sys_prompt, temp) in enumerate(attempts):
            try:
                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_message},
                ]
                if attempt_idx > 0:
                    logger.warning("  -> segment %s 第 %d 次重试 (temperature=%.1f)",
                                   seg.get("id"), attempt_idx, temp)
                raw_response = _client.chat(
                    messages=messages,
                    temperature=temp,
                    max_tokens=4096,
                    timeout=300,
                    enable_thinking=True,  # 开启思考模式，提高视频提示词生成质量
                )
                logger.debug("F06-B 原始响应长度: %d 字符", len(raw_response))
                vp_result: dict[str, Any] = _safe_parse_json(raw_response)
                if vp_result is None:
                    logger.error("F06-B 原始响应(attempt %d): %s", attempt_idx + 1, raw_response[:500])
                    raise ValueError("F06-B JSON 解析失败且无法修复")
                vp_result = _clean_result(vp_result)
                logger.info("  -> segment %s 解析成功(attempt %d), keys=%s",
                            seg.get("id"), attempt_idx + 1, list(vp_result.keys()))
                return i, vp_result
            except Exception as e:
                last_exc = e
                logger.warning("  -> segment %s attempt %d 失败: %s", seg.get("id"), attempt_idx + 1, e)

        logger.error("  -> segment %s 全部重试失败: %s", seg.get("id"), last_exc)
        _err_chars = [f"{char_map.get(c, c)}({c})" for c in seg.get("characters_present", [])]
        return i, {
            "segment_id": seg.get("id"),
            "duration_seconds": seg.get("duration_estimate", 10),
            "entity_bindings": {
                "characters_present": _err_chars,
                "scene_binding": {
                    "scene_id": seg.get("scene_id", ""),
                    "scene_name": seg.get("scene_name", ""),
                },
                "time_of_day": "",
            },
            "final_video_prompt": f"[处理失败] segment={seg.get('id')}, error={last_exc}",
        }

    # 并发执行，按原始顺序收集结果
    results: list[tuple[int, dict]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_one, (i, seg)): i for i, seg in enumerate(segments)}
        for future in as_completed(futures):
            results.append(future.result())

    # 按原始顺序排序
    results.sort(key=lambda x: x[0])
    video_prompts = [r for _, r in results]

    return {
        "task_id": task_id,
        "total_video_prompts": len(video_prompts),
        "video_prompts": video_prompts,
    }


def run_f06_pipeline(
    original_text: str,
    characters: list[dict],
    scenes: list[dict],
    visual_tone: dict,
    task_id: str | None = None,
) -> dict[str, Any]:
    """
    运行完整 F06 流水线：A(分段绑定) → B(视频提示词)
    """
    import logging
    logger = logging.getLogger(__name__)
    
    task_id = task_id or str(uuid.uuid4())

    logger.info("=" * 50)
    logger.info("F06 流水线开始: task_id=%s", task_id)
    logger.info("=" * 50)

    # 阶段 A: 文本分段与实体绑定
    logger.info("--- 阶段 A: 文本分段与实体绑定 ---")
    segmentation_result = segment_and_bind(
        original_text=original_text,
        characters=characters,
        scenes=scenes,
        visual_tone=visual_tone,
        task_id=task_id,
    )

    # 容错解析 segments（LLM 可能用不同键名）
    _raw_segs = segmentation_result.get("segments")
    if _raw_segs and isinstance(_raw_segs, list):
        segments = _raw_segs
    else:
        segments = (
            segmentation_result.get("segment_list")
            or (segmentation_result.get("data", {}).get("segments") if isinstance(segmentation_result.get("data"), dict) else None)
            or []
        )
    total_segments = len(segments)

    if total_segments == 0:
        logger.warning("F06-A 返回 0 个 segment！原始结果已保存到 f06_stageA_raw.json，请检查 LLM 返回内容")

    # 场景合理性校验：检测 scene_id 不在合法场景列表中的 segment
    valid_scene_ids = {sc["id"] for sc in scenes if isinstance(sc, dict) and sc.get("id")}
    for seg in segments:
        sid = seg.get("scene_id", "")
        if sid and sid not in valid_scene_ids and sid != "scene_unknown":
            sname = seg.get("scene_name", "")
            logger.warning(
                "场景绑定异常: %s → scene_id='%s'(%s) 不在场景列表中，建议填 scene_unknown",
                seg.get("id"), sid, sname,
            )

    # 调试：保存 A 阶段原始结果
    _debug_path_a = PROJECT_ROOT / "tests" / "output" / "f06_stageA_raw.json"
    with open(_debug_path_a, "w", encoding="utf-8") as f:
        json.dump(segmentation_result, f, ensure_ascii=False, indent=2)

    logger.info("阶段 A 完成: 共 %d 个 segment (原始 keys=%s)", total_segments, list(segmentation_result.keys()))

    # 阶段 B: 视频提示词生成
    logger.info("--- 阶段 B: 视频提示词生成 ---")
    video_prompt_result = generate_video_prompts(
        segments=segments,
        visual_tone=visual_tone,
        characters=characters,
        task_id=task_id,
    )

    logger.info("阶段 B 完成: 共 %d 条视频提示词", video_prompt_result.get("total_video_prompts", 0))

    # 合并结果
    final_result = {
        "task_id": task_id,
        "pipeline": "f06_a_to_b",
        "stage_a": {
            "total_segments": total_segments,
            "segmentation_strategy": segmentation_result.get("segmentation_strategy"),
            "resolution_statistics": segmentation_result.get("resolution_statistics"),
        },
        "stage_b": {
            "total_video_prompts": video_prompt_result.get("total_video_prompts", 0),
            "video_prompts": video_prompt_result.get("video_prompts", []),
        },
    }

    logger.info("F06 流水线完成")
    return final_result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="F06 视频生成提示词流水线")
    parser.add_argument("novel_txt", help="原始小说文本文件路径")
    parser.add_argument("f02_output", help="F02 角色提取输出 JSON 文件路径")
    parser.add_argument("f03_output", help="F03 场景提取输出 JSON 文件路径")
    parser.add_argument("f01_output", help="F01 视觉基调输出 JSON 文件路径")
    parser.add_argument("-o", "--output", default="f06_output.json", help="输出文件路径 (默认: f06_output.json)")
    parser.add_argument("--task-id", default=None, help="任务 ID (默认自动生成)")

    args = parser.parse_args()

    # 配置日志输出到控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )

    import time
    start_time = time.time()

    print("=" * 60)
    print("F06 视频生成提示词流水线")
    print("=" * 60)
    print(f"小说文本: {args.novel_txt}")
    print(f"F02 输入: {args.f02_output}")
    print(f"F03 输入: {args.f03_output}")
    print(f"F01 输入: {args.f01_output}")
    print(f"输出文件: {args.output}")
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 读取原始小说文本
    novel_txt_path = Path(args.novel_txt)
    original_text = novel_txt_path.read_text(encoding="utf-8")
    print(f"文本长度: {len(original_text)} 字")

    # 读取 f02 输出（角色）
    with open(args.f02_output, "r", encoding="utf-8") as f:
        f02_data = json.load(f)
    characters_data = f02_data.get("characters", [])
    if isinstance(characters_data, dict) and "list" in characters_data:
        characters = characters_data["list"]
    else:
        characters = characters_data
    print(f"角色数量: {len(characters)}")

    # 读取 f03 输出（场景）
    with open(args.f03_output, "r", encoding="utf-8") as f:
        f03_data = json.load(f)
    scenes_data = f03_data.get("scenes", [])
    if isinstance(scenes_data, dict) and "list" in scenes_data:
        scenes = scenes_data["list"]
    else:
        scenes = scenes_data
    print(f"场景数量: {len(scenes)}")

    # 读取 f01 输出（视觉基调）
    with open(args.f01_output, "r", encoding="utf-8") as f:
        visual_tone = json.load(f)

    visual_style = visual_tone.get("visual_style", {})
    genre = visual_tone.get("genre", {})
    tone_name = (
        visual_style.get("style_name")
        or visual_style.get("medium")
        or genre.get("primary")
        or "未知"
    )
    print(f"视觉基调: {tone_name}")

    # 运行流水线
    print("\n开始执行 F06 流水线...")
    result = run_f06_pipeline(
        original_text=original_text,
        characters=characters,
        scenes=scenes,
        visual_tone=visual_tone,
        task_id=args.task_id,
    )

    # 显示结果摘要
    print("\n" + "=" * 60)
    print("处理结果:")
    print("=" * 60)
    print(f"任务 ID: {result.get('task_id', 'N/A')}")

    # 显示阶段 A 结果
    stage_a = result.get("stage_a", {})
    if stage_a:
        print(f"\n阶段 A (分段与绑定):")
        print(f"  分段数量: {stage_a.get('total_segments', 0)}")

    # 显示阶段 B 结果
    stage_b = result.get("stage_b", {})
    if stage_b:
        print(f"\n阶段 B (视频提示词):")
        video_prompts = stage_b.get("video_prompts", [])
        print(f"  镜头数量: {stage_b.get('total_video_prompts', len(video_prompts))}")
        if video_prompts:
            print("  前3个镜头:")
            for i, vp in enumerate(video_prompts[:3], 1):
                seg_id = vp.get("segment_id", "?")
                duration = vp.get("duration_seconds", 0)
                prompt_preview = vp.get("final_video_prompt", "")[:40]
                print(f"    [{i}] {seg_id} - {duration}秒 | {prompt_preview}...")

    # 保存结果
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    elapsed_time = time.time() - start_time
    print(f"\n结果已保存到: {output_path}")
    print(f"总耗时: {elapsed_time:.2f} 秒 ({elapsed_time/60:.2f} 分钟)")
    print("=" * 60)




