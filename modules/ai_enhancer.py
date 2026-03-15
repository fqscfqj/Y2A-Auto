#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import logging
import re
import time
import json
import base64
import traceback
from typing import Any, Dict, List, Optional, Sequence
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from .utils import (
    get_app_subdir,
    safe_str,
    openai_chat_create_with_thinking_control,
    extract_chat_message_json,
)

import openai

# Pre-compiled regex patterns for _pre_clean (performance optimization)
_URL_PATTERNS = [
    re.compile(r'https?://[^\s\u4e00-\u9fff]+', re.IGNORECASE),
    re.compile(r'www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE),
    re.compile(r'ftp://[^\s\u4e00-\u9fff]+', re.IGNORECASE),
    re.compile(r'[a-zA-Z0-9.-]+\.(com|org|net|io|me|tv|cn|co|uk)(?:[/\s]|$)', re.IGNORECASE),
    re.compile(r'\b[a-zA-Z0-9]+\.[a-zA-Z0-9]+/[a-zA-Z0-9_-]+\b', re.IGNORECASE),
]
_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
_SOCIAL_HANDLE_RE = re.compile(r'@[A-Za-z0-9_]+')
_HASHTAG_RE = re.compile(r'#[A-Za-z0-9_]+')
_SPONSOR_URL_PATTERNS = [
    re.compile(r'patreon\.com/[^\s]*', re.IGNORECASE),
    re.compile(r'ko-fi\.com/[^\s]*', re.IGNORECASE),
    re.compile(r'buymeacoffee\.com/[^\s]*', re.IGNORECASE),
]
_CTA_PATTERNS = [
    re.compile(r'link\s+in\s+[the\s]*description', re.IGNORECASE),
    re.compile(r'links?\s+[in\s]*[the\s]*bio', re.IGNORECASE),
    re.compile(r'check\s+[the\s]*description\s+for', re.IGNORECASE),
    re.compile(r'visit\s+[our\s]*website\s+at', re.IGNORECASE),
    re.compile(r'more\s+info\s+at\s+[^\s]+', re.IGNORECASE),
    re.compile(r'download\s+link[:\s]+[^\s]+', re.IGNORECASE),
]
_WHITESPACE_RE = re.compile(r'[ \t\f\v]+')
_TRAILING_SPACE_RE = re.compile(r'[ \t]+\n')
_MULTIPLE_NEWLINES_RE = re.compile(r'\n{3,}')
_LIST_LINE_RE = re.compile(r'^\s*(?:[-*•►]|\d+[.)])\s+', re.IGNORECASE | re.MULTILINE)
_NON_TEXT_RE = re.compile(r'[^\w\u4e00-\u9fff]+', re.IGNORECASE)
_MEANINGFUL_TEXT_RE = re.compile(r'[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+')

_PROMO_LINE_PATTERNS = [
    re.compile(r'^\s*video playlists?\s*:?', re.IGNORECASE),
    re.compile(r'^\s*all playlists?\s*:?', re.IGNORECASE),
    re.compile(r'^\s*website\s*:?', re.IGNORECASE),
    re.compile(r'^\s*official\s+site\s*:?', re.IGNORECASE),
    re.compile(r'^\s*(listen|watch)\s+to\s+', re.IGNORECASE),
    re.compile(r'^\s*(patreon|spotify|itunes|apple music|cdbaby)\b', re.IGNORECASE),
    re.compile(r'^\s*(follow|subscribe|like|share|download|buy)\b', re.IGNORECASE),
    re.compile(r'^\s*(播放列表|更多内容|关注|订阅|点赞|分享|评论区|下载链接|购买链接|联系方式)\s*[:：]?', re.IGNORECASE),
]
_PROMO_BLOCK_PATTERNS = [
    re.compile(r'\bvideo\s*playlists?\b', re.IGNORECASE),
    re.compile(r'\ball\s*playlists?\b', re.IGNORECASE),
    re.compile(r'\blisten\s+to\b.*\boutside\b', re.IGNORECASE),
    re.compile(r'\b(link\s+in|links?\s+in|follow|subscribe|visit|website|patreon|download|buy)\b', re.IGNORECASE),
    re.compile(r'(播放列表|站外|关注|订阅|点赞|分享|联系方式|社交媒体|外部平台)', re.IGNORECASE),
]
_EXTERNAL_PLATFORM_PATTERNS = [
    re.compile(
        r'\b('
        r'youtube|spotify|itunes|apple\s*music|patreon|cdbaby|soundcloud|bandcamp|'
        r'twitter|instagram|facebook|tiktok|discord|telegram|ko-?fi|buymeacoffee'
        r')\b',
        re.IGNORECASE
    ),
    re.compile(r'(油管|推特|脸书|外部平台|社交平台|官网|官方网站|个人网站|独立站)', re.IGNORECASE),
]
_PROMO_SIGNAL_PATTERNS = [
    re.compile(r'►'),
    re.compile(r'\b(playlists?|follow|subscribe|link\s+in|website|patreon|download|buy)\b', re.IGNORECASE),
    re.compile(r'(播放列表|关注|订阅|点赞|分享|链接在|站外|外部平台|联系方式)', re.IGNORECASE),
]

# Pre-compiled patterns for post-translation cleanup in translate_text
_TRANSLATION_COMMENT_PATTERNS = [
    re.compile(r'（注：.*?）', re.IGNORECASE),
    re.compile(r'\(注：.*?\)', re.IGNORECASE),
    re.compile(r'【注：.*?】', re.IGNORECASE),
    re.compile(r'（.*?已移除）', re.IGNORECASE),
    re.compile(r'\(.*?已移除\)', re.IGNORECASE),
    re.compile(r'（.*?联系方式.*?）', re.IGNORECASE),
    re.compile(r'\(.*?联系方式.*?\)', re.IGNORECASE),
    re.compile(r'（.*?社交媒体.*?）', re.IGNORECASE),
    re.compile(r'\(.*?社交媒体.*?\)', re.IGNORECASE),
    re.compile(r'（.*?标签.*?）', re.IGNORECASE),
    re.compile(r'\(.*?标签.*?\)', re.IGNORECASE),
    re.compile(r'（.*?链接.*?）', re.IGNORECASE),
    re.compile(r'\(.*?链接.*?\)', re.IGNORECASE),
    re.compile(r'（.*?推广.*?）', re.IGNORECASE),
    re.compile(r'\(.*?推广.*?\)', re.IGNORECASE),
    re.compile(r'（.*?广告.*?）', re.IGNORECASE),
    re.compile(r'\(.*?广告.*?\)', re.IGNORECASE),
    re.compile(r'（.*?removed.*?）', re.IGNORECASE),
    re.compile(r'\(.*?removed.*?\)', re.IGNORECASE),
    re.compile(r'（.*?filtered.*?）', re.IGNORECASE),
    re.compile(r'\(.*?filtered.*?\)', re.IGNORECASE),
]
_INTERACTION_PATTERNS = [
    re.compile(r'订阅[我们的]*[频道]*'),
    re.compile(r'关注[我们]*'),
    re.compile(r'点赞[这个]*[视频]*'),
    re.compile(r'分享[给]*[朋友们]*'),
    re.compile(r'评论[区]*[见]*'),
    re.compile(r'更多[内容]*请访问'),
    re.compile(r'详情见[链接]*'),
    re.compile(r'链接在[描述]*[中]*'),
    re.compile(r'访问[我们的]*[网站]*'),
    re.compile(r'查看[完整]*[版本]*'),
    re.compile(r'下载[链接]*'),
    re.compile(r'购买[链接]*'),
    re.compile(r'subscribe\s+to\s+[our\s]*channel', re.IGNORECASE),
    re.compile(r'follow\s+[us\s]*', re.IGNORECASE),
    re.compile(r'like\s+[this\s]*video', re.IGNORECASE),
    re.compile(r'share\s+[with\s]*[friends\s]*', re.IGNORECASE),
    re.compile(r'check\s+out\s+[our\s]*[websit\s]*', re.IGNORECASE),
    re.compile(r'visit\s+[our\s]*[site\s]*', re.IGNORECASE),
    re.compile(r'download\s+[link\s]*', re.IGNORECASE),
    re.compile(r'buy\s+[link\s]*', re.IGNORECASE),
    re.compile(r'more\s+info\s+at', re.IGNORECASE),
    re.compile(r'see\s+[full\s]*[version\s]*', re.IGNORECASE),
]

# --- Helpers: logger/client/cleaner (restored) ---
def setup_task_logger(task_id):
    """
    为特定任务设置日志记录器。
    """
    log_dir = get_app_subdir('logs')
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f'task_{task_id}.log')
    logger = logging.getLogger(f'ai_enhancer_{task_id}')

    if not logger.handlers:
        logger.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        logger.propagate = False

    return logger

