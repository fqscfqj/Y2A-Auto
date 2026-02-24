# -*- coding: utf-8 -*-

import logging
import os
import json
import time
from typing import Dict, Optional

import requests


logger = logging.getLogger("acfun_auth")


def _safe_result_code(value) -> int:
    try:
        return int(value)
    except Exception:
        return -1


def _result_message(result_code: int, default: str = "") -> str:
    mapping = {
        0: "成功",
        21: "签名校验失败",
        100400002: "二维码已过期",
    }
    if result_code in mapping:
        return mapping[result_code]
    return default or f"未知状态码: {result_code}"


def save_session_cookies_to_json(
    session: requests.Session,
    cookie_file: str,
    domain_keyword: str = "acfun.cn",
) -> int:
    if not cookie_file:
        raise ValueError("cookie_file 不能为空")

    os.makedirs(os.path.dirname(cookie_file) or ".", exist_ok=True)

    cookie_items = []
    count = 0

    for cookie in session.cookies:
        domain = str(cookie.domain or "").strip()
        if domain_keyword and domain_keyword not in domain:
            continue
        name = str(cookie.name or "").strip()
        if not name:
            continue
        path = str(cookie.path or "/").strip() or "/"
        value = str(cookie.value or "")
        cookie_items.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
            }
        )
        count += 1

    with open(cookie_file, "w", encoding="utf-8") as f:
        json.dump(cookie_items, f, ensure_ascii=False, indent=2)

    return count


class AcfunQrLoginSession:
    """In-memory AcFun QR login session."""

    START_URL = "https://scan.acfun.cn/rest/pc-direct/qr/start"
    SCAN_URL = "https://scan.acfun.cn/rest/pc-direct/qr/scanResult"
    ACCEPT_URL = "https://scan.acfun.cn/rest/pc-direct/qr/acceptResult"
    SCAN_TIMEOUT_SECONDS = 6
    ACCEPT_TIMEOUT_SECONDS = 6

    def __init__(self):
        self.created_at = int(time.time())
        self.generated = False
        self.phase = "scan"  # scan -> accept -> done/timeout/failed
        self.qr_login_token = ""
        self.qr_login_signature = ""
        self.qr_expire_ms = 120000
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.acfun.cn",
                "Referer": "https://www.acfun.cn/login",
            }
        )

    def generate(self) -> Dict[str, object]:
        try:
            resp = self.session.get(
                self.START_URL,
                params={"type": "WEB_LOGIN"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"获取二维码失败: {e}")

        code = _safe_result_code(data.get("result"))
        if code != 0:
            message = str(data.get("error_msg") or _result_message(code))
            raise RuntimeError(f"获取二维码失败: {message}")

        image_data = str(data.get("imageData") or "")
        token = str(data.get("qrLoginToken") or "")
        signature = str(data.get("qrLoginSignature") or "")
        if not image_data or not token or not signature:
            raise RuntimeError("获取二维码失败: 返回字段不完整")

        self.generated = True
        self.phase = "scan"
        self.qr_login_token = token
        self.qr_login_signature = signature
        self.qr_expire_ms = int(data.get("expireTime") or 120000)

        return {
            "image_base64": image_data,
            "mime_type": "image/png",
            "expires_in_ms": self.qr_expire_ms,
        }

    def _check_scan(self) -> Dict[str, object]:
        try:
            resp = self.session.get(
                self.SCAN_URL,
                params={
                    "qrLoginToken": self.qr_login_token,
                    "qrLoginSignature": self.qr_login_signature,
                },
                timeout=self.SCAN_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ReadTimeout:
            return {"status": "wait_scan", "message": "等待扫码"}
        except Exception as e:
            self.phase = "failed"
            return {"status": "failed", "message": f"查询扫码状态失败: {e}"}

        code = _safe_result_code(data.get("result"))
        if code == 0:
            next_signature = str(data.get("qrLoginSignature") or "").strip()
            if next_signature:
                self.qr_login_signature = next_signature
            self.phase = "accept"
            return {"status": "scan", "message": "已扫码，等待手机确认"}
        if code == 100400002:
            self.phase = "timeout"
            return {"status": "timeout", "message": "二维码已过期，请重新获取"}

        message = str(data.get("error_msg") or _result_message(code))
        self.phase = "failed"
        return {"status": "failed", "message": message, "code": code}

    def _warmup_acfun_domains(self):
        for url in ("https://www.acfun.cn/", "https://member.acfun.cn/"):
            try:
                self.session.get(url, timeout=10)
            except Exception:
                pass

    def _check_accept(self, cookie_file: Optional[str] = None) -> Dict[str, object]:
        try:
            resp = self.session.get(
                self.ACCEPT_URL,
                params={
                    "qrLoginToken": self.qr_login_token,
                    "qrLoginSignature": self.qr_login_signature,
                },
                timeout=self.ACCEPT_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.ReadTimeout:
            return {"status": "confirm", "message": "等待手机端确认登录"}
        except Exception as e:
            self.phase = "failed"
            return {"status": "failed", "message": f"确认登录状态失败: {e}"}

        code = _safe_result_code(data.get("result"))
        if code == 0:
            payload: Dict[str, object] = {"status": "done", "message": "登录成功"}
            if cookie_file:
                self._warmup_acfun_domains()
                count = save_session_cookies_to_json(self.session, cookie_file)
                payload["cookies_saved"] = bool(count > 0)
                payload["cookies_count"] = count
                payload["cookies_path"] = cookie_file
                if count <= 0:
                    self.phase = "failed"
                    return {
                        "status": "failed",
                        "message": "登录成功但未获取到有效 Cookie，请重试",
                    }
            self.phase = "done"
            return payload

        if code == 100400002:
            self.phase = "timeout"
            return {"status": "timeout", "message": "二维码已过期，请重新获取"}

        message = str(data.get("error_msg") or _result_message(code))
        self.phase = "failed"
        return {"status": "failed", "message": message, "code": code}

    def check_status(self, cookie_file: Optional[str] = None) -> Dict[str, object]:
        if not self.generated:
            return {"status": "not_started", "message": "二维码未初始化"}
        if self.phase == "done":
            return {"status": "done", "message": "登录成功"}
        if self.phase == "timeout":
            return {"status": "timeout", "message": "二维码已过期，请重新获取"}
        if self.phase == "failed":
            return {"status": "failed", "message": "登录失败，请重新获取二维码"}

        if self.phase == "scan":
            return self._check_scan()
        if self.phase == "accept":
            return self._check_accept(cookie_file=cookie_file)

        return {"status": "failed", "message": f"未知会话状态: {self.phase}"}
