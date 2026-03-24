#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
from PIL import Image

def get_app_root_dir():
    """
    获取应用根目录，兼容开发环境和打包环境
    
    Returns:
        str: 应用根目录路径
    """
    if getattr(sys, 'frozen', False):
        # 在PyInstaller打包环境中
        # sys.executable 指向的是实际的可执行文件
        app_root = os.path.dirname(sys.executable)
    else:
        # 在开发环境中
        # __file__ 是当前文件的路径，需要向上两级找到项目根目录
        app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    return app_root

def get_app_subdir(subdir_name):
    """
    获取应用子目录路径
    
    Args:
        subdir_name (str): 子目录名称，如 'config', 'logs', 'db' 等
        
    Returns:
        str: 子目录的完整路径
    """
    return os.path.join(get_app_root_dir(), subdir_name)

import re
import copy
import json
from typing import Any, Optional
from urllib.parse import urlparse

def process_cover(image_path, output_path=None, mode='crop'):
    """
    处理视频封面图片，使其适合AcFun上传要求（16:10比例）
    
    Args:
        image_path (str): 输入图片路径
        output_path (str, optional): 输出图片路径，如果不提供则覆盖原图片
        mode (str): 处理模式，'crop'表示裁剪，'pad'表示添加黑边
        
    Returns:
        str: 处理后的图片路径
    """
    if not output_path:
        output_path = image_path
        
    try:
        # 打开图片
        img = Image.open(image_path)
        width, height = img.size
        
        # 目标比例 16:10
        target_ratio = 16 / 10
        current_ratio = width / height
        
        if mode == 'crop':
            # 裁剪模式
            if current_ratio > target_ratio:
                # 图片太宽，需要裁剪宽度
                new_width = int(height * target_ratio)
                left = (width - new_width) // 2
                img = img.crop((left, 0, left + new_width, height))
            elif current_ratio < target_ratio:
                # 图片太高，需要裁剪高度
                new_height = int(width / target_ratio)
                top = (height - new_height) // 2
                img = img.crop((0, top, width, top + new_height))
        elif mode == 'pad':
            # 填充模式
            if current_ratio > target_ratio:
                # 图片太宽，需要增加高度
                new_height = int(width / target_ratio)
                new_img = Image.new('RGB', (width, new_height), (0, 0, 0))
                paste_y = (new_height - height) // 2
                new_img.paste(img, (0, paste_y))
                img = new_img
            elif current_ratio < target_ratio:
                # 图片太高，需要增加宽度
                new_width = int(height * target_ratio)
                new_img = Image.new('RGB', (new_width, height), (0, 0, 0))
                paste_x = (new_width - width) // 2
                new_img.paste(img, (paste_x, 0))
                img = new_img
        
        # 保存处理后的图片
        img.save(output_path, quality=95)
        return output_path
    except Exception as e:
        print(f"处理封面图片时出错: {str(e)}")
        return image_path 

# -----------------------------
# LLM 输出清洗与兼容辅助函数
# -----------------------------

# Pre-compiled regex patterns for strip_reasoning_thoughts (performance optimization)
_THINK_TAG_RE = re.compile(r'<\s*think\s*>.*?<\s*/\s*think\s*>', re.IGNORECASE | re.DOTALL)
_THINK_BLOCK_RE = re.compile(r'```\s*think[^\n]*\n.*?```', re.IGNORECASE | re.DOTALL)
_CODE_FENCE_RE = re.compile(r'^```[a-zA-Z0-9_-]*\s*|\s*```$', re.DOTALL)

def strip_reasoning_thoughts(text):
    """
    屏蔽/移除思考模型产出的思考内容，仅保留最终答案。
    - 兼容 DeepSeek 的 <think>...</think> 标签
    - 兼容 ```think ...``` 代码块形式

    Args:
        text (str): 原始模型输出

    Returns:
        str: 已移除思考内容的纯净文本
    """
    try:
        if not isinstance(text, str):
            return text

        cleaned = text

        # 移除 <think>...</think>（大小写不敏感，跨行匹配）
        cleaned = _THINK_TAG_RE.sub('', cleaned)

        # 移除 ```think ...``` 样式的思考内容代码块（仅当语言标记包含 think 时）
        cleaned = _THINK_BLOCK_RE.sub('', cleaned)

        # 去除多余空白
        cleaned = cleaned.strip()
        return cleaned
    except Exception:
        return text


def strip_code_fences(text):
    """移除 Markdown 代码块围栏。"""
    try:
        if not isinstance(text, str):
            return text
        cleaned = text.strip()
        if cleaned.startswith('```'):
            cleaned = _CODE_FENCE_RE.sub('', cleaned)
        return cleaned.strip()
    except Exception:
        return text

