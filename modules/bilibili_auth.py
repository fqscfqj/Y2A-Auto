#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import base64
import json
import logging
import os
import time
from typing import Dict, Optional, Tuple

from bilibili_api import login_v2
from bilibili_api.exceptions import ArgsException
from bilibili_api.utils.network import Credential


logger = logging.getLogger("bilibili_auth")


def _run_async(coro):
    """Run async coroutine in sync context."""
    return asyncio.run(coro)


def _parse_cookies_text(content: str) -> Dict[str, str]:
    content = (content or "").strip()
    if not content:
        return {}

    cookies: Dict[str, str] = {}

    # Netscape format
    if content.startswith("# Netscape HTTP Cookie File") or "\t" in content:
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name = str(parts[5]).strip()
                value = str(parts[6]).strip()
                if name:
                    cookies[name] = value
        return cookies

    # JSON format
    data = json.loads(content)
    if isinstance(data, dict):
        # 兼容 {"cookies": [{"name":"...","value":"..."}]} 结构
        cookies_list = data.get("cookies")
        if isinstance(cookies_list, list):
            for item in cookies_list:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if name is None or value is None:
                    continue
                cookies[str(name)] = str(value)
            if cookies:
                return cookies
        for k, v in data.items():
            if isinstance(v, (str, int, float)):
                cookies[str(k)] = str(v)
        return cookies

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name is None or value is None:
                continue
            cookies[str(name)] = str(value)
        return cookies

    return cookies


def load_cookie_dict(cookie_file: str) -> Dict[str, str]:
    if not cookie_file or not os.path.exists(cookie_file):
        raise FileNotFoundError(f"Cookie 文件不存在: {cookie_file}")
    with open(cookie_file, "r", encoding="utf-8") as f:
        content = f.read()
    cookies = _parse_cookies_text(content)
    if not cookies:
        raise ValueError("Cookie 文件为空或格式不正确")
    return cookies


def build_credential(cookies: Dict[str, str]) -> Credential:
    cookies = cookies or {}
    return Credential(
        sessdata=cookies.get("SESSDATA") or cookies.get("sessdata"),
        bili_jct=cookies.get("bili_jct") or cookies.get("biliJct"),
        dedeuserid=cookies.get("DedeUserID") or cookies.get("dedeuserid"),
        buvid3=cookies.get("buvid3"),
        buvid4=cookies.get("buvid4"),
        ac_time_value=cookies.get("ac_time_value"),
    )


def validate_credential(credential: Credential) -> Tuple[bool, str]:
    missing = []
    try:
        if not credential.has_sessdata():
            missing.append("SESSDATA")
    except Exception:
        missing.append("SESSDATA")
    try:
        if not credential.has_bili_jct():
            missing.append("bili_jct")
    except Exception:
        missing.append("bili_jct")
    try:
        if not credential.has_dedeuserid():
            missing.append("DedeUserID")
    except Exception:
        missing.append("DedeUserID")

    if missing:
        return False, f"缺少必要 Cookie 字段: {', '.join(missing)}"
    return True, "credential 有效"


def load_credential_from_file(cookie_file: str) -> Credential:
    cookies = load_cookie_dict(cookie_file)
    credential = build_credential(cookies)
    ok, msg = validate_credential(credential)
    if not ok:
        raise ValueError(msg)
    return credential


def save_credential_to_file(credential: Credential, cookie_file: str) -> bool:
    if not cookie_file:
        return False

    os.makedirs(os.path.dirname(cookie_file) or ".", exist_ok=True)

    try:
        cookies = credential.get_cookies()
    except Exception:
        cookies = {}

    ordered_keys = [
        "SESSDATA",
        "bili_jct",
        "DedeUserID",
        "buvid3",
        "buvid4",
        "ac_time_value",
    ]
    cookie_items = []
    for key in ordered_keys:
        value = cookies.get(key)
        if value is None or str(value) == "":
            continue
        cookie_items.append(
            {
                "name": key,
                "value": str(value),
                "domain": ".bilibili.com",
                "path": "/",
            }
        )

    with open(cookie_file, "w", encoding="utf-8") as f:
        json.dump(cookie_items, f, ensure_ascii=False, indent=2)
    return True


class BilibiliQrLoginSession:
    """In-memory Bilibili QR login session wrapper."""

    def __init__(self):
        self.created_at = int(time.time())
        self.qr = login_v2.QrCodeLogin()
        self.generated = False
        self.last_state = None

    def generate(self) -> Dict[str, str]:
        try:
            _run_async(self.qr.generate_qrcode())
        except ArgsException as e:
            raise RuntimeError(str(e))
        self.generated = True
        pic = self.qr.get_qrcode_picture()
        raw = pic.content if pic else b""
        image_b64 = base64.b64encode(raw).decode("ascii") if raw else ""
        return {
            "image_base64": image_b64,
            "mime_type": "image/png",
        }

    def check_status(self, cookie_file: Optional[str] = None) -> Dict[str, object]:
        if not self.generated:
            return {"status": "not_started"}

        try:
            event = _run_async(self.qr.check_state())
        except ArgsException as e:
            raise RuntimeError(str(e))

        status = getattr(event, "value", str(event))
        self.last_state = status
        payload: Dict[str, object] = {"status": status}

        if status == login_v2.QrCodeLoginEvents.DONE.value:
            credential = self.qr.get_credential()
            ok, msg = validate_credential(credential)
            if not ok:
                payload["status"] = "failed"
                payload["message"] = msg
                return payload

            if cookie_file:
                save_credential_to_file(credential, cookie_file)
                payload["cookies_saved"] = True
                payload["cookies_path"] = cookie_file
            payload["credential_ok"] = True

        return payload
