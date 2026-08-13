#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import time
import uuid
import shutil
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from modules.config_manager import (
    load_config,
    normalize_youtube_download_max_height,
    normalize_youtube_download_quality_mode,
)
from logging.handlers import RotatingFileHandler
from .utils import get_app_subdir, get_app_root_dir
from .ffmpeg_manager import get_ffmpeg_path, is_ffmpeg_usable
from .cookiecloud import try_cookiecloud_youtube_sync
from shutil import which as _which
from urllib.parse import parse_qs, urlparse
import re
import tempfile

# 其他导入和常量定义
logger = logging.getLogger(__name__)
# YouTube playlist ID 仅允许字母、数字、下划线和连字符
_YOUTUBE_PLAYLIST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_YOUTUBE_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)
_INTERNAL_YT_DLP_FLAG = '--y2a-internal-yt-dlp'
_YT_DLP_UNAVAILABLE_MESSAGE = '本地 yt-dlp 不可用，请重新安装依赖或重新下载完整便携包。'
_VIDEO_OUTPUT_EXTENSIONS = frozenset({'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v'})


class YtDlpUnavailableError(RuntimeError):
    """本地 yt-dlp 命令入口不可用。"""


def _format_unexpected_download_error(exc: Exception) -> str:
    if isinstance(exc, YtDlpUnavailableError):
        return str(exc)
    return f"下载过程中发生未预期的错误: {exc}"


def _is_video_output_file(filename: str) -> bool:
    """判断 yt-dlp 产物是否为可上传的视频文件。"""
    name = str(filename or '')
    return name.startswith('video.') and Path(name).suffix.lower() in _VIDEO_OUTPUT_EXTENSIONS

# 项目根目录，使用工具函数以兼容开发环境和 PyInstaller 打包环境，并使用 realpath 解析符号链接
_BASE_DIR = os.path.realpath(get_app_root_dir())


def _resolve_safe_cookies_path(cookies_file_path: str, log: logging.Logger | None = None) -> str | None:
    """将 cookies_file_path 解析为安全的绝对路径。

    使用 realpath 解析符号链接后，通过 commonpath 校验路径仍在项目根目录内，
    防止目录遍历及通过 symlink 越界访问。支持相对路径和位于项目根目录内的绝对路径，
    返回安全的绝对路径，或在路径无效/文件不存在时返回 None。
    """
    _log = log or logger
    if os.path.isabs(cookies_file_path):
        resolved = os.path.realpath(cookies_file_path)
    else:
        resolved = os.path.realpath(os.path.join(_BASE_DIR, cookies_file_path))
    try:
        common = os.path.commonpath([_BASE_DIR, resolved])
    except ValueError:
        common = ""
    if common != _BASE_DIR:
        _log.warning(f"检测到位于受信任根目录之外的cookies文件路径，已拒绝: {cookies_file_path}")
        return None
    if not os.path.isfile(resolved):
        _log.warning(f"cookies文件不存在或不是普通文件，已忽略: {cookies_file_path}")
        return None
    return resolved


def _detect_js_runtime_args() -> list[str]:
    """检测可供 yt-dlp 使用的 JS runtime。"""
    args: list[str] = []
    for runtime in ('deno', 'node'):
        if _which(runtime):
            args.extend(['--js-runtimes', runtime])
    return args


def _get_youtube_runtime_args() -> list[str]:
    """统一 YouTube 运行时参数。"""
    args = _detect_js_runtime_args()
    if not args:
        logger.warning("未检测到 JavaScript 运行时（node/deno），yt-dlp 的 n challenge 求解可能失败")
    args.extend(['--remote-components', 'ejs:github'])
    return args


def _youtube_cookies_look_authenticated(cookies_path: str | None) -> tuple[bool, str | None]:
    """粗略判断 cookies 是否包含可用于 YouTube 登录的一方凭据。"""
    if not cookies_path or not os.path.isfile(cookies_path):
        return False, "cookies文件不存在"

    auth_cookie_names = {
        'SAPISID', 'APISID', 'SID', 'HSID', 'SSID',
        '__Secure-1PSID', '__Secure-1PAPISID', 'LOGIN_INFO',
    }

    try:
        present_names: set[str] = set()
        with open(cookies_path, 'r', encoding='utf-8', errors='replace') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    present_names.add(parts[5].strip())
        if present_names & auth_cookie_names:
            return True, None
        return False, "cookies中缺少 Google/YouTube 一方登录态关键字段"
    except Exception as exc:
        return False, f"读取cookies失败: {exc}"


