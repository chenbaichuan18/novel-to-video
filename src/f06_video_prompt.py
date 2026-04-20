"""F06 视频生成提示词——两阶段：A(分段与绑定) + B(视频提示词)"""

import json
import re
import sys
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from src.llm_client import LLMClient


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


def _load_skill_content() -> str:
    """加载完整的 F06 Skill 文件内容"""
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
    import logging
    _logger = logging.getLogger(__name__)

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
    )

    _logger.info("F06-A 原始响应长度: %d 字符", len(raw_response))

    # 从文本中提取 JSON（处理 ```json ... ``` 包裹）
    json_str = _extract_json_from_text(raw_response)

    result: dict[str, Any] = _safe_parse_json(raw_response)
    if result is None:
        raise ValueError("F06-A JSON 解析失败且无法修复")
    result["task_id"] = task_id
    result = _clean_result(result)
    _logger.info("F06-A 解析后 keys=%s, total_segments=%s",
                 list(result.keys()), result.get("total_segments", 0))

    result["task_id"] = task_id
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

    # 策略1：如果最后是未闭合的字符串（有奇数个引号），截断到最后一个完整 key 的位置
    # 找到最后一个完整的 key-value 对
    # 先尝试简单补全：在末尾补 "" 和 }
    open_braces = fixed.count("{") - fixed.count("}")
    open_brackets = fixed.count("[") - fixed.count("]")

    # 如果字符串内部有奇数个引号（未闭合字符串），去掉最后一个片段
    in_string = False
    last_complete_pos = -1
    i = 0
    while i < len(fixed):
        ch = fixed[i]
        if ch == '"':
            if not in_string:
                in_string = True
                start_of_string = i
            else:
                # 检查是否是转义引号
                backslash_count = 0
                j = i - 1
                while j >= 0 and fixed[j] == '\\':
                    backslash_count += 1
                    j -= 1
                if backslash_count % 2 == 0:
                    in_string = False
                    last_complete_pos = i
        elif not in_string and ch in (",", "}", "]"):
            last_complete_pos = i
        i += 1

    if last_complete_pos >= 0:
        # 截断到最后一个完整的位置
        fixed = fixed[:last_complete_pos + 1]
        # 移除尾部逗号（如果有）
        fixed = fixed.rstrip(",")
        # 补全闭合括号
        fixed += " " * open_brackets + "]" * open_brackets + "}" * open_braces
        return fixed

    return json_str  # 无法修复，返回原始


def _safe_parse_json(raw_response: str) -> dict | None:
    """安全解析 LLM 返回的 JSON，带截断修复重试。"""
    import json as _json

    json_str = _extract_json_from_text(raw_response)

    # 第一次尝试：直接解析
    try:
        result = _json.loads(json_str, strict=False)
        return result
    except _json.JSONDecodeError:
        pass

    # 第二次尝试：修复截断后解析
    try:
        fixed = _fix_truncated_json(json_str)
        _logger.info("JSON 解析失败，尝试截断修复... 原始长度=%d, 修复后长度=%d",
                     len(json_str), len(fixed))
        result = _json.loads(fixed, strict=False)
        _logger.info("截断修复成功")
        return result
    except _json.JSONDecodeError as e2:
        _logger.warning("截断修复也失败: %s", e2)

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
) -> dict[str, Any]:
    """F06-B: 视频提示词生成（精简提示词）"""
    import logging
    _b_logger = logging.getLogger(__name__)
    from src.config import DEFAULT_MODEL_ID

    task_id = task_id or str(uuid.uuid4())
    client = LLMClient(model=DEFAULT_MODEL_ID)

    # 构建 char_id -> name 映射（用于 "角色名(char_id)" 格式）
    char_map: dict[str, str] = {}
    if characters:
        for ch in characters:
            if isinstance(ch, dict) and ch.get("id") and ch.get("name"):
                char_map[ch["id"]] = ch["name"]
    _b_logger.info("角色映射: %s", char_map)

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

    # 为每个 segment 生成视频提示词
    video_prompts = []
    total = len(segments)

    for i, seg in enumerate(segments):
        user_message = _build_f06b_prompt(seg, tone_summary, char_map=char_map)

        messages = [
            {"role": "system", "content": _load_system_prompt_b()},
            {"role": "user", "content": user_message},
        ]

        _b_logger.info("F06-B 处理 segment %d/%d: %s", i + 1, total, seg.get("id"))

        try:
            raw_response = client.chat(
                messages=messages,
                temperature=0.7,
                max_tokens=4096,
            )
            vp_result: dict[str, Any] = _safe_parse_json(raw_response)
            if vp_result is None:
                raise ValueError("F06-B JSON 解析失败且无法修复")
            vp_result = _clean_result(vp_result)
            _b_logger.info("  -> 解析成功, keys=%s", list(vp_result.keys()))
            video_prompts.append(vp_result)
        except Exception as e:
            _b_logger.error("  -> segment %s 处理失败: %s", seg.get("id"), e)
            # 异常时也用格式化角色名
            _err_chars = [f"{char_map.get(c, c)}({c})" for c in seg.get("characters_present", [])]
            video_prompts.append({
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
                "final_video_prompt": f"[处理失败] segment={seg.get('id')}, error={e}",
            })

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


