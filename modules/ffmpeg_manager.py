#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Centralized helpers for resolving the bundled FFmpeg toolchain.

The project ships precompiled binaries for the most common platforms in the
`ffmpeg/` directory. This module detects the runtime platform, lazily resolves
the best matching binary, and falls back to system installations when needed.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from typing import Optional


import requests
from shutil import which as _which

from .config_manager import load_config
from .utils import get_app_root_dir

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FFmpegContext:
    """Describes the resolved ffmpeg/ffprobe toolchain."""

    ffmpeg: Optional[str]
    ffprobe: Optional[str]
    source: str
    platform: str


_BINARY_CACHE: dict[str, Optional[str]] = {"ffmpeg": None, "ffprobe": None}
_META_CACHE: dict[str, Optional[str]] = {"source": None, "platform": None}


def _platform_signature() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def _binary_variants(tool: str) -> list[str]:
    exe = f"{tool}.exe"
    return [exe, tool] if os.name == 'nt' else [tool, exe]


def _normalize_path(path: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(path.strip()))
    if not os.path.isabs(expanded):
        expanded = os.path.join(get_app_root_dir(), expanded)
    return os.path.normpath(expanded)


def _resolve_from_location(location: Optional[str], tool: str) -> Optional[str]:
    if not location:
        return None
    try:
        normalized = _normalize_path(location)
    except OSError:
        return None

    if os.path.isdir(normalized):
        for variant in _binary_variants(tool):
            candidate = os.path.join(normalized, variant)
            if os.path.exists(candidate):
                return candidate
    elif os.path.isfile(normalized):
        return normalized
    return None


def _bundled_candidates(tool: str) -> list[str]:
    base_dir = os.path.join(get_app_root_dir(), 'ffmpeg')
    names = _binary_variants(tool)
    candidates: list[str] = []

    for name in names:
        candidates.append(os.path.join(base_dir, name))

    system_key = platform.system().lower()
    machine_key = platform.machine().lower()
    platform_variants = [system_key, machine_key, f"{system_key}-{machine_key}"]
    for variant in platform_variants:
        sub_dir = os.path.join(base_dir, variant)
        if os.path.isdir(sub_dir):
            for name in names:
                candidates.append(os.path.join(sub_dir, name))

    bin_dir = os.path.join(base_dir, 'bin')
    for root in (bin_dir, os.path.join(bin_dir, system_key), os.path.join(bin_dir, machine_key)):
        if os.path.isdir(root):
            for name in names:
                candidates.append(os.path.join(root, name))

    deduped: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        norm = os.path.normpath(path)
        if norm not in seen:
            seen.add(norm)
            deduped.append(norm)
    return deduped


def _windows_arch_tag() -> str:
    arch_env = (os.environ.get('PROCESSOR_ARCHITEW6432')
                or os.environ.get('PROCESSOR_ARCHITECTURE')
                or platform.machine() or '').lower()
    if any(token in arch_env for token in ('arm64', 'aarch64')):
        return 'win64-arm64'
    if '64' in arch_env or 'amd64' in arch_env or 'x86_64' in arch_env:
        return 'win64'
    return 'win32'


def _windows_ffmpeg_download_url() -> tuple[str, str]:
    arch_tag = _windows_arch_tag()
    if arch_tag == 'win64-arm64':
        # BtbN 提供的 ARM64 版本仍然位于 win64 目录，但添加标记方便日志
        archive = 'ffmpeg-master-latest-win64-gpl.zip'
    else:
        archive = f"ffmpeg-master-latest-{arch_tag}-gpl.zip"
    url = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/{archive}"
    return arch_tag, url


def get_windows_ffmpeg_manual_setup_message(app_root: Optional[str] = None) -> str:
    root_dir = app_root or get_app_root_dir()
    ffmpeg_dir = os.path.join(root_dir, 'ffmpeg')
    ffmpeg_exe = os.path.join(ffmpeg_dir, 'ffmpeg.exe')
    ffprobe_exe = os.path.join(ffmpeg_dir, 'ffprobe.exe')
    return (
        '请手动下载 Windows 版 FFmpeg，并将 ffmpeg.exe 和 ffprobe.exe 放到项目根目录 '
        f'ffmpeg/ 下。目标路径示例：{ffmpeg_exe}；{ffprobe_exe}。'
        ' 也可以在设置页填写 FFMPEG_LOCATION 指向本地 ffmpeg.exe。'
    )