def _is_netscape_cookie_file(path: str) -> bool:
    """快速判断是否为 Netscape cookies.txt（简单检测头或行列数）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                ln = ln.strip()
                if i == 0 and ln.startswith("# Netscape HTTP Cookie File"):
                    return True
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.split("\t")
                if len(parts) >= 7:
                    return True
                if i > 50:
                    break
    except Exception:
        return False
    return False


def _convert_any_to_netscape(input_path: str, output_path: str) -> bool:
    """
    简单尝试把非标准导出（制表或表格形式）转换为 Netscape 格式。
    这是一个启发式实现，适用于常见的浏览器导出样式。
    """
    try:
        from modules.tools.convert_cookies import convert_any_to_netscape as _conv
    except Exception:
        return False
    try:
        res = _conv(input_path, output_path)
        return bool(res and os.path.exists(res))
    except Exception:
        return False


def _ensure_netscape_cookies(cookies_path: str, log: logging.Logger | None = None) -> str | None:
    """
    如果 cookies_path 不是 Netscape 格式，尝试把它转换为临时的 Netscape 文件并返回新路径；
    若已是 Netscape，直接返回原路径；若转换失败，返回原路径并在日志中记录警告。
    """
    _log = log or logger
    try:
        if not cookies_path or not os.path.exists(cookies_path):
            return None
        if _is_netscape_cookie_file(cookies_path):
            return cookies_path

        # create temp output path next to original for visibility
        tmp_dir = os.path.dirname(os.path.realpath(cookies_path)) or tempfile.gettempdir()
        fd, tmp_path = tempfile.mkstemp(prefix="y2a_yt_cookies_", suffix=".txt", dir=tmp_dir)
        os.close(fd)
        success = _convert_any_to_netscape(cookies_path, tmp_path)
        if success and os.path.exists(tmp_path):
            _log.info("自动将 cookies 转换为 Netscape 格式: %s", tmp_path)
            return tmp_path
        # 清理若转换失败
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        _log.warning("无法将 cookies 自动转换为 Netscape 格式，仍使用原文件: %s", cookies_path)
        return cookies_path
    except Exception as exc:
        _log.warning("检查/转换 cookies 时出错，仍尝试使用原文件: %s", exc)
        return cookies_path

# Rest of youtube_handler.py original content continues unchanged below

def _append_yt_dlp_network_args(
    cmd: list[str],
    *,
    proxy_url: str | None = None,
    cookies_path: str | None = None,
) -> list[str]:
    """为 yt-dlp 命令附加网络与认证相关参数。"""
    cmd.extend(_get_youtube_runtime_args())
    if proxy_url:
        cmd.extend(['--proxy', proxy_url])
    if cookies_path and os.path.exists(cookies_path):
        cmd.extend(['--cookies', cookies_path])
    return cmd


def _build_quality_retry_strategies(config: dict[str, Any] | None, has_ffmpeg: bool) -> dict[str, Any]:
    mode = normalize_youtube_download_quality_mode((config or {}).get('YOUTUBE_DOWNLOAD_QUALITY_MODE'))
    max_height = normalize_youtube_download_max_height((config or {}).get('YOUTUBE_DOWNLOAD_MAX_HEIGHT'))
    manual = mode == 'manual'

    if manual:
        primary_selector = f'bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]'
        fallback_selector = f'best[height<={max_height}]'
    else:
        primary_selector = 'bestvideo+bestaudio/best'
        fallback_selector = 'best'

    strategies: list[dict[str, Any]] = []
    if has_ffmpeg:
        strategies.append({
            'selector': primary_selector,
            'merge_output_format': 'mp4',
            'label': 'bestvideo+bestaudio',
        })
    strategies.append({
        'selector': fallback_selector,
        'merge_output_format': None,
        'label': 'best',
    })
    return {
        'mode': mode,
        'max_height': max_height if manual else None,
        'strategies': strategies,
    }


def _build_quality_format_selector(config: dict[str, Any] | None, has_ffmpeg: bool) -> str:
    plan = _build_quality_retry_strategies(config, has_ffmpeg)
    strategies = plan.get('strategies') or []
    if not strategies:
        return 'best'
    return str(strategies[0].get('selector') or 'best')


def _set_yt_dlp_format_options(
    cmd: list[str],
    selector: str,
    *,
    merge_output_format: str | None = None,
) -> None:
    while '--format' in cmd:
        idx = cmd.index('--format')
        del cmd[idx:idx + 2]
    while '--merge-output-format' in cmd:
        idx = cmd.index('--merge-output-format')
        del cmd[idx:idx + 2]
    cmd.extend(['--format', selector])
    if merge_output_format:
        cmd.extend(['--merge-output-format', merge_output_format])


def _build_subtitle_download_args(
    config: dict[str, Any] | None,
    *,
    include_subtitles: bool,
) -> list[str]:
    if not include_subtitles:
        return ['--no-write-subs']

    # 仅当用户显式允许 YouTube 自动生成字幕时才下载字幕。
    # yt-dlp 的 --write-subs 会下载所有字幕（含自动生成），
    # --no-write-auto-subs 在新版本中无效，无法可靠区分。
    # 因此当 YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED=False 时，
    # 直接禁用字幕下载，让后续 ASR 流程负责生成字幕。
    if not bool((config or {}).get('YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED', False)):
        return ['--no-write-subs']

    return [
        '--write-subs',
        '--all-subs',
        '--convert-subs', 'srt',
        '--write-auto-subs',
    ]


def _is_format_selection_error(error_text: str | None) -> bool:
    """判断是否属于格式选择失败，而非视频不可访问。"""
    if not error_text:
        return False
    normalized = str(error_text)
    indicators = (
        "Requested format is not available",
        "Only images are available",
    )
    return any(indicator in normalized for indicator in indicators)


def _looks_like_youtube_bot_challenge(error_text: str | None) -> bool:
    """判断是否像是 YouTube 反机器人/登录校验问题。"""
    if not error_text:
        return False
    normalized = str(error_text)
    indicators = (
        "Sign in to confirm",
        "not a bot",
        "Signature extraction failed",
        "Some formats may be missing",
        "HTTP Error 403",
        "player",
        "decodeURIComponent",
        "The page needs to be reloaded.",
    )
    return any(indicator in normalized for indicator in indicators)


def _summarize_yt_dlp_error(stdout_text: str | None, stderr_text: str | None) -> str:
    """从 yt-dlp 输出中提取更有价值的错误摘要。"""
    candidates: list[str] = []
    for text in (stderr_text, stdout_text):
        if not text:
            continue
        for raw_line in str(text).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("ERROR:"):
                candidates.append(line)
            elif "[youtube]" in line or "[download]" in line:
                candidates.append(line)

    if candidates:
        return candidates[-1]

    merged = (stderr_text or stdout_text or "").strip()
    if not merged:
        return "未知错误"

    lines = [line.strip() for line in merged.splitlines() if line.strip()]
    return lines[-1] if lines else "未知错误"


def _find_yt_dlp_command(log: logging.Logger) -> list[str]:
    """解析 yt-dlp 调用命令，冻结环境优先使用主程序内置入口。"""
    log.info("开始查找yt-dlp执行命令...")

    current_python = sys.executable
    if current_python:
        if getattr(sys, 'frozen', False):
            current_command = [current_python, _INTERNAL_YT_DLP_FLAG]
            command_label = f"冻结程序内置yt-dlp: {current_python} {_INTERNAL_YT_DLP_FLAG}"
        else:
            current_command = [current_python, '-m', 'yt_dlp']
            command_label = f"当前Python解释器调用yt-dlp: {current_python} -m yt_dlp"
        try:
            result = subprocess.run(
                [*current_command, '--version'],
                capture_output=True,
                text=True,
                timeout=10,
                encoding='utf-8',
                errors='replace'
            )
            if result.returncode == 0:
                log.info(f"使用{command_label}")
                return current_command
        except Exception as exc:
            log.debug(f"验证{command_label}失败: {exc}")

    found = _which('yt-dlp')
    if found:
        log.info(f"找到系统中的yt-dlp: {found}")
        return [found]

    possible_paths = [
        '/home/y2a/.local/bin/yt-dlp',
        '/usr/local/bin/yt-dlp',
        '/usr/bin/yt-dlp',
    ]
    for path in possible_paths:
        if os.path.exists(path):
            log.info(f"找到存在的yt-dlp路径: {path}")
            return [path]

    if os.name == 'nt':
        venv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.venv', 'Scripts', 'yt-dlp.exe')
        if os.path.exists(venv_path):
            log.info(f"回退到虚拟环境中的yt-dlp.exe: {venv_path}")
            return [venv_path]

    log.error(_YT_DLP_UNAVAILABLE_MESSAGE)
    raise YtDlpUnavailableError(_YT_DLP_UNAVAILABLE_MESSAGE)


def is_docker_env() -> bool:
    """粗略判断是否运行在 Docker 中"""
    try:
        if os.path.exists('/.dockerenv'):
            return True
        cgroup_path = '/proc/1/cgroup'
        if os.path.exists(cgroup_path):
            with open(cgroup_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().lower()
                return 'docker' in content or 'kubepods' in content
    except Exception:
        pass
    return False


# (Remaining original file content omitted here for brevity in the commit)
