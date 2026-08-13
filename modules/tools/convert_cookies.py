#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""将多种浏览器导出格式转换为 yt-dlp 可用的 Netscape cookies.txt。

支持格式：
- Netscape 标准 cookies.txt（直接透传）
- 浏览器扩展导出的 JSON 数组（每项含 name/value/domain/path 等字段）
- 文本表格导出（制表符/逗号/多空格分隔，形如 "name value domain path expiration"）

命令行：
    python modules/tools/convert_cookies.py input_file [output_file]

程序内：
    from modules.tools.convert_cookies import convert_any_to_netscape
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys
import tempfile
from typing import Optional

# YouTube 域名（允许子域，如 www.youtube.com / music.youtube.com）
_DOMAIN_YOUTUBE_RX = re.compile(r"(^|\.)youtube\.com$", re.IGNORECASE)
# __Secure- / __Host- 前缀 Cookie 必须带 secure 标志
_SECURE_NAME_RX = re.compile(r"^__(?:Secure|Host)-", re.IGNORECASE)
# ISO 时间戳（如 2027-09-14T13:19:23.436Z）
_ISO_TS_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T")
# Netscape 字段中的制表符/换行会破坏行列结构，可能被用于注入伪造 cookie，必须清理
_FIELD_CONTROL_CHARS_RX = re.compile(r"[\t\r\n]+")
# curl/http.cookiejar 导出的 HttpOnly cookie 行前缀，去掉后即为标准 Netscape 数据行
_HTTPONLY_PREFIX = "#HttpOnly_"


def _strip_httponly_prefix(ln: str) -> str:
    """去掉 #HttpOnly_ 前缀（带 HttpOnly 标志的合法 Netscape 数据行）。"""
    if ln.startswith(_HTTPONLY_PREFIX):
        return ln[len(_HTTPONLY_PREFIX):]
    return ln


def _sanitize_field(value: object) -> str:
    """移除字段中的制表符/换行符，避免破坏 Netscape 文件结构或注入伪造 cookie。"""
    if value is None:
        return ""
    return _FIELD_CONTROL_CHARS_RX.sub("", str(value))


