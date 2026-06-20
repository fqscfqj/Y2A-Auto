#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一 Prompt 中心

职责：
- 注册与管理所有 LLM system prompt 的定义（ID、默认模板、模式等）
- 提供模板渲染（变量替换）与协议壳拼装
- 保证 JSON 输出、一一对应、残句边界等硬约束不被用户自定义 Prompt 破坏
- 回退机制：渲染失败或文本为空时自动回退 builtin 模式

首期覆盖 4 组翻译 Prompt：
  - SUBTITLE_TRANSLATE:        字幕翻译主 Prompt
  - SUBTITLE_TRANSLATE_STRICT: 字幕翻译严格补救 Prompt
  - METADATA_TRANSLATE:        标题/简介翻译主 Prompt
  - METADATA_DESC_RETRY:       简介重试 Prompt
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("prompt_manager")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MODE_BUILTIN = "builtin"
MODE_APPEND = "append"
MODE_OVERRIDE = "override"
VALID_MODES = frozenset({MODE_BUILTIN, MODE_APPEND, MODE_OVERRIDE})

# 后端字符上限（保护 system prompt 不要过长，压缩正文 token 空间）
MAX_PROMPT_TEXT_LENGTH = 3000

# ---------------------------------------------------------------------------
# Prompt 定义注册表
# ---------------------------------------------------------------------------

_PROMPT_REGISTRY: Dict[str, Dict[str, Any]] = {}


def _register_prompt(
    prompt_id: str,
    *,
    label: str,
    description: str,
    builtin_template: str,
    variables: Optional[List[str]] = None,
    is_advanced: bool = False,
    applies_to: str = "subtitle",
) -> None:
    """注册一个 Prompt 定义。仅模块加载时调用。"""
    _PROMPT_REGISTRY[prompt_id] = {
        "id": prompt_id,
        "label": label,
        "description": description,
        "builtin_template": builtin_template,
        "variables": variables or [],
        "is_advanced": is_advanced,
        "applies_to": applies_to,
    }


# ---------------------------------------------------------------------------
# 内置协议壳（硬约束，不可被用户覆盖）
# ---------------------------------------------------------------------------

_SUBTITLE_SHARED_RULES = (
    "必须保持与输入数组一一对应：第 N 条输入只翻译成第 N 条输出。"
    "禁止跨条目借用、合并、拆分、提前翻译下一条或重复上一条内容。"
    "若某条源文本本身是不完整短语、半句或续句，译文也必须保持同样边界，不要擅自补成完整句。"
    "不要根据上下文重写相邻条目，不要消除原始断句。"
)

_SUBTITLE_STRICT_SHARED_RULES = (
    "必须保持与输入数组一一对应：第 N 条输入只翻译成第 N 条输出。"
    "禁止跨条目借用、合并、拆分、提前翻译下一条或重复上一条内容。"
    "即使上下文相关，也不得把相邻条目的信息揉进当前条目。"
    "若源文本是不完整短语、半句或续句，译文也保持不完整，不要补全。"
)

_SUBTITLE_JSON_SUFFIX = '只返回 JSON：{"translations":["译文1","译文2"]}。'

_METADATA_JSON_SUFFIX = '只返回 JSON：{"title":"","description":""}。'
_METADATA_DESC_RETRY_JSON_SUFFIX = '只返回 JSON：{"description":""}。'

# ---------------------------------------------------------------------------
# 首期 4 组 Prompt 的内置行为层模板
# ---------------------------------------------------------------------------

# ---------- 字幕翻译主 Prompt ----------
_SUBTITLE_ZH_BUILTIN_BEHAVIOR = (
    "你是字幕翻译器。按顺序把 texts 每一项翻译成简体中文。"
    "等价翻译，不解释、不扩写；数字、代码、URL、占位符和无公认译法的专有名词可保留，"
    "其余可翻译内容尽量译成自然简体中文。"
)

