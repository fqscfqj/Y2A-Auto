#!/usr/bin/env python
# -*- coding: utf-8 -*-

import base64
import hashlib
import json
import logging
import os
import sys
from typing import Any, Iterable
from urllib.parse import quote, urlparse, urlunparse

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger("cookiecloud")

COOKIECLOUD_CRYPTO_AUTO = "auto"
COOKIECLOUD_CRYPTO_LEGACY = "legacy"
COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED = "aes-128-cbc-fixed"
SUPPORTED_CRYPTO_TYPES = (
    COOKIECLOUD_CRYPTO_AUTO,
    COOKIECLOUD_CRYPTO_LEGACY,
    COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED,
)
DEFAULT_CRYPTO_TYPE = COOKIECLOUD_CRYPTO_AUTO
DEFAULT_YOUTUBE_COOKIES_PATH = "cookies/yt_cookies.txt"
DEFAULT_TIMEOUT = (5, 20)
_YOUTUBE_DOMAIN_KEYWORDS = (
    "youtube.com",
    "youtu.be",
    "google.com",
)


class CookieCloudError(RuntimeError):
    """CookieCloud 集成相关错误基类。"""


class CookieCloudConfigError(CookieCloudError):
    """配置无效。"""


class CookieCloudRequestError(CookieCloudError):
    """请求远端服务失败。"""


class CookieCloudDecryptError(CookieCloudError):
    """解密失败。"""


class CookieCloudDataError(CookieCloudError):
    """返回数据不符合预期。"""


def _get_app_root_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _coerce_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_crypto_type(value: Any) -> str:
    normalized = _coerce_text(value).lower() or DEFAULT_CRYPTO_TYPE
    if normalized not in SUPPORTED_CRYPTO_TYPES:
        return DEFAULT_CRYPTO_TYPE
    return normalized


def normalize_server_url(server_url: Any) -> str:
    text = _coerce_text(server_url)
    if not text:
        raise CookieCloudConfigError("请先填写 CookieCloud 服务地址。")

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CookieCloudConfigError("CookieCloud 服务地址格式无效，仅支持 http/https。")

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        (parsed.path or "").rstrip("/"),
        "",
        "",
        "",
    ))


def validate_cookiecloud_settings(settings: dict[str, Any] | None, require_enabled: bool = True) -> dict[str, Any]:
    normalized = dict(settings or {})
    enabled = _as_bool(normalized.get("COOKIECLOUD_ENABLED", False))
    if require_enabled and not enabled:
        raise CookieCloudConfigError("请先启用 CookieCloud。")

    server_url = normalize_server_url(normalized.get("COOKIECLOUD_SERVER_URL", ""))
    uuid_value = _coerce_text(normalized.get("COOKIECLOUD_UUID", ""))
    password_value = _coerce_text(normalized.get("COOKIECLOUD_PASSWORD", ""))
    if not uuid_value:
        raise CookieCloudConfigError("请先填写 CookieCloud UUID。")
    if not password_value:
        raise CookieCloudConfigError("请先填写 CookieCloud 密码。")

    return {
        "COOKIECLOUD_ENABLED": enabled,
        "COOKIECLOUD_SERVER_URL": server_url,
        "COOKIECLOUD_UUID": uuid_value,
        "COOKIECLOUD_PASSWORD": password_value,
        "COOKIECLOUD_CRYPTO_TYPE": normalize_crypto_type(
            normalized.get("COOKIECLOUD_CRYPTO_TYPE", DEFAULT_CRYPTO_TYPE)
        ),
        "YOUTUBE_COOKIES_PATH": _coerce_text(
            normalized.get("YOUTUBE_COOKIES_PATH", DEFAULT_YOUTUBE_COOKIES_PATH)
        ) or DEFAULT_YOUTUBE_COOKIES_PATH,
    }


def build_cookiecloud_get_url(server_url: str, uuid_value: str, crypto_type: str = DEFAULT_CRYPTO_TYPE) -> str:
    normalized_server_url = normalize_server_url(server_url)
    safe_uuid = quote(_coerce_text(uuid_value), safe="")
    base_url = f"{normalized_server_url}/get/{safe_uuid}"
    normalized_crypto_type = normalize_crypto_type(crypto_type)
    if normalized_crypto_type == COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED:
        return f"{base_url}?crypto_type={COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED}"
    return base_url