def safe_str(value, default=''):
    """
    将任意值安全转换为字符串，如果为 None 则返回默认值（默认为空字符串）。

    Args:
        value: 可能为 None 或其他类型的值
        default: 当 value 为 None 或空时返回的默认字符串

    Returns:
        str: 安全的字符串表示
    """
    try:
        if value is None:
            return default
        # 如果已经是字符串，直接返回（保持原样）
        if isinstance(value, str):
            return value
        # 否则尝试转换为字符串
        return str(value)
    except Exception:
        return default


def _extract_balanced_json_block(text: str, start_char: str, end_char: str) -> Optional[str]:
    start = text.find(start_char)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == start_char:
            depth += 1
        elif char == end_char:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def extract_json_from_text(text, expected_type=None):
    """从文本中提取 JSON，兼容 reasoning/代码块/包裹文本。"""
    raw = strip_code_fences(strip_reasoning_thoughts(safe_str(text))).strip()
    if not raw:
        return None

    candidates = [raw]
    for start_char, end_char in (('{', '}'), ('[', ']')):
        block = _extract_balanced_json_block(raw, start_char, end_char)
        if block and block not in candidates:
            candidates.append(block)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if expected_type is not None and not isinstance(parsed, expected_type):
            continue
        return parsed
    return None


def get_chat_message_text(message) -> str:
    """提取 chat.completions message 的纯文本内容。"""
    if message is None:
        return ''

    content = getattr(message, 'content', None)
    if isinstance(content, list):
        parts = []
        for segment in content:
            if isinstance(segment, dict):
                parts.append(safe_str(segment.get('text')))
            else:
                parts.append(safe_str(getattr(segment, 'text', '')))
        text = ''.join(parts)
    else:
        text = safe_str(content) or safe_str(getattr(message, 'reasoning_content', ''))

    return strip_code_fences(strip_reasoning_thoughts(text)).strip()


def extract_chat_message_json(message, expected_type=dict):
    """优先读取 message.parsed，失败时从文本中提取 JSON。"""
    parsed = getattr(message, 'parsed', None)
    if expected_type is None:
        if isinstance(parsed, (dict, list)):
            return parsed
    elif isinstance(parsed, expected_type):
        return parsed

    return extract_json_from_text(
        get_chat_message_text(message),
        expected_type=expected_type,
    )


_THINKING_FALLBACK_WARNED_SCENES = set()


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _mask_base_url(base_url):
    text = safe_str(base_url).strip()
    if not text:
        return 'unknown'
    try:
        parsed = urlparse(text)
        if parsed.scheme and parsed.hostname:
            if parsed.port:
                return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
            return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        pass
    return 'configured-endpoint'


def _is_thinking_param_unsupported_error(exc):
    text = safe_str(exc).lower()
    signals = (
        'unknown parameter',
        'unrecognized',
        'unsupported',
        'invalid parameter',
        'extra_body',
        'thinking',
        'reasoning_effort',
        'not permitted',
    )
    return any(sig in text for sig in signals)


def openai_chat_create_with_thinking_control(
    client,
    create_kwargs,
    thinking_enabled=False,
    logger=None,
    scene_name='unknown',
):
    """统一 chat.completions 请求，支持“尝试关闭思考 + 自动降级”策略。"""
    if _coerce_bool(thinking_enabled, default=False):
        return client.chat.completions.create(**create_kwargs)

    disabled_kwargs = copy.deepcopy(create_kwargs or {})
    extra_body = disabled_kwargs.get('extra_body')
    if not isinstance(extra_body, dict):
        extra_body = {}
    extra_body = copy.deepcopy(extra_body)
    thinking_body = extra_body.get('thinking')
    if not isinstance(thinking_body, dict):
        thinking_body = {}
    thinking_body.update({'type': 'disabled', 'enabled': False})
    extra_body['thinking'] = thinking_body
    disabled_kwargs['extra_body'] = extra_body

    try:
        return client.chat.completions.create(**disabled_kwargs)
    except Exception as exc:
        if not _is_thinking_param_unsupported_error(exc):
            raise

        model_name = safe_str((create_kwargs or {}).get('model'), default='unknown')
        endpoint_label = _mask_base_url(getattr(client, 'base_url', None))
        scene = safe_str(scene_name, default='unknown')
        warn_key = f"{scene}:{model_name}:{endpoint_label}"
        if logger:
            if warn_key not in _THINKING_FALLBACK_WARNED_SCENES:
                logger.warning(
                    "模型不支持 thinking 控制参数，已降级为普通请求"
                )
                _THINKING_FALLBACK_WARNED_SCENES.add(warn_key)
            else:
                logger.debug(
                    "thinking 控制参数不受支持，继续普通请求"
                )
        return client.chat.completions.create(**create_kwargs)