_SUBTITLE_DEFAULT_BUILTIN_BEHAVIOR = (
    "你是字幕翻译器。按顺序把 texts 每一项翻译成{target_language_name}。"
    "等价翻译，不解释、不扩写；保留数字、代码、URL、占位符和无公认译名的专有名词。"
)

# ---------- 字幕翻译严格补救 Prompt ----------
_SUBTITLE_STRICT_ZH_BUILTIN_BEHAVIOR = (
    "你是字幕翻译器（严格模式）。按顺序把 texts 每一项尽量完整翻译成自然简体中文。"
    "普通句子和说明文字不得整句保留原文；仅保留数字、代码、URL、占位符和必要专有名词。"
)

_SUBTITLE_STRICT_DEFAULT_BUILTIN_BEHAVIOR = (
    "你是字幕翻译器（严格模式）。按顺序把 texts 每一项完整翻译成{target_language_name}。"
    "除数字、代码、URL、占位符和专有名词外，不要保留原文。"
)

# ---------- 标题/简介翻译主 Prompt ----------
_METADATA_BUILTIN_BEHAVIOR = (
    "你是视频标题和简介翻译器。将输入字段改写为{target_language_name}。"
    "只允许重述原文事实，删除导流、社媒、外链、联系方式和互动引导。"
    "title 必须是自然单行标题；description 必须是自然简介，可多段，但不能写成列表、备注或说明。"
    "禁止补充新事实、解释或备注。"
)

# ---------- 简介重试 Prompt ----------
_DESC_RETRY_BUILTIN_BEHAVIOR = (
    "你是视频简介翻译器。将 description 翻译并改写为{target_language_name}自然简介。"
    "只允许重述原文事实，删除导流、社媒、外链、联系方式和互动引导。"
    "description 可以多段，不限制段落数，但不能输出列表、备注、解释或额外说明。"
)


# ---------- AI 智能分段 Prompt（基于字级/段级时间戳的语义重分段，非翻译型） ----------
# 字级模式：输入带字级时间戳的词序列，AI 按语义重新分组为字幕条目，时间精确到词边界。
# 遵循 Netflix Timed Text Style Guide 的断句原则。
_SMART_SEGMENT_WORD_BUILTIN_BEHAVIOR = (
    "基于提供的字级时间戳词序列，重新切分为可直接上线的字幕条目。"
    "\n\n"
    "## 核心原则：每条字幕必须是一个自足的「呼吸单元」\n"
    "- 单看这一条字幕，观众就应该能理解一个完整的意思片段。\n"
    "- 绝不要把一句完整的话硬生生从中间劈开，让上半条不知所云、下半条才补完意思。\n"
    "- 好的字幕：每一条都是一个自然停顿点，像说话时的换气一样。\n"
    "\n"
    "## 断句优先级（从高到低）\n"
    "1. 【句末标点】句号 . 、问号 ? 、感叹号 ! 之后 —— 这是最理想的切分点，必须优先使用。\n"
    "2. 【分句标点】分号 ; 、冒号 : 之后 —— 非常好的次级切分点。\n"
    "3. 【从句/并列连接词之前】and、but、or、that、which、who、where、when、if、because、so、like 等之前 —— 把连接词留给下一条，让下一条以连接词开头是自然的。\n"
    "4. 【逗号 + 长停顿处】逗号后如果跟着明显的语意转折，可以切分；但不要把逗号前后的短小成分拆成两条碎片。\n"
    "5. 【介词短语开始处】before、after、with、without、for、from 等引导新短语时，可在此前切分。\n"
    "\n"
    "## 绝对禁止的切分（红线）\n"
    "- 禁止在冠词与名词之间切分（如 the | Skyrim）\n"
    "- 禁止在形容词与名词之间切分（如 beautiful | river）\n"
    "- 禁止在介词与其宾语之间切分（如 in | Skyrim、from | the game）\n"
    "- 禁止在助动词与主动词之间切分（如 have | been、is | going）\n"
    "- 禁止在「的」与所修饰名词之间切分（中文）\n"
    "- 禁止把一个人名/地名/专有名词切到两条里\n"
    "- 禁止让任何一条字幕以孤立的介词/连词结尾（如 from、and、in、to、的）\n"
    "\n"
    "## 节奏约束\n"
    "- 目标时长 2-4 秒，最短 {min_duration_s} 秒，最长 {max_duration_s} 秒。\n"
    "- 可见字符速率不超过 {max_cps} 字/秒。\n"
    "- 单行不超过 42 个可见字符，超过则需换行或拆分。\n"
    "\n"
    "## 如何处理时长冲突\n"
    "- 如果一个语意单元在 2-4 秒内完整结束 → 直接作为一条，不要多切。\n"
    "- 如果一个语意单元短于 2 秒 → 必须与其后紧跟的语意单元合并，直到总时长接近 2-4 秒。\n"
    "- 如果一个语意单元超过 {max_duration_s} 秒 → 在最自然的从句/短语边界处切分（参考断句优先级），优先保证语义完整而非精确掐时间。\n"
    "- 排比、列举、并列短语（如 house, food, and job）→ 合并为一条，不要逐项拆散。\n"
    "\n"
    "## 技术约束\n"
    "- 每条字幕的开始时间 = 该条首个词的 start_s，结束时间 = 该条末个词的 end_s。严禁编造或修改时间戳。\n"
    "- 必须覆盖所有输入词，保持原顺序，禁止丢失、重复、改写、增删任何词。\n"
    "- 输出前自查：每条字幕单独拿出来读，是否像一句自然完整的话？如果不是，请调整边界。\n"
)