def _dispatch_progress(progress_callback, payload, state, *, force=False):
    if not progress_callback:
        return

    now = time.monotonic()
    stage = payload.get('stage')
    percent = payload.get('percent')
    last_stage = state.get('stage')
    last_percent = state.get('percent')
    last_time = state.get('time', 0.0)

    percent_advanced = False
    if isinstance(percent, (int, float)) and isinstance(last_percent, (int, float)):
        percent_advanced = (percent - last_percent) >= 1.0
    elif isinstance(percent, (int, float)) and last_percent is None:
        percent_advanced = True

    if not force and stage == last_stage and not percent_advanced and (now - last_time) < 0.25:
        return

    state['stage'] = stage
    state['percent'] = percent
    state['time'] = now
    progress_callback(payload)


def download_ffmpeg_bundled(
    logger: Optional[logging.Logger] = None,
    progress_callback=None
) -> Optional[str]:
    """Download the official Windows build into ffmpeg/ when missing."""
    log_obj = logger or log
    if os.name != 'nt':
        log_obj.debug("Skipping ffmpeg auto-download on non-Windows platform")
        return None

    app_root = get_app_root_dir()
    target_dir = os.path.join(app_root, 'ffmpeg')
    os.makedirs(target_dir, exist_ok=True)

    ffmpeg_exe = os.path.join(target_dir, 'ffmpeg.exe')
    ffprobe_exe = os.path.join(target_dir, 'ffprobe.exe')

    if os.path.exists(ffmpeg_exe):
        if is_ffmpeg_usable(ffmpeg_exe, log_obj):
            return ffmpeg_exe
        log_obj.warning("Bundled ffmpeg exists but is unusable, refreshing the download")
        try:
            os.remove(ffmpeg_exe)
        except OSError as exc:
            log_obj.debug("Failed to remove stale ffmpeg.exe: %s", exc)
        try:
            if os.path.exists(ffprobe_exe):
                os.remove(ffprobe_exe)
        except OSError:
            pass

    arch_tag, url = _windows_ffmpeg_download_url()
    zip_path = os.path.join(target_dir, 'ffmpeg.zip')
    progress_state = {'stage': None, 'percent': None, 'time': 0.0}

    try:
        log_obj.info("Downloading bundled ffmpeg for Windows (%s): %s", arch_tag, url)
        _dispatch_progress(
            progress_callback,
            {
                'stage': 'downloading_ffmpeg',
                'message': '正在下载 FFmpeg',
                'detail': f'正在获取 Windows {arch_tag} 版本。',
                'percent': None,
                'downloaded_bytes': 0,
                'total_bytes': None,
                'level': 'info'
            },
            progress_state,
            force=True
        )
        with requests.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            total_bytes = None
            total_header = response.headers.get('Content-Length')
            try:
                if total_header:
                    total_bytes = max(int(total_header), 0)
            except (TypeError, ValueError):
                total_bytes = None
            with open(zip_path, 'wb') as archive:
                downloaded_bytes = 0
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        archive.write(chunk)
                        downloaded_bytes += len(chunk)
                        percent = None
                        if total_bytes:
                            percent = round(min((downloaded_bytes / total_bytes) * 100, 100), 1)
                        _dispatch_progress(
                            progress_callback,
                            {
                                'stage': 'downloading_ffmpeg',
                                'message': '正在下载 FFmpeg',
                                'detail': '正在拉取压缩包，请稍候。',
                                'percent': percent,
                                'downloaded_bytes': downloaded_bytes,
                                'total_bytes': total_bytes,
                                'level': 'info'
                            },
                            progress_state
                        )

        _dispatch_progress(
            progress_callback,
            {
                'stage': 'extracting_ffmpeg',
                'message': '正在解压 FFmpeg',
                'detail': '下载完成，正在展开压缩包并复制可执行文件。',
                'percent': None,
                'downloaded_bytes': os.path.getsize(zip_path) if os.path.exists(zip_path) else None,
                'total_bytes': os.path.getsize(zip_path) if os.path.exists(zip_path) else None,
                'level': 'info'
            },
            progress_state,
            force=True
        )

        extract_dir = os.path.join(target_dir, 'tmp_extract')
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)

        ffmpeg_path = None
        ffprobe_path = None
        for root, _dirs, files in os.walk(extract_dir):
            if not ffmpeg_path and 'ffmpeg.exe' in files:
                ffmpeg_path = os.path.join(root, 'ffmpeg.exe')
            if not ffprobe_path and 'ffprobe.exe' in files:
                ffprobe_path = os.path.join(root, 'ffprobe.exe')
            if ffmpeg_path and ffprobe_path:
                break

        if not ffmpeg_path:
            raise RuntimeError('Downloaded archive does not contain ffmpeg.exe')

        shutil.copy2(ffmpeg_path, ffmpeg_exe)
        if ffprobe_path:
            shutil.copy2(ffprobe_path, os.path.join(target_dir, 'ffprobe.exe'))

        _dispatch_progress(
            progress_callback,
            {
                'stage': 'downloaded_ffmpeg',
                'message': 'FFmpeg 已下载',
                'detail': '压缩包已解压，正在验证 ffmpeg.exe 是否可用。',
                'percent': 100.0,
                'downloaded_bytes': os.path.getsize(zip_path) if os.path.exists(zip_path) else None,
                'total_bytes': os.path.getsize(zip_path) if os.path.exists(zip_path) else None,
                'level': 'info'
            },
            progress_state,
            force=True
        )
        log_obj.info("Bundled ffmpeg (%s) downloaded to %s", arch_tag, ffmpeg_exe)
        return ffmpeg_exe
    except Exception as exc:
        manual_setup_message = get_windows_ffmpeg_manual_setup_message(app_root)
        log_obj.warning("Failed to download bundled ffmpeg (%s): %s. %s", arch_tag, exc, manual_setup_message)
        _dispatch_progress(
            progress_callback,
            {
                'stage': 'failed',
                'message': 'FFmpeg 下载失败',
                'detail': f'{manual_setup_message} 下载失败原因：{exc}',
                'percent': None,
                'downloaded_bytes': None,
                'total_bytes': None,
                'level': 'error'
            },
            progress_state,
            force=True
        )
        return None
    finally:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except OSError:
            pass
        extract_dir = os.path.join(target_dir, 'tmp_extract')
        shutil.rmtree(extract_dir, ignore_errors=True)


