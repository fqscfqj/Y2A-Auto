#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import logging
import re
import time
import json
import traceback
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from .utils import (
    get_app_subdir,
    strip_reasoning_thoughts,
    safe_str,
    openai_chat_create_with_thinking_control,
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

def _compress_description_blocks(text: str, max_blocks: int = 2) -> str:
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
    return '\n\n'.join(blocks[:max_blocks]).strip()

def _pre_clean(text: str, content_type: str = "description") -> str:
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

    return _compress_description_blocks(cleaned, max_blocks=2)

def _build_prompt(cleaned_source_text: str, target_language: str, content_type: str = "description", strict: bool = False) -> str:
    ct_lower = str(content_type).lower().strip()
    target_language = str(target_language).lower().strip()
    target_lang_map = {
        "zh": "中文",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어",
    }
    target_lang_name = target_lang_map.get(str(target_language).lower().strip(), target_language)

    if target_language != "zh":
        if ct_lower == "title":
            base_rules = [
                f"输出自然的{target_lang_name}标题，信息优先，不要标题党。",
                "保留核心含义，但避免机械直译和原词序复刻。",
                "严禁补充原文没有的新事实。",
                "移除广告、导流、站外平台和互动引导。",
                "仅输出单行标题。"
            ]
        else:
            base_rules = [
                f"输出1-2段完整{target_lang_name}简介，风格像站内UP主直接发布。",
                "只能重组原文已有事实，严禁补充新事实。",
                "移除URL/邮箱/@/#/播放列表/站外平台/赞助/联系方式/关注订阅点赞分享等导流信息。",
                "不要清单格式、不要箭头、不要“以下是译文/已移除”等提示语。",
                "保留必要数字与专有名词。"
            ]
        if strict:
            base_rules.append("若输出与原文高度相似或仍含导流痕迹，必须重写后再输出。")
    elif ct_lower == "title":
        base_rules = [
            "输出自然的简体中文标题，信息优先，不要标题党。",
            "保留核心含义，但避免机械直译和原词序复刻。",
            "严禁补充原文没有的新事实。",
            "使用自然、完整的简体中文表达，避免生硬直译和明显的中英混排。",
            "移除广告、导流、站外平台和互动引导。",
            "仅输出单行标题。"
        ]
    else:
        base_rules = [
            "输出1-2段完整简体中文简介，风格像站内UP主直接发布。",
            "只能重组原文已有事实，严禁补充新事实。",
            "使用自然、连贯的简体中文重述内容，避免生硬直译和明显的中英混排。",
            "移除URL/邮箱/@/#/播放列表/站外平台/赞助/联系方式/关注订阅点赞分享等导流信息。",
            "不要清单格式、不要箭头、不要“以下是译文/已移除”等提示语。",
            "保留必要数字与专有名词。"
        ]

    if strict:
        if target_language == "zh":
            base_rules.append("若输出与原文高度相似、中文表达不自然，或仍含导流痕迹，必须重写为更自然的简体中文后再输出。")

    rules = '\n'.join(f"{idx + 1}. {rule}" for idx, rule in enumerate(base_rules))
    purpose = "标题" if ct_lower == "title" else "简介"
    return (
        f"任务：将以下视频{purpose}处理为{'简体中文' if target_language == 'zh' else target_lang_name}。\n\n"
        f"要求：\n{rules}\n\n"
        f"原文：\n{cleaned_source_text}\n\n"
        f"仅返回JSON：{{\"translation\":\"...\"}}"
    )

def _extract_json_payload(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
    return None

def _extract_translation_from_message(message) -> str:
    parsed = getattr(message, 'parsed', None)
    if isinstance(parsed, dict):
        v = parsed.get('translation')
        if isinstance(v, str) and v.strip():
            return v.strip()

    content_value = getattr(message, 'content', None)
    if isinstance(content_value, list):
        raw = ''.join(seg.get('text', '') for seg in content_value if isinstance(seg, dict))
    else:
        raw = content_value or getattr(message, 'reasoning_content', None) or ''

    raw = strip_reasoning_thoughts(raw).strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z0-9]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    payload = _extract_json_payload(raw)
    if isinstance(payload, dict) and isinstance(payload.get('translation'), str):
        return payload.get('translation', '').strip()
    return raw.strip()

def _post_clean(text: str, content_type: str = "description") -> str:
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

    cleaned = _compress_description_blocks(cleaned, max_blocks=2)
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

def _is_natural_description(text: str) -> bool:
    blocks = _split_blocks(text)
    if not blocks or len(blocks) > 2:
        return False
    if _LIST_LINE_RE.search(text):
        return False
    if '►' in text:
        return False
    if any(len(block.strip()) < 6 for block in blocks):
        return False
    return True

def _validate_output(source_clean: str, output_text: str, content_type: str = "description"):
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

    if ct_lower != 'title' and out and not _is_natural_description(out):
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

def _request_translation(
    client,
    model_name: str,
    prompt: str,
    max_tokens: int = 4096,
    thinking_enabled: bool = False,
    logger_obj=None,
):
    create_kwargs = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": '内容改写器。只输出JSON：{"translation":"..."}，不要其他内容。'},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
    }
    try:
        create_kwargs["response_format"] = {"type": "json_object"}
    except Exception:
        pass
    response = openai_chat_create_with_thinking_control(
        client=client,
        create_kwargs=create_kwargs,
        thinking_enabled=thinking_enabled,
        logger=logger_obj,
        scene_name='ai_enhancer_translation',
    )
    return _extract_translation_from_message(response.choices[0].message)