# 段级模式（降级）：输入仅有段级时间戳，AI 只能在段边界上拆分/合并，精度较低但仍优于纯规则。
_SMART_SEGMENT_SEGMENT_BUILTIN_BEHAVIOR = (
    "基于提供的段落级时间戳，重新切分为可直接上线的字幕条目。"
    "\n\n"
    "## 核心原则：每条字幕必须是一个自足的「呼吸单元」\n"
    "- 单看这一条字幕，观众就应该能理解一个完整的意思片段。\n"
    "- 绝不要把一句完整的话从中间劈开，让上半条不知所云。\n"
    "\n"
    "## 断句优先级\n"
    "1. 句末标点（. ! ?）之后 —— 最优先。\n"
    "2. 分句标点（; :）之后。\n"
    "3. 从句/并列连接词之前（and、but、or、that、which、where、when、if、because、so）。\n"
    "4. 逗号后有明显语意转折处。\n"
    "\n"
    "## 绝对禁止\n"
    "- 禁止让任何一条字幕以孤立的介词/连词结尾。\n"
    "- 禁止在人名/地名/专有名词中间切分。\n"
    "- 禁止在冠词-名词、形容词-名词、介词-宾语之间切分。\n"
    "\n"
    "## 节奏约束\n"
    "- 目标 2-4 秒，最短 {min_duration_s} 秒，最长 {max_duration_s} 秒。\n"
    "- 可见字符速率不超过 {max_cps} 字/秒。\n"
    "\n"
    "## 技术约束\n"
    "- 每条字幕的 start_s/end_s 必须精确落在某段的边界上（段首 start_s 或段尾 end_s）。\n"
    "- 必须覆盖所有输入段的文本，保持原顺序。\n"
    "- 可将一段拆为多条，或将相邻短段合并。\n"
    "- 输出前自查：每条字幕单独读是否自然完整？\n"
)

# 协议壳：严格 JSON 输出格式 + 时间单调递增 + 不越界约束
_SMART_SEGMENT_SHARED_RULES = (
    "输出严格 JSON，不要任何解释或多余文本。"
    "格式：{\"cues\":[{\"start_s\":数字,\"end_s\":数字,\"text\":\"该条所含词/段的原文拼接\"}]}。"
    "cues 必须按 start_s 升序排列，每条 end_s > start_s，相邻条目时间不得重叠。"
    "所有 start_s/end_s 必须精确取自输入数据，不得四舍五入或估算。"
)

