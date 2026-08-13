#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
将多种浏览器导出格式（表格型 / 简易制表符 / Netscape 标准）尝试转换为 yt-dlp 可用的 Netscape cookies.txt。
提供命令行使用也可供程序内 import 使用。
"""
import sys
import os
import re
import datetime
import tempfile
from typing import Optional


_DOMAIN_YOUTUBE_RX = re.compile(r"(^|\.)youtube\.com$", re.IGNORECASE)


def is_netscape_file(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                ln = ln.strip()
                # explicit Netscape header
                if i == 0 and ln.startswith("# Netscape HTTP Cookie File"):
                    return True
                if not ln or ln.startswith("#"):
                    continue
                # Skip a common CSV/TSV header like: name value domain path expiration
                lower = ln.lower()
                if all(tok in lower for tok in ("name", "value", "domain")) and len(lower.split()) <= 8:
                    # treat as header line and skip
                    continue
                parts = ln.split("\t")
                if len(parts) >= 7:
                    return True
                # avoid scanning too many lines
                if i > 50:
                    break
    except Exception:
        return False
    return False


def _iso8601_to_epoch(s: str) -> str:
    try:
        # accept forms like 2027-09-14T13:19:23.436Z or without Z
        s2 = s.strip()
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s2)
        # Treat naive datetimes as UTC to avoid local tz surprises
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return str(int(dt.timestamp()))
    except Exception:
        return "0"


def convert_any_to_netscape(input_path: str, output_path: Optional[str] = None) -> Optional[str]:
    """
    尝试将任意常见导出格式转换为 Netscape cookies.txt。
    返回生成的文件路径（如果成功），失败返回 None。
    """
    if not os.path.exists(input_path):
        return None

    if is_netscape_file(input_path):
        # already netscape: copy to output_path or return original path
        if output_path:
            try:
                with open(input_path, "r", encoding="utf-8", errors="ignore") as fr, \
                     open(output_path, "w", encoding="utf-8") as fw:
                    fw.write(fr.read())
                return output_path
            except Exception:
                return None
        return input_path

    # create output temp file if not provided
    if not output_path:
        fd, output_path = tempfile.mkstemp(prefix="y2a_yt_cookies_", suffix=".txt")
        os.close(fd)

    out_lines = ["# Netscape HTTP Cookie File"]

    # heuristics parse each non-empty line
    with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
        for i, raw in enumerate(f):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Skip header lines like: name value domain path expiration
            lower = line.lower()
            if all(tok in lower for tok in ("name", "value", "domain")) and len(lower.split()) <= 8:
                continue
            parts = re.split(r"\t|,\s*|\s{2,}", line)
            # try to find domain index
            domain_idx = None
            for j, p in enumerate(parts):
                if _DOMAIN_YOUTUBE_RX.search(p.strip()):
                    domain_idx = j
                    break
            if domain_idx is None:
                for j, p in enumerate(parts):
                    if "youtube" in p.lower():
                        domain_idx = j
                        break

            # common case in your sample: name value domain path iso_expiration ...
            name = None
            value = None
            domain = ".youtube.com"
            path = "/"
            expire = "0"
            flag = "FALSE"
            secure_flag = "FALSE"

            if len(parts) >= 2 and domain_idx is None:
                # aggressive heuristic: first token is name, second token is value, next tokens may contain domain/path/expiry
                name = parts[0].strip()
                value = parts[1].strip()
                # look for domain in remaining tokens
                for p in parts[2:]:
                    if _DOMAIN_YOUTUBE_RX.search(p.strip()):
                        domain = p.strip()
                    elif p.strip().startswith("/"):
                        path = p.strip()
                    elif re.match(r"\d{4}-\d{2}-\d{2}T", p.strip()):
                        expire = _iso8601_to_epoch(p.strip())
            else:
                # if domain index found, try to map fields relative to domain index
                if domain_idx is not None:
                    # search for name/value near the beginning
                    if domain_idx >= 2:
                        # assume name and value at 0 and 1
                        name = parts[0].strip()
                        value = parts[1].strip()
                    else:
                        # fallback searching for token that looks like a cookie name (no dots/spaces)
                        for p in parts:
                            if p and " " not in p and "=" not in p and "." not in p:
                                name = p.strip()
                                # try next token as value
                                try:
                                    idx = parts.index(p)
                                    value = parts[idx + 1].strip()
                                except Exception:
                                    value = ""
                                break
                    domain = parts[domain_idx].strip()
                    # path maybe next token
                    if domain_idx + 1 < len(parts) and parts[domain_idx + 1].startswith("/"):
                        path = parts[domain_idx + 1].strip()
                    # try to find iso date
                    for p in parts:
                        if re.match(r"\d{4}-\d{2}-\d{2}T", p.strip()):
                            expire = _iso8601_to_epoch(p.strip())
                            break

            if not name:
                continue
            if value is None:
                value = ""

            if domain.startswith("."):
                flag = "TRUE"
            else:
                flag = "FALSE"

            out_lines.append("\t".join([domain, flag, path, secure_flag, expire, name, value]))

    try:
        with open(output_path, "w", encoding="utf-8") as fw:
            fw.write("\n".join(out_lines))
        return output_path
    except Exception:
        return None


def main():
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