def get_openai_client(openai_config):
    """
    创建OpenAI客户端。
    """
    api_key = openai_config.get('OPENAI_API_KEY', '')
    options = {}
    if openai_config.get('OPENAI_BASE_URL'):
        options['base_url'] = openai_config.get('OPENAI_BASE_URL')
    return openai.OpenAI(api_key=api_key, **options)

def _normalize_whitespace(text: str) -> str:
    if not text:
        return ''
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = _WHITESPACE_RE.sub(' ', text)
    text = _TRAILING_SPACE_RE.sub('\n', text)
    text = _MULTIPLE_NEWLINES_RE.sub('\n\n', text)
    return text.strip()

def _strip_external_platforms(text: str) -> str:
    if not text:
        return ''
    cleaned = text
    for pat in _EXTERNAL_PLATFORM_PATTERNS:
        cleaned = pat.sub('', cleaned)
    return cleaned

def _split_blocks(text: str) -> list:
    if not text:
        return []
    return [b.strip() for b in re.split(r'\n\s*\n+', text) if b and b.strip()]

def _cleanup_list_prefix(line: str) -> str:
    return _LIST_LINE_RE.sub('', line or '').strip()

def _looks_like_promo_line(line: str) -> bool:
    if not line:
        return False
    compact = line.strip()
    if not compact:
        return False
    if '►' in compact:
        return True
    if _URL_PATTERNS[0].search(compact) or _URL_PATTERNS[1].search(compact):
        return True
    for pat in _PROMO_LINE_PATTERNS:
        if pat.search(compact):
            return True
    for pat in _CTA_PATTERNS:
        if pat.search(compact):
            return True
    return False