# Agent 上下文指令：告知 AI 如何使用已确认的历史 cues 作为参考
_SMART_SEGMENT_CONTEXT_INSTRUCTIONS = (
    "\n## 上下文参考（已确认的历史字幕）\n"
    "- 输入数据中附带 `context_cues` 字段，包含前一批次已确认的字幕条目。\n"
    "- 这些是 **已定稿结果**，你 **不得修改、覆盖或重新分段** context_cues 中的任何条目。\n"
    "- context_cues 的作用：\n"
    "  1. **风格延续**：保持与前文一致的断句节奏和分段风格。\n"
    "  2. **语义接续**：如果前一批次末条字幕在语意上不完整（如半句话、排比列举未完），"
    "你应让当前批次的第一条字幕自然接续完成该语意单元。\n"
    "  3. **避免重复**：当前批次输出的时间戳不得与 context_cues 的时间范围重叠。\n"
    "- 当前批次的首个 start_s 应在 context_cues 末条 end_s 之后（或紧接）。\n"
)

# Agent 边界精炼 prompt：用于跨批次边界审视
_SMART_SEGMENT_BOUNDARY_REFINE_PROMPT = (
    "检查相邻两个批次之间的字幕分段边界，"
    "判断是否存在语义割裂——即一句完整的话被不恰当地切到了两个批次里。\n\n"
    "## 输入\n"
    "- `words`：边界区域内的所有词（带时间戳），来自两个批次的交界处。\n"
    "- `current_cues`：当前已分段的字幕条目（覆盖上述词的范围）。\n\n"
    "## 判断标准\n"
    "- 如果 current_cues 的最后一条在语意上是完整的（句末有标点、意思完整），"
    "则边界合理，直接原样返回 current_cues。\n"
    "- 如果最后一条以半句话、连词、介词等不完整的形式结束，"
    "且下一条明显是该句的延续，则需要调整边界：\n"
    "  - 找到最自然的断句点（参考句末标点 > 分句标点 > 从句连接词前）\n"
    "  - 在该点重新切分，使每条字幕都是自足的语意单元\n\n"
    "## 节奏约束\n"
    "- 目标时长 2-4 秒，最短 {min_duration_s} 秒，最长 {max_duration_s} 秒。\n"
    "- 可见字符速率不超过 {max_cps} 字/秒。\n\n"
    "## 技术约束\n"
    "- 仅输出需要调整的 cue，未调整的保持不变。\n"
    "- 每条 cue 的 start_s/end_s 必须精确取自 words 中的时间戳，不得编造。\n"
    "- 所有 words 必须被覆盖，不得丢失任何词。\n"
    "- 输出严格 JSON，不要任何解释。格式："
    "{\"cues\":[{\"start_s\":数字,\"end_s\":数字,\"text\":\"原文拼接\"}]}。\n"
)


# ---------------------------------------------------------------------------
# 注册 4 组 Prompt
# ---------------------------------------------------------------------------

_register_prompt(
    "SUBTITLE_TRANSLATE",
    label="字幕翻译主提示词",
    description="控制字幕批量翻译时的 system prompt。会影响翻译风格、术语策略等。",
    builtin_template=_SUBTITLE_ZH_BUILTIN_BEHAVIOR,
    variables=["target_language_name"],
    applies_to="subtitle",
)

_register_prompt(
    "SUBTITLE_TRANSLATE_STRICT",
    label="字幕翻译严格补救提示词",
    description="首轮翻译后若检测到大量漏译条目，会用此 Prompt 再补翻一次。",
    builtin_template=_SUBTITLE_STRICT_ZH_BUILTIN_BEHAVIOR,
    is_advanced=True,
    variables=["target_language_name"],
    applies_to="subtitle",
)

_register_prompt(
    "METADATA_TRANSLATE",
    label="标题/简介翻译主提示词",
    description="控制标题和简介翻译时的 system prompt。",
    builtin_template=_METADATA_BUILTIN_BEHAVIOR,
    variables=["target_language_name"],
    applies_to="metadata",
)

