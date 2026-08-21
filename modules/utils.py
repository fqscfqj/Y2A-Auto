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
import logging
import threading
import time
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

    if isinstance(message, dict):
        content = message.get('content')
        reasoning_content = message.get('reasoning_content')
    else:
        content = getattr(message, 'content', None)
        reasoning_content = getattr(message, 'reasoning_content', None)
    if isinstance(content, list):
        parts = []
        for segment in content:
            if isinstance(segment, dict):
                segment_text = segment.get('text', '')
                if isinstance(segment_text, dict):
                    segment_text = segment_text.get('value', '')
                parts.append(safe_str(segment_text))
            else:
                parts.append(safe_str(getattr(segment, 'text', '')))
        text = ''.join(parts)
    elif isinstance(content, (dict, tuple)):
        try:
            text = json.dumps(content, ensure_ascii=False)
        except Exception:
            text = safe_str(content)
    else:
        text = safe_str(content) or safe_str(reasoning_content)

    return strip_code_fences(strip_reasoning_thoughts(text)).strip()


def extract_chat_message_json(message, expected_type=dict):
    """优先读取 message.parsed，失败时从文本中提取 JSON。"""
    parsed = message.get('parsed') if isinstance(message, dict) else getattr(message, 'parsed', None)
    if expected_type is None:
        if isinstance(parsed, (dict, list)):
            return parsed
    elif isinstance(parsed, expected_type):
        return parsed

    return extract_json_from_text(
        get_chat_message_text(message),
        expected_type=expected_type,
    )


_OPENAI_COMPATIBILITY_CACHE = {}
_OPENAI_COMPATIBILITY_CACHE_MAX = 256
_OPENAI_COMPATIBILITY_CACHE_TTL_SECONDS = 3600
_OPENAI_COMPATIBILITY_WARNED = set()
_OPENAI_COMPATIBILITY_LOCK = threading.Lock()
_OPENAI_URL_LOGGER = logging.getLogger(__name__)


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def normalize_openai_base_url(base_url) -> str:
    """接受 API 根地址或完整 Chat Completions 地址，统一为 SDK 所需根地址。"""
    value = safe_str(base_url).strip()
    if not value:
        return ''
    if urlparse(value).query:
        _OPENAI_URL_LOGGER.warning(
            'OpenAI Base URL 含查询参数，无法安全规范化，已保持原样'
        )
        return value
    value = value.rstrip('/')
    return re.sub(r'/chat/completions$', '', value, flags=re.IGNORECASE).rstrip('/')


def _compatibility_error_text(exc) -> str:
    """汇总 OpenAI SDK/兼容网关异常中的可诊断文本。"""
    parts = [safe_str(exc)]
    for attr in ('body', 'message', 'code', 'param', 'type'):
        value = getattr(exc, attr, None)
        if value in (None, ''):
            continue
        if isinstance(value, (dict, list, tuple)):
            try:
                parts.append(json.dumps(value, ensure_ascii=False))
            except Exception:
                parts.append(safe_str(value))
        else:
            parts.append(safe_str(value))
    return ' '.join(parts).lower()


def _is_parameter_compatibility_error(exc, parameter: str) -> bool:
    """仅识别明确指向某参数/消息角色的 4xx 兼容性错误。"""
    status_code = getattr(exc, 'status_code', None)
    if status_code is not None:
        try:
            if int(status_code) not in (400, 422):
                return False
        except Exception:
            pass

    text = _compatibility_error_text(exc)
    rejection_signals = (
        'unsupported', 'not supported', 'unknown parameter', 'unknown field',
        'unrecognized', 'invalid parameter', 'invalid_request', 'not permitted',
        'extra inputs are not permitted', 'does not support', 'not allowed',
    )
    if not any(signal in text for signal in rejection_signals):
        return False

    parameter_signals = {
        'thinking': ('thinking', 'enable_thinking'),
        'response_format': ('response_format', 'response format', 'json_object', 'json mode'),
        'max_tokens': ('max_tokens', 'max tokens'),
        'max_completion_tokens': ('max_completion_tokens', 'max completion tokens'),
        'temperature': ('temperature',),
        'system_role': (
            'system role', "role 'system'", 'role: system', 'messages[0].role',
            "'system' with this model", 'system message',
        ),
        'developer_role': (
            'developer role', "role 'developer'", 'role: developer',
            'messages[0].role', "'developer' with this model", 'developer message',
        ),
    }
    return any(signal in text for signal in parameter_signals.get(parameter, (parameter,)))