def _to_bool(value: object) -> bool:
    """把常见布尔表示解析为 bool。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _looks_like_epoch(value: str) -> bool:
    """判断字符串是否为有限数字（Unix 时间戳）；inf/nan 等非有限值不算。"""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _iso8601_to_epoch(s: str) -> str:
    """ISO 8601 时间转 Unix 时间戳；无法解析时返回 '0'（会话 cookie）。"""
    try:
        s2 = s.strip()
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s2)
        # 无时区的朴素时间按 UTC 处理，避免本地时区偏差
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return str(int(dt.timestamp()))
    except Exception:
        return "0"


def _to_epoch_str(value: object) -> str:
    """把数字（秒/毫秒）或 ISO 时间统一为 Unix 秒字符串。"""
    if value is None or value == "":
        return "0"
    text = str(value).strip()
    if not text:
        return "0"
    if _looks_like_epoch(text):
        try:
            num = float(text)
            if abs(num) > 10_000_000_000:  # 毫秒时间戳换算成秒
                num /= 1000.0
            return str(int(num))
        except (ValueError, OverflowError):
            pass
    return _iso8601_to_epoch(text)


def _clean_domain(value: object) -> str:
    """去除 domain 可能附带的 scheme 与路径，保留前导点。"""
    d = _sanitize_field(value).strip()
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0]
    return d


def _render_netscape_line(
    domain: object,
    name: object,
    value: object,
    path: object = "/",
    secure: object = None,
    expiration: object = None,
    include_subdomains: object = None,
) -> Optional[str]:
    """把一条 cookie 渲染为 Netscape 7 列制表符行；字段非法时返回 None。"""
    domain = _clean_domain(domain)
    name = _sanitize_field(name)
    value = _sanitize_field(value)
    path = _sanitize_field(path) or "/"

    if not domain or not name:
        return None
    # 不含点的 domain 不是合法 cookie 域，安全起见丢弃
    if "." not in domain:
        return None
    if not path.startswith("/"):
        path = "/" + path

    if include_subdomains is None:
        include_subdomains = domain.startswith(".")
    include_flag = "TRUE" if _to_bool(include_subdomains) else "FALSE"

    if secure is None:
        secure = bool(_SECURE_NAME_RX.match(name))
    secure_flag = "TRUE" if _to_bool(secure) else "FALSE"

    expire = _to_epoch_str(expiration)
    return "\t".join([domain, include_flag, path, secure_flag, expire, name, value])


def _looks_like_netscape_row(parts) -> bool:
    """判断一个 tab 分割的行是否符合 Netscape 7 列结构。

    Netscape 列序：domain includeSubdomains path secure expiry name value
    - 第 2 列(parts[1])与第 4 列(parts[3])必须为 TRUE/FALSE（忽略大小写）
    - 第 3 列(parts[2])必须以 / 开头
    - 第 5 列(parts[4])必须是数字（epoch，可为 0）
    """
    if len(parts) < 7:
        return False
    if parts[1].upper() not in {"TRUE", "FALSE"}:
        return False
    if parts[3].upper() not in {"TRUE", "FALSE"}:
        return False
    if not parts[2].startswith("/"):
        return False
    return _looks_like_epoch(parts[4])


def is_netscape_file(path: str) -> bool:
    """判断文件是否已经是 Netscape cookies.txt 格式。

    头部（# Netscape HTTP Cookie File）本身不足以判定有效；必须至少存在一行
    符合 Netscape 7 列结构的数据行才返回 True。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                ln = ln.strip()
                if not ln:
                    continue
                # 普通注释行跳过；但 #HttpOnly_ 前缀是带 HttpOnly 标志的合法 Netscape 数据行
                if ln.startswith("#") and not ln.startswith(_HTTPONLY_PREFIX):
                    continue
                ln = _strip_httponly_prefix(ln)
                # 跳过形如 "name value domain ..." 的表头
                lower = ln.lower()
                if lower.startswith("name") and "value" in lower and "domain" in lower:
                    continue
                if _looks_like_netscape_row(ln.split("\t")):
                    return True
                if i > 50:
                    break
    except Exception:
        return False
    return False


def _try_parse_json_records(text: str) -> Optional[list]:
    """尝试把文本解析为 JSON（数组或单对象）；失败返回 None。"""
    stripped = text.strip()
    if not (stripped.startswith("[") or stripped.startswith("{")):
        return None
    try:
        data = json.loads(stripped)
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    records = [item for item in data if isinstance(item, dict)]
    return records or None


def _parse_text_cookie_line(line: str) -> Optional[dict]:
    """解析一条文本 cookie 行，返回字段 dict 或 None。

    仅支持文档化列序 "name value domain ..."（domain 至少在第 3 列，即
    domain_idx >= 2）。path 取 domain 后一列（若以 / 开头）；过期时间从
    path 之后的第一列起，从左往右取第一个匹配 ISO 时间戳或有限数字的字段，
    避免右侧的 httpOnly/secure 等标志覆盖真实过期时间。
    """
    if "\t" in line:
        parts = [p.strip() for p in line.split("\t")]
    elif "," in line:
        parts = [p.strip() for p in line.split(",")]
    else:
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) == 1:
            parts = re.split(r"\s+", line.strip())
    parts = [p for p in parts if p != ""]
    if len(parts) < 3:
        return None

    # 定位 domain（YouTube 域名）
    domain_idx = None
    for j, p in enumerate(parts):
        if _DOMAIN_YOUTUBE_RX.search(p):
            domain_idx = j
            break

    # 只支持 "name value domain ..." 列序，不支持的列序直接丢弃
    if domain_idx is None or domain_idx < 2:
        return None

    name = parts[0]
    value = parts[1]
    domain = parts[domain_idx]
    path = "/"
    expiration: Optional[str] = None

    # path 列：domain 后一列以 / 开头则视为 path
    scan_start = domain_idx + 1
    if scan_start < len(parts) and parts[scan_start].startswith("/"):
        path = parts[scan_start]
        scan_start += 1

    # 过期时间：path 之后（无 path 则从 domain 之后）从左往右取第一个 ISO/epoch 字段
    for p in parts[scan_start:]:
        if _ISO_TS_RX.match(p) or _looks_like_epoch(p):
            expiration = p
            break

    if not name or domain is None:
        return None
    return {
        "domain": domain,
        "name": name,
        "value": value or "",
        "path": path,
        "expiration": expiration,
    }