def translate_text(text, target_language="zh-CN", openai_config=None, task_id=None, content_type: str = "description"):
    """
    使用OpenAI对标题/简介进行“清洗 + 原生化改写”。
    返回最终文本，失败返回None。
    """
    if not text or not text.strip():
        return text

    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始翻译文本，目标语言: {target_language}")
    logger.info(f"原始文本 (截取前100字符用于显示): {text[:100]}...")
    logger.info(f"原始文本总长度: {len(text)} 字符")

    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.error("缺少OpenAI配置或API密钥")
        return None

    try:
        ct_lower = str(content_type).lower().strip()
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')

        cleaned_source_text = _pre_clean(text, content_type=ct_lower)
        if cleaned_source_text != text:
            logger.info("已在提示阶段前执行结构化预清洗（去导流/站外信息/列表化噪声）")
        if not cleaned_source_text:
            cleaned_source_text = _normalize_whitespace(text)[:1000]

        start_time = time.time()
        prompt = _build_prompt(
            cleaned_source_text=cleaned_source_text,
            target_language=target_language,
            content_type=ct_lower,
            strict=False
        )
        translated_text = _request_translation(
            client=client,
            model_name=model_name,
            prompt=prompt,
            max_tokens=4096 if ct_lower != 'title' else 1024,
            thinking_enabled=openai_config.get('OPENAI_THINKING_ENABLED', False),
            logger_obj=logger,
        )
        translated_text = _post_clean(translated_text, content_type=ct_lower)
        translated_text = _apply_output_limits(translated_text, content_type=ct_lower, logger=logger)

        ok, reasons = _validate_output(cleaned_source_text, translated_text, content_type=ct_lower)
        if not ok:
            logger.info(f"首次输出未通过质量门槛，触发严格重试: {reasons}")
            strict_prompt = _build_prompt(
                cleaned_source_text=cleaned_source_text,
                target_language=target_language,
                content_type=ct_lower,
                strict=True
            )
            retried_text = _request_translation(
                client=client,
                model_name=model_name,
                prompt=strict_prompt,
                max_tokens=3072 if ct_lower != 'title' else 1024,
                thinking_enabled=openai_config.get('OPENAI_THINKING_ENABLED', False),
                logger_obj=logger,
            )
            retried_text = _post_clean(retried_text, content_type=ct_lower)
            retried_text = _apply_output_limits(retried_text, content_type=ct_lower, logger=logger)
            retry_ok, retry_reasons = _validate_output(cleaned_source_text, retried_text, content_type=ct_lower)
            if retry_ok:
                translated_text = retried_text
            else:
                logger.warning(f"严格重试仍未完全通过质量门槛: {retry_reasons}")
                if retried_text.strip():
                    translated_text = retried_text

        if not translated_text:
            translated_text = _apply_output_limits(
                _post_clean(cleaned_source_text, content_type=ct_lower),
                content_type=ct_lower,
                logger=logger
            )

        elapsed = time.time() - start_time
        logger.info(f"翻译完成，耗时: {elapsed:.2f}秒")
        logger.info(f"翻译结果总长度: {len(translated_text)} 字符")
        logger.info(f"翻译结果 (截取前100字符用于显示): {translated_text[:100]}...")
        return translated_text

    except Exception as e:
        logger.error(f"翻译过程中发生错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None

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
    logger.info(f"开始生成AcFun标签")
    # 防御性处理：确保 title/description 为字符串，避免 None 导致切片/len 时抛出异常
    title = safe_str(title)
    description = safe_str(description)
    logger.info(f"视频标题: {title}")
    logger.info(f"视频描述 (截取前100字符用于显示): {description[:100]}...")
    logger.info(f"视频描述总长度: {len(description)} 字符")
    
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.error("缺少OpenAI配置或API密钥")
        return []
    
    try:
        # 获取OpenAI客户端 (1.x版本)
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')

        # 构建标签生成提示（优化版：精简提示词）
        # 截取描述前200字符以减少token
        short_desc = description[:200] if len(description) > 200 else description
        prompt = f"""为视频生成6个AcFun标签（每个≤10汉字）。

标题：{title}
描述：{short_desc}

请以JSON格式返回：{{"tags":["标签1","标签2","标签3","标签4","标签5","标签6"]}}"""
        
        start_time = time.time()

        # 使用新版API调用格式
        create_kwargs = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": '标签生成器。仅输出JSON格式：{"tags":[...]}，共6个标签。'},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 300,
        }
        # 尝试启用结构化JSON输出
        try:
            create_kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass

        response = openai_chat_create_with_thinking_control(
            client=client,
            create_kwargs=create_kwargs,
            thinking_enabled=openai_config.get('OPENAI_THINKING_ENABLED', False),
            logger=logger,
            scene_name='ai_enhancer_tags',
        )
        response_time = time.time() - start_time
        logger.info(f"标签生成完成，耗时: {response_time:.2f}秒")

        # 提取响应内容并屏蔽思考
        message = response.choices[0].message
        tags_response = (message.content or getattr(message, 'reasoning_content', None) or '')
        tags_response = strip_reasoning_thoughts(tags_response).strip()

        # 尝试解析JSON
        import json
        import re

        # 去除可能的代码块围栏
        if tags_response.startswith("```"):
            try:
                tags_response = re.sub(r'^```[a-zA-Z0-9]*\s*', '', tags_response)
                tags_response = re.sub(r'\s*```$', '', tags_response)
            except Exception:
                pass

        # 优先解析对象JSON {"tags": [...]} 
        try:
            data = json.loads(tags_response)
            if isinstance(data, dict) and isinstance(data.get('tags'), list):
                tags = data['tags']
            else:
                # 兼容旧格式：直接数组
                tags = data if isinstance(data, list) else None
        except Exception:
            # 兼容：从文本中提取JSON对象或数组
            obj_match = re.search(r'\{[^{}]*\}', tags_response, re.DOTALL)
            arr_match = re.search(r'\[[^\[\]]*\]', tags_response, re.DOTALL)
            raw_json = obj_match.group(0) if obj_match else (arr_match.group(0) if arr_match else None)
            tags = None
            if raw_json:
                try:
                    data = json.loads(raw_json)
                    if isinstance(data, dict) and isinstance(data.get('tags'), list):
                        tags = data['tags']
                    elif isinstance(data, list):
                        tags = data
                except Exception:
                    pass

        if not tags:
            logger.error(f"无法从响应中提取标签: {tags_response}")
            return []

        # 归一化并确保我们有6个标签
        tags = [str(tag).strip() for tag in tags]
        if len(tags) > 6:
            tags = tags[:6]
        elif len(tags) < 6:
            tags.extend([''] * (6 - len(tags)))

        # 确保每个标签不超过长度限制
        tags = [str(tag)[:20] for tag in tags]

        logger.info(f"生成标签: {tags}")
        return tags
    
    except Exception as e:
        logger.error(f"生成标签过程中发生错误: {str(e)}")
        import traceback
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