def _is_generic_schema_compatibility_error(exc) -> bool:
    """识别未指出具体字段的请求 schema 错误，用于最后的最小请求兜底。"""
    status_code = getattr(exc, 'status_code', None)
    if status_code is not None:
        try:
            if int(status_code) not in (400, 422):
                return False
        except Exception:
            pass
    text = _compatibility_error_text(exc)
    if any(signal in text for signal in (
        'unauthorized', 'forbidden', 'api key', 'rate limit', 'quota',
        'internal server error', 'service unavailable', 'bad gateway',
    )):
        return False
    if re.search(r'(?:error code|status|http)\D{0,12}5\d{2}\b', text):
        return False
    return any(signal in text for signal in (
        'param incorrect', 'parameter incorrect', 'invalid request',
        'invalid_request', 'schema validation', 'validation error',
        'request schema', 'malformed request', 'invalid payload',
        'extra fields not permitted', 'extra inputs are not permitted',
        'unprocessable entity',
    ))


def _client_compatibility_key(client, create_kwargs) -> str:
    endpoint_value = safe_str(getattr(client, 'base_url', '')).strip()
    try:
        parsed = urlparse(endpoint_value)
        if parsed.scheme and parsed.netloc:
            endpoint = f'{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip("/")}'
        else:
            endpoint = endpoint_value or 'unknown'
    except Exception:
        endpoint = endpoint_value or 'unknown'
    model = safe_str((create_kwargs or {}).get('model'), default='unknown').strip().lower()
    return f'{endpoint}:{model}'


def _thinking_control_style(client, create_kwargs) -> str:
    """仅对已知官方端点和匹配模型注入非标准思考开关。"""
    endpoint = safe_str(getattr(client, 'base_url', '')).strip()
    try:
        hostname = (urlparse(endpoint).hostname or '').lower()
    except Exception:
        hostname = ''
    model = safe_str((create_kwargs or {}).get('model')).lower()

    def host_matches(*domains):
        return any(
            hostname == domain or hostname.endswith(f'.{domain}')
            for domain in domains
        )

    def model_matches(family):
        return any(
            part == family or part.startswith(family)
            for part in re.split(r'[/.:_-]+', model)
            if part
        )

    if host_matches('aliyuncs.com') and model_matches('qwen'):
        return 'qwen'
    if host_matches('api.xiaomimimo.com') and model_matches('mimo'):
        return 'mimo'
    if host_matches('api.deepseek.com') and model_matches('deepseek'):
        return 'thinking_object'
    return ''


def _inject_thinking_control(create_kwargs, style: str):
    extra_body = create_kwargs.get('extra_body')
    if not isinstance(extra_body, dict):
        extra_body = {}
    extra_body = copy.deepcopy(extra_body)
    if style == 'qwen':
        extra_body['enable_thinking'] = False
    elif style in {'thinking_object', 'mimo'}:
        thinking_body = extra_body.get('thinking')
        if not isinstance(thinking_body, dict):
            thinking_body = {}
        thinking_body = copy.deepcopy(thinking_body)
        thinking_body['type'] = 'disabled'
        if style == 'thinking_object':
            # 保留项目既有 DeepSeek 兼容字段；MiMo 使用更严格的 type 形态。
            thinking_body['enabled'] = False
        extra_body['thinking'] = thinking_body
    if extra_body:
        create_kwargs['extra_body'] = extra_body


def _get_cached_compatibility_actions(cache_key: str):
    with _OPENAI_COMPATIBILITY_LOCK:
        entry = _OPENAI_COMPATIBILITY_CACHE.get(cache_key)
        if not entry:
            return set()
        actions, expires_at = entry
        if expires_at <= time.monotonic():
            _OPENAI_COMPATIBILITY_CACHE.pop(cache_key, None)
            return set()
        return set(actions)


def _cache_compatibility_actions(cache_key: str, discovered_actions) -> None:
    """仅在降级请求成功后记录端点/模型能力。"""
    discovered_actions = set(discovered_actions or ())
    with _OPENAI_COMPATIBILITY_LOCK:
        now = time.monotonic()
        entry = _OPENAI_COMPATIBILITY_CACHE.get(cache_key)
        if entry and entry[1] > now:
            actions = set(entry[0])
        else:
            actions = set()
            _OPENAI_COMPATIBILITY_CACHE.pop(cache_key, None)
        actions.update(discovered_actions)
        if 'use_max_completion_tokens' in discovered_actions:
            actions.discard('use_max_tokens')
        elif 'use_max_tokens' in discovered_actions:
            actions.discard('use_max_completion_tokens')
        if 'inline_instructions' in discovered_actions or 'minimal_request' in discovered_actions:
            actions.discard('use_developer_role')
        if (
            cache_key not in _OPENAI_COMPATIBILITY_CACHE
            and len(_OPENAI_COMPATIBILITY_CACHE) >= _OPENAI_COMPATIBILITY_CACHE_MAX
        ):
            _OPENAI_COMPATIBILITY_CACHE.clear()
        _OPENAI_COMPATIBILITY_CACHE[cache_key] = (
            actions,
            now + _OPENAI_COMPATIBILITY_CACHE_TTL_SECONDS,
        )