def fetch_cookiecloud_payload(
    server_url: str,
    uuid_value: str,
    *,
    crypto_type: str = DEFAULT_CRYPTO_TYPE,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    request_url = build_cookiecloud_get_url(server_url, uuid_value, crypto_type=crypto_type)
    requester = session or requests
    try:
        response = requester.get(request_url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CookieCloudRequestError("CookieCloud 服务请求失败，请检查服务地址与网络连通性。") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise CookieCloudDataError("CookieCloud 返回了无法解析的 JSON 数据。") from exc

    if not isinstance(payload, dict):
        raise CookieCloudDataError("CookieCloud 返回数据格式无效。")
    if not payload.get("encrypted") and not payload.get("cookie_data"):
        raise CookieCloudDataError("CookieCloud 未返回可用的加密 Cookie 数据。")
    return payload


def _derive_key_seed(uuid_value: str, password: str) -> bytes:
    salt = uuid_value.encode("utf-8")
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        200000,
        dklen=16,
    )


def _pbkdf2_key_iv(password: bytes, salt: bytes, key_len: int, iv_len: int) -> tuple[bytes, bytes]:
    total_length = key_len + iv_len
    derived = hashlib.pbkdf2_hmac("sha256", password, salt, 200000, dklen=total_length)
    return derived[:key_len], derived[key_len:total_length]


def _aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _decrypt_legacy_payload(ciphertext: str, uuid_value: str, password: str) -> bytes:
    decoded = base64.b64decode(ciphertext)
    if len(decoded) < 17 or decoded[:8] != b"Salted__":
        raise CookieCloudDecryptError("CookieCloud legacy 密文格式无效。")
    salt = decoded[8:16]
    body = decoded[16:]
    password_seed = _derive_key_seed(uuid_value, password)
    key, iv = _pbkdf2_key_iv(password_seed, salt, key_len=32, iv_len=16)
    return _aes_cbc_decrypt(body, key, iv)


def _decrypt_fixed_iv_payload(ciphertext: str, uuid_value: str, password: str) -> bytes:
    decoded = base64.b64decode(ciphertext)
    key = _derive_key_seed(uuid_value, password)
    fixed_iv = b"\x00" * 16
    return _aes_cbc_decrypt(decoded, key, fixed_iv)


def _resolve_crypto_candidates(requested_crypto_type: str, payload: dict[str, Any] | None) -> list[str]:
    requested = normalize_crypto_type(requested_crypto_type)
    payload_crypto_type = normalize_crypto_type((payload or {}).get("crypto_type", ""))
    candidates: list[str] = []

    def _push(value: str):
        normalized = normalize_crypto_type(value)
        if normalized == COOKIECLOUD_CRYPTO_AUTO:
            return
        if normalized not in candidates:
            candidates.append(normalized)

    if requested == COOKIECLOUD_CRYPTO_AUTO:
        _push(payload_crypto_type)
        _push(COOKIECLOUD_CRYPTO_LEGACY)
        _push(COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED)
    else:
        _push(requested)
        _push(payload_crypto_type)

    if not candidates:
        candidates.append(COOKIECLOUD_CRYPTO_LEGACY)
    return candidates


def decrypt_cookiecloud_payload(
    payload: dict[str, Any],
    uuid_value: str,
    password: str,
    *,
    crypto_type: str = DEFAULT_CRYPTO_TYPE,
) -> tuple[dict[str, Any], str]:
    if not isinstance(payload, dict):
        raise CookieCloudDataError("CookieCloud 返回数据格式无效。")

    if isinstance(payload.get("cookie_data"), dict):
        return payload, normalize_crypto_type(payload.get("crypto_type", crypto_type))

    ciphertext = payload.get("encrypted")
    if not isinstance(ciphertext, str) or not ciphertext.strip():
        raise CookieCloudDataError("CookieCloud 未返回可用的加密 Cookie 数据。")

    last_error: Exception | None = None
    for candidate in _resolve_crypto_candidates(crypto_type, payload):
        try:
            if candidate == COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED:
                decrypted = _decrypt_fixed_iv_payload(ciphertext, uuid_value, password)
            else:
                decrypted = _decrypt_legacy_payload(ciphertext, uuid_value, password)
            parsed = json.loads(decrypted.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise CookieCloudDataError("CookieCloud 解密后的数据不是对象。")
            return parsed, candidate
        except Exception as exc:  # pragma: no cover - 自动回退需要保留宽口径
            last_error = exc
            continue

    raise CookieCloudDecryptError("CookieCloud 凭据无效或解密失败，请检查 UUID、密码与加密算法。") from last_error


def _iter_cookie_items(cookie_payload: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    cookie_data = cookie_payload.get("cookie_data")
    if isinstance(cookie_data, dict):
        for bucket, items in cookie_data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    yield str(bucket or ""), item
        return

    if isinstance(cookie_data, list):
        for item in cookie_data:
            if isinstance(item, dict):
                yield "", item


def _is_youtube_related_domain(domain: str, bucket: str = "") -> bool:
    candidates = [domain, bucket]
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower().lstrip(".")
        if any(keyword in normalized for keyword in _YOUTUBE_DOMAIN_KEYWORDS):
            return True
    return False


def _normalize_cookie_domain(domain: str, host_only: bool) -> tuple[str, str]:
    cleaned = _coerce_text(domain).lstrip(".")
    if not cleaned:
        return "", "FALSE"
    if host_only:
        return cleaned, "FALSE"
    return f".{cleaned}", "TRUE"


def _sanitize_cookie_value(value: Any) -> str:
    return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _extract_expiration(item: dict[str, Any]) -> int:
    for key in ("expirationDate", "expires", "expiry", "expiration"):
        raw_value = item.get(key)
        if raw_value in (None, ""):
            continue
        try:
            return max(0, int(float(str(raw_value).strip())))
        except (TypeError, ValueError):
            continue
    return 0


def build_youtube_netscape_cookies(cookie_payload: dict[str, Any]) -> tuple[str, int]:
    lines_by_key: dict[tuple[str, str, str], str] = {}
    for bucket, item in _iter_cookie_items(cookie_payload):
        domain = _coerce_text(item.get("domain") or bucket)
        if not domain or not _is_youtube_related_domain(domain, bucket):
            continue

        name = _coerce_text(item.get("name"))
        if not name:
            continue

        path = _coerce_text(item.get("path")) or "/"
        host_only = _as_bool(item.get("hostOnly", False))
        normalized_domain, include_subdomains = _normalize_cookie_domain(domain, host_only)
        if not normalized_domain:
            continue

        secure = "TRUE" if _as_bool(item.get("secure", False)) else "FALSE"
        expires = _extract_expiration(item)
        value = _sanitize_cookie_value(item.get("value", ""))
        line = "\t".join([
            normalized_domain,
            include_subdomains,
            path,
            secure,
            str(expires),
            name,
            value,
        ])
        lines_by_key[(normalized_domain, path, name)] = line

    if not lines_by_key:
        raise CookieCloudDataError("CookieCloud 中未找到可用的 YouTube / Google Cookies。")

    header = [
        "# Netscape HTTP Cookie File",
        "# Generated by Y2A-Auto CookieCloud integration.",
    ]
    body = [lines_by_key[key] for key in sorted(lines_by_key)]
    content = "\n".join(header + body) + "\n"
    return content, len(body)


def resolve_cookie_output_path(path_value: Any, default_relative_path: str = DEFAULT_YOUTUBE_COOKIES_PATH) -> str:
    raw_path = _coerce_text(path_value) or default_relative_path
    app_root = os.path.realpath(_get_app_root_dir())
    resolved = os.path.realpath(raw_path) if os.path.isabs(raw_path) else os.path.realpath(os.path.join(app_root, raw_path))
    try:
        common_path = os.path.commonpath([app_root, resolved])
    except ValueError as exc:
        raise CookieCloudConfigError("YouTube Cookies 输出路径无效。") from exc
    if common_path != app_root:
        raise CookieCloudConfigError("YouTube Cookies 输出路径必须位于项目目录内。")
    return resolved


def make_display_path(path_value: str) -> str:
    app_root = os.path.realpath(_get_app_root_dir())
    resolved = os.path.realpath(path_value)
    try:
        common_path = os.path.commonpath([app_root, resolved])
    except ValueError:
        return os.path.basename(resolved)
    if common_path != app_root:
        return os.path.basename(resolved)
    return os.path.relpath(resolved, app_root).replace("\\", "/")


def test_cookiecloud_youtube_sync(
    settings: dict[str, Any] | None,
    *,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    normalized = validate_cookiecloud_settings(settings, require_enabled=True)
    payload = fetch_cookiecloud_payload(
        normalized["COOKIECLOUD_SERVER_URL"],
        normalized["COOKIECLOUD_UUID"],
        crypto_type=normalized["COOKIECLOUD_CRYPTO_TYPE"],
        timeout=timeout,
        session=session,
    )
    decrypted, crypto_type_used = decrypt_cookiecloud_payload(
        payload,
        normalized["COOKIECLOUD_UUID"],
        normalized["COOKIECLOUD_PASSWORD"],
        crypto_type=normalized["COOKIECLOUD_CRYPTO_TYPE"],
    )
    content, cookie_count = build_youtube_netscape_cookies(decrypted)
    return {
        "content": content,
        "cookie_count": cookie_count,
        "crypto_type_used": crypto_type_used,
    }


def sync_cookiecloud_to_youtube_file(
    settings: dict[str, Any] | None,
    *,
    output_path: str | None = None,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    result = test_cookiecloud_youtube_sync(settings, timeout=timeout, session=session)
    normalized = validate_cookiecloud_settings(settings, require_enabled=True)
    target_path = resolve_cookie_output_path(
        output_path or normalized.get("YOUTUBE_COOKIES_PATH") or DEFAULT_YOUTUBE_COOKIES_PATH,
        default_relative_path=DEFAULT_YOUTUBE_COOKIES_PATH,
    )
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8", newline="\n") as file_obj:
        file_obj.write(result["content"])
    return {
        **result,
        "output_path": target_path,
        "output_path_display": make_display_path(target_path),
    }