def recommend_bilibili_partition(title, description, zone_data, openai_config=None, task_id=None):
    """
    使用 OpenAI + 规则策略推荐 Bilibili 分区。

    Returns:
        str | None: 推荐分区ID
    """
    logger = setup_task_logger(task_id or "unknown")
    logger.info("开始推荐 Bilibili 视频分区")

    title = safe_str(title)
    description = safe_str(description)
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

    def _find_partition_id_by_name(name_sub: str):
        name_sub = safe_str(name_sub)
        if not name_sub:
            return None
        for p in partitions:
            if name_sub in p.get("name", ""):
                return p["id"]
        return None

    def _rule_based_fallback(t: str, d: str):
        text = f"{t or ''}\n{d or ''}".lower()
        if any(k in text for k in ["music", "歌曲", "演唱", "mv", "翻唱", "乐器"]):
            return _find_partition_id_by_name("音乐") or _find_partition_id_by_name("音乐综合")
        if any(k in text for k in ["舞蹈", "dance", "编舞", "翻跳", "宅舞"]):
            return _find_partition_id_by_name("舞蹈")
        if any(k in text for k in ["game", "游戏", "实况", "电竞", "攻略"]):
            return _find_partition_id_by_name("游戏")
        if any(k in text for k in ["科技", "数码", "评测", "开箱"]):
            return _find_partition_id_by_name("科技") or _find_partition_id_by_name("数码")
        if any(k in text for k in ["vlog", "生活", "美食", "旅行", "宠物"]):
            return _find_partition_id_by_name("生活") or _find_partition_id_by_name("美食")
        if any(k in text for k in ["电影", "影视", "预告", "花絮", "trailer"]):
            return _find_partition_id_by_name("影视")
        if any(k in text for k in ["教程", "科普", "知识", "教学"]):
            return _find_partition_id_by_name("知识") or _find_partition_id_by_name("科普")
        return None

    if not openai_config or not openai_config.get("OPENAI_API_KEY"):
        logger.info("未配置 OpenAI，使用规则回退")
        return _rule_based_fallback(title, description)

    try:
        client = get_openai_client(openai_config)
        model_name = openai_config.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")

        partition_lines = []
        for p in partitions:
            prefix = f"{p.get('parent_name')} - " if p.get("parent_name") else ""
            partition_lines.append(
                f"{prefix}{p['name']} (ID: {p['id']}): {p.get('description', '无描述')}"
            )
        partitions_text = "\n".join(partition_lines)

        short_desc = (description[:500] + "...") if len(description) > 500 else description
        prompt = f"""请从分区列表中选择最适合该视频的 Bilibili 分区。

标题：{title}
描述：{short_desc}

分区列表：
{partitions_text}

要求：
1. 只返回JSON
2. 格式必须是：{{"id":"分区ID","reason":"理由"}}
3. id 必须来自列表中的 ID
"""

        create_kwargs = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": '视频分区选择器。仅输出JSON：{"id":"...","reason":"..."}。'},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 200,
        }
        try:
            create_kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass

        response = openai_chat_create_with_thinking_control(
            client=client,
            create_kwargs=create_kwargs,
            thinking_enabled=openai_config.get('OPENAI_THINKING_ENABLED', False),
            logger=logger,
            scene_name='ai_enhancer_partition_bilibili',
        )
        message = response.choices[0].message
        result = (message.content or getattr(message, "reasoning_content", None) or "")
        result = strip_reasoning_thoughts(result).strip()
        if result.startswith("```"):
            result = re.sub(r"^```[a-zA-Z0-9]*\s*", "", result)
            result = re.sub(r"\s*```$", "", result)

        logger.info(f"bilibili分区推荐原始响应: {result}")

        try:
            data = json.loads(result)
            pid = str(data.get("id", "")).strip()
            if pid in available_partition_ids:
                return pid
        except Exception:
            pass

        id_match = re.search(r'"id"\s*:\s*"?(?P<id>\d+)"?', result)
        if id_match:
            pid = id_match.group("id")
            if pid in available_partition_ids:
                return pid

        # 在输出文本里兜底匹配合法 ID
        joined = "|".join(re.escape(pid) for pid in available_partition_ids)
        if joined:
            any_match = re.search(rf"\b({joined})\b", result)
            if any_match:
                return any_match.group(1)

        logger.warning("OpenAI 分区推荐解析失败，使用规则回退")
        return _rule_based_fallback(title, description)

    except Exception as e:
        logger.error(f"bilibili分区推荐异常: {e}")
        logger.error(traceback.format_exc())
        return _rule_based_fallback(title, description)