# def main():
#     """CLI 入口"""
#     import argparse
#     import logging

#     logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
#     parser = argparse.ArgumentParser(description="F06 视频提示词生成（两阶段）")
#     parser.add_argument("input_file", nargs="?", default=None, help="输入 JSON 文件路径（默认读 tests/fixtures/f06_input.json）")
#     args = parser.parse_args()

#     # 确定输入文件
#     if args.input_file:
#         inpath = Path(args.input_file)
#     else:
#         inpath = PROJECT_ROOT / "tests" / "fixtures" / "f06_input.json"

#     if not inpath.exists():
#         print(f"[ERROR] 输入文件不存在: {inpath}")
#         sys.exit(1)

#     print(f">>> 读取输入: {inpath}")
#     with open(inpath, "r", encoding="utf-8") as f:
#         test_input = json.load(f)

#     print(f"    task_id: {test_input['task_id']}")
#     print(f"    原始文本长度: {len(test_input.get('original_text', ''))} 字")
#     print(f"    人物数量: {len(test_input.get('characters', []))}")
#     print(f"    场景数量: {len(test_input.get('scenes', []))}")

#     # 运行流水线
#     result = run_f06_pipeline(
#         original_text=test_input["original_text"],
#         characters=test_input["characters"],
#         scenes=test_input["scenes"],
#         visual_tone=test_input["visual_tone"],
#         task_id=test_input["task_id"],
#     )

#     # 写入输出
#     outpath = PROJECT_ROOT / "tests" / "output" / "f06_output.json"
#     outpath.parent.mkdir(parents=True, exist_ok=True)
#     with open(outpath, "w", encoding="utf-8") as f:
#         json.dump(result, f, ensure_ascii=False, indent=2)

#     # 打印摘要
#     stage_a = result.get("stage_a", {})
#     stage_b = result.get("stage_b", {})
#     print(f"\n>>> 阶段 A: {stage_a.get('total_segments', 0)} 个 segment")
#     print(f">>> 阶段 B: {stage_b.get('total_video_prompts', 0)} 条视频提示词")

#     for i, vp in enumerate(stage_b.get("video_prompts", [])):
#         sid = vp.get("segment_id", "?")
#         dur = vp.get("duration_seconds", "?")
#         prompt = vp.get("final_video_prompt", "")
#         print(f"\n  [{i+1}] {sid} ({dur}s): {prompt[:100]}...")

#     print(f"\n>>> 完整结果已写入: {outpath}")


# if __name__ == "__main__":
#     main()
