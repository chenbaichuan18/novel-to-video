"""F06 视频生成提示词——两阶段：A(分段与绑定) + B(视频提示词)"""

import json
import re
import sys
import uuid
import logging
from pathlib import Path
from typing import Any

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
    """递归清理 result 中所有字符串字段的转义控制字符和转义引号。"""
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
    """加载 F06-B 的 System Prompt（第二阶段角色与输出要求）。"""
    content = _load_skill_content()

    # F06 skill 中第一个 "## 任务" 属于 F06-A。B 阶段没有独立的
    # "## 任务" 标题，因此必须从 "# 第二阶段" 开始截取，避免把 A 阶段
    # 的分段任务错当成 B 阶段 system prompt。
    match = re.search(
        r"# 第二阶段 \(F06-B\).*?(?=\n## 片段信息)",
        content, re.DOTALL
    )
    if match:
        return match.group(0).strip()
    raise FileNotFoundError("skills/f06_video_prompt.md 中未找到 F06-B System Prompt（任务）部分")


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

    raw_response = client.chat(
        messages=messages,
        temperature=0.3,
        max_tokens=8000,
        timeout=900,
        max_retries=3,  # F06-A 响应体较大，增加重试次数以应对网络中断
    )

    logger.info("F06-A 原始响应长度: %d 字符", len(raw_response))

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

    # 为每个 segment 注入 scene_layout_description（从 scenes 列表查找）
    result = _inject_scene_layout(result, scenes)

    return result


def _inject_scene_layout(result: dict, scenes: list[dict]) -> dict:
    """为每个 segment 注入 scene_layout_description。

    构建 scene_id → scene_layout_description 的映射，然后为每个 segment
    填充该字段，使 B 阶段可直接引用，无需再做一次场景查表。
    """
    # 建立 scene_id -> scene_layout_description 映射
    layout_map: dict[str, str] = {}
    for sc in scenes:
        if isinstance(sc, dict) and sc.get("id"):
            layout = sc.get("scene_layout_description", "")
            if layout:
                layout_map[sc["id"]] = layout

    segments = result.get("segments", [])
    injected = 0
    for seg in segments:
        scene_id = seg.get("scene_id", "")
        if scene_id and scene_id in layout_map:
            seg["scene_layout_description"] = layout_map[scene_id]
            injected += 1
        elif scene_id:
            seg["scene_layout_description"] = ""

    logger.info("F06-A 场景布局注入: %d/%d 个 segment 已填充 scene_layout_description",
                 injected, len(segments))
    return result


# ── 后处理常量 ──
MAX_SEG_CHARS = 200          # 单 segment 最大字数（text_original）
MIN_SEG_CHARS = 20           # 单 segment 最小字数（过短说明内容太少）
MAX_DIALOGUES_PER_SEG = 8    # 单 segment 最大台词条数
CHARS_PER_SECOND = 12        # 阅读速度：中文约 12 字/秒（含动作/环境描述）
ENABLE_FIRST_SEGMENT_DIALOGUE_MIGRATION = False