def is_ffmpeg_usable(path: Optional[str], logger: Optional[logging.Logger] = None) -> bool:
    if not path:
        return False
    log_obj = logger or log
    try:
        result = subprocess.run(
            [path, '-version'],
            capture_output=True,
            text=True,
            timeout=5,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode == 0:
            return True
        log_obj.warning("ffmpeg unusable (code %s): %s", result.returncode, result.stderr.strip()[:200])
    except FileNotFoundError:
        log_obj.debug("ffmpeg not found at %s", path)
    except OSError as exc:
        log_obj.warning("Cannot execute ffmpeg (%s): %s", path, exc)
    except subprocess.TimeoutExpired:
        log_obj.warning("ffmpeg version probe timed out for %s", path)
    except Exception as exc:
        log_obj.debug("Unexpected error probing ffmpeg (%s): %s", path, exc)
    return False


def _resolve_ffmpeg_path(
    *,
    allow_system: bool,
    logger: Optional[logging.Logger],
    progress_callback=None
) -> Optional[str]:
    log_obj = logger or log
    log_obj.debug("Resolving ffmpeg for platform %s", _platform_signature())

    try:
        config = load_config()
    except Exception:
        config = {}

    search_order: list[tuple[str, Optional[str]]] = []

    config_candidate = _resolve_from_location(config.get('FFMPEG_LOCATION'), 'ffmpeg')
    if config_candidate:
        search_order.append(('config', config_candidate))

    for candidate in _bundled_candidates('ffmpeg'):
        search_order.append(('bundled', candidate))

    auto_download = config.get('FFMPEG_AUTO_DOWNLOAD', True)
    if os.name == 'nt' and auto_download:
        downloaded = download_ffmpeg_bundled(log_obj, progress_callback=progress_callback)
        if downloaded:
            search_order.append(('downloaded', downloaded))

    if allow_system:
        which_target = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
        sys_candidate = _which(which_target)
        if sys_candidate:
            search_order.append(('system', sys_candidate))

    for source, candidate in search_order:
        if not candidate:
            continue
        if source != 'system' and not os.path.exists(candidate):
            continue
        if progress_callback and source in ('downloaded', 'bundled', 'config'):
            progress_callback({
                'stage': 'verifying_ffmpeg',
                'message': '正在验证 FFmpeg',
                'detail': f'正在检查可执行文件：{candidate}',
                'percent': 100.0 if source == 'downloaded' else None,
                'downloaded_bytes': None,
                'total_bytes': None,
                'level': 'info'
            })
        if is_ffmpeg_usable(candidate, log_obj):
            _META_CACHE['source'] = source
            _META_CACHE['platform'] = platform.platform()
            log_obj.info("Using %s ffmpeg: %s", source, candidate)
            return candidate

    log_obj.error("No usable ffmpeg binary found; checked bundled and system paths")
    return None


def get_ffmpeg_path(
    *,
    allow_system: bool = True,
    force_refresh: bool = False,
    logger: Optional[logging.Logger] = None,
    progress_callback=None
) -> Optional[str]:
    if not force_refresh and _BINARY_CACHE['ffmpeg']:
        return _BINARY_CACHE['ffmpeg']

    path = _resolve_ffmpeg_path(
        allow_system=allow_system,
        logger=logger,
        progress_callback=progress_callback
    )
    _BINARY_CACHE['ffmpeg'] = path
    return path


def _ffprobe_candidates(ffmpeg_path: Optional[str]) -> list[str]:
    names = _binary_variants('ffprobe')
    candidates: list[str] = []
    if ffmpeg_path:
        base = os.path.dirname(ffmpeg_path)
        for name in names:
            candidates.append(os.path.join(base, name))
    for candidate in _bundled_candidates('ffprobe'):
        candidates.append(candidate)
    return candidates


def get_ffprobe_path(
    *,
    ffmpeg_path: Optional[str] = None,
    allow_system: bool = True,
    force_refresh: bool = False,
    logger: Optional[logging.Logger] = None
) -> Optional[str]:
    if not force_refresh and _BINARY_CACHE['ffprobe']:
        return _BINARY_CACHE['ffprobe']

    candidates = _ffprobe_candidates(ffmpeg_path)
    if allow_system:
        which_target = 'ffprobe.exe' if os.name == 'nt' else 'ffprobe'
        sys_candidate = _which(which_target)
        if sys_candidate:
            candidates.append(sys_candidate)

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            _BINARY_CACHE['ffprobe'] = candidate
            return candidate

    log_obj = logger or log
    log_obj.warning("No ffprobe binary found alongside ffmpeg")
    return None


def get_ffmpeg_context(
    *,
    allow_system: bool = True,
    force_refresh: bool = False,
    logger: Optional[logging.Logger] = None
) -> FFmpegContext:
    ffmpeg_path = get_ffmpeg_path(allow_system=allow_system, force_refresh=force_refresh, logger=logger)
    ffprobe_path = get_ffprobe_path(ffmpeg_path=ffmpeg_path, allow_system=allow_system, force_refresh=force_refresh, logger=logger)
    return FFmpegContext(
        ffmpeg=ffmpeg_path,
        ffprobe=ffprobe_path,
        source=_META_CACHE.get('source') or 'unknown',
        platform=_META_CACHE.get('platform') or platform.platform(),
    )


def refresh_ffmpeg_cache(logger: Optional[logging.Logger] = None) -> FFmpegContext:
    """Force a re-resolution of ffmpeg/ffprobe paths."""
    _BINARY_CACHE['ffmpeg'] = None
    _BINARY_CACHE['ffprobe'] = None
    return get_ffmpeg_context(force_refresh=True, logger=logger)