def _looks_like_promo_block(block: str) -> bool:
    if not block:
        return False
    for pat in _PROMO_BLOCK_PATTERNS:
        if pat.search(block):
            return True
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if len(lines) >= 2:
        promo_like = sum(1 for ln in lines if _looks_like_promo_line(ln))
        if promo_like >= max(2, len(lines) // 2):
            return True
    return False

def _compress_description_blocks(text: str, max_blocks: Optional[int] = 2) -> str:
    blocks = []
    for block in _split_blocks(text):
        if _looks_like_promo_block(block):
            continue
        clean_lines = []
        for line in block.splitlines():
            normalized = _cleanup_list_prefix(_normalize_whitespace(line))
            if not normalized:
                continue
            if _looks_like_promo_line(normalized):
                continue
            clean_lines.append(normalized)
        if clean_lines:
            # 将块内列表折叠成自然段
            blocks.append(' '.join(clean_lines))
    if not blocks:
        lines = []
        for line in _normalize_whitespace(text).split('\n'):
            normalized = _cleanup_list_prefix(line)
            if normalized and not _looks_like_promo_line(normalized):
                lines.append(normalized)
        if lines:
            blocks = [' '.join(lines)]
    if max_blocks is not None:
        blocks = blocks[:max(0, int(max_blocks))]
    return '\n\n'.join(blocks).strip()

def _pre_clean(text: str, content_type: str = "description", max_blocks: Optional[int] = 2) -> str:
    """在发送给模型前做确定性去噪：移除导流信息，并将描述压缩成自然段。"""
    if not text:
        return text

    ct_lower = str(content_type).lower().strip()
    cleaned = text

    for pat in _URL_PATTERNS:
        cleaned = pat.sub('', cleaned)
    cleaned = _EMAIL_RE.sub('', cleaned)
    cleaned = _SOCIAL_HANDLE_RE.sub('', cleaned)
    cleaned = _HASHTAG_RE.sub('', cleaned)
    for pat in _SPONSOR_URL_PATTERNS:
        cleaned = pat.sub('', cleaned)
    for pat in _CTA_PATTERNS:
        cleaned = pat.sub('', cleaned)
    cleaned = _strip_external_platforms(cleaned)
    cleaned = _normalize_whitespace(cleaned)

    if ct_lower == 'title':
        # 标题不需要多段结构，压缩为单行
        title = _cleanup_list_prefix(cleaned.replace('\n', ' '))
        title = _normalize_whitespace(title).replace('\n', ' ')
        return title.strip()

    return _compress_description_blocks(cleaned, max_blocks=max_blocks)


def _has_meaningful_content(text: str, content_type: str = "description") -> bool:
    cleaned = safe_str(text).strip()
    if not cleaned:
        return False
    tokens = _MEANINGFUL_TEXT_RE.findall(cleaned)
    if not tokens:
        return False
    if str(content_type).lower().strip() == "title":
        return True
    total_chars = sum(len(token) for token in tokens)
    return total_chars > 3 or len(tokens) > 1

_LANGUAGE_NAME_MAP = {
    "zh": "简体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
}
_DESCRIPTION_ONLY_RETRY_REASONS = frozenset({"empty_output", "description_not_natural"})


def _normalize_target_language(target_language: str) -> str:
    value = safe_str(target_language).strip().lower()
    for prefix in ("zh", "en", "ja", "ko"):
        if value.startswith(prefix):
            return prefix
    return value or "zh"


def _target_language_name(target_language: str) -> str:
    normalized = _normalize_target_language(target_language)
    return _LANGUAGE_NAME_MAP.get(normalized, safe_str(target_language).strip() or "简体中文")

def _post_clean(text: str, content_type: str = "description", max_blocks: Optional[int] = 2) -> str:
    if not text:
        return ''

    ct_lower = str(content_type).lower().strip()
    cleaned = text

    for prefix in ["翻译：", "译文：", "这是翻译：", "以下是译文：", "以下是我的翻译："]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()

    for pattern in _TRANSLATION_COMMENT_PATTERNS:
        cleaned = pattern.sub('', cleaned)
    for pattern in _URL_PATTERNS:
        cleaned = pattern.sub('', cleaned)
    cleaned = _EMAIL_RE.sub('', cleaned)
    cleaned = _SOCIAL_HANDLE_RE.sub('', cleaned)
    cleaned = _HASHTAG_RE.sub('', cleaned)
    for pattern in _INTERACTION_PATTERNS:
        cleaned = pattern.sub('', cleaned)
    cleaned = _strip_external_platforms(cleaned)
    cleaned = _normalize_whitespace(cleaned)

    if ct_lower == 'title':
        cleaned = _cleanup_list_prefix(cleaned.replace('\n', ' '))
        cleaned = _normalize_whitespace(cleaned).replace('\n', ' ')
        return cleaned.strip()

    cleaned = _compress_description_blocks(cleaned, max_blocks=max_blocks)
    return _normalize_whitespace(cleaned)

def _normalize_for_similarity(text: str) -> str:
    if not text:
        return ''
    normalized = _NON_TEXT_RE.sub('', text.lower())
    return normalized.strip()

def _contains_promo_signal(text: str) -> bool:
    if not text:
        return False
    if _EMAIL_RE.search(text) or _SOCIAL_HANDLE_RE.search(text) or _HASHTAG_RE.search(text):
        return True
    for pat in _URL_PATTERNS[:3]:
        if pat.search(text):
            return True
    for pat in _PROMO_SIGNAL_PATTERNS:
        if pat.search(text):
            return True
    for pat in _EXTERNAL_PLATFORM_PATTERNS:
        if pat.search(text):
            return True
    return False

def _is_natural_description(text: str, max_blocks: Optional[int] = 2) -> bool:
    blocks = _split_blocks(text)
    if not blocks:
        return False
    if max_blocks is not None and len(blocks) > max_blocks:
        return False
    if _LIST_LINE_RE.search(text):
        return False
    if '►' in text:
        return False
    if any(len(block.strip()) < 6 for block in blocks):
        return False
    return True

def _validate_output(
    source_clean: str,
    output_text: str,
    content_type: str = "description",
    *,
    description_max_blocks: Optional[int] = 2,
):
    reasons = []
    ct_lower = str(content_type).lower().strip()
    out = (output_text or '').strip()
    src = (source_clean or '').strip()

    if not out:
        reasons.append('empty_output')
    if _contains_promo_signal(out):
        reasons.append('contains_promo_signal')

    src_norm = _normalize_for_similarity(src)
    out_norm = _normalize_for_similarity(out)
    if src_norm and out_norm:
        if len(src_norm) >= 12 and len(out_norm) >= 8:
            ratio = SequenceMatcher(None, src_norm, out_norm).ratio()
            if ratio >= 0.90:
                reasons.append(f'too_similar:{ratio:.2f}')
        elif src_norm == out_norm and len(src_norm) >= 6:
            reasons.append('identical_to_source')

    if ct_lower != 'title' and out and not _is_natural_description(out, max_blocks=description_max_blocks):
        reasons.append('description_not_natural')

    return len(reasons) == 0, reasons

def _apply_output_limits(text: str, content_type: str = "description", logger=None) -> str:
    limited = text or ''
    ct_lower = str(content_type).lower().strip()
    if ct_lower == 'title' and len(limited) > 50:
        if logger:
            logger.info(f"标题超过AcFun限制(50字符)，将被截断: {len(limited)} -> 50")
        limited = limited[:50]
    if ct_lower != 'title' and len(limited) > 1000:
        if logger:
            logger.info(f"描述超过AcFun限制(1000字符)，将被截断: {len(limited)} -> 1000")
        limited = limited[:997] + "..."
    return limited

def _build_fallback_text(source_clean: str, content_type: str, logger=None, max_blocks: Optional[int] = 2) -> str:
    if not source_clean:
        return ''
    fallback = _post_clean(source_clean, content_type=content_type, max_blocks=max_blocks)
    return _apply_output_limits(fallback, content_type=content_type, logger=logger)


def _build_metadata_translation_system_prompt(target_language: str, retry: bool = False) -> str:
    target_name = _target_language_name(target_language)
    prompt = (
        f"你是视频标题和简介翻译器。将输入字段改写为{target_name}。"
        "只允许重述原文事实，删除导流、社媒、外链、联系方式和互动引导。"
        "title 必须是自然单行标题；description 必须是自然简介，可多段，但不能写成列表、备注或说明。"
        "禁止补充新事实、解释或备注。"
        '只返回 JSON：{"title":"","description":""}。'
    )
    if retry:
        prompt += "仅重写本次输入中提供的失败字段；无法安全输出时返回空字符串。"
    return prompt


def _build_description_retry_system_prompt(target_language: str) -> str:
    target_name = _target_language_name(target_language)
    return (
        f"你是视频简介翻译器。将 description 翻译并改写为{target_name}自然简介。"
        "只允许重述原文事实，删除导流、社媒、外链、联系方式和互动引导。"
        "description 可以多段，不限制段落数，但不能输出列表、备注、解释或额外说明。"
        '只返回 JSON：{"description":""}。'
    )


def _build_metadata_translation_payload(
    title: str,
    description: str,
    target_language: str,
    translate_title: bool = True,
    translate_description: bool = True,
) -> Dict[str, str]:
    payload: Dict[str, str] = {"target_language": safe_str(target_language).strip() or "zh-CN"}
    if translate_title and title:
        payload["title"] = title
    if translate_description and description:
        payload["description"] = description
    return payload


def _request_json_object(
    client,
    model_name: str,
    system_prompt: str,
    payload: Dict[str, Any],
    *,
    max_tokens: int,
    temperature: float,
    thinking_enabled: bool,
    logger_obj,
    scene_name: str,
    user_content=None,
) -> Optional[Dict[str, Any]]:
    user_message_content = user_content
    if user_message_content is None:
        user_message_content = json.dumps(payload, ensure_ascii=False)
    create_kwargs = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    response = openai_chat_create_with_thinking_control(
        client=client,
        create_kwargs=create_kwargs,
        thinking_enabled=thinking_enabled,
        logger=logger_obj,
        scene_name=scene_name,
    )
    if not getattr(response, "choices", None):
        return None
    parsed = extract_chat_message_json(response.choices[0].message, expected_type=dict)
    if isinstance(parsed, dict):
        return parsed
    return None


def _sanitize_metadata_field(
    value: Any,
    content_type: str,
    logger=None,
    max_blocks: Optional[int] = 2,
) -> str:
    cleaned = _post_clean(safe_str(value), content_type=content_type, max_blocks=max_blocks)
    return _apply_output_limits(cleaned, content_type=content_type, logger=logger)


def _estimate_metadata_max_tokens(field_names: Sequence[str]) -> int:
    total = 0
    field_set = set(field_names)
    if "title" in field_set:
        total += 160
    if "description" in field_set:
        total += 900
    return max(total, 160)


def _collect_invalid_metadata_fields(
    cleaned_sources: Dict[str, str],
    outputs: Dict[str, str],
    *,
    description_max_blocks: Optional[int] = 2,
) -> Dict[str, List[str]]:
    invalid_fields: Dict[str, List[str]] = {}
    for field_name in ("title", "description"):
        source_clean = cleaned_sources.get(field_name, '')
        output_text = outputs.get(field_name, '')
        if not source_clean:
            if output_text:
                invalid_fields[field_name] = ["unexpected_output"]
            continue
        is_valid, reasons = _validate_output(
            source_clean,
            output_text,
            content_type=field_name,
            description_max_blocks=description_max_blocks,
        )
        if not is_valid:
            invalid_fields[field_name] = reasons
    return invalid_fields


def _count_description_blocks(text: str) -> int:
    return len(_split_blocks(text))


def _should_use_description_only_retry(reasons: Sequence[str]) -> bool:
    return bool(_DESCRIPTION_ONLY_RETRY_REASONS.intersection(reasons or ()))


def _log_description_field_state(logger, phase: str, raw_value: Any, sanitized_value: str) -> None:
    raw_text = safe_str(raw_value).strip()
    sanitized_text = safe_str(sanitized_value).strip()
    if not raw_text:
        logger.warning(f"{phase} description 模型输出为空")
    elif not sanitized_text:
        logger.warning(f"{phase} description 模型有输出，但后处理后为空")


def _request_translated_metadata_fields(
    client,
    model_name: str,
    system_prompt: str,
    payload: Dict[str, Any],
    *,
    max_tokens: int,
    thinking_enabled: bool,
    logger,
    scene_name: str,
    description_max_blocks: Optional[int] = 2,
    description_log_phase: Optional[str] = None,
) -> Dict[str, str]:
    parsed = _request_json_object(
        client=client,
        model_name=model_name,
        system_prompt=system_prompt,
        payload=payload,
        max_tokens=max_tokens,
        temperature=0.2,
        thinking_enabled=thinking_enabled,
        logger_obj=logger,
        scene_name=scene_name,
    )
    raw_title = (parsed or {}).get("title", '')
    raw_description = (parsed or {}).get("description", '')
    translated_fields = {
        "title": _sanitize_metadata_field(raw_title, "title", logger=logger),
        "description": _sanitize_metadata_field(
            raw_description,
            "description",
            logger=logger,
            max_blocks=description_max_blocks,
        ),
    }
    if description_log_phase and "description" in payload:
        _log_description_field_state(
            logger,
            description_log_phase,
            raw_description,
            translated_fields["description"],
        )
    return translated_fields


def translate_video_metadata(
    title,
    description,
    target_language="zh-CN",
    openai_config=None,
    task_id=None,
    translate_title: bool = True,
    translate_description: bool = True,
):
    """一次请求翻译视频标题和简介，返回结构化 JSON 字段。"""
    logger = setup_task_logger(task_id or "unknown")
    raw_title = safe_str(title)
    raw_description = safe_str(description)

    logger.info(f"开始翻译视频元数据，目标语言: {target_language}")
    logger.info(f"原标题 (截取前100字符): {raw_title[:100]}...")
    logger.info(f"原简介长度: {len(raw_description)} 字符")

    cleaned_title = _pre_clean(raw_title, content_type="title") if translate_title and raw_title else ''
    cleaned_description = (
        _pre_clean(raw_description, content_type="description", max_blocks=None)
        if translate_description and raw_description
        else ''
    )
    if cleaned_title and not _has_meaningful_content(cleaned_title, content_type="title"):
        cleaned_title = ''
    if cleaned_description and not _has_meaningful_content(cleaned_description, content_type="description"):
        cleaned_description = ''

    if translate_description and raw_description and not cleaned_description:
        logger.info("简介预清洗后无有效内容，直接留空")
    elif cleaned_description:
        logger.info(
            f"简介预清洗后长度: {len(cleaned_description)} 字符，段落数: {_count_description_blocks(cleaned_description)}"
        )
    if (translate_title and raw_title and cleaned_title != raw_title) or (
        translate_description and raw_description and cleaned_description != raw_description
    ):
        logger.info("已在提示阶段前执行结构化预清洗（去导流/站外信息/列表化噪声）")

    title_fallback = _build_fallback_text(cleaned_title, "title", logger=logger) if translate_title else ''
    final_result = {
        "title": title_fallback if translate_title else '',
        "description": '',
    }

    payload = _build_metadata_translation_payload(
        cleaned_title,
        cleaned_description,
        target_language=target_language,
        translate_title=translate_title,
        translate_description=translate_description and bool(cleaned_description),
    )
    requested_fields = [name for name in ("title", "description") if name in payload]
    if not requested_fields:
        logger.info("没有可发送给模型的有效元数据字段，直接返回清洗结果")
        return final_result

    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.warning("缺少OpenAI配置或API密钥，直接回退到清洗结果")
        return final_result

    try:
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        thinking_enabled = openai_config.get('OPENAI_THINKING_ENABLED', False)

        start_time = time.time()
        translated_fields = _request_translated_metadata_fields(
            client=client,
            model_name=model_name,
            system_prompt=_build_metadata_translation_system_prompt(target_language, retry=False),
            payload=payload,
            max_tokens=_estimate_metadata_max_tokens(requested_fields),
            thinking_enabled=thinking_enabled,
            scene_name='ai_enhancer_metadata_translate',
            logger=logger,
            description_max_blocks=None,
            description_log_phase="首轮",
        )
        cleaned_sources = {
            "title": cleaned_title if "title" in payload else '',
            "description": cleaned_description if "description" in payload else '',
        }
        invalid_fields = _collect_invalid_metadata_fields(
            cleaned_sources,
            translated_fields,
            description_max_blocks=None,
        )

        if invalid_fields:
            logger.info(f"元数据首轮输出未通过校验，失败字段: {invalid_fields}")

            if "title" in invalid_fields:
                retry_payload = _build_metadata_translation_payload(
                    cleaned_title,
                    '',
                    target_language=target_language,
                    translate_title=True,
                    translate_description=False,
                )
                retry_fields = _request_translated_metadata_fields(
                    client=client,
                    model_name=model_name,
                    system_prompt=_build_metadata_translation_system_prompt(target_language, retry=True),
                    payload=retry_payload,
                    max_tokens=_estimate_metadata_max_tokens(["title"]),
                    thinking_enabled=thinking_enabled,
                    scene_name='ai_enhancer_metadata_translate_title_retry',
                    logger=logger,
                    description_max_blocks=None,
                )
                translated_fields["title"] = retry_fields["title"]

            if "description" in invalid_fields:
                description_retry_prompt = _build_metadata_translation_system_prompt(target_language, retry=True)
                description_retry_scene = 'ai_enhancer_metadata_translate_retry'
                if _should_use_description_only_retry(invalid_fields["description"]):
                    logger.info(
                        f"description 字段触发定向重试，失败原因: {invalid_fields['description']}"
                    )
                    description_retry_prompt = _build_description_retry_system_prompt(target_language)
                    description_retry_scene = 'ai_enhancer_metadata_translate_description_retry'

                description_retry_payload = _build_metadata_translation_payload(
                    '',
                    cleaned_description,
                    target_language=target_language,
                    translate_title=False,
                    translate_description=True,
                )
                retry_fields = _request_translated_metadata_fields(
                    client=client,
                    model_name=model_name,
                    system_prompt=description_retry_prompt,
                    payload=description_retry_payload,
                    max_tokens=_estimate_metadata_max_tokens(["description"]),
                    thinking_enabled=thinking_enabled,
                    scene_name=description_retry_scene,
                    logger=logger,
                    description_max_blocks=None,
                    description_log_phase="重试",
                )
                translated_fields["description"] = retry_fields["description"]

            invalid_fields = _collect_invalid_metadata_fields(
                cleaned_sources,
                translated_fields,
                description_max_blocks=None,
            )
            if invalid_fields:
                logger.warning(f"元数据重试后仍有失败字段: {invalid_fields}")

        for field_name in ("title", "description"):
            if field_name not in payload:
                continue
            if field_name in invalid_fields or not translated_fields.get(field_name):
                if field_name == "description":
                    final_result[field_name] = ''
                    logger.warning("简介翻译失败，按策略置空，不回退原语言文本")
                else:
                    final_result[field_name] = title_fallback
            else:
                final_result[field_name] = translated_fields[field_name]

        elapsed = time.time() - start_time
        logger.info(f"元数据翻译完成，耗时: {elapsed:.2f}秒")
        logger.info(f"翻译标题: {final_result['title'][:100]}...")
        logger.info(f"翻译简介长度: {len(final_result['description'])} 字符")
        return final_result

    except Exception as e:
        logger.error(f"翻译视频元数据时发生错误: {str(e)}")
        logger.error(traceback.format_exc())
        return final_result

def generate_acfun_tags(title, description, openai_config=None, task_id=None):
    """
    使用OpenAI生成AcFun风格的标签
    
    Args:
        title (str): 视频标题
        description (str): 视频描述
        openai_config (dict): OpenAI配置信息，包含api_key, base_url, model_name等
        task_id (str, optional): 任务ID，用于日志记录
        
    Returns:
        list: 标签列表，出错时返回空列表
    """
    logger = setup_task_logger(task_id or "unknown")
    logger.info("开始生成AcFun标签")
    title = _pre_clean(safe_str(title), content_type="title")
    description = _pre_clean(safe_str(description), content_type="description")
    if not _has_meaningful_content(title, content_type="title"):
        title = ''
    if not _has_meaningful_content(description, content_type="description"):
        description = ''
    logger.info(f"标签标题输入: {title[:100]}...")
    logger.info(f"标签简介输入长度: {len(description)} 字符")

    if not (title or description):
        logger.warning("缺少有效标题和简介，跳过标签生成")
        return []

    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.warning("缺少OpenAI配置或API密钥，跳过标签生成")
        return []

    try:
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        start_time = time.time()
        parsed = _request_json_object(
            client=client,
            model_name=model_name,
            system_prompt=(
                "你是视频标签生成器。基于标题和简介输出 6 个简体中文标签。"
                "标签必须短、去重、无序号、无解释。"
                '只返回 JSON：{"tags":["","","","","",""]}。'
            ),
            payload={
                "title": title,
                "description": description[:200],
            },
            max_tokens=160,
            temperature=0.2,
            thinking_enabled=openai_config.get('OPENAI_THINKING_ENABLED', False),
            logger_obj=logger,
            scene_name='ai_enhancer_tags',
        )
        response_time = time.time() - start_time
        logger.info(f"标签生成完成，耗时: {response_time:.2f}秒")

        raw_tags = parsed.get("tags") if isinstance(parsed, dict) else None
        if not isinstance(raw_tags, list):
            logger.error(f"标签响应不是对象JSON或缺少 tags 字段: {safe_str(parsed)[:200]}")
            return []

        normalized_tags: List[str] = []
        seen = set()
        for raw_tag in raw_tags:
            tag = _normalize_whitespace(safe_str(raw_tag)).strip()
            if not tag:
                continue
            tag = tag[:10]
            lowered = tag.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized_tags.append(tag)
            if len(normalized_tags) >= 6:
                break

        while len(normalized_tags) < 6:
            normalized_tags.append('')

        logger.info(f"生成标签: {normalized_tags}")
        return normalized_tags

    except Exception as e:
        logger.error(f"生成标签过程中发生错误: {str(e)}")
        logger.error(traceback.format_exc())
        return []

def flatten_partitions(id_mapping_data):
    """
    将id_mapping_data扁平化为分区列表
    
    Args:
        id_mapping_data (list): id_mapping.json解析后的数据
        
    Returns:
        list: 分区列表，每个元素包含id, name等信息
    """
    if not id_mapping_data:
        return []
        
    partitions = []
    
    for category_item in id_mapping_data:
        # 兼容两种格式："name"或"category"作为分类名称
        category_name = category_item.get('name', '') or category_item.get('category', '')
        for partition in category_item.get('partitions', []):
            # 记录一级分区信息
            partition_id = partition.get('id')
            partition_name = partition.get('name', '')
            partition_desc = partition.get('description', '')
            
            if partition_id:
                partitions.append({
                    'id': partition_id,
                    'name': partition_name,
                    'description': partition_desc,
                    'parent_name': category_name
                })
            
            # 处理二级分区
            for sub_partition in partition.get('sub_partitions', []):
                sub_id = sub_partition.get('id')
                sub_name = sub_partition.get('name', '')
                sub_desc = sub_partition.get('description', '')
                
                if sub_id:
                    partitions.append({
                        'id': sub_id,
                        'name': sub_name,
                        'description': sub_desc,
                        'parent_name': partition_name
                    })
    
    return partitions

def flatten_bilibili_partitions(zone_data):
    """
    将 Bilibili video_zone.get_zone_list_sub() 扁平化为分区列表。

    Args:
        zone_data (list): bilibili分区原始数据

    Returns:
        list: 统一结构分区列表
    """
    if not zone_data:
        return []

    partitions = []
    for item in zone_data:
        if not isinstance(item, dict):
            continue

        parent_tid = item.get("tid")
        parent_name = safe_str(item.get("name"))
        parent_desc = safe_str(item.get("desc"))

        # 跳过“全部分区”等无效顶层
        if parent_tid not in (None, "", 0, "0") and parent_name:
            partitions.append(
                {
                    "id": str(parent_tid),
                    "name": parent_name,
                    "description": parent_desc,
                    "parent_name": "",
                }
            )

        for sub in item.get("sub", []) or []:
            if not isinstance(sub, dict):
                continue
            sub_tid = sub.get("tid")
            sub_name = safe_str(sub.get("name"))
            if sub_tid in (None, "", 0, "0") or not sub_name:
                continue
            partitions.append(
                {
                    "id": str(sub_tid),
                    "name": sub_name,
                    "description": safe_str(sub.get("desc")),
                    "parent_name": parent_name,
                }
            )

    return partitions

def _find_partition_id_by_name(partitions, name_sub: str):
    keyword = safe_str(name_sub).strip()
    if not keyword:
        return None
    for partition in partitions:
        if keyword in safe_str(partition.get("name")):
            return str(partition.get("id"))
    return None


def _rule_based_partition_fallback(title: str, description: str, partitions) -> Optional[str]:
    text = f"{title or ''}\n{description or ''}".lower()
    rules = (
        (["music", "歌曲", "演唱", "mv", "翻唱", "乐器", "单曲", "专辑"], ("综合音乐", "原创·翻唱", "演奏·乐器", "音乐综合", "音乐")),
        (["舞蹈", "dance", "编舞", "翻跳", "宅舞"], ("综合舞蹈", "宅舞", "舞蹈")),
        (["预告", "花絮", "trailer", "behind the scenes", "影视", "电影"], ("预告·花絮", "影视")),
        (["game", "游戏", "实况", "攻略", "电竞"], ("主机单机", "电子竞技", "网络游戏", "游戏")),
        (["科技", "数码", "评测", "开箱", "测评"], ("数码家电", "科技制造", "科技", "数码")),
        (["vlog", "生活", "美食", "旅行", "宠物"], ("生活日常", "美食", "旅行", "生活")),
        (["教程", "科普", "知识", "教学"], ("知识", "科普")),
    )
    for keywords, candidate_names in rules:
        if not any(keyword in text for keyword in keywords):
            continue
        for candidate_name in candidate_names:
            matched = _find_partition_id_by_name(partitions, candidate_name)
            if matched:
                return matched
    return None


def _compact_partition_candidates(partitions) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    for partition in partitions:
        description = _normalize_whitespace(safe_str(partition.get("description")))
        candidates.append(
            {
                "id": safe_str(partition.get("id")).strip(),
                "name": safe_str(partition.get("name")).strip(),
                "parent": safe_str(partition.get("parent_name")).strip(),
                "description": description[:48],
            }
        )
    return [candidate for candidate in candidates if candidate["id"] and candidate["name"]]


def _build_cover_data_url(cover_path: str) -> Optional[str]:
    path = safe_str(cover_path).strip()
    if not path:
        return None
    if not os.path.isfile(path):
        raise FileNotFoundError(f"封面文件不存在: {path}")

    extension = os.path.splitext(path)[1].lower()
    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    mime_type = mime_types.get(extension)
    if not mime_type:
        raise ValueError(f"不支持的封面格式: {extension or 'unknown'}")

    with open(path, "rb") as file_obj:
        raw = file_obj.read()
    if not raw:
        raise ValueError("封面文件为空")

    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _is_multimodal_input_unsupported_error(exc: Exception) -> bool:
    text = safe_str(exc).lower()
    signals = (
        "image_url",
        "input_image",
        "image input",
        "vision",
        "multimodal",
        "content type",
        "invalid chat format",
        "unsupported content",
        "does not support image",
        "only text",
        "not support image",
    )
    return any(signal in text for signal in signals)


def _request_partition_id(
    *,
    title: str,
    description: str,
    partitions,
    openai_config,
    logger,
    scene_name: str,
    cover_path: Optional[str] = None,
) -> Optional[str]:
    if not openai_config or not openai_config.get("OPENAI_API_KEY"):
        return None

    candidates = _compact_partition_candidates(partitions)
    if not candidates:
        return None

    client = get_openai_client(openai_config)
    model_name = openai_config.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")
    payload = {
        "title": title,
        "description": description,
        "candidates": candidates,
    }
    system_prompt = (
        "你是视频分区选择器。只从 candidates 中选 1 个最匹配的分区。"
        '只返回 JSON：{"id":"候选ID"}，不要解释。'
    )

    parsed = None
    if cover_path:
        try:
            cover_data_url = _build_cover_data_url(cover_path)
            parsed = _request_json_object(
                client=client,
                model_name=model_name,
                system_prompt=system_prompt,
                payload=payload,
                max_tokens=80,
                temperature=0.0,
                thinking_enabled=openai_config.get('OPENAI_THINKING_ENABLED', False),
                logger_obj=logger,
                scene_name=scene_name,
                user_content=[
                    {
                        "type": "text",
                        "text": (
                            "请结合以下 JSON 信息与封面图片，从 candidates 中选择最合适的分区并只返回 JSON。\n"
                            f"{json.dumps(payload, ensure_ascii=False)}"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": cover_data_url},
                    },
                ],
            )
        except (FileNotFoundError, ValueError, OSError) as exc:
            logger.warning(f"{scene_name} 封面不可用于分区推荐，已回退文本模式: {exc}")
        except Exception as exc:
            if _is_multimodal_input_unsupported_error(exc):
                logger.warning(f"{scene_name} 当前模型或接口不支持图片输入，已回退文本模式: {exc}")
            else:
                raise

    if parsed is None:
        parsed = _request_json_object(
            client=client,
            model_name=model_name,
            system_prompt=system_prompt,
            payload=payload,
            max_tokens=80,
            temperature=0.0,
            thinking_enabled=openai_config.get('OPENAI_THINKING_ENABLED', False),
            logger_obj=logger,
            scene_name=scene_name,
        )
    partition_id = safe_str((parsed or {}).get("id")).strip()
    valid_ids = {safe_str(partition.get("id")).strip() for partition in partitions}
    if partition_id in valid_ids:
        return partition_id
    return None


def recommend_bilibili_partition(
    title,
    description,
    zone_data,
    openai_config=None,
    task_id=None,
    cover_path: Optional[str] = None,
    include_cover_for_ai: bool = False,
):
    """
    使用 OpenAI + 规则策略推荐 Bilibili 分区。

    Returns:
        str | None: 推荐分区ID
    """
    logger = setup_task_logger(task_id or "unknown")
    logger.info("开始推荐 Bilibili 视频分区")

    title = _pre_clean(safe_str(title), content_type="title")
    description = _pre_clean(safe_str(description), content_type="description")
    if not _has_meaningful_content(title, content_type="title"):
        title = ''
    if not _has_meaningful_content(description, content_type="description"):
        description = ''
    if not title and not description:
        logger.warning("缺少标题和描述，无法推荐 bilibili 分区")
        return None

    partitions = flatten_bilibili_partitions(zone_data)
    if not partitions:
        logger.warning("bilibili分区数据为空，无法推荐")
        return None

    available_partition_ids = [p["id"] for p in partitions]

    fixed_pid = safe_str((openai_config or {}).get("FIXED_PARTITION_ID_BILIBILI"))
    if fixed_pid:
        if fixed_pid in available_partition_ids:
            logger.info(f"命中 bilibili 固定分区ID: {fixed_pid}")
            return fixed_pid
        logger.warning(f"配置的 FIXED_PARTITION_ID_BILIBILI 无效: {fixed_pid}")

    rule_based_id = _rule_based_partition_fallback(title, description, partitions)
    if rule_based_id:
        logger.info(f"规则优先命中 bilibili 分区ID: {rule_based_id}")
        return rule_based_id

    if not openai_config or not openai_config.get("OPENAI_API_KEY"):
        logger.info("未配置 OpenAI，且规则未命中")
        return None

    try:
        partition_id = _request_partition_id(
            title=title,
            description=description[:240],
            partitions=partitions,
            openai_config=openai_config,
            logger=logger,
            scene_name='ai_enhancer_partition_bilibili',
            cover_path=cover_path if include_cover_for_ai else None,
        )
        if partition_id:
            logger.info(f"LLM 命中 bilibili 分区ID: {partition_id}")
            return partition_id
        logger.warning("bilibili 分区推荐未返回有效 ID")
        return None

    except Exception as e:
        logger.error(f"bilibili分区推荐异常: {e}")
        logger.error(traceback.format_exc())
        return None

def recommend_acfun_partition(
    title,
    description,
    id_mapping_data,
    openai_config=None,
    task_id=None,
    cover_path: Optional[str] = None,
    include_cover_for_ai: bool = False,
):
    """
    使用OpenAI推荐AcFun视频分区
    
    Args:
        title (str): 视频标题
        description (str): 视频描述
        id_mapping_data (list): 分区ID映射数据
        openai_config (dict): OpenAI配置信息
        task_id (str, optional): 任务ID，用于日志记录
        
    Returns:
        str or None: 推荐分区ID，出错时返回None
    """
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始推荐AcFun视频分区")

    title = _pre_clean(safe_str(title), content_type="title")
    description = _pre_clean(safe_str(description), content_type="description")
    if not _has_meaningful_content(title, content_type="title"):
        title = ''
    if not _has_meaningful_content(description, content_type="description"):
        description = ''

    # 检查必要信息
    if not title and not description:
        logger.warning("缺少标题和描述，无法推荐分区")
        return None
    
    if not id_mapping_data:
        logger.warning("缺少分区映射数据 (id_mapping_data is empty or None)，无法推荐分区")
        return None
    
    # 将分区数据扁平化为易于处理的列表
    partitions = flatten_partitions(id_mapping_data)
    if not partitions:
        logger.warning("分区映射数据格式错误或为空 (flatten_partitions returned empty list)，无法推荐分区")
        return None
    
    fixed_pid = safe_str((openai_config or {}).get('FIXED_PARTITION_ID')).strip()
    if fixed_pid:
        available_ids = {safe_str(partition.get('id')).strip() for partition in partitions}
        if fixed_pid in available_ids:
            logger.info(f"命中 AcFun 固定分区ID: {fixed_pid}")
            return fixed_pid
        logger.warning(f"配置的 FIXED_PARTITION_ID 无效: {fixed_pid}")

    rule_based_id = _rule_based_partition_fallback(title, description, partitions)
    if rule_based_id:
        logger.info(f"规则优先命中 AcFun 分区ID: {rule_based_id}")
        return rule_based_id

    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.info("缺少OpenAI配置或API密钥，且规则未命中")
        return None

    try:
        partition_id = _request_partition_id(
            title=title,
            description=description[:240],
            partitions=partitions,
            openai_config=openai_config,
            logger=logger,
            scene_name='ai_enhancer_partition_acfun',
            cover_path=cover_path if include_cover_for_ai else None,
        )
        if partition_id:
            logger.info(f"LLM 命中 AcFun 分区ID: {partition_id}")
            return partition_id
        logger.warning("AcFun 分区推荐未返回有效 ID")
        return None

    except Exception as e:
        logger.error(f"推荐分区过程中发生严重错误: {str(e)}")
        logger.error(traceback.format_exc())
        return None
