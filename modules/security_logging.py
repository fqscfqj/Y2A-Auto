#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED_VALUE = "***REDACTED***"

SENSITIVE_KEY_TOKENS = (
    "password",
    "passwd",
    "pwd",
    "api_key",
    "apikey",
    "secret",
    "token",
    "cookie",
    "access_key",
    "authorization",
    "auth",
)

DEFAULT_SENSITIVE_SUMMARY_KEYS = (
    "password",
    "ACFUN_PASSWORD",
    "OPENAI_API_KEY",
    "SUBTITLE_OPENAI_API_KEY",
    "SUBTITLE_QC_API_KEY",
    "WHISPER_API_KEY",
    "VOXTRAL_API_KEY",
    "FIREREDASR_API_KEY",
    "ALIYUN_ACCESS_KEY_ID",
    "ALIYUN_ACCESS_KEY_SECRET",
    "YOUTUBE_API_KEY",
    "YOUTUBE_PROXY_USERNAME",
    "YOUTUBE_PROXY_PASSWORD",
    "YOUTUBE_API_PROXY_USERNAME",
    "YOUTUBE_API_PROXY_PASSWORD",
)

DEFAULT_FEATURE_FLAG_KEYS = (
    "AUTO_MODE_ENABLED",
    "password_protection_enabled",
    "CONTENT_MODERATION_ENABLED",
    "YOUTUBE_PROXY_ENABLED",
    "YOUTUBE_API_PROXY_ENABLED",
    "SUBTITLE_TRANSLATION_ENABLED",
    "SPEECH_RECOGNITION_ENABLED",
)

DEFAULT_PATH_KEYS = (
    "YOUTUBE_COOKIES_PATH",
    "ACFUN_COOKIES_PATH",
    "BILIBILI_COOKIES_PATH",
    "YOUTUBE_PROXY_URL",
    "YOUTUBE_API_PROXY_URL",
)


def _has_effective_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return bool(str(value).strip())


def _mask_value(value: Any) -> str:
    return REDACTED_VALUE if _has_effective_value(value) else ""


def is_sensitive_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(token in lowered for token in SENSITIVE_KEY_TOKENS)


def redact_config_for_logging(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """递归脱敏配置数据，避免敏感值出现在日志中。"""

    def _redact(key: str, value: Any) -> Any:
        if is_sensitive_key(key):
            return _mask_value(value)
        if isinstance(value, Mapping):
            return {str(k): _redact(str(k), v) for k, v in value.items()}
        if isinstance(value, list):
            return [_redact(key, item) for item in value]
        return value

    normalized = dict(config or {})
    return {str(key): _redact(str(key), value) for key, value in normalized.items()}


def strip_url_credentials(url: str | None) -> str:
    """移除 URL 中的 userinfo，仅保留协议、主机、端口及其余非凭据部分。"""
    normalized = str(url or "").strip()
    if not normalized:
        return ""

    parsed = urlsplit(normalized)
    if not parsed.scheme or not parsed.hostname:
        return normalized

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{host}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def describe_network_mode(
    proxy_url: str | None,
    direct_description: str = "直连（不继承环境变量代理）",
) -> str:
    """返回适合日志展示的网络模式描述，不包含凭据。"""
    sanitized = strip_url_credentials(proxy_url)
    if not sanitized:
        return direct_description

    parsed = urlsplit(sanitized)
    if not parsed.scheme or not parsed.hostname:
        return "代理已启用（地址格式异常）"

    authority = parsed.netloc
    return f"代理 {parsed.scheme}://{authority}"


def build_config_summary_for_logging(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    构建脱敏后的配置摘要：
    - feature_flags: 开关状态
    - credentials_configured: 敏感字段是否已配置
    - paths_configured: 关键路径/地址是否已配置
    """
    normalized = dict(config or {})

    feature_flags = {
        key: bool(normalized.get(key, False))
        for key in DEFAULT_FEATURE_FLAG_KEYS
    }
    credentials_configured = {
        key: _has_effective_value(normalized.get(key))
        for key in DEFAULT_SENSITIVE_SUMMARY_KEYS
    }
    paths_configured = {
        key: _has_effective_value(normalized.get(key))
        for key in DEFAULT_PATH_KEYS
    }

    return {
        "feature_flags": feature_flags,
        "credentials_configured": credentials_configured,
        "credentials_configured_count": sum(
            1 for configured in credentials_configured.values() if configured
        ),
        "paths_configured": paths_configured,
        "config_keys_total": len(normalized),
    }