_register_prompt(
    "METADATA_DESC_RETRY",
    label="简介重试提示词",
    description="标题/简介首轮翻译后，若简介字段校验失败，会用此 Prompt 单独重试简介。",
    builtin_template=_DESC_RETRY_BUILTIN_BEHAVIOR,
    is_advanced=True,
    variables=["target_language_name"],
    applies_to="metadata",
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def get_prompt_ids() -> List[str]:
    """返回所有已注册的 Prompt ID（按注册顺序）。"""
    return list(_PROMPT_REGISTRY.keys())


def get_prompt_info(prompt_id: str) -> Optional[Dict[str, Any]]:
    """返回某个 Prompt 的元数据。"""
    return _PROMPT_REGISTRY.get(prompt_id)


def normalize_mode(value: Any) -> str:
    """标准化模式值，非法值回退 builtin。"""
    text = str(value or "").strip().lower()
    return text if text in VALID_MODES else MODE_BUILTIN


def normalize_text(value: Any, max_length: int = MAX_PROMPT_TEXT_LENGTH) -> str:
    """标准化 Prompt 文本：strip、换行规范化、长度截断。"""
    text = str(value or "")
    # 统一换行符
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()
    # 长度兜底
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length]
    return text


def _render_template(template: str, variables: Dict[str, Any]) -> str:
    """用简单占位符 {key} 渲染模板。缺少变量时保留原始占位符。"""
    result = template
    for key, value in (variables or {}).items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _target_language_name(target_language: str) -> str:
    """目标语言代码 → 自然语言名称。"""
    code = str(target_language or "").strip().lower()
    mapping = {
        "zh": "简体中文",
        "zh-cn": "简体中文",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어",
    }
    # 匹配前缀
    for prefix, name in mapping.items():
        if code.startswith(prefix):
            return name
    return code or "简体中文"


# ---------------------------------------------------------------------------
# 核心 API
# ---------------------------------------------------------------------------

def _resolve_builtin_template(prompt_id: str, target_language: str) -> str:
    """根据目标语言解析对应的内置模板。

    字幕翻译系列在非中文目标时需要使用带 {target_language_name} 占位符的模板，
    而不是硬编码中文的模板。
    """
    info = _PROMPT_REGISTRY.get(prompt_id)
    if not info:
        return ""
    target_lang = str(target_language or "zh").strip().lower()

    if prompt_id == "SUBTITLE_TRANSLATE":
        return _SUBTITLE_DEFAULT_BUILTIN_BEHAVIOR if target_lang != "zh" else _SUBTITLE_ZH_BUILTIN_BEHAVIOR
    if prompt_id == "SUBTITLE_TRANSLATE_STRICT":
        return _SUBTITLE_STRICT_DEFAULT_BUILTIN_BEHAVIOR if target_lang != "zh" else _SUBTITLE_STRICT_ZH_BUILTIN_BEHAVIOR

    return info["builtin_template"]


def get_final_system_prompt(
    prompt_id: str,
    *,
    mode: str = MODE_BUILTIN,
    user_text: str = "",
    target_language: str = "zh",
    extra_variables: Optional[Dict[str, Any]] = None,
) -> str:
    """
    获取最终 system prompt 的行为层文本。

    模式处理逻辑：
    - builtin:  直接使用内置模板
    - append:   内置模板 + 换行 + 用户文本
    - override: 用户文本替换内置模板
    - 任何异常/空值回退 builtin

    注意：此处只返回 **行为层**，不包含协议壳。
    调用方需要自行拼装协议壳 + 行为层 + JSON 后缀。
    """
    info = _PROMPT_REGISTRY.get(prompt_id)
    if not info:
        logger.warning("未知 Prompt ID: %s，回退空字符串", prompt_id)
        return ""

    # 构建渲染变量
    render_vars = {"target_language_name": _target_language_name(target_language)}
    if extra_variables:
        render_vars.update(extra_variables)

    mode = normalize_mode(mode)
    builtin_template = _resolve_builtin_template(prompt_id, target_language)
    builtin_rendered = _render_template(builtin_template, render_vars)

    if mode == MODE_BUILTIN:
        return builtin_rendered

    user_text = _render_template(normalize_text(user_text), render_vars)
    if not user_text:
        # 用户文本为空，无论 append 还是 override 都回退 builtin
        logger.info("Prompt %s 用户文本为空，回退 builtin", prompt_id)
        return builtin_rendered

    if mode == MODE_APPEND:
        return builtin_rendered + "\n" + user_text

    # MODE_OVERRIDE
    return user_text