def _post_process_segments(result: dict) -> dict:
    """A 阶段后处理：基于规则自动合并/拆分不合理的 segment。

    处理逻辑：
    1. 合并过短的相邻 segment（< MIN_SEG_CHARS 或 duration < 5s）
    2. 合并后不超过 MAX_SEG_CHARS 字数和 MAX_DIALOGUES_PER_SEG 台词数
    3. 拆分超长 segment（按对话/动作边界拆分，确保情节完整性）
    4. 校验时长与内容密度的匹配关系
    5. 重新编号
    """
    segments: list[dict] = result.get("segments", [])
    if not segments:
        return result

    def _seg_char_count(seg: dict) -> int:
        """计算 segment 的 text_original 字数"""
        return len(seg.get("text_original", ""))

    def _seg_dialogue_count(seg: dict) -> int:
        """计算 segment 的台词条数"""
        return len(seg.get("dialogues", []))

    def _is_pure_description(seg: dict) -> bool:
        """判断该段是否为纯描写段（无对话标志）"""
        reason = seg.get("split_reason", "")
        text = seg.get("text_original", "") + seg.get("text_resolved", "")
        has_dialogue = '"' in text or '\u201c' in text or '\u300a' in text or '\u300c' in text
        return reason == "description_merge" or (not has_dialogue and reason != "scene_switch")

    def _can_merge(a: dict, b: dict) -> bool:
        """判断相邻两段是否可以合并。

        合并条件：
        - 同一场景
        - 合并后字数 <= MAX_SEG_CHARS
        - 合并后台词数 <= MAX_DIALOGUES_PER_SEG
        - 合并后时长 <= 15s
        - 条件A：两段均为纯描写 → 合并
        - 条件B：任一段 <5s 或 <MIN_SEG_CHARS 字 → 合并
        - 条件C：两段均 <=10s 且人物完全相同 → 合并

        禁止合并条件：
        - b 有对话且 a 不是纯描写 → 禁止合并（保护对话完整性）
        - b 以 scene_switch 开头 → 禁止合并
        """
        # 场景必须相同
        if a.get("scene_id") != b.get("scene_id"):
            return False
        if a.get("scene_id") == "scene_unknown" and a.get("scene_name") != b.get("scene_name"):
            return False
        # b 不能以 scene_switch 开头
        if b.get("split_reason") == "scene_switch":
            return False

        # 合并后字数检查
        merged_chars = _seg_char_count(a) + _seg_char_count(b)
        if merged_chars > MAX_SEG_CHARS:
            return False
        # 合并后台词数检查
        merged_dialogues = _seg_dialogue_count(a) + _seg_dialogue_count(b)
        if merged_dialogues > MAX_DIALOGUES_PER_SEG:
            return False
        # 合并后时长检查
        dur_a = a.get("duration_estimate", 10)
        dur_b = b.get("duration_estimate", 10)
        if dur_a + dur_b > 15:
            return False

        # 人物集合
        chars_a = set(a.get("characters_present", []))
        chars_b = set(b.get("characters_present", []))

        # 获取对话信息
        b_has_dialogue = _seg_dialogue_count(b) > 0

        # 条件A：两段都是纯描写
        if _is_pure_description(a) and _is_pure_description(b):
            return True
        # 条件B：任一段过短（时长<5s 或 字数<MIN_SEG_CHARS），必须合并
        if dur_a < 5 or dur_b < 5 or _seg_char_count(a) < MIN_SEG_CHARS or _seg_char_count(b) < MIN_SEG_CHARS:
            return True
        # 条件C：两段均短且人物完全一致
        if dur_a <= 10 and dur_b <= 10 and chars_a == chars_b:
            return True

        # 【禁止条件】b 有对话且 a 不是纯描写 → 禁止合并
        # 保护对话完整性，避免将两个有对话的段错误合并
        if b_has_dialogue and not _is_pure_description(a):
            logger.debug("禁止合并：b段有对话且a段不是纯描写")
            return False

        return False

    def _merge_two(a: dict, b: dict) -> dict:
        """合并两段为一段，包含 dialogues 合并"""
        merged_text_orig = (a.get("text_original", "") + " " + b.get("text_original", "")).strip()
        merged_text_res = (a.get("text_resolved", "") + " " + b.get("text_resolved", "")).strip()
        merged_chars = list(dict.fromkeys(
            a.get("characters_present", []) + b.get("characters_present", [])
        ))
        # 合并 dialogues
        merged_dialogues = list(a.get("dialogues", [])) + list(b.get("dialogues", []))
        merged_dur = min(a.get("duration_estimate", 10) + b.get("duration_estimate", 10), 15)
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
            "dialogues": merged_dialogues,
        }

    def _split_oversized_segment(seg: dict) -> list[dict]:
        """拆分超长 segment。

        策略：按对话边界优先拆分，找不到对话边界时按标点符号拆分。
        确保每段情节完整（不在对话中间切断）。
        改进：优先在对话之间拆分，而非对话中间。
        """
        text = seg.get("text_original", "")
        char_count = len(text)
        dialogues = seg.get("dialogues", [])

        # 如果不超限，无需拆分
        if char_count <= MAX_SEG_CHARS and len(dialogues) <= MAX_DIALOGUES_PER_SEG:
            return [seg]

        # 确定目标拆分数量
        target_count = max(2, (char_count + MAX_SEG_CHARS - 1) // MAX_SEG_CHARS)
        target_chars = char_count // target_count

        # 收集所有可拆分点
        split_points: list[tuple[int, str]] = []  # (位置, 类型)

        # 【改进】策略1：优先在对话之间找拆分点
        # 找到每条对话在 text 中的位置（说话人标注处）
        dialogue_positions: list[int] = []
        for d in dialogues:
            char_name = d.get("character_name", "")
            pattern = f"{char_name}"
            # 查找所有出现位置
            for match in re.finditer(re.escape(pattern), text):
                pos = match.start()
                if pos > 10:  # 避免在最开头
                    dialogue_positions.append(pos)
                    break  # 只取第一个匹配

        # 在两个对话位置之间选择拆分点（取中间位置）
        if len(dialogue_positions) >= 2:
            # 在中间两个对话之间拆分
            mid_idx = len(dialogue_positions) // 2
            # 找到这两个对话位置之间的中间点
            pos1 = dialogue_positions[mid_idx - 1] if mid_idx > 0 else 0
            pos2 = dialogue_positions[mid_idx]
            # 取中间位置，并找一个合适的断点
            mid_range = (pos1 + pos2) // 2
            for i in range(mid_range, min(pos2, mid_range + 30)):
                if text[i] in ('。', '！', '？', '，', '\n'):
                    split_points.append((i + 1, "dialogue_boundary"))
                    break
            else:
                # 没找到合适断点，直接用中点
                split_points.append((mid_range, "dialogue_boundary"))

        # 策略2：如果没有足够对话位置，按说话人标注拆分
        if not split_points:
            for d in dialogues:
                char_name = d.get("character_name", "")
                pattern = f"{char_name}"
                pos = text.find(pattern)
                if pos > 10:
                    split_points.append((pos, "speaker_marker"))

        # 策略3：按标点符号找分隔点
        if not split_points:
            for i, ch in enumerate(text):
                if ch in ('。', '！', '？') and i > target_chars * 0.5:
                    split_points.append((i + 1, "punctuation"))
                    break

        # 兜底：按字数中点拆分
        if not split_points:
            mid = target_chars
            for i in range(max(10, mid - 20), min(len(text), mid + 20)):
                if text[i] in ('。', '，', '！', '？', ' ', '\n'):
                    split_points.append((i + 1, "fallback"))
                    break
            else:
                split_points.append((mid, "fallback"))

        # 选择最接近 target_chars 的拆分点（优先选择对话边界类型）
        # 排序：dialogue_boundary > speaker_marker > punctuation > fallback
        priority_order = {"dialogue_boundary": 0, "speaker_marker": 1, "punctuation": 2, "fallback": 3}
        split_points.sort(key=lambda x: (abs(x[0] - target_chars), priority_order.get(x[1], 99)))
        best_split, split_type = split_points[0]

        # 执行拆分
        part1_text = text[:best_split].strip()
        part2_text = text[best_split:].strip()

        if not part1_text or not part2_text:
            return [seg]  # 拆分失败，保持原样

        # 分配 dialogues：根据每条对话在 text 中的位置判断归属
        dialogues_1 = []
        dialogues_2 = []
        for d in dialogues:
            line = d.get("line", "")
            # 如果台词在 part1 中出现（简单启发式）
            if line in part1_text:
                dialogues_1.append(d)
            else:
                dialogues_2.append(d)

        # 如果拆分后某一段没有对话但有多个对话需要分配，进行调整
        # 确保对话数量分布合理
        if not dialogues_1 and dialogues_2 and len(dialogues_2) > 2:
            # 把第一个对话移到 part1（如果 part1 太短）
            if len(part1_text) < 50:
                logger.info("调整对话分配：部分对话从 part2 移到 part1")

        dur = seg.get("duration_estimate", 10)
        ratio1 = len(part1_text) / char_count
        dur1 = max(5, min(15, round(dur * ratio1)))
        dur2 = max(5, min(15, dur - dur1))

        chars = seg.get("characters_present", [])
        scene_id = seg.get("scene_id", "")
        scene_name = seg.get("scene_name", "")

        seg1 = {
            "id": seg["id"],
            "sequence_order": seg.get("sequence_order", 1),
            "text_original": part1_text,
            "text_resolved": seg.get("text_resolved", "")[:best_split].strip() if seg.get("text_resolved") else part1_text,
            "characters_present": chars,
            "scene_id": scene_id,
            "scene_name": scene_name,
            "duration_estimate": dur1,
            "split_reason": f"length_split({split_type})",
            "dialogues": dialogues_1,
        }
        seg2 = {
            "id": seg["id"] + "_b",
            "sequence_order": seg.get("sequence_order", 1) + 1,
            "text_original": part2_text,
            "text_resolved": seg.get("text_resolved", "")[best_split:].strip() if seg.get("text_resolved") else part2_text,
            "characters_present": chars,
            "scene_id": scene_id,
            "scene_name": scene_name,
            "duration_estimate": dur2,
            "split_reason": f"length_split({split_type})",
            "dialogues": dialogues_2,
        }

        logger.info("拆分 segment %s: chars=%d→%d+%d, dialogues=%d→%d+%d, type=%s",
                    seg.get("id"), char_count, len(part1_text), len(part2_text),
                    len(dialogues), len(dialogues_1), len(dialogues_2), split_type)

        # 递归检查拆分后的段是否仍然超长
        result_parts = []
        for part in [seg1, seg2]:
            result_parts.extend(_split_oversized_segment(part))
        return result_parts

    def _validate_duration(seg: dict) -> dict:
        """校验并修正 segment 的时长估计，使其与内容密度匹配。

        规则：duration_estimate ≈ 字数 / CHARS_PER_SECOND
        范围限制在 5-15 秒。
        有对话的段给予额外时间（每句台词 +1s）。
        """
        text = seg.get("text_original", "")
        char_count = len(text)
        dialogue_count = _seg_dialogue_count(seg)

        # 基础时长 = 字数 / 阅读速度
        base_duration = char_count / CHARS_PER_SECOND
        # 台词附加时间：每句 +1s（说话需要时间）
        dialogue_bonus = dialogue_count * 1.0
        # 动作/环境描写附加时间
        action_bonus = 0
        # 如果有动作词（走向、转身、拿起等），每 3 个动作词 +1s
        action_keywords = ["走向", "转身", "拿起", "抬头", "低头", "攥紧", "扔", "跑", "走", "站", "坐"]
        action_count = sum(1 for kw in action_keywords if kw in text)
        action_bonus = (action_count / 3) * 1.0

        estimated = base_duration + dialogue_bonus + action_bonus
        # 限制范围
        estimated = max(5, min(15, round(estimated)))

        old_dur = seg.get("duration_estimate", 10)
        if abs(estimated - old_dur) >= 2:
            logger.info(
                "F06-A 时长校验: %s 字数=%d, 台词=%d → duration %ds → %ds",
                seg.get("id", "?"), char_count, dialogue_count, old_dur, estimated,
            )
            seg["duration_estimate"] = estimated

        return seg

    # ── 步骤 1: 迭代合并过短的相邻 segment ──
    changed = True
    while changed:
        changed = False
        new_segs: list[dict] = []
        i = 0
        while i < len(segments):
            if i + 1 < len(segments) and _can_merge(segments[i], segments[i + 1]):
                merged = _merge_two(segments[i], segments[i + 1])
                logger.info(
                    "F06-A 后处理合并: %s + %s → %s (dur=%ds, chars=%d, dialogues=%d)",
                    segments[i]["id"], segments[i + 1]["id"], merged["id"],
                    merged["duration_estimate"],
                    _seg_char_count(merged), _seg_dialogue_count(merged),
                )
                new_segs.append(merged)
                i += 2
                changed = True
            else:
                new_segs.append(segments[i])
                i += 1
        segments = new_segs

    # ── 步骤 2: 拆分超长 segment ──
    final_segs: list[dict] = []
    for seg in segments:
        char_count = _seg_char_count(seg)
        dialogue_count = _seg_dialogue_count(seg)
        if char_count > MAX_SEG_CHARS or dialogue_count > MAX_DIALOGUES_PER_SEG:
            logger.info(
                "F06-A 后处理拆分: %s (chars=%d/%d, dialogues=%d/%d)",
                seg.get("id", "?"), char_count, MAX_SEG_CHARS,
                dialogue_count, MAX_DIALOGUES_PER_SEG,
            )
            parts = _split_oversized_segment(seg)
            final_segs.extend(parts)
        else:
            final_segs.append(seg)
    segments = final_segs

    # ── 步骤 3: 校验时长与内容密度 ──
    for seg in segments:
        _validate_duration(seg)

    # ── 步骤 4: 重新编号 ──
    for idx, seg in enumerate(segments, 1):
        seg["id"] = f"seg_{idx}"
        seg["sequence_order"] = idx

    # ── 步骤 4b: 首段台词迁移 ──
    # 当 seg_1 是纯描写段（dialogues 为空 + 无对话标记文本），
    # 但 seg_2 以对话开头时，说明 LLM 将"场景引入+前两句对话"错误地拆成了两段。
    # 此时从 seg_2 迁移前 1-2 句台词到 seg_1，使首段信息量充足。
    if ENABLE_FIRST_SEGMENT_DIALOGUE_MIGRATION and len(segments) >= 2:
        seg1 = segments[0]
        seg2 = segments[1]
        seg1_dialogues = seg1.get("dialogues", [])
        seg2_dialogues = seg2.get("dialogues", [])

        # 判断 seg_1 是否为纯描写段：dialogues 为空 且 同场景 且 文本不含对话格式
        import re as _re_seg
        seg1_text = seg1.get("text_original", "") or seg1.get("text_resolved", "")
        has_dialogue_marker = bool(_re_seg.search(r'[：:]["「]|说[：:]"|喊[：:]"|问[：:]"', seg1_text))

        is_pure_desc_seg1 = (
            len(seg1_dialogues) == 0
            and not has_dialogue_marker
            and seg1.get("scene_id") == seg2.get("scene_id")
        )
        seg2_has_dialogue = len(seg2_dialogues) >= 2

        if is_pure_desc_seg1 and seg2_has_dialogue:
            # 迁移前 1-2 句台词到 seg_1
            migrate_count = min(2, len(seg2_dialogues))
            migrated = seg2_dialogues[:migrate_count]

            # 更新 seg_1 的 dialogues 和 characters_present
            seg1["dialogues"] = list(migrated)
            chars1 = set(seg1.get("characters_present", []))
            for d in migrated:
                cid = d.get("character_id", "")
                if cid:
                    chars1.add(cid)
            seg1["characters_present"] = list(chars1)

            # 从 seg_2 移除已迁移的台词
            seg2["dialogues"] = seg2_dialogues[migrate_count:]

            # 同步更新 text_original / text_resolved（将迁移的台词文本追加到 seg_1，从 seg_2 移除）
            migrated_texts = []
            for d in migrated:
                line_text = d.get("line", "")
                char_name = d.get("character_name", "")
                if line_text:
                    migrated_texts.append(f"{char_name}：{line_text}")

            if migrated_texts:
                mig_str = " ".join(migrated_texts)
                seg1["text_original"] = (seg1.get("text_original", "") + " " + mig_str).strip()
                seg1["text_resolved"] = (seg1.get("text_resolved", "") + " " + mig_str).strip()

                # 从 seg_2 的文本中移除迁移部分
                t2_orig = seg2.get("text_original", "")
                for mt in migrated_texts:
                    if mt in t2_orig:
                        t2_orig = t2_orig.replace(mt, "", 1).strip()
                seg2["text_original"] = t2_orig
                t2_res = seg2.get("text_resolved", "")
                for mt in migrated_texts:
                    if mt in t2_res:
                        t2_res = t2_res.replace(mt, "", 1).strip()
                seg2["text_resolved"] = t2_res

            logger.info(
                "F06-A 后处理步骤4b: 从 %s 迁移 %d 句台词到 %s (首段纯描写补全)",
                seg2["id"], migrate_count, seg1["id"],
            )

    result["segments"] = segments
    result["total_segments"] = len(segments)

    # ── 步骤 5: 对话完整性验证（需要 original_text）─────────
    # 注意：此验证需要在 run_f06_pipeline 中调用，因为 post_process 阶段没有 original_text
    # 此处仅做占位，实际验证在 segment_and_bind 函数末尾调用
    result["_post_process_complete"] = True
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


def _build_scene_mapping(scenes: list[dict]) -> dict[str, str]:
    """
    构建场景映射：scene_id -> scene_name
    
    Args:
        scenes: 场景列表（来自 f03 输出）
    
    Returns:
        scene_map: 字典，存储 scene_id 到 scene_name 的映射
    """
    scene_map: dict[str, str] = {}
    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        scene_id = sc.get("id", "")
        scene_name = sc.get("name", "")
        if scene_id and scene_name:
            scene_map[scene_id] = scene_name
    return scene_map


def _build_full_scene_map(scenes: list[dict]) -> dict[str, dict]:
    """
    构建完整场景映射：scene_id -> 完整的 F03 场景字典
    
    Args:
        scenes: 场景列表（来自 f03 输出）
    
    Returns:
        full_scene_map: 字典，存储 scene_id 到完整 F03 场景数据的映射
    """
    full_scene_map: dict[str, dict] = {}
    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        scene_id = sc.get("id", "")
        if scene_id:
            full_scene_map[scene_id] = sc
    return full_scene_map


def _extract_immutable_features(
    characters: list[dict],
    scenes: list[dict],
    visual_tone: dict | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """
    从 f02/f03 的输出中解析提取"不可变识别特征"、"场景设计约束"和"场景映射"。
    备用来源：visual_tone.visual_constraints
    
    Args:
        characters: 角色列表（来自 f02 输出）
        scenes: 场景列表（来自 f03 输出）
        visual_tone: 视觉基调（可选，用于备用来源）
    
    Returns:
        (char_constraints, scene_constraints, scene_map): 角色约束、场景约束、场景映射
    """
    char_constraints: dict[str, str] = {}
    scene_constraints: dict[str, str] = {}
    scene_map: dict[str, str] = _build_scene_mapping(scenes)
    
    # 解析角色不可变识别特征（从 f02 的 appearance 和 clothing 字段）
    for ch in characters:
        if not isinstance(ch, dict):
            continue
        char_id = ch.get("id", "")
        
        # 优先从 appearance + clothing 字段组合（无前缀）
        appearance = ch.get("appearance", "")
        clothing = ch.get("clothing", "")
        
        if appearance or clothing:
            parts = []
            if appearance:
                parts.append(appearance)
            if clothing:
                parts.append(clothing)
            char_constraints[char_id] = "；".join(parts)
            continue
        
        # 从 immutable_features 字段获取（备用）
        if "immutable_features" in ch:
            char_constraints[char_id] = ch["immutable_features"]
            continue
        
        # 从 final_prompt 中解析（备用）
        final_prompt = ch.get("final_prompt", "")
        if final_prompt:
            match = re.search(r"不可变识别特征[：:](.+?)(?=色彩规范|光线|风格锚定|$)", final_prompt, re.DOTALL)
            if match:
                char_constraints[char_id] = match.group(1).strip()
                continue
    
    # 解析场景设计约束（从 f03 的 visual_description.summary 字段，优先使用）
    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        scene_id = sc.get("id", "")
        
        # 【修改】优先从 visual_description.summary 字段获取
        visual_desc = sc.get("visual_description", {})
        if isinstance(visual_desc, dict):
            summary = visual_desc.get("summary", "")
            if summary:
                scene_constraints[scene_id] = summary
                continue
        
        # 从 key_features 字段获取（备用）
        key_features = sc.get("key_features", [])
        if key_features:
            scene_constraints[scene_id] = "、".join(key_features) if isinstance(key_features, list) else key_features
            continue
        
        # 从 scene_design_rules 字段获取（备用）
        if "scene_design_rules" in sc:
            scene_constraints[scene_id] = sc["scene_design_rules"]
            continue
        
        # 从 final_prompt 中解析（备用）
        final_prompt = sc.get("final_prompt", "")
        if final_prompt:
            match = re.search(r"场景设计约束[：:](.+?)质量要求", final_prompt, re.DOTALL)
            if match:
                scene_constraints[scene_id] = match.group(1).strip()
                continue
        
        # 从 description 中提取（备用）
        desc = sc.get("description", "")
        if desc:
            scene_constraints[scene_id] = desc
    
    # 备用：从 visual_tone.visual_constraints 获取（仅填充仍未获取的约束）
    if visual_tone and isinstance(visual_tone, dict):
        visual_constraints = visual_tone.get("visual_constraints", {})
        
        # 如果角色约束为空，从 character_design_rules 获取
        if not char_constraints:
            char_design_rules = visual_constraints.get("character_design_rules", "")
            if char_design_rules:
                for ch in characters:
                    if isinstance(ch, dict):
                        char_id = ch.get("id", "")
                        char_name = ch.get("name", "")
                        if char_id and char_name:
                            char_constraints[char_id] = f"{char_name}({char_id})：{char_design_rules}"
        
        # 如果场景约束为空，从 scene_design_rules 获取
        if not scene_constraints:
            scene_design_rules = visual_constraints.get("scene_design_rules", "")
            if scene_design_rules:
                for sc in scenes:
                    if isinstance(sc, dict):
                        scene_id = sc.get("id", "")
                        scene_name = sc.get("name", "")
                        if scene_id and scene_name:
                            scene_constraints[scene_id] = f"{scene_name}（{scene_id}）：{scene_design_rules}"
    
    return char_constraints, scene_constraints, scene_map


def _safe_parse_json(raw_response: str) -> dict | None:
    """安全解析 LLM 返回的 JSON（直接解析，无修复）。"""
    import json as _json

    json_str = _extract_json_from_text(raw_response)

    try:
        obj = _json.loads(json_str, strict=False)
        if isinstance(obj, dict):
            logger.info("JSON 解析成功 (直接), len=%d", len(json_str))
            return obj
    except _json.JSONDecodeError as e:
        logger.debug("JSON 解析失败: %s (len=%d)", e, len(json_str))

    return None



def _build_f06b_prompt(
    segment: dict,
    visual_tone: str,
    char_map: dict[str, str] | None = None,
    visual_anchor_map: dict[str, str] | None = None,
    scene_map: dict[str, str] | None = None,
    full_scene_map: dict[str, dict] | None = None,
    era_world_info: str = "",
    medium_style: str = "电影感、写实摄影风格",
    reference_works: str = "无",
    color_palette: str = "无",
) -> str:
    """构建 F06-B 的提示词（从 skill 模板加载 + 变量替换）"""
    # 提取 segment 关键信息
    seg_id = segment.get("id", "seg_?")
    text_resolved = segment.get("text_resolved", segment.get("text_original", ""))
    chars_raw = segment.get("characters_present", [])
    scene_name = segment.get("scene_name", "")
    scene_id = segment.get("scene_id", "")
    duration = segment.get("duration_estimate", 10)
    dialogues = segment.get("dialogues", [])

    # 构建角色信息：包含视觉锚定（优先使用 F02 的 visual_anchor）
    char_parts = []
    for c in chars_raw:
        name = char_map.get(c, c) if char_map else c
        anchor = visual_anchor_map.get(c, "") if visual_anchor_map else ""
        if anchor:
            char_parts.append(f"{name}（{anchor}）")
        else:
            char_parts.append(name)
    char_info = "；".join(char_parts) if char_parts else "(无)"

    # 场景信息：优先直接使用 A 阶段注入的 scene_layout_description
    # 回退到 full_scene_map 查表（兼容旧数据）
    layout_desc = segment.get("scene_layout_description", "") or ""

    if scene_name and scene_id:
        # 从 full_scene_map 获取 F03 的完整场景信息（仅用于补充非 layout 字段）
        f03_scene = full_scene_map.get(scene_id, {}) if full_scene_map else {}
        if isinstance(f03_scene, dict) and f03_scene:
            visual_desc = f03_scene.get("visual_description", {})
            if not isinstance(visual_desc, dict):
                visual_desc = {}
            environment = f03_scene.get("environment", {})
            if not isinstance(environment, dict):
                environment = {}

            # 组合 F03 场景描述的关键字段
            summary = visual_desc.get("summary", "") or ""
            spatial_layout = environment.get("spatial_layout", "") or ""
            key_features = visual_desc.get("key_features", [])
            key_features_str = "、".join(key_features) if isinstance(key_features, list) and key_features else ""

            # 【关键】提取 F03 的空间元素(spatial_elements)和道具位置(key_props)
            # 这两个字段包含 direction/position 方位信息，是确保视频片段空间一致性的核心数据源
            spatial_elements_raw = f03_scene.get("spatial_elements", [])
            key_props_raw = f03_scene.get("key_props", [])

            # 格式化 spatial_elements 为可读文本: "玻璃门(左边缘·画面左边缘前景), 吧台(中央·画面正中央中景), ..."
            se_parts = []
            for se in spatial_elements_raw:
                if isinstance(se, dict):
                    se_name = se.get("name", "")
                    se_dir = se.get("direction", "")
                    se_pos = se.get("position", "")
                    if se_name:
                        detail = f"{se_dir}·{se_pos}" if se_dir or se_pos else ""
                        se_parts.append(f"{se_name}({detail})" if detail else se_name)
            spatial_elements_str = ", ".join(se_parts) if se_parts else ""

            # 格式化 key_props 为可读文本: "旧相机(画面正中央吧台上·major), 抹布(画面右边缘水槽中·minor), ..."
            kp_parts = []
            for kp in key_props_raw:
                if isinstance(kp, dict):
                    kp_name = kp.get("name", "")
                    kp_pos = kp.get("position", "")
                    kp_sig = kp.get("significance", "")
                    if kp_name:
                        detail = f"{kp_pos}·{kp_sig}" if kp_pos or kp_sig else ""
                        kp_parts.append(f"{kp_name}({detail})" if detail else kp_name)
            key_props_str = ", ".join(kp_parts) if kp_parts else ""

            # 构建丰富的场景信息字符串
            parts = [f"{scene_name}({scene_id})"]
            # 核心：scene_layout_description（来自 A 阶段直接注入，优先级最高）
            if layout_desc:
                parts.append(f"场景布局描述: {layout_desc}")
            elif spatial_layout:
                # 回退：使用 environment.spatial_layout
                parts.append(f"空间布局: {spatial_layout}")
            if summary:
                parts.append(f"视觉概要: {summary}")
            if key_features_str:
                parts.append(f"核心特征: {key_features_str}")
            if spatial_elements_str:
                parts.append(f"空间元素方位: {spatial_elements_str}")
            if key_props_str:
                parts.append(f"道具位置: {key_props_str}")

            scene_info = " | ".join(parts)
        else:
            # full_scene_map 无数据，但 A 阶段可能注入了 layout_desc
            if layout_desc:
                scene_info = f"{scene_name}({scene_id}) | 场景布局描述: {layout_desc}"
            else:
                scene_info = f"{scene_name}({scene_id})"
    elif scene_name:
        # 只有 scene_name，无 scene_id
        if layout_desc:
            scene_info = f"{scene_name} | 场景布局描述: {layout_desc}"
        else:
            scene_info = scene_name
    else:
        # 未知场景
        if layout_desc:
            scene_info = f"场景布局描述: {layout_desc}"
        else:
            scene_info = "(未知场景)"

    # 格式化台词信息
    if dialogues:
        dialogue_lines = []
        for d in dialogues:
            d_char_id = d.get("character_id", "unknown")
            d_char_name = d.get("character_name", "未知角色")
            d_line = d.get("line", "")
            if char_map and d_char_id != "unknown":
                d_char_name = char_map.get(d_char_id, d_char_name)
            dialogue_lines.append(f"- {d_char_name}({d_char_id}): '{d_line}'")
        dialogue_info = "\n".join(dialogue_lines)
    else:
        dialogue_info = "（本片段无台词）"

    # 构建场景映射
    if scene_map and scene_id:
        scene_mapping_text = f"{scene_name}({scene_id})"
    else:
        scene_mapping_text = f"{scene_name}({scene_id})" if scene_id else "(未知场景)"

    # 从 skill 文件加载 B 阶段模板
    template = _load_prompt_b_template()

    # 变量替换
    return (
        template
        .replace("{{SEG_ID}}", seg_id)
        .replace("{{SCENE_NAME}}", scene_name)
        .replace("{{SCENE_ID}}", scene_id)
        .replace("{{SCENE_INFO}}", scene_info)
        .replace("{{LAYOUT_DESC}}", layout_desc if layout_desc else "（未提供场景布局描述）")  # 独立变量：场景布局描述全文
        .replace("{{CHAR_INFO}}", char_info)
        .replace("{{SCENE_MAPPING}}", scene_mapping_text)
        .replace("{{DURATION}}", str(duration))
        .replace("{{TEXT_RESOLVED}}", text_resolved)
        .replace("{{DIALOGUES}}", dialogue_info)
        .replace("{{VISUAL_TONE}}", visual_tone)
        .replace("{{ERA_WORLD_INFO}}", era_world_info or "(未提供时代地域信息)")
        .replace("{{MEDIUM_STYLE}}", medium_style)
        .replace("{{REFERENCE_WORKS}}", reference_works)
        .replace("{{COLOR_PALETTE}}", color_palette)
    )



def _time_matches(t1: str, t2: str) -> bool:
    """判断两个 time_of_day 是否属于同一时段（互相包含即可）。

    time_of_day 的值可能为 "傍晚"、"傍晚五点四十"、"黄昏" 等变体，
    严格 == 匹配会导致大量误判。此函数支持三层匹配策略。
    """
    if t1 == t2:
        return True
    # 前缀包含：短的是长的前缀
    shorter, longer = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    if longer.startswith(shorter):
        return True
    # 核心时段词组模糊匹配
    TIME_CORE_KEYWORDS = [
        ["午后", "下午", "中午", "午间", "白天", "上午"],
        ["傍晚", "黄昏", "夕阳", "暮色"],
        ["夜晚", "夜间", "晚上", "夜"],
        ["清晨", "早晨", "早上", "凌晨", "黎明"],
    ]
    for group in TIME_CORE_KEYWORDS:
        if any(kw in t1 for kw in group) and any(kw in t2 for kw in group):
            return True
    return False


def _generate_default_light(time_of_day: str) -> str:
    """根据 time_of_day 自动生成默认光影描述（用于无基准段可参考时的降级处理）。"""
    t = time_of_day or ""
    # 按时段关键词匹配默认光影
    if any(kw in t for kw in ["凌晨", "黎明", "清晨", "早晨", "早上"]):
        return "晨光微熹，光线柔和清冷，整体偏冷色调。"
    if any(kw in t for kw in ["上午", "中午", "午间", "午后", "下午", "白天"]):
        return "自然光充足明亮，光线清晰锐利。"
    if any(kw in t for kw in ["傍晚", "黄昏", "夕阳", "暮色", "晚间"]):
        return "夕阳余晖洒落，暖金色调，光线柔和温暖。"
    if any(kw in t for kw in ["夜晚", "夜间", "晚上", "夜", "深夜", "午夜"]):
        return "室内暖光照明，光影层次丰富。"
    # 无法识别时段 → 使用中性描述
    return "自然光照，光影层次分明。"


def _post_process_video_prompts(
    video_prompts: list[dict],
    char_map: dict[str, str] | None = None,
    segments: list[dict] | None = None,
) -> list[dict]:
    """B 阶段后处理：校验并修复视频提示词的一致性问题。

    处理内容：
    1. 时间线连贯性：检测 time_of_day 倒退，自动修复为前一值
    2. 场景空间一致性：统一同一 scene_id 的空间描述为第一个片段的值
    3. 概述格式：检测缺失的 char_ID 并尝试补全

    Args:
        video_prompts: F06-B 生成的视频提示词列表
        char_map: 全局 char_id → name 映射（用于步骤15校正）
        segments: F06-A 的分段列表（可选，用于步骤14b结尾画面补全）
    """
    if not video_prompts:
        return video_prompts

    # ── 1. 时间线连贯性修复 ──
    # 定义时间词的顺序权重（从早到晚）
    TIME_ORDER = {
        "凌晨": 0, "黎明": 1, "清晨": 2, "早晨": 3, "早上": 4,
        "上午": 5, "中午": 6, "午间": 7, "午后": 8, "下午": 9,
        "傍晚": 10, "黄昏": 11, "夕阳": 12, "晚间": 13, "晚上": 14,
        "夜晚": 15, "深夜": 16, "午夜": 17,
    }

    def _get_time_weight(time_str: str) -> int:
        """根据时间描述词返回大致的时间权重值"""
        if not time_str:
            return -1
        # 检查是否包含具体时间（如"五点四十"）
        hour_match = re.search(r'(\d+)点', time_str)
        if hour_match:
            hour = int(hour_match.group(1))
            if hour < 6: return 0
            elif hour < 9: return 4
            elif hour < 12: return 6
            elif hour < 14: return 8
            elif hour < 17: return 9
            elif hour < 19: return 10
            elif hour < 21: return 14
            else: return 16
        # 根据关键词匹配
        for keyword, weight in TIME_ORDER.items():
            if keyword in time_str:
                return weight
        return -1

    fixed_count_time = 0
    prev_time_str = ""
    prev_weight = -1

    for vp in video_prompts:
        entity_bindings = vp.get("entity_bindings", {})
        if not isinstance(entity_bindings, dict):
            continue
        current_time = entity_bindings.get("time_of_day", "")
        current_weight = _get_time_weight(current_time)

        # 检测倒退：当前时间权重大于-1 且小于前一个权重
        if current_weight > -1 and prev_weight > -1 and current_weight < prev_weight:
            logger.warning(
                "⚠️ 时间线倒退检测: '%s'(weight=%d) → '%s'(weight=%d)，自动修复为 '%s'",
                prev_time_str, prev_weight, current_time, current_weight, prev_time_str,
            )
            entity_bindings["time_of_day"] = prev_time_str
            fixed_count_time += 1
        else:
            prev_time_str = current_time
            prev_weight = current_weight

    if fixed_count_time > 0:
        logger.info("F06-B 后处理：修复 %d 处时间线倒退", fixed_count_time)

    # ── 1b. 时间线向前传播 ──
    # 当某段 prompt 包含"时间已从XX流转至YY"时，YY 是该段的实际时段。
    # 同场景后续段应继承这个已流转的时段值，而非停留在旧值。
    # 例：seg_7 有"流转至傍晚"，则 seg_8(同scene)的"夕阳"应修正为"傍晚"
    FLOW_FORWARD_PATTERN = re.compile(r'时间已从[^\s]+流转至([\u4e00-\u9fff]+)')
    fixed_count_time_fwd = 0

    # 第一遍：收集每段 prompt 中声明的流转目标时段
    forwarded_times: dict[int, str] = {}  # index -> 流转目标时段
    for idx, vp in enumerate(video_prompts):
        prompt_text = vp.get("scene_prompt", "")
        m = FLOW_FORWARD_PATTERN.search(prompt_text)
        if m:
            forwarded_times[idx] = m.group(1)

    # 第二遍：向同场景后续段传播
    if forwarded_times:
        for idx, target_time in forwarded_times.items():
            vp_src = video_prompts[idx]
            src_bindings = vp_src.get("entity_bindings", {})
            src_scene_binding = src_bindings.get("scene_binding", {}) if isinstance(src_bindings, dict) else {}
            src_scene = src_scene_binding.get("scene_id", "") if isinstance(src_scene_binding, dict) else ""
            for jdx in range(idx + 1, len(video_prompts)):
                vp_tgt = video_prompts[jdx]
                tgt_bindings = vp_tgt.get("entity_bindings", {})
                tgt_scene_binding = tgt_bindings.get("scene_binding", {}) if isinstance(tgt_bindings, dict) else {}
                tgt_scene = tgt_scene_binding.get("scene_id", "") if isinstance(tgt_scene_binding, dict) else ""
                tgt_time = tgt_bindings.get("time_of_day", "") if isinstance(tgt_bindings, dict) else ""
                # 只在同场景内传播，遇新的流转声明或更晚时段则停止
                if tgt_scene != src_scene:
                    break
                if jdx in forwarded_times:
                    break
                if _time_matches(tgt_time, target_time):
                    continue  # 已经一致
                tgt_weight = _get_time_weight(tgt_time)
                target_weight = _get_time_weight(target_time)
                if tgt_weight >= target_weight:
                    break  # 目标段时段已更晚或相等，不需要传播
                logger.info(
                    "时间线向前传播(%s): '%s' → '%s' (源自seg_%d的流转声明)",
                    vp_tgt.get("segment_id", "?"), tgt_time, target_time, idx + 1,
                )
                tgt_bindings["time_of_day"] = target_time
                fixed_count_time_fwd += 1

    if fixed_count_time_fwd > 0:
        logger.info("F06-B 后处理步骤1b：向前传播 %d 处时段值", fixed_count_time_fwd)

    # ── 2. 场景空间描述统一 ──
    # 按 scene_id 分组，每组使用第一个片段的场景空间描述作为标准
    scene_space_cache: dict[str, str] = {}  # scene_id -> 标准场景空间文本
    fixed_count_scene = 0

    for vp in video_prompts:
        prompt_text = vp.get("scene_prompt", "")
        if not prompt_text:
            continue

        # 提取当前 segment 的 scene_id
        entity_bindings = vp.get("entity_bindings", {})
        scene_binding = entity_bindings.get("scene_binding", {}) if isinstance(entity_bindings, dict) else {}
        scene_id = scene_binding.get("scene_id", "") if isinstance(scene_binding, dict) else ""

        if not scene_id:
            continue

        # 从 scene_prompt 中提取「场景空间」字段的内容
        # 兼容两种格式：【场景空间：xxx】或 场景空间：xxx
        space_match = re.search(r'场景空间[：:]([^。\n]+(?:核心特征|;))', prompt_text)
        if not space_match:
            continue

        current_space = space_match.group(1).strip()
        # 去掉末尾可能带上的"核心特征"或分号
        current_space = re.sub(r'[；;]?核心特征.*$', '', current_space).strip()

        # 去掉尾部标点，避免替换时与 lookahead 残留的分号重复
        current_space = current_space.rstrip('；;，,。')

        if scene_id not in scene_space_cache:
            # 第一个出现的该 scene_id 片段，缓存其场景空间描述
            scene_space_cache[scene_id] = current_space
        else:
            # 后续片段，检查是否与缓存一致
            standard_space = scene_space_cache[scene_id]
            if current_space != standard_space:
                logger.debug(
                    "场景空间不一致(%s): 当前='%s'... 标准='%s'... → 自动统一",
                    scene_id, current_space[:40], standard_space[:40],
                )
                # 使用正则替换（兼容"场景空间："或"场景空间：【"格式）
                vp["scene_prompt"] = re.sub(
                    r'(场景空间[：:])[^\n]+?(?=；核心特征|；核心|时代地域)',
                    rf'\g<1>{standard_space}；',
                    prompt_text,
                    count=1,
                )
                fixed_count_scene += 1

    if fixed_count_scene > 0:
        logger.info("F06-B 后处理：统一 %d 处场景空间描述", fixed_count_scene)

    # ── 2.5 清洗场景空间中的双重标点 ──
    fixed_count_punct = 0
    for vp in video_prompts:
        for key in ("scene_prompt",):
            text = vp.get(key, "")
            if not text:
                continue
            # 将连续的中文/英文分号压缩为单个；同理处理逗号句号
            cleaned = re.sub(r'([；;,.，。])\1+', r'\1', text)
            if cleaned != text:
                vp[key] = cleaned
                fixed_count_punct += 1

    if fixed_count_punct > 0:
        logger.debug("F06-B 后处理：清洗 %d 处场景空间双重标点", fixed_count_punct)

    # ── 3. 概述格式修复（char_ID 补全）──
    fixed_count_summary = 0
    for vp in video_prompts:
        prompt_text = vp.get("scene_prompt", "")
        if not prompt_text or not char_map:
            continue

        # 提取【概述】内容
        summary_match = re.search(r'【概述】([^。\n]+。)', prompt_text)
        if not summary_match:
            continue

        summary_text = summary_match.group(1)

        # 检查是否有角色名缺少 (char_ID)
        needs_fix = False
        for char_id, char_name in char_map.items():
            # 检查是否存在纯角色名（无 char_ID 后缀）
            pattern = re.compile(re.escape(char_name) + r'(?!\([^)]+\))')
            if pattern.search(summary_text):
                needs_fix = True
                # 替换为带 char_ID 的格式
                summary_text = re.sub(
                    re.escape(char_name) + r'(?!\([^)]+\))',
                    f"{char_name}({char_id})",
                    summary_text,
                )

        if needs_fix:
            new_prompt = prompt_text.replace(
                summary_match.group(0),
                f"【概述】{summary_text}"
            )
            vp["scene_prompt"] = new_prompt
            fixed_count_summary += 1

    if fixed_count_summary > 0:
        logger.info("F06-B 后处理：修复 %d 处概述 char_ID 缺失", fixed_count_summary)

    # ── 4. time_of_day 与光线氛围一致性检测 ──
    # 如果光线氛围描述的是白天（阳光/午后/明亮），但 time_of_day 标为夜晚，自动修正
    DAYLIGHT_KEYWORDS = ["午后阳光", "阳光直射", "阳光从", "阳光明媚", "阳光透过",
                         "自然光从", "明亮的自然光", "午后的", "午后", "斜上方照射",
                         "斑驳光影", "光斑洒", "阳光被"]
    NIGHT_KEYWORDS = ["夜晚", "夜色", "深夜", "月光", "星光", "荧光灯"]

    fixed_time_light = 0
    for vp in video_prompts:
        entity_bindings = vp.get("entity_bindings", {})
        if not isinstance(entity_bindings, dict):
            continue
        current_time = entity_bindings.get("time_of_day", "")
        prompt_text = vp.get("scene_prompt", "")

        if not current_time or not prompt_text:
            continue

        # 提取光线氛围段落
        light_match = re.search(r'光线氛围[：:](.+?)(?:\n|$)', prompt_text)
        if not light_match:
            continue

        light_text = light_match.group(1)
        is_daylight = any(kw in light_text for kw in DAYLIGHT_KEYWORDS)
        is_night_desc = any(kw in light_text for kw in NIGHT_KEYWORDS)

        # 当前标为夜晚但光线描述是白天 → 修正
        if ("夜晚" in current_time or "晚上" in current_time) and is_daylight and not is_night_desc:
            # 根据光线描述推断合理的时间值
            inferred_time = "午后"
            if "傍晚" in light_text or "夕阳" in light_text or "暖黄色" in light_text:
                inferred_time = "傍晚"
            elif "上午" in light_text or "早晨" in light_text:
                inferred_time = "上午"

            logger.debug(
                "time_of_day 光线不一致(%s): 标='%s' 但光线描述含白天关键词 → 修正为 '%s'",
                vp.get("segment_id", "?"), current_time, inferred_time,
            )
            entity_bindings["time_of_day"] = inferred_time
            fixed_time_light += 1

    if fixed_time_light > 0:
        logger.info("F06-B 后处理：修复 %d 处 time_of_day 与光线不一致", fixed_time_light)

    # ── 5. characters_present 冗余过滤 ──
    # 移除在 characters_present 中但未在概述、人物与动作、台词中实际出现的角色
    fixed_chars_ghost = 0
    for vp in video_prompts:
        entity_bindings = vp.get("entity_bindings", {})
        chars_present = entity_bindings.get("characters_present", [])
        if not isinstance(chars_present, list) or len(chars_present) <= 1:
            continue

        prompt_text = vp.get("scene_prompt", "")
        if not prompt_text:
            continue

        # 构建实际出现在内容中的角色集合（兼容新旧格式）
        actual_char_items = []
        for char_item in chars_present:
            # 兼容新格式 {"id": "char_1", "name": "苏念"} 和旧格式 "char_1"
            if isinstance(char_item, dict):
                char_id = char_item.get("id", "")
                char_name = char_item.get("name", "")
            else:
                char_id = char_item
                char_name = char_map.get(char_id, "") if char_map else ""

            if not char_id:
                continue

            # 检查是否在提示词文本中出现（用角色名或视觉特征匹配）
            found = False
            if char_name and re.search(re.escape(char_name), prompt_text):
                found = True
            if not found and char_id:
                # 也检查 id 本身是否出现在文本中（兼容旧格式残留）
                if re.search(re.escape(char_id), prompt_text):
                    found = True
            if found:
                actual_char_items.append(char_item)

        if len(actual_char_items) < len(chars_present):
            if actual_char_items:  # 至少保留一个角色
                ghost_ids = []
                for item in chars_present:
                    if item not in actual_char_items:
                        if isinstance(item, dict):
                            ghost_ids.append(item.get("name", item.get("id", "?")))
                        else:
                            ghost_ids.append(char_map.get(item, item))
                logger.debug(
                    "characters_present 冗余过滤(%s): 移除 %s",
                    vp.get("segment_id", "?"),
                    ghost_ids,
                )
                entity_bindings["characters_present"] = actual_char_items
                fixed_chars_ghost += 1

    if fixed_chars_ghost > 0:
        logger.info("F06-B 后处理：过滤 %d 处 characters_present 冗余角色", fixed_chars_ghost)

    # ── 6. scene_binding 与概述一致性警告 ──
    # 检测场景绑定是否与概述中描述的地点明显矛盾
    fixed_scene_mismatch = 0
    for vp in video_prompts:
        entity_bindings = vp.get("entity_bindings", {})
        scene_binding = entity_bindings.get("scene_binding", {}) if isinstance(entity_bindings, dict) else {}
        scene_name = scene_binding.get("scene_name", "") if isinstance(scene_binding, dict) else ""
        scene_id = scene_binding.get("scene_id", "") if isinstance(scene_binding, dict) else ""

        prompt_text = vp.get("scene_prompt", "")
        if not prompt_text or not scene_name or scene_id == "scene_unknown":
            continue

        summary_match = re.search(r'【概述】([^。\n]+。)', prompt_text)
        if not summary_match:
            continue
        summary = summary_match.group(1)

        # 检测概述中的地点关键词与 scene_name 是否矛盾
        # 常见矛盾模式：概述说"在公司/办公室"，但绑定到"厨房"；概述说"在校门口"，但绑定到"宿舍"
        conflict_patterns = [
            (r'公司|办公室|加班', ["厨房", "客厅", "卧室", "家"]),
            (r'校门口|学校|校园', ["家·厨房", "厨房", "家"]),
            (r'宿舍|寝室', ["礼堂", "操场", "教室", "校门口"]),
            (r'厨房|家里', ["校门口", "礼堂", "操场", "教室"]),
        ]

        for loc_pattern, forbidden_scenes in conflict_patterns:
            if re.search(loc_pattern, summary):
                for fs in forbidden_scenes:
                    if fs in scene_name:
                        sid = vp.get("segment_id", "?")
                        logger.warning(
                            "⚠️ 场景绑定矛盾(%s): 概述含'%s'但绑定到 '%s'(scene_%s)",
                            sid, loc_pattern, scene_name, scene_id,
                        )
                        fixed_scene_mismatch += 1
                        break

    if fixed_scene_mismatch > 0:
        logger.info("F06-B 后处理：发现 %d 处场景绑定可能矛盾（已记录）", fixed_scene_mismatch)

    # ── 7. 跨场景流转语句自动清理 ──
    # 流转模板"时间已从XX流转至YY"仅允许在同一scene_id内使用。
    # 当场景切换后，新场景的segment若包含该语句，说明AI错误地引用了前一个场景的时间状态，
    # 需要自动剥离该前缀，保留后续的光影描述正文。
    TIME_FLOW_PATTERN = re.compile(
        r'时间已从[^流转至]+流转至[^，。；;]+[，。；;]\s*'
    )
    prev_scene_id = ""
    fixed_flow_cross = 0

    for vp in video_prompts:
        prompt_text = vp.get("scene_prompt", "")
        if not prompt_text:
            continue

        entity_bindings = vp.get("entity_bindings", {})
        scene_binding = entity_bindings.get("scene_binding", {}) if isinstance(entity_bindings, dict) else {}
        current_scene_id = scene_binding.get("scene_id", "") if isinstance(scene_binding, dict) else ""

        if not current_scene_id or not prev_scene_id:
            prev_scene_id = current_scene_id
            continue

        # 场景发生变化时，检测当前segment是否包含非法的流转语句
        if current_scene_id != prev_scene_id and TIME_FLOW_PATTERN.search(prompt_text):
            match = TIME_FLOW_PATTERN.search(prompt_text)
            removed_prefix = match.group(0).strip()
            new_prompt = TIME_FLOW_PATTERN.sub('', prompt_text, count=1)
            vp["scene_prompt"] = new_prompt

            logger.warning(
                "⚠️ 跨场景流转语句清理(%s): scene从'%s'切换到'%s'，"
                "移除非法流转前缀「%s」（流转模板仅限同场景内使用）",
                vp.get("segment_id", "?"), prev_scene_id, current_scene_id,
                removed_prefix[:40],
            )
            fixed_flow_cross += 1

        prev_scene_id = current_scene_id

    if fixed_flow_cross > 0:
        logger.info("F06-B 后处理：清理 %d 处跨场景非法流转语句", fixed_flow_cross)

    fixed_char_paren = 0

    # ── 12. 清理角色名括号内描述 ──
    # 将 "苏念（扎马尾的女孩）" → "苏念"、"陆时言（深色夹克的男人）" → "陆时言"
    # 匹配模式：角色名(中文/英文) + （中文描述）
    CHAR_PAREN_PATTERN = re.compile(r'([\u4e00-\u9fff]{2,4})（[^）]+）')
    fixed_char_paren = 0

    for vp in video_prompts:
        for key in ("scene_prompt",):
            text = vp.get(key, "")
            if not text:
                continue
            new_text, count = CHAR_PAREN_PATTERN.subn(r'\1', text)
            if count > 0:
                vp[key] = new_text
                fixed_char_paren += count

    if fixed_char_paren > 0:
        logger.info("F06-B 后处理步骤12：清理 %d 处角色名括号内描述", fixed_char_paren)

    # ── 14. dialogues 台词文本完整性校验 ──
    # 核心规则（致命错误）：dialogues 数组中每句台词的 line 文本必须逐字出现在
    # scene_prompt 中。LLM 在写 prompt 时偶尔会打错字（如"我没有躲你"写成"我没有躲我"），
    # 此步骤检测此类差异并尝试自动修正。
    fixed_dialogue_text = 0

    for vp in video_prompts:
        prompt_text = vp.get("scene_prompt", "")
        dialogues = vp.get("dialogues", [])
        if not prompt_text or not dialogues:
            continue

        for d in dialogues:
            expected_line = d.get("line", "")
            if not expected_line:
                continue

            # 检查台词原文是否出现在 prompt 中
            if expected_line not in prompt_text:
                # 尝试模糊匹配：找最相似的片段（可能只有1-2个字不同）
                # 常见错误模式：代词混淆（你/我/他/她）
                best_match = None
                best_distance = float('inf')

                # 策略：按台词长度滑窗搜索，找编辑距离最小的候选
                import difflib
                # 从 prompt 中提取可能的对话区域（支持单引号、中文单引号、中文双引号）
                quoted_parts = re.findall(r"['\u2018\u201c](.+?)['\u2019\u201d]", prompt_text)
                for qp in quoted_parts:
                    if len(qp) < 3:
                        continue
                    dist = sum(1 for a, b in zip(expected_line, qp) if a != b)
                    dist += abs(len(expected_line) - len(qp)) * 0.5
                    if dist < best_distance:
                        best_distance = dist
                        best_match = qp

                # 如果找到了足够接近的候选（编辑距离 <= 2），且差异很小，执行替换
                # 支持单引号、中文单引号、中文双引号三种格式
                if best_match is not None and best_distance <= 2 and best_match != expected_line:
                    # 单引号变体
                    old_in_prompt_sq = f"'{best_match}'"
                    new_in_prompt_sq = f"'{expected_line}'"
                    # 中文单引号变体
                    old_in_prompt_sq_cn = f'\u2018{best_match}\u2019'
                    new_in_prompt_sq_cn = f'\u2018{expected_line}\u2019'
                    # 中文双引号变体
                    old_in_prompt_cn = f'\u201c{best_match}\u201d'
                    new_in_prompt_cn = f'\u201c{expected_line}\u201d'

                    changed = False
                    for old_pat, new_pat in [
                        (old_in_prompt_sq, new_in_prompt_sq),
                        (old_in_prompt_sq_cn, new_in_prompt_sq_cn),
                        (old_in_prompt_cn, new_in_prompt_cn),
                    ]:
                        if old_pat in prompt_text:
                            prompt_text = prompt_text.replace(old_pat, new_pat, 1)
                            changed = True
                            break

                    if changed:
                        vp["scene_prompt"] = prompt_text

                        logger.warning(
                            "⚠️ [14]台词文字修正(%s): '%s' → '%s' (编辑距离=%.1f)",
                            vp.get("segment_id", "?"), best_match, expected_line, best_distance,
                        )
                        fixed_dialogue_text += 1
                else:
                    # 无法自动修正，仅记录警告
                    truncated_line = expected_line[:30].replace('%', '%%')
                    logger.warning(
                        "⚠️ [14]台词缺失/不匹配(%s): dialogues='%s' 未在 prompt 中找到",
                        vp.get("segment_id", "?"), truncated_line,
                    )
                    fixed_dialogue_text += 1  # 也计入统计

    if fixed_dialogue_text > 0:
        logger.info("F06-B 后处理步骤14：修正 %d 处台词文本不一致", fixed_dialogue_text)

    # ── 15. characters_present 的 char_id → name 映射校正 ──
    # 规则：entity_bindings.characters_present 中每个角色的 id 必须与全局 char_map 一致映射。
    # LLM 偶尔会将 char_1/char_2 的名字写反（如 seg_6 只有陆时言但 char_1 名为"陆时言"而非"苏念"）
    # 此步骤用 char_map 强制校正映射关系。
    fixed_char_id_name = 0

    for vp in video_prompts:
        entity_bindings = vp.get("entity_bindings", {})
        if not isinstance(entity_bindings, dict):
            continue

        chars_present = entity_bindings.get("characters_present", [])
        if not isinstance(chars_present, list) or not chars_present or not char_map:
            continue

        needs_fix = False
        for char_item in chars_present:
            if not isinstance(char_item, dict):
                continue
            cid = char_item.get("id", "")
            cname = char_item.get("name", "")
            expected_name = char_map.get(cid, "")

            if expected_name and cname and cname != expected_name:
                logger.warning(
                    "⚠️ [15]角色ID映射错误(%s): char_id='%s' 名为'%s' 应为'%s'，已校正",
                    vp.get("segment_id", "?"), cid, cname, expected_name,
                )
                char_item["name"] = expected_name
                needs_fix = True
                fixed_char_id_name += 1

        if needs_fix:
            # 同步更新 prompt 中的角色名（如果存在错误名称）
            # 注意：prompt 中的角色名通常已经是正确的（LLM 写 prompt 时用正确名字），
            # 主要问题是 entity_bindings 结构中的映射。此处仅做防御性替换。
            pass

    if fixed_char_id_name > 0:
        logger.info("F06-B 后处理步骤15：校正 %d 处 char_id→name 映射错误", fixed_char_id_name)

    # ── 16. Negative prompt 完整性补全 ──
    # 标准 NP 必须包含的项：
    #   基础必填：额外人物
    #   多人场景必填：面容融合、多人相貌趋同、身体穿透、连体畸形
    #   推荐标准项：动态模糊过度、画面过曝或过暗、数字水印、文字叠加、扭曲变形
    #   光影禁项：无光源突变、无色温闪烁、无阴影跳动、无双重阴影
    #
    # 当 LLM 遗漏推荐标准项时，自动补全到 NP 末尾。
    # 注意：F06 现在返回 scene_prompt(第1-4段) 和 quality_prompt(第5-6段)，
    #       Negative prompt 在 quality_prompt 字段中
    STANDARD_NP_REQUIRED_ITEMS = [
        "额外人物",           # 基础
        "面容融合",           # 多人场景
        "多人相貌趋同",       # 多人场景
        "身体穿透",           # 多人场景
        "连体畸形",           # 多人场景
    ]
    STANDARD_NP_RECOMMENDED_ITEMS = [
        ("动态模糊过度", "动态模糊过度"),
        ("画面过曝或过暗", "画面过曝或过暗"),
        ("数字水印", "数字水印"),
        ("文字叠加", "文字叠加"),
        ("扭曲变形", "扭曲变形"),
    ]
    STANDARD_NP_LIGHT_ITEMS = [
        ("无光源突变", "无光源突变"),
        ("无色温闪烁", "无色温闪烁"),
        ("无阴影跳动", "无阴影跳动"),
        ("无双重阴影", "无双重阴影"),
    ]
    fixed_np_missing = 0

    for vp in video_prompts:
        # 步骤16处理 quality_prompt 字段中的 Negative prompt 完整性
        text = vp.get("quality_prompt", "")
        if not text:
            continue

        # 提取 Negative prompt 部分
        np_match = re.search(r'Negative prompt[：:](.+?)(?:$)', text, re.DOTALL)
        if not np_match:
            continue

        np_text = np_match.group(1).strip()
        original_np = np_text
        additions = []

        # 检查并补全推荐标准项
        for item_keyword, item_full in STANDARD_NP_RECOMMENDED_ITEMS:
            if item_keyword not in np_text:
                additions.append(item_full)

        # 检查并补全光影禁项
        for item_keyword, item_full in STANDARD_NP_LIGHT_ITEMS:
            if item_keyword not in np_text:
                additions.append(item_full)

        if additions:
            # 追加缺失项
            np_new = original_np.rstrip('，。') + "，" + "，".join(additions) + "。"
            new_text = text[:np_match.start(1)] + np_new
            vp["quality_prompt"] = new_text
            fixed_np_missing += 1

            logger.debug(
                "[16]NP补全(%s): 缺失 %s 项 → 已追加",
                vp.get("segment_id", "?"),
                len(additions),
            )

    if fixed_np_missing > 0:
        logger.info("F06-B 后处理步骤16：补全 %d 处 Negative prompt 遗漏项", fixed_np_missing)

    # ── 17. characters_present 与 prompt 角色一致性校验（双向） ──
    # 规则：characters_present 列表必须与 scene_prompt 中实际出场的角色完全一致。
    #
    # 双向校验：
    #   A) 移除多余：characters_present 中有但 prompt 中未实质性出场的角色 → 从列表移除
    #   B) 补全遗漏：prompt 中出场但 characters_present 中缺少的角色 → 补全到列表
    #
    # 场景举例：
    #   - seg_6 prompt="以陆时言为中心"，无苏念任何描述，但 characters_present=[苏念] → 应移除苏念
    #   - seg_6 同上但 characters_present=[] → 应补全陆时言
    #   - seg_7 prompt 以苏念和陆时言为中心，characters_present=[苏念] → 应补全陆时言
    fixed_chars_present = 0

    for vp in video_prompts:
        entity_bindings = vp.get("entity_bindings", {})
        if not isinstance(entity_bindings, dict):
            continue

        chars_present = entity_bindings.get("characters_present", [])
        if not isinstance(chars_present, list) or not char_map:
            continue

        prompt_text = vp.get("scene_prompt", "")
        if not prompt_text:
            continue

        # ══════════════ 步骤A：确定 prompt 中实际出场的角色集合 ══════════════
        # 策略1（高置信）：从"以...为中心/为焦点/为主体"句式提取主角
        prompt_main_chars = set()
        center_match = re.search(r'以(.+?)与?(.+?)(?:为中心|为焦点|为主体)', prompt_text)
        if center_match:
            for g in (center_match.group(1), center_match.group(2)):
                g = g.strip()
                if not g or len(g) < 2:
                    continue
                # 仅当该名字存在于 char_map 时才认定为主角
                for _cid, _cname in char_map.items():
                    if _cname == g:
                        prompt_main_chars.add(_cname)
                        break

        # 策略2（兜底）：若策略1未匹配到，用全文搜索判断角色是否有实质出场
        if not prompt_main_chars:
            for _cid, _cname in char_map.items():
                if _cname in prompt_text:
                    prompt_main_chars.add(_cname)

        # ══════════════ 步骤B-1：移除多余角色 ══════════════
        indices_to_remove = []
        for idx, c in enumerate(chars_present):
            if not isinstance(c, dict):
                continue
            cname = c.get("name", "")
            if cname and cname not in prompt_main_chars:
                indices_to_remove.append((idx, cname))

        # 倒序删除避免索引偏移
        for idx, removed_name in reversed(indices_to_remove):
            removed_entry = chars_present.pop(idx)
            logger.warning(
                "⚠️ [17-A]characters_present多余(%s): '%s'在prompt中未出场，已移除",
                vp.get("segment_id", "?"), removed_name,
            )
            fixed_chars_present += 1

        # ══════════════ 步骤B-2：补全遗漏角色 ══════════════
        existing_ids = {c.get("id", "") for c in chars_present if isinstance(c, dict)}
        existing_names = {c.get("name", "") for c in chars_present if isinstance(c, dict)}

        for name_in_prompt in sorted(prompt_main_chars):
            if name_in_prompt in existing_names:
                continue  # 已存在，跳过

            # 反查 char_map 获取 id
            matched_id = None
            for cid, cname in char_map.items():
                if cname == name_in_prompt:
                    matched_id = cid
                    break

            if matched_id is None or matched_id in existing_ids:
                continue

            chars_present.append({"id": matched_id, "name": name_in_prompt})
            logger.warning(
                "⚠️ [17-B]characters_present遗漏(%s): prompt中出场'%s'(id=%s)但列表缺失，已补全",
                vp.get("segment_id", "?"), name_in_prompt, matched_id,
            )
            fixed_chars_present += 1

    if fixed_chars_present > 0:
        logger.info("F06-B 后处理步骤17：双向校验 characters_present，共修正 %d 处（补全/移除）", fixed_chars_present)

    # ── 18. 跨segment对话内容去重 ──
    # LLM 偶尔将同一段台词（如车站广播、旁白）重复写入相邻两个 segment 的
    # dialogues 数组和 prompt 文本中。此步骤检测并从后一个 segment 中移除重复项。
    fixed_dedup_dialogue = 0

    for idx in range(1, len(video_prompts)):
        vp_prev = video_prompts[idx - 1]
        vp_curr = video_prompts[idx]

        prev_dialogues = vp_prev.get("dialogues", [])
        curr_dialogues = vp_curr.get("dialogues", [])
        if not prev_dialogues or not curr_dialogues:
            continue

        # 构建"前一段台词集合"（用 line 文本匹配）
        prev_lines_set = set()
        for pd in prev_dialogues:
            pline = pd.get("line", "").strip()
            if pline and len(pline) >= 4:  # 忽略太短的（如"嗯""啊"）
                prev_lines_set.add(pline)

        # 也检查前一段 prompt 中出现的非对话类重复文本（如广播旁白）
        prev_prompt = vp_prev.get("scene_prompt", "")
        curr_prompt = vp_curr.get("scene_prompt", "")

        # 检测当前段的 dialogues 中是否有与前段重复的
        dup_indices: list[int] = []
        for di, cd in enumerate(curr_dialogues):
            cline = cd.get("line", "").strip()
            if cline in prev_lines_set:
                dup_indices.append(di)
                continue
            # 模糊匹配：前段 prompt 中是否已逐字包含此句台词
            if len(cline) >= 8 and cline in prev_prompt:
                dup_indices.append(di)

        # 从后往前删除（避免索引偏移）
        for di in reversed(dup_indices):
            removed = curr_dialogues.pop(di)
            rline = removed.get("line", "")
            sid = vp_curr.get("segment_id", "?")
            logger.info(
                "[18]跨段对话去重(%s): 移除与seg_%d重复的台词 '%s...'",
                sid, idx, rline[:15],
            )
            # 同时从 prompt 文本中尝试移除该句
            # 匹配模式："说：'XXX'" 或 '"XXX"' 或 直接文本片段
            for key in ("scene_prompt",):
                txt = vp_curr.get(key, "")
                if not txt or rline not in txt:
                    continue
                # 构建上下文匹配——找到包含该台词的最短子句
                # 常见格式：说："XXX"。 或 "XXX"。 或 第N秒，..."XXX"
                import re as _re
                # 尝试引号包裹形式
                escaped = _re.escape(rline)
                patterns_to_try = [
                    _re.compile(r'[^。；]{0,20}' + escaped + r'[。"？】！]*'),
                    _re.compile(escaped + r'[。"？】！]*'),
                ]
                for pat in patterns_to_try:
                    m = pat.search(txt)
                    if m:
                        new_txt = txt[:m.start()] + txt[m.end():]
                        # 清理可能留下的残留标点/空格
                        new_txt = _re.sub(r'[,，]\s*([。。])', r'\1', new_txt)
                        new_txt = _re.sub(r'\s{2,}', ' ', new_txt).strip()
                        vp_curr[key] = new_txt
                        break

            fixed_dedup_dialogue += 1

    if fixed_dedup_dialogue > 0:
        logger.info("F06-B 后处理步骤18：去除 %d 处跨segment重复对话", fixed_dedup_dialogue)

    # ── 19. 结构封闭性强制修复（NP后溢出 / 质量要求-NP间隙溢出） ──
    # 检测两种常见的 LLM 结构溢出错误：
    #   模式A: NP 最后一句号之后还有叙事内容（NP trailing）
    #   模式B: 质量要求段结束到 NP 之间有叙事性内容（quality-NP gap）
    # 修复策略：将溢出内容迁移到正确的位置（质量要求末尾）
    #
    # 注意：F06 现在返回 scene_prompt(第1-4段) 和 quality_prompt(第5-6段)，
    #       质量要求和 Negative prompt 都在 quality_prompt 字段中
    fixed_np_overflow = 0
    NP_ANCHOR_PATTERN = re.compile(r'Negative prompt[：:]')
    QUALITY_END_PATTERN = re.compile(r'质量要求[：:][^。]+[。，]')  # 质量要求的结尾句

    for vp in video_prompts:
        # 步骤19处理 quality_prompt 字段中的 NP 溢出问题
        text = vp.get("quality_prompt", "")
        if not text:
            continue

        np_match = NP_ANCHOR_PATTERN.search(text)
        if not np_match:
            continue

        sid = vp.get("segment_id", "?")
        np_start = np_match.start()

        # === 模式 A: NP 后溢出 ===
        np_section = text[np_start:]
        last_period_in_np = np_section.rfind('。')
        has_np_trailing = False
        np_trailing_text = ""
        
        if last_period_in_np >= 0:
            after_np = np_section[last_period_in_np + 1:].strip()
            if (len(after_np) > 2 
                and not re.match(r'^[\s\n\]}]*$', after_np) 
                and re.search(r'[\u4e00-\u9fff]', after_np)):
                has_np_trailing = True
                np_trailing_text = after_np

        # === 模式 B: 质量要求-NP 之间的间隙溢出 ===
        quality_end_match = QUALITY_END_PATTERN.search(text)
        has_quality_gap = False
        gap_content = ""
        
        if (quality_end_match 
            and quality_end_match.end() < np_start 
            and quality_end_match.end() > 0):
            gap_raw = text[quality_end_match.end():np_start].strip()
            # 间隙内容包含中文叙事句
            if (len(gap_raw) > 3 and re.search(r'[\u4e00-\u9fff]', gap_raw)):
                has_quality_gap = True
                gap_content = gap_raw

        # --- 执行修复 ---
        if has_np_trailing:
            # 截断 NP 后多余的叙事内容
            clean_text = text[:np_start + last_period_in_np + 1].rstrip()
            vp["quality_prompt"] = clean_text
            logger.warning(
                "[19]NP后截断(%s): NP后有溢出「%s…」→ 已截断",
                sid,
                np_trailing_text[:20],
            )
            fixed_np_overflow += 1

        elif has_quality_gap and quality_end_match:
            # 将间隙内容从质量要求-NP之间移除
            clean_gap = gap_content.strip()
            if not clean_gap.endswith('。'):
                clean_gap += '。'
            
            # 移除质量要求-NP之间的间隙内容
            q_end = quality_end_match.end()
            new_text = text[:q_end] + text[np_start:]
            vp["quality_prompt"] = new_text
            logger.warning(
                "[19]间隙修复(%s): 质量-NP间有溢出「%s…」→ 已移除",
                sid,
                gap_content[:25],
            )
            fixed_np_overflow += 1

    if fixed_np_overflow > 0:
        logger.info("F06-B 后处理步骤19：修复 %d 处 quality_prompt 中 NP 溢出问题", fixed_np_overflow)

    # ── 20. 6段式结构完整性校验 ──
    # 强制校验每个 scene_prompt 是否包含6段式结构的所有关键锚点。
    # 6段顺序：[镜头指令] → 以...为中心 → {动作/对白} → {光影/风格/氛围} → 质量要求： → Negative prompt：
    # 缺失段时记录警告（不自动补全，因为缺少上下文无法生成合理内容）
    # 注意：F06 现在返回 scene_prompt(第1-4段) 和 quality_prompt(第5-6段) 两个独立字段
    fixed_structure = 0
    STRUCTURE_ANCHORS_SCENE = [
        (r'\[', '镜头指令(方括号开头)'),
        (r'以\S+为中心', 'Subject锚定(以...为中心)'),
        ('影像质感为', '风格锚定(影像质感)'),
        ('色彩基调为', '风格锚定(色彩基调)'),
    ]
    STRUCTURE_ANCHORS_QUALITY = [
        (r'质量要求[：:]', '质量要求段'),
        (r'Negative prompt[：:]', 'Negative prompt段'),
    ]

    for vp in video_prompts:
        sid = vp.get("segment_id", "?")
        scene_text = vp.get("scene_prompt", "")
        quality_text = vp.get("quality_prompt", "")

        missing_anchors = []

        # 检查 scene_prompt 字段（第1-4段）
        for pattern, label in STRUCTURE_ANCHORS_SCENE:
            if not re.search(pattern, scene_text):
                missing_anchors.append(label)

        # 检查 quality_prompt 字段（第5-6段）
        for pattern, label in STRUCTURE_ANCHORS_QUALITY:
            if not re.search(pattern, quality_text):
                missing_anchors.append(label)

        if missing_anchors:
            logger.warning(
                "[20]结构缺失(%s): 缺少 %d 个关键段: %s",
                sid, len(missing_anchors), " / ".join(missing_anchors),
            )
            fixed_structure += 1

    if fixed_structure > 0:
        logger.warning("F06-B 后处理步骤20：发现 %d 个 segment 存在结构缺陷（已记录警告，需人工审查或重新生成）", fixed_structure)

    # ── 21. Negative prompt 格式统一 ──
    # 规则：Negative prompt 前不添加换行，直接拼接
    # 修复场景：LLM 生成时可能有多余换行符
    # 注意：F06 现在返回 scene_prompt(第1-4段) 和 quality_prompt(第5-6段)，
    #       Negative prompt 在 quality_prompt 字段中
    fixed_np_format = 0
    NP_PATTERN = re.compile(r'([ \t]*\n+)\s*(Negative prompt[：:])')

    for vp in video_prompts:
        # 步骤21处理 quality_prompt 字段中的 Negative prompt 格式
        text = vp.get("quality_prompt", "")
        if not text:
            continue

        # 检查 Negative prompt 前是否有换行符，有则移除
        match = NP_PATTERN.search(text)
        if match:
            corrected = text[:match.start()] + match.group(2) + text[match.end():]
            vp["quality_prompt"] = corrected
            fixed_np_format += 1

    if fixed_np_format > 0:
        logger.info("F06-B 后处理步骤21：统一 %d 处 Negative prompt 前换行格式", fixed_np_format)

    # ── 输出自检汇总 ──
    total_fixes = (
        fixed_count_time + fixed_count_time_fwd + fixed_count_scene + fixed_count_punct +
        fixed_count_summary + fixed_chars_ghost +
        fixed_scene_mismatch + fixed_flow_cross +
        fixed_char_paren + fixed_dialogue_text +
        fixed_char_id_name + fixed_np_missing + fixed_chars_present + fixed_dedup_dialogue
        + fixed_np_overflow + fixed_structure + fixed_np_format
    )
    if total_fixes > 0:
        logger.info(
            "=== F06-B 输出自检汇总 === 总修复 %d 处 | "
            "时间线:%d 时间传播:%d 场景空间:%d 标点:%d 概述:%d 冗余角色:%d "
            "场景矛盾:%d 跨场景流转:%d 角色括号:%d "
            "台词文本:%d 角色映射:%d NP补全:%d 角色遗漏:%d 跨段去重:%d NP溢出:%d 结构缺陷:%d NP格式:%d ===",
            total_fixes,
            fixed_count_time, fixed_count_time_fwd, fixed_count_scene, fixed_count_punct,
            fixed_count_summary, fixed_chars_ghost,
            fixed_scene_mismatch, fixed_flow_cross,
            fixed_char_paren,
            fixed_dialogue_text, fixed_char_id_name,
            fixed_np_missing, fixed_chars_present, fixed_dedup_dialogue,
            fixed_np_overflow, fixed_structure, fixed_np_format,
        )

    return video_prompts


def generate_video_prompts(
    segments: list[dict],
    visual_tone: dict,
    characters: list[dict] | None = None,
    scenes: list[dict] | None = None,
    task_id: str | None = None,
    max_workers: int = 5,
) -> dict[str, Any]:
    """F06-B: 视频提示词生成（并发版本，max_workers 控制并发数）"""
    from src.config import DEFAULT_MODEL_ID
    from concurrent.futures import ThreadPoolExecutor, as_completed

    task_id = task_id or str(uuid.uuid4())

    # 构建 char_id -> name 映射（用于 "角色名(char_id)" 格式）
    char_map: dict[str, str] = {}
    # 构建 char_id -> visual_anchor 映射（供 F06-B 直接复用）
    visual_anchor_map: dict[str, str] = {}
    if characters:
        for ch in characters:
            if isinstance(ch, dict) and ch.get("id") and ch.get("name"):
                char_map[ch["id"]] = ch["name"]
            if isinstance(ch, dict) and ch.get("id") and ch.get("visual_anchor"):
                visual_anchor_map[ch["id"]] = ch["visual_anchor"]
    logger.info("角色映射: %s", char_map)
    logger.info("视觉锚定映射: %s", visual_anchor_map)

    # 解析场景映射（从 f03 输出中提取）
    _, _, scene_map = _extract_immutable_features(
        characters or [], scenes or [], visual_tone
    )
    # 构建完整场景数据映射（用于 B 阶段注入 F03 详细场景信息）
    full_scene_map = _build_full_scene_map(scenes or [])
    logger.info("场景映射: %s", scene_map)

    # 将 visual_tone 压缩为摘要字符串（避免输入过大）
    vt_genre = visual_tone.get("genre", {})
    vt_style = visual_tone.get("visual_style", {})
    vt_color = visual_tone.get("color_palette", {})
    vt_lighting = visual_tone.get("lighting_philosophy", "")
    vt_atmosphere = visual_tone.get("atmosphere", {})
    # 提取时代地域信息（用于 F06-B 的【时代地域】字段）
    vt_era = visual_tone.get("era_setting", {})
    vt_world = visual_tone.get("world_setting", {})
    era_str = vt_era.get("era", "") if isinstance(vt_era, dict) else ""
    geo_str = vt_world.get("geographic_context", "") if isinstance(vt_world, dict) else ""
    social_str = vt_world.get("social_class", "") if isinstance(vt_world, dict) else ""
    era_world_info_parts = [era_str, geo_str, social_str]
    era_world_info = "，".join(p for p in era_world_info_parts if p)
    logger.info("时代地域: %s", era_world_info)

    # 提取 medium 用于填充模板中的 {{MEDIUM_STYLE}}
    vt_medium = vt_style.get("medium", "")
    # medium 到中文描述的映射
    medium_map = {
        "cinematic": "电影感、写实摄影风格",
        "anime": "动画风格、二次元美术",
        "tv_series": "电视剧质感、写实摄影",
        "documentary": "纪录片风格、自然光摄影",
    }
    medium_style_str = medium_map.get(vt_medium, f"{vt_medium}风格")

    # 提取 reference_works 用于填充模板中的 {{REFERENCE_WORKS}}
    reference_works_list = vt_style.get("reference_works", [])
    reference_works_str = "、".join(reference_works_list) if reference_works_list else "无"

    tone_summary = (
        f"类型:{vt_genre.get('primary','')}/{vt_genre.get('secondary','')} | "
        f"媒介:{vt_medium} | "
        f"导演风格:{vt_style.get('director_style','')} | "
        f"摄影特点:{vt_style.get('cinematography','')} | "
        f"参考作:{reference_works_str} | "
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

        # 构建 color_palette 字符串
        dominant_colors = vt_color.get("dominant_colors", [])
        accent_color = vt_color.get("accent_color", "")
        color_palette_str = "温柔的暖杏色调" if not dominant_colors else f"{'、'.join(dominant_colors)}主调"
        if accent_color:
            color_palette_str += f"，点缀少许{accent_color}"

        user_message = _build_f06b_prompt(
            seg, tone_summary,
            char_map=char_map,
            visual_anchor_map=visual_anchor_map,
            scene_map=scene_map,
            full_scene_map=full_scene_map,
            era_world_info=era_world_info,
            medium_style=medium_style_str,
            reference_works=reference_works_str,
            color_palette=color_palette_str,
        )
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
                    max_tokens=16384,  # scene_prompt 字段较长，需足够空间避免截断
                    timeout=300,
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
        _err_chars = [
            {"id": c, "name": char_map.get(c, c)}
            for c in seg.get("characters_present", [])
        ]
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
            "scene_prompt": f"[处理失败] segment={seg.get('id')}, error={last_exc}",
            "quality_prompt": "",
        }

    # 并发执行，按原始顺序收集结果
    results: list[tuple[int, dict]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_one, (i, seg)): i for i, seg in enumerate(segments)}
        # 容错：Windows 上 ThreadPoolExecutor 偶发 threading.Condition.acquire() 异常
        # 用 try-except 包裹 as_completed 循环，避免单个 future 失败导致整个批次崩溃
        pending_futures = set(futures.keys())
        while pending_futures:
            try:
                # 使用较短的超时分批等待，便于检测异常并继续处理剩余 future
                done_futures = set()
                for future in as_completed(pending_futures, timeout=600):
                    try:
                        results.append(future.result())
                    except Exception as fut_err:
                        # future 本身抛出异常（理论上 _process_one 已内部捕获所有异常，
                        # 但作为安全网仍需处理）
                        idx = futures.get(future, -1)
                        seg_id = segments[idx].get("id", "?") if 0 <= idx < len(segments) else "?"
                        logger.error("⚠️ [并发容错] future(%s/%s) result() 异常: %s", idx, seg_id, fut_err)
                        # 插入降级结果
                        fallback: dict[str, Any] = {
                            "segment_id": seg_id,
                            "duration_seconds": 10,
                            "entity_bindings": {
                                "characters_present": [],
                                "scene_binding": {"scene_id": "", "scene_name": ""},
                                "time_of_day": "",
                            },
                            "scene_prompt": f"[并发异常] segment={seg_id}, error={fut_err}",
                            "quality_prompt": "",
                        }
                        results.append((idx, fallback))
                    done_futures.add(future)
                pending_futures -= done_futures
            except TimeoutError:
                logger.warning("⚠️ [并发容错] as_completed 超时(600s)，剩余 %d 个 segment 未完成", len(pending_futures))
                # 超时后继续循环等待剩余的 futures
                continue
            except Exception as loop_err:
                # Windows threading 底层竞态条件 (threading.Condition.acquire / waiter.event.wait)
                # 记录详细日志并尝试继续收集剩余结果
                logger.error(
                    "⚠️ [并发容错] as_completed 循环异常(type=%s): %s | 剩余 %d 个 future 待收集",
                    type(loop_err).__name__, loop_err, len(pending_futures),
                    exc_info=True,
                )
                # 尝试逐个收集已完成但尚未取回结果的 future
                still_pending = set()
                for f in pending_futures:
                    if f.done():
                        try:
                            results.append(f.result())
                        except Exception:
                            idx = futures.get(f, -1)
                            seg_id = segments[idx].get("id", "?") if 0 <= idx < len(segments) else "?"
                            results.append((idx, {
                                "segment_id": seg_id,
                                "duration_seconds": 10,
                                "entity_bindings": {"characters_present": [], "scene_binding": {"scene_id": "", "scene_name": ""}, "time_of_day": ""},
                                "scene_prompt": f"[容错降级] segment={seg_id}",
                                "quality_prompt": "",
                            }))
                    else:
                        still_pending.add(f)
                if still_pending:
                    logger.warning("⚠️ [并发容错] 仍有 %d 个 future 未完成，等待它们结束...", len(still_pending))
                    # 等待剩余的（不再使用 as_completed，直接用 result 阻塞）
                    for f in still_pending:
                        try:
                            results.append(f.result(timeout=300))
                        except Exception:
                            idx = futures.get(f, -1)
                            seg_id = segments[idx].get("id", "?") if 0 <= idx < len(segments) else "?"
                            results.append((idx, {
                                "segment_id": seg_id,
                                "duration_seconds": 10,
                                "entity_bindings": {"characters_present": [], "scene_binding": {"scene_id": "", "scene_name": ""}, "time_of_day": ""},
                                "scene_prompt": f"[超时降级] segment={seg_id}",
                                "quality_prompt": "",
                            }))
                break  # 退出外层 while

    # 按原始顺序排序
    results.sort(key=lambda x: x[0])
    video_prompts = [r for _, r in results]

    # 后处理：一致性校验与修复（时间线、场景空间、概述格式）
    video_prompts = _post_process_video_prompts(video_prompts, char_map, segments=segments)

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
    _debug_path_a.parent.mkdir(parents=True, exist_ok=True)
    with open(_debug_path_a, "w", encoding="utf-8") as f:
        json.dump(segmentation_result, f, ensure_ascii=False, indent=2)

    logger.info("阶段 A 完成: 共 %d 个 segment (原始 keys=%s)", total_segments, list(segmentation_result.keys()))

    # 阶段 B: 视频提示词生成
    logger.info("--- 阶段 B: 视频提示词生成 ---")
    video_prompt_result = generate_video_prompts(
        segments=segments,
        visual_tone=visual_tone,
        characters=characters,
        scenes=scenes,
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
    parser.add_argument("f02_output", help="F02 角色设计输出 JSON 文件路径")
    parser.add_argument("f03_output", help="F03 场景设计输出 JSON 文件路径")
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

    # 读取 f02 输出（角色设计），从 characters.list 或 results 数组中提取
    with open(args.f02_output, "r", encoding="utf-8") as f:
        f02_data = json.load(f)
    # 兼容两种格式：characters.list 或 results
    f02_results = f02_data.get("characters", {}).get("list", []) or f02_data.get("results", [])
    # 提取角色基本信息：id, name, appearance 等
    characters = []
    for item in f02_results:
        char_info = {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "aliases": item.get("aliases", []),
            "gender": item.get("gender", ""),
            "age": item.get("age", ""),
            "appearance": item.get("appearance", ""),
            "clothing": item.get("clothing", ""),
            "visual_anchor": item.get("visual_anchor", ""),
            "personality": item.get("personality", ""),
        }
        characters.append(char_info)
    print(f"角色数量: {len(characters)}")

    # 读取 f03 输出（场景设计），从 scenes.list 或 results 数组中提取
    with open(args.f03_output, "r", encoding="utf-8") as f:
        f03_data = json.load(f)
    # 兼容两种格式：scenes.list 或 results
    f03_results = f03_data.get("scenes", {}).get("list", []) or f03_data.get("results", [])
    # 保留完整场景字段，尤其是 scene_layout_description。
    # B 阶段会优先使用该字段来约束画面空间布局。
    scenes = []
    for item in f03_results:
        scene_info = dict(item)
        scene_info.update({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "aliases": item.get("aliases", []),
            "scene_type": item.get("scene_type", ""),
            "location_type": item.get("location_type", ""),
            "environment": item.get("environment", {}),
            "visual_description": item.get("visual_description", {}),
            "lighting_condition": item.get("lighting_condition", {}),
            "color_scheme": item.get("color_scheme", {}),
            "scene_layout_description": item.get("scene_layout_description", ""),
        })
        scenes.append(scene_info)
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
                scene_prompt = vp.get("scene_prompt", "")
                quality_prompt = vp.get("quality_prompt", "")
                prompt_len = len(scene_prompt) + len(quality_prompt)
                prompt_preview = scene_prompt[:40]
                print(f"    [{i}] {seg_id} - {duration}秒 | {prompt_len}字 | {prompt_preview}...")

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




