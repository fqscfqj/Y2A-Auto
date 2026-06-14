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