# ---------------------------------------------------------------------------
# 字幕翻译专用 API（封装协议壳）
# ---------------------------------------------------------------------------

def get_subtitle_system_prompt(
    *,
    mode: str = MODE_BUILTIN,
    user_text: str = "",
    target_language: str = "zh",
) -> str:
    """获取字幕翻译最终 system prompt（含协议壳和 JSON 后缀）。"""
    behavior = get_final_system_prompt(
        "SUBTITLE_TRANSLATE",
        mode=mode,
        user_text=user_text,
        target_language=target_language,
    )
    return f"{behavior}{_SUBTITLE_SHARED_RULES}{_SUBTITLE_JSON_SUFFIX}"


def get_subtitle_strict_system_prompt(
    *,
    mode: str = MODE_BUILTIN,
    user_text: str = "",
    target_language: str = "zh",
) -> str:
    """获取字幕翻译严格补救最终 system prompt（含协议壳和 JSON 后缀）。"""
    behavior = get_final_system_prompt(
        "SUBTITLE_TRANSLATE_STRICT",
        mode=mode,
        user_text=user_text,
        target_language=target_language,
    )
    return f"{behavior}{_SUBTITLE_STRICT_SHARED_RULES}{_SUBTITLE_JSON_SUFFIX}"


# ---------------------------------------------------------------------------
# 元数据翻译专用 API（封装协议壳）
# ---------------------------------------------------------------------------

def get_metadata_translate_prompt(
    *,
    mode: str = MODE_BUILTIN,
    user_text: str = "",
    target_language: str = "zh",
    retry: bool = False,
) -> str:
    """获取元数据翻译最终 system prompt（含 JSON 后缀）。

    retry=True 时追加重试说明。
    """
    behavior = get_final_system_prompt(
        "METADATA_TRANSLATE",
        mode=mode,
        user_text=user_text,
        target_language=target_language,
    )
    suffix = _METADATA_JSON_SUFFIX
    if retry:
        suffix = "仅重写本次输入中提供的失败字段；无法安全输出时返回空字符串。" + suffix
    return behavior + suffix


def get_metadata_desc_retry_prompt(
    *,
    mode: str = MODE_BUILTIN,
    user_text: str = "",
    target_language: str = "zh",
) -> str:
    """获取简介重试最终 system prompt（含 JSON 后缀）。"""
    behavior = get_final_system_prompt(
        "METADATA_DESC_RETRY",
        mode=mode,
        user_text=user_text,
        target_language=target_language,
    )
    return behavior + _METADATA_DESC_RETRY_JSON_SUFFIX


# ---------------------------------------------------------------------------
# AI 智能分段专用 API（非翻译型，独立于 Prompt 中心用户覆盖）
# ---------------------------------------------------------------------------

def get_smart_segment_system_prompt(
    *,
    has_word_timestamps: bool,
    min_duration_s: float = 0.8,
    max_duration_s: float = 7.0,
    max_cps: float = 18.0,
    has_context: bool = False,
) -> str:
    """获取 AI 智能分段最终 system prompt（含协议壳与 JSON 后缀）。

    has_word_timestamps=True 使用字级模式（精度高），False 使用段级降级模式。
    节奏阈值会渲染进行为层，指导模型遵守最短/最长时长与字符速率上限。
    has_context=True 时注入上下文指令，告知 AI 如何使用已确认的历史 cues。
    """
    template = (
        _SMART_SEGMENT_WORD_BUILTIN_BEHAVIOR
        if has_word_timestamps
        else _SMART_SEGMENT_SEGMENT_BUILTIN_BEHAVIOR
    )
    behavior = _render_template(
        template,
        {
            "min_duration_s": f"{float(min_duration_s):.2f}",
            "max_duration_s": f"{float(max_duration_s):.2f}",
            "max_cps": f"{float(max_cps):.1f}",
        },
    )
    context_block = _SMART_SEGMENT_CONTEXT_INSTRUCTIONS if has_context else ''
    return behavior + context_block + _SMART_SEGMENT_SHARED_RULES