def recommend_acfun_partition(title, description, id_mapping_data, openai_config=None, task_id=None):
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
    from typing import Optional
    
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始推荐AcFun视频分区")

    # 防御性归一化
    title = safe_str(title)
    description = safe_str(description)

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
    
    # 如果没有OpenAI配置，直接尝试规则匹配
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.info("缺少OpenAI配置或API密钥，尝试使用规则匹配")
        
        def rule_based_fallback(t: str, d: str) -> Optional[str]:
            """基于简单关键词的回退分类策略。"""
            text = f"{t or ''}\n{d or ''}".lower()
            # 音乐相关
            if any(k in text for k in [' mv', '官方mv', 'official video', 'music', '歌曲', '演唱', '单曲', '专辑', 'mv']):
                for p in partitions:
                    if '综合音乐' in p.get('name', '') or '原创·翻唱' in p.get('name', '') or '演奏·乐器' in p.get('name', ''):
                        return p['id']
            # 舞蹈相关
            if any(k in text for k in ['舞蹈', 'dance', '编舞', '翻跳']):
                for p in partitions:
                    if '综合舞蹈' in p.get('name', '') or '宅舞' in p.get('name', ''):
                        return p['id']
            # 影视预告/花絮
            if any(k in text for k in ['预告', '花絮', 'trailer', 'behind the scenes']):
                for p in partitions:
                    if '预告·花絮' in p.get('name', ''):
                        return p['id']
            # 游戏相关
            if any(k in text for k in ['game', '游戏', '实况', '攻略', '电竞']):
                for p in partitions:
                    if '主机单机' in p.get('name', '') or '电子竞技' in p.get('name', '') or '网络游戏' in p.get('name', ''):
                        return p['id']
            # 科技/数码
            if any(k in text for k in ['科技', '数码', '评测', '开箱', '测评']):
                for p in partitions:
                    if '数码家电' in p.get('name', '') or '科技制造' in p.get('name', ''):
                        return p['id']
            # 生活
            if any(k in text for k in ['vlog', '生活', '美食', '旅行', '宠物']):
                for p in partitions:
                    if '生活日常' in p.get('name', '') or '美食' in p.get('name', '') or '旅行' in p.get('name', ''):
                        return p['id']
            return None
        
        fallback_result = rule_based_fallback(title or '', description or '')
        if fallback_result:
            logger.info(f"规则匹配成功，推荐分区ID: {fallback_result}")
            return fallback_result
        else:
            logger.warning("规则匹配未找到合适的分区")
            return None
    
    try:
        # 获取OpenAI客户端 (1.x版本)
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        # 准备分区描述信息
        partitions_info = []
        for p in partitions:
            parent_name = p.get('parent_name', '') 
            prefix = f"{parent_name} - " if parent_name else ""
            partitions_info.append(f"{prefix}{p['name']} (ID: {p['id']}): {p.get('description', '无描述')}")
        
        partitions_text = "\n".join(partitions_info)
        
        # 构建提示内容
        # 在构造 prompt 时防护 description 的切片
        short_desc = (description[:500] + '...') if len(description) > 500 else description
        prompt = f"""从分区列表选择最匹配的分区。

标题：{title}
描述：{short_desc[:200] if len(short_desc) > 200 else short_desc}

分区列表：
{partitions_text}

返回JSON：{{"id":"分区ID","reason":"理由"}}"""
        
        # 如果配置指定固定分区ID，优先返回
        fixed_pid = (openai_config or {}).get('FIXED_PARTITION_ID')
        if fixed_pid and fixed_pid in [p['id'] for p in partitions]:
            logger.info(f"根据配置固定分区ID直接返回: {fixed_pid}")
            return fixed_pid

        # 先尝试规则直推，尽量一次成功
        pre_rule_id = None
        def _pre_rule_based(title_in, desc_in):
            text = f"{title_in or ''}\n{desc_in or ''}".lower()
            if any(k in text for k in [' mv', '官方mv', 'official video', 'music', '歌曲', '演唱', '单曲', '专辑', 'mv']):
                return (
                    next((p['id'] for p in partitions if '综合音乐' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '原创·翻唱' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '演奏·乐器' in p.get('name','')), None)
                )
            if any(k in text for k in ['舞蹈', 'dance', '编舞', '翻跳']):
                return (
                    next((p['id'] for p in partitions if '综合舞蹈' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '宅舞' in p.get('name','')), None)
                )
            if any(k in text for k in ['预告', '花絮', 'trailer', 'behind the scenes']):
                return next((p['id'] for p in partitions if '预告·花絮' in p.get('name','')), None)
            if any(k in text for k in ['game', '游戏', '实况', '攻略', '电竞']):
                return (
                    next((p['id'] for p in partitions if '主机单机' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '电子竞技' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '网络游戏' in p.get('name','')), None)
                )
            if any(k in text for k in ['科技', '数码', '评测', '开箱', '测评']):
                return (
                    next((p['id'] for p in partitions if '数码家电' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '科技制造' in p.get('name','')), None)
                )
            if any(k in text for k in ['vlog', '生活', '美食', '旅行', '宠物']):
                return (
                    next((p['id'] for p in partitions if '生活日常' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '美食' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '旅行' in p.get('name','')), None)
                )
            return None
        pre_rule_id = _pre_rule_based(title, description)
        if pre_rule_id:
            logger.info(f"规则优先直接命中分区ID: {pre_rule_id}")
            return pre_rule_id

        # 使用新版API调用格式（尽量结构化）
        create_kwargs = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": '视频分区选择器。仅输出JSON格式：{"id":"...","reason":"..."}。'},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 200,
        }
        try:
            create_kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass

        response = openai_chat_create_with_thinking_control(
            client=client,
            create_kwargs=create_kwargs,
            thinking_enabled=openai_config.get('OPENAI_THINKING_ENABLED', False),
            logger=logger,
            scene_name='ai_enhancer_partition_acfun',
        )
        
        message = response.choices[0].message
        result = (message.content or getattr(message, 'reasoning_content', None) or '')
        result = strip_reasoning_thoughts(result).strip()
        # 处理可能的Markdown代码块围栏
        if result.startswith('```'):
            # 去除围栏与语言标记
            tmp = result.strip('`')
            tmp = tmp.replace('\njson\n', '\n').replace('\njson', '\n').replace('json\n', '\n')
            result = tmp.strip()
        logger.info(f"分区推荐原始响应: {result}")
        
        # 解析结果
        import json
        import re
        from typing import Optional
        
        available_partition_ids = [p['id'] for p in partitions]

        def extract_first_json_object(text: str) -> Optional[str]:
            """从文本中提取第一个完整的JSON对象（使用括号计数，忽略引号内的括号）。"""
            if not text:
                return None
            start = text.find('{')
            if start == -1:
                return None
            brace = 0
            in_str = False
            esc = False
            for i, ch in enumerate(text[start:], start):
                if esc:
                    esc = False
                    continue
                if ch == '\\':
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                if not in_str:
                    if ch == '{':
                        brace += 1
                    elif ch == '}':
                        brace -= 1
                        if brace == 0:
                            return text[start:i+1]
            return None

        def find_partition_id_by_name(name_sub: str) -> Optional[str]:
            """根据分区名称包含关系查找ID。"""
            name_sub = (name_sub or '').strip()
            if not name_sub:
                return None
            for p in partitions:
                if name_sub in p.get('name', ''):
                    return p['id']
            return None

        def rule_based_fallback(t: str, d: str) -> Optional[str]:
            """基于简单关键词的回退分类策略。"""
            text = f"{t or ''}\n{d or ''}".lower()
            # 音乐相关
            if any(k in text for k in [' mv', '官方mv', 'official video', 'music', '歌曲', '演唱', '单曲', '专辑', 'mv']):
                # 优先 综合音乐 -> 原创·翻唱 -> 演奏·乐器
                return (
                    find_partition_id_by_name('综合音乐') or
                    find_partition_id_by_name('原创·翻唱') or
                    find_partition_id_by_name('演奏·乐器')
                )
            # 舞蹈相关
            if any(k in text for k in ['舞蹈', 'dance', '编舞', '翻跳']):
                return find_partition_id_by_name('综合舞蹈') or find_partition_id_by_name('宅舞')
            # 影视预告/花絮
            if any(k in text for k in ['预告', '花絮', 'trailer', 'behind the scenes']):
                return find_partition_id_by_name('预告·花絮')
            # 游戏相关
            if any(k in text for k in ['game', '游戏', '实况', '攻略', '电竞']):
                return find_partition_id_by_name('主机单机') or find_partition_id_by_name('电子竞技') or find_partition_id_by_name('网络游戏')
            # 科技/数码
            if any(k in text for k in ['科技', '数码', '评测', '开箱', '测评']):
                return find_partition_id_by_name('数码家电') or find_partition_id_by_name('科技制造')
            # 生活
            if any(k in text for k in ['vlog', '生活', '美食', '旅行', '宠物']):
                return find_partition_id_by_name('生活日常') or find_partition_id_by_name('美食') or find_partition_id_by_name('旅行')
            return None

        # 尝试直接解析JSON
        try:
            data = json.loads(result)
            if 'id' in data:
                # 验证ID是否存在于分区列表中
                partition_id = str(data['id'])
                if partition_id in available_partition_ids:
                    logger.info(f"推荐分区: ID {partition_id}, 理由: {data.get('reason', '无')}")
                    # 直接返回分区ID字符串，而不是整个字典
                    return partition_id
                else:
                    logger.warning(f"推荐的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。原始响应: {result}")
        except json.JSONDecodeError as e_direct:
            logger.warning(f"直接解析JSON响应失败: {e_direct}. 原始响应: {result}")
            # 如果直接解析失败，尝试从文本中提取JSON（使用括号计数）
            extracted_json_text = extract_first_json_object(result)
            if not extracted_json_text:
                # 退而求其次，使用简单正则（不跨嵌套）
                match = re.search(r'\{[^{}]*\}', result, re.DOTALL)
                extracted_json_text = match.group(0) if match else None
            if extracted_json_text:
                try:
                    data = json.loads(extracted_json_text)
                    if 'id' in data:
                        partition_id = str(data['id'])
                        if partition_id in available_partition_ids:
                            logger.info(f"从提取内容中推荐分区: ID {partition_id}, 理由: {data.get('reason', '无')}")
                            return partition_id
                        else:
                            logger.warning(f"提取内容中推荐的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。提取的文本: {extracted_json_text}")
                except json.JSONDecodeError as e_extract:
                    logger.warning(f"无法从提取的文本中解析JSON: {e_extract}. 提取的文本: {extracted_json_text}")
        
        # 如果上述方法都失败，尝试提取ID
        id_match = re.search(r'"id"\s*:\s*"?(\d+)"?', result)
        if id_match:
            partition_id = id_match.group(1)
            if partition_id in available_partition_ids:
                reason_match = re.search(r'"reason"\s*:\s*"([^"]+)"', result)
                reason = reason_match.group(1) if reason_match else "未提供理由 (正则提取)"
                logger.info(f"正则提取的推荐分区: ID {partition_id}, 理由: {reason}")
                return partition_id
            else:
                logger.warning(f"正则提取的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。原始响应: {result}")

        # 最后尝试：在文本中直接匹配已知ID集合
        joined_ids = '|'.join(re.escape(pid) for pid in available_partition_ids)
        id_any_match = re.search(rf'\b({joined_ids})\b', result)
        if id_any_match:
            pid = id_any_match.group(1)
            logger.info(f"在响应文本中直接匹配到合法分区ID: {pid}")
            return pid

        # 规则回退
        fallback_id = rule_based_fallback(title or '', description or '')
        if fallback_id and fallback_id in available_partition_ids:
            logger.warning(f"无法从OpenAI响应可靠解析，启用规则回退，得到分区ID: {fallback_id}")
            return fallback_id
        
        logger.warning(f"无法从OpenAI响应中解析或验证有效的分区ID。最终原始响应: {result}")
        return None
        
    except Exception as e:
        logger.error(f"推荐分区过程中发生严重错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None 