def _drop_thinking_control(create_kwargs):
    extra_body = create_kwargs.get('extra_body')
    if not isinstance(extra_body, dict):
        return
    extra_body = copy.deepcopy(extra_body)
    extra_body.pop('thinking', None)
    extra_body.pop('enable_thinking', None)
    chat_template_kwargs = extra_body.get('chat_template_kwargs')
    if isinstance(chat_template_kwargs, dict) and 'enable_thinking' in chat_template_kwargs:
        chat_template_kwargs = copy.deepcopy(chat_template_kwargs)
        chat_template_kwargs.pop('enable_thinking', None)
        if chat_template_kwargs:
            extra_body['chat_template_kwargs'] = chat_template_kwargs
        else:
            extra_body.pop('chat_template_kwargs', None)
    if extra_body:
        create_kwargs['extra_body'] = extra_body
    else:
        create_kwargs.pop('extra_body', None)


def _replace_instruction_role(create_kwargs, source_role: str, target_role: str):
    messages = copy.deepcopy(create_kwargs.get('messages') or [])
    for message in messages:
        if isinstance(message, dict) and message.get('role') == source_role:
            message['role'] = target_role
    create_kwargs['messages'] = messages


def _inline_instruction_messages(create_kwargs):
    """为不支持 system/developer role 的旧网关把指令合并到首条 user 消息。"""
    messages = copy.deepcopy(create_kwargs.get('messages') or [])
    instructions = []
    remaining = []
    for message in messages:
        if isinstance(message, dict) and message.get('role') in {'system', 'developer'}:
            instructions.append(safe_str(message.get('content')).strip())
        else:
            remaining.append(message)
    prefix = '\n\n'.join(item for item in instructions if item)
    if prefix:
        for message in remaining:
            if isinstance(message, dict) and message.get('role') == 'user':
                message['content'] = f"{prefix}\n\n{safe_str(message.get('content'))}"
                break
        else:
            remaining.insert(0, {'role': 'user', 'content': prefix})
    create_kwargs['messages'] = remaining


def _apply_compatibility_actions(create_kwargs, actions):
    adapted = copy.deepcopy(create_kwargs or {})
    if 'minimal_request' in actions:
        minimal = {
            'model': adapted.get('model'),
            'messages': adapted.get('messages') or [],
        }
        _inline_instruction_messages(minimal)
        return minimal
    if 'drop_thinking' in actions:
        _drop_thinking_control(adapted)
    if 'drop_response_format' in actions:
        adapted.pop('response_format', None)
    if 'use_max_completion_tokens' in actions and 'max_tokens' in adapted:
        adapted['max_completion_tokens'] = adapted.pop('max_tokens')
    if 'use_max_tokens' in actions and 'max_completion_tokens' in adapted:
        adapted['max_tokens'] = adapted.pop('max_completion_tokens')
    if 'drop_temperature' in actions:
        adapted.pop('temperature', None)
    if 'inline_instructions' in actions:
        _inline_instruction_messages(adapted)
    elif 'use_developer_role' in actions:
        _replace_instruction_role(adapted, 'system', 'developer')
    return adapted


def _warn_compatibility_fallback(logger, cache_key: str, action: str, scene_name: str):
    scene = safe_str(scene_name, default='unknown').strip() or 'unknown'
    warn_key = f'{cache_key}:{scene}:{action}'
    with _OPENAI_COMPATIBILITY_LOCK:
        is_new = warn_key not in _OPENAI_COMPATIBILITY_WARNED
        if is_new:
            if len(_OPENAI_COMPATIBILITY_WARNED) >= _OPENAI_COMPATIBILITY_CACHE_MAX * 4:
                _OPENAI_COMPATIBILITY_WARNED.clear()
            _OPENAI_COMPATIBILITY_WARNED.add(warn_key)
    if not logger or not is_new:
        return
    descriptions = {
        'drop_thinking': '不支持当前模型的思考控制参数，已移除该扩展参数',
        'drop_response_format': '不支持 JSON response_format，已改用提示词约束并解析文本 JSON',
        'use_max_completion_tokens': '不支持 max_tokens，已改用 max_completion_tokens',
        'use_max_tokens': '不支持 max_completion_tokens，已回退 max_tokens',
        'drop_temperature': '不支持自定义 temperature，已使用模型默认值',
        'use_developer_role': '不支持 system role，已改用 developer role',
        'inline_instructions': '不支持独立指令角色，已将指令合并到 user 消息',
        'minimal_request': '网关仅返回通用 schema 错误，已降级为最小标准请求',
    }
    logger.warning('模型兼容降级[%s]：%s', scene, descriptions.get(action, action))