def get_boundary_refine_system_prompt(
    *,
    min_duration_s: float = 0.8,
    max_duration_s: float = 7.0,
    max_cps: float = 18.0,
) -> str:
    """获取边界精炼 system prompt，用于跨批次边界审视。"""
    return _render_template(
        _SMART_SEGMENT_BOUNDARY_REFINE_PROMPT,
        {
            "min_duration_s": f"{float(min_duration_s):.2f}",
            "max_duration_s": f"{float(max_duration_s):.2f}",
            "max_cps": f"{float(max_cps):.1f}",
        },
    )


# ---------------------------------------------------------------------------
# 配置键映射辅助
# ---------------------------------------------------------------------------

def config_key_for_mode(prompt_id: str) -> str:
    """Prompt ID → 配置文件中的模式键名。"""
    return f"{prompt_id}_MODE"


def config_key_for_text(prompt_id: str) -> str:
    """Prompt ID → 配置文件中的文本键名。"""
    return f"{prompt_id}_TEXT"


def get_default_config_entries() -> Dict[str, Any]:
    """返回所有 Prompt 中心在 DEFAULT_CONFIG 中需要注册的键值对。"""
    entries: Dict[str, Any] = {}
    for prompt_id in _PROMPT_REGISTRY:
        entries[config_key_for_mode(prompt_id)] = MODE_BUILTIN
        entries[config_key_for_text(prompt_id)] = ""
    return entries


def read_prompt_config_from_app_config(app_config: Dict[str, Any], prompt_id: str) -> tuple:
    """
    从 app_config 中读取某个 Prompt 的 (mode, text)。

    Returns:
        (mode, text) 标准化后的元组。
    """
    mode = normalize_mode(app_config.get(config_key_for_mode(prompt_id), MODE_BUILTIN))
    text = normalize_text(app_config.get(config_key_for_text(prompt_id), ""))
    return mode, text


# ---------------------------------------------------------------------------
# 内置 Prompt 预览（供设置页展示）
# ---------------------------------------------------------------------------

def get_builtin_prompt_previews() -> Dict[str, Dict[str, str]]:
    """返回所有 Prompt 的内置模板预览，供设置页面展示。

    Returns:
        dict: {prompt_id: {"label": str, "description": str, "builtin_text": str}}
    """
    previews: Dict[str, Dict[str, str]] = {}
    # 使用默认目标语言（简体中文）渲染变量占位符
    render_vars = {"target_language_name": "简体中文"}
    for prompt_id, info in _PROMPT_REGISTRY.items():
        rendered = _render_template(
            _resolve_builtin_template(prompt_id, "zh"),
            render_vars,
        )
        # 拼接协议壳和 JSON 后缀，展示最终完整 system prompt
        full_prompt = rendered
        if prompt_id == "SUBTITLE_TRANSLATE":
            full_prompt = rendered + _SUBTITLE_SHARED_RULES + _SUBTITLE_JSON_SUFFIX
        elif prompt_id == "SUBTITLE_TRANSLATE_STRICT":
            full_prompt = rendered + _SUBTITLE_STRICT_SHARED_RULES + _SUBTITLE_JSON_SUFFIX
        elif prompt_id == "METADATA_TRANSLATE":
            full_prompt = rendered + _METADATA_JSON_SUFFIX
        elif prompt_id == "METADATA_DESC_RETRY":
            full_prompt = rendered + _METADATA_DESC_RETRY_JSON_SUFFIX

        previews[prompt_id] = {
            "label": info["label"],
            "description": info["description"],
            "builtin_text": full_prompt,
        }
    return previews
