#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import os
from typing import Optional

logger = logging.getLogger("bilibili_runtime")

_INITIALIZED = False
_LAST_ERROR: Optional[str] = None


def configure_bilibili_runtime() -> bool:
    """Configure the internal Bilibili SDK network runtime once per process."""
    global _INITIALIZED, _LAST_ERROR
    if _INITIALIZED:
        return True

    try:
        from .bili_sdk import request_settings

        impersonate = os.environ.get("BILIBILI_IMPERSONATE", "chrome131").strip()
        if impersonate:
            request_settings.set("impersonate", impersonate)

        proxy = os.environ.get("BILIBILI_PROXY", "").strip()
        if proxy:
            request_settings.set_proxy(proxy)
            logger.info("Bilibili SDK 代理已设置为: %s", proxy)

        timeout_env = os.environ.get("BILIBILI_TIMEOUT", "").strip()
        if timeout_env:
            try:
                timeout_val = float(timeout_env)
                if timeout_val > 0:
                    request_settings.set_timeout(timeout_val)
                    logger.info("Bilibili SDK 超时已设置为: %s 秒", timeout_val)
            except ValueError:
                pass

        _INITIALIZED = True
        _LAST_ERROR = None
        return True
    except Exception as exc:
        _LAST_ERROR = str(exc)
        logger.warning("配置 bilibili-api 网络运行时失败: %s", exc)
        return False


def get_bilibili_runtime_error() -> Optional[str]:
    return _LAST_ERROR