def openai_chat_create_with_thinking_control(
    client,
    create_kwargs,
    thinking_enabled=False,
    logger=None,
    scene_name='unknown',
):
    """统一 Chat Completions 请求，并按端点实际能力自动降级可选参数。

    默认只对可识别的 DeepSeek、Qwen、MiMo 端点/模型发送对应的私有思考开关，
    避免污染其他标准 OpenAI 请求。若兼容网关明确拒绝 JSON 模式、token 参数、
    temperature 或消息角色，会只移除/替换对应能力后重试；未说明字段的 schema
    错误则最终降为最小标准请求。只有真正成功的组合才会缓存到同一端点+模型。
    鉴权、限流、配额和服务端错误不会在这里被误判为兼容问题。
    """
    base_kwargs = copy.deepcopy(create_kwargs or {})
    if not _coerce_bool(thinking_enabled, default=False):
        _inject_thinking_control(
            base_kwargs,
            _thinking_control_style(client, base_kwargs),
        )

    cache_key = _client_compatibility_key(client, base_kwargs)
    cached_actions = _get_cached_compatibility_actions(cache_key)
    actions = set(cached_actions)
    discovered_actions = set()
    attempted_actions = set()

    while True:
        request_kwargs = _apply_compatibility_actions(base_kwargs, actions)
        try:
            response = client.chat.completions.create(**request_kwargs)
            if discovered_actions:
                _cache_compatibility_actions(cache_key, discovered_actions)
            return response
        except Exception as exc:
            action = None
            extra_body = request_kwargs.get('extra_body')
            if (
                isinstance(extra_body, dict)
                and (
                    'thinking' in extra_body
                    or 'enable_thinking' in extra_body
                    or 'enable_thinking' in (extra_body.get('chat_template_kwargs') or {})
                )
                and _is_parameter_compatibility_error(exc, 'thinking')
            ):
                action = 'drop_thinking'
            elif (
                'response_format' in request_kwargs
                and _is_parameter_compatibility_error(exc, 'response_format')
            ):
                action = 'drop_response_format'
            elif (
                'max_tokens' in request_kwargs
                and _is_parameter_compatibility_error(exc, 'max_tokens')
            ):
                action = 'use_max_completion_tokens'
            elif (
                'max_completion_tokens' in request_kwargs
                and _is_parameter_compatibility_error(exc, 'max_completion_tokens')
            ):
                action = 'use_max_tokens'
            elif (
                'temperature' in request_kwargs
                and _is_parameter_compatibility_error(exc, 'temperature')
            ):
                action = 'drop_temperature'
            else:
                roles = {
                    message.get('role') for message in request_kwargs.get('messages', [])
                    if isinstance(message, dict)
                }
                if (
                    'system' in roles
                    and _is_parameter_compatibility_error(exc, 'system_role')
                ):
                    action = 'use_developer_role'
                elif (
                    'developer' in roles
                    and _is_parameter_compatibility_error(exc, 'developer_role')
                ):
                    action = 'inline_instructions'
                elif _is_generic_schema_compatibility_error(exc):
                    action = 'minimal_request'

            if action is None or action in attempted_actions or action in actions:
                raise

            attempted_actions.add(action)
            actions.add(action)
            discovered_actions.add(action)
            if action == 'use_max_completion_tokens':
                actions.discard('use_max_tokens')
                discovered_actions.discard('use_max_tokens')
            elif action == 'use_max_tokens':
                actions.discard('use_max_completion_tokens')
                discovered_actions.discard('use_max_completion_tokens')
            if action == 'inline_instructions':
                actions.discard('use_developer_role')
                discovered_actions.discard('use_developer_role')
            elif action == 'minimal_request':
                actions.discard('use_developer_role')
                discovered_actions.discard('use_developer_role')
            _warn_compatibility_fallback(logger, cache_key, action, scene_name)