def convert_to_netscape_lines(text: str) -> Optional[list]:
    """把文本（JSON 或文本表格）转换为 Netscape 行列表。

    第一行固定为 "# Netscape HTTP Cookie File"；没有任何有效 cookie 记录时返回 None。
    """
    out_lines = ["# Netscape HTTP Cookie File"]

    json_records = _try_parse_json_records(text)
    if json_records is not None:
        for item in json_records:
            domain = item.get("domain") or item.get("host") or ".youtube.com"
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            path = item.get("path", "/")
            secure = item.get("secure")
            expiration = item.get("expirationDate") or item.get("expires") or item.get("expiration")
            host_only = item.get("hostOnly")
            include_subdomains = (not host_only) if host_only is not None else domain.startswith(".")
            line = _render_netscape_line(
                domain, name, value, path=path, secure=secure,
                expiration=expiration, include_subdomains=include_subdomains,
            )
            if line:
                out_lines.append(line)
    else:
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            # 普通注释行跳过；#HttpOnly_ 行去掉前缀后按 Netscape 数据行透传
            if line.startswith("#"):
                if not line.startswith(_HTTPONLY_PREFIX):
                    continue
                stripped = _strip_httponly_prefix(line)
                if _looks_like_netscape_row(stripped.split("\t")):
                    out_lines.append(stripped)
                continue
            # 跳过表头
            lower = line.lower()
            if lower.startswith("name") and "value" in lower and "domain" in lower:
                continue
            parsed = _parse_text_cookie_line(line)
            if not parsed:
                continue
            rendered = _render_netscape_line(
                parsed["domain"], parsed["name"], parsed["value"],
                path=parsed["path"], expiration=parsed["expiration"],
            )
            if rendered:
                out_lines.append(rendered)

    if len(out_lines) <= 1:
        return None
    return out_lines


def convert_any_to_netscape(input_path: str, output_path: Optional[str] = None) -> Optional[str]:
    """尝试将任意常见导出格式转换为 Netscape cookies.txt。

    返回生成的文件路径（成功），失败返回 None。
    """
    if not os.path.isfile(input_path):
        return None

    # 已经是 Netscape 格式时直接透传
    if is_netscape_file(input_path):
        if output_path:
            try:
                with open(input_path, "r", encoding="utf-8", errors="ignore") as fr, \
                     open(output_path, "w", encoding="utf-8") as fw:
                    fw.write(fr.read())
                return output_path
            except Exception:
                return None
        return input_path

    try:
        with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
            raw_text = f.read()
    except Exception:
        return None

    out_lines = convert_to_netscape_lines(raw_text)
    if out_lines is None:
        # 没有有效 cookie 记录：清理可能已创建的输出文件（如调用方预建的空文件），返回 None
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        return None

    # 生成输出文件（未指定时使用临时文件）
    created_tmp = False
    if not output_path:
        fd, output_path = tempfile.mkstemp(prefix="y2a_yt_cookies_", suffix=".txt")
        os.close(fd)
        created_tmp = True

    try:
        with open(output_path, "w", encoding="utf-8") as fw:
            fw.write("\n".join(out_lines))
        return output_path
    except Exception:
        if created_tmp and output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: convert_cookies.py input_file [output_file]")
        sys.exit(1)
    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) >= 3 else None
    res = convert_any_to_netscape(inp, out)
    if res:
        print("Converted ->", res)
        sys.exit(0)
    print("Conversion failed")
    sys.exit(2)


if __name__ == "__main__":
    main()
