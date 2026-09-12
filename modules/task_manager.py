#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import uuid
import sqlite3
import html
import logging
import shutil
import threading
import gc
import shlex
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import re
import unicodedata
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor
from apscheduler.schedulers.base import SchedulerNotRunningError
import queue
from .utils import get_app_root_dir, get_app_subdir
from .ffmpeg_manager import get_ffmpeg_path, get_ffprobe_path
from .subtitle_style import (
    apply_style_overrides,
    parse_style_overrides,
    diagnose_style,
)
from .video_encoder_params import (
    build_audio_params,
    build_encoder_params,
    format_quality_value,
    normalize_color_metadata,
    normalize_cpu_codec,
    parse_encoder_config,
    recommend_quality,
    resolve_color_metadata,
)
from .notifications import (
    EVENT_TASK_ADDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    NotificationEvent,
    emit_notification_event,
)
import subprocess
from typing import Any, Dict
from werkzeug.security import safe_join

# 导入其他模块
# 这些导入会在函数内部使用，避免循环导入问题
# from modules.youtube_handler import download_video_data
# from modules.ai_enhancer import translate_video_metadata, generate_acfun_tags, recommend_acfun_partition
# from modules.content_moderator import AlibabaCloudModerator
# from modules.acfun_uploader import AcfunUploader

def _get_memory_usage_percent():
    """获取系统内存使用百分比，用于内存感知处理"""
    try:
        try:
            import psutil  # type: ignore
        except Exception:
            # psutil可能未安装，返回保守估计
            return 50.0
        memory = psutil.virtual_memory()
        return memory.percent
    except ImportError:
        # 如果没有psutil，返回一个保守的估计值
        return 50.0
    except Exception:
        return 50.0

def _should_reduce_concurrency():
    """判断是否应该降低并发数以节省内存"""
    memory_percent = _get_memory_usage_percent()
    return memory_percent > 80.0  # 内存使用超过80%时降低并发

# 全局变量
DB_DIR = get_app_subdir('db')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'tasks.db')
LOGS_DIR = get_app_subdir('logs')
DOWNLOADS_DIR = get_app_subdir('downloads')
PROCESS_TERMINATE_WAIT_SECONDS = 5
DB_CONNECT_TIMEOUT_SECONDS = 10
DB_BUSY_TIMEOUT_MS = 30000
DB_WRITE_RETRY_TIMES = 5
DB_WRITE_RETRY_SLEEP_SECONDS = 0.2
METADATA_TRANSLATION_ERROR_CATEGORY = 'metadata_translation_failed'
CONTENT_MODERATION_ERROR_CATEGORY = 'content_moderation_failed'

# 字幕烧录超时估算参数（见 TaskManager._estimate_embed_timeout）。
EMBED_TIMEOUT_MIN_SECONDS = 1800
EMBED_TIMEOUT_MAX_SECONDS = 10800
# libx265 的编码耗时实测约为同 preset 下 libx264 的 5 倍（仓库自带 FFmpeg
# N-123313，带 subtitles 滤镜的真实烧录，1080p30 20s：veryfast 1.70s -> 8.44s）。
# 若沿用 x264 的估算，长视频会被超时强杀；而 CPU 路径失败后只剩「x265 -> x264」
# 一级降级，被超时杀掉时连这一级也救不回成片，只能整任务失败。故按实测倍数放大。
EMBED_TIMEOUT_X265_FACTOR = 5.0
EMBED_TIMEOUT_MAX_SECONDS_X265 = 28800


def _convert_vtt_text_to_srt_text(vtt_content: str) -> str:
    """将普通/YouTube 自动字幕 VTT 文本稳健转换为 SRT 文本。"""
    import re

    def _normalize_newlines(text: str) -> str:
        normalized = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
        if normalized.startswith('\ufeff'):
            normalized = normalized[1:]
        return normalized

    def _time_to_ms(value: str) -> int:
        hours, minutes, seconds = str(value).split(':')
        whole_seconds, milliseconds = str(seconds).split('.')
        return (
            int(hours) * 3600000
            + int(minutes) * 60000
            + int(whole_seconds) * 1000
            + int(milliseconds)
        )

    def _clean_text_line(line: str) -> str:
        text = html.unescape(str(line or ''))
        text = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', text)
        text = re.sub(r'</?[^>]+>', '', text)
        text = re.sub(r'{[^}]*}', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _dedupe_lines(lines):
        result = []
        seen = set()
        for line in lines:
            normalized = str(line or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _select_cue_text(cleaned_lines):
        unique_lines = _dedupe_lines(cleaned_lines)
        if not unique_lines:
            return ''
        if len(unique_lines) == 1:
            return unique_lines[0]

        longest_line = max(unique_lines, key=lambda item: (len(item), item))
        if all(line in longest_line for line in unique_lines):
            return longest_line
        return '\n'.join(unique_lines)

    content = _normalize_newlines(vtt_content)
    blocks = re.split(r'\n\s*\n+', content.strip())
    cues = []

    for block in blocks:
        lines = [line.rstrip() for line in block.split('\n')]
        if not lines:
            continue

        first_line = lines[0].strip()
        upper_first_line = first_line.upper()
        if (
            upper_first_line.startswith('WEBVTT')
            or first_line.startswith('Kind:')
            or first_line.startswith('Language:')
            or first_line.startswith('X-TIMESTAMP-MAP=')
            or upper_first_line.startswith('NOTE')
            or upper_first_line == 'STYLE'
            or upper_first_line == 'REGION'
        ):
            continue

        time_line_index = None
        for index, line in enumerate(lines):
            if '-->' in line:
                time_line_index = index
                break
        if time_line_index is None:
            continue

        time_match = re.search(
            r'(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})',
            lines[time_line_index],
        )
        if not time_match:
            continue

        payload_lines = lines[time_line_index + 1:]
        has_inline_timestamps = any(
            re.search(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', raw_line or '')
            for raw_line in payload_lines
        )
        cleaned_lines = [_clean_text_line(line) for line in payload_lines]
        text = _select_cue_text(cleaned_lines)
        if not text:
            continue

        start = time_match.group('start')
        end = time_match.group('end')
        cues.append({
            'start': start,
            'end': end,
            'text': text,
            'has_inline_timestamps': has_inline_timestamps,
            'duration_ms': max(0, _time_to_ms(end) - _time_to_ms(start)),
        })

    if not cues:
        return ''

    optimized_cues = []
    consumed_indices = set()
    youtube_like = any(cue['has_inline_timestamps'] for cue in cues)

    if youtube_like:
        for index, cue in enumerate(cues):
            if cue['has_inline_timestamps'] or cue['duration_ms'] > 120 or index == 0:
                continue
            previous_cue = cues[index - 1]
            if not previous_cue['has_inline_timestamps']:
                continue
            optimized_cues.append({
                'start': previous_cue['start'],
                'end': cue['end'],
                'text': cue['text'],
            })
            consumed_indices.add(index - 1)
            consumed_indices.add(index)

        for index, cue in enumerate(cues):
            if index in consumed_indices or not cue['text']:
                continue
            optimized_cues.append({
                'start': cue['start'],
                'end': cue['end'],
                'text': cue['text'],
            })
    else:
        optimized_cues = [
            {'start': cue['start'], 'end': cue['end'], 'text': cue['text']}
            for cue in cues
            if cue['text']
        ]

    optimized_cues.sort(key=lambda cue: (_time_to_ms(cue['start']), _time_to_ms(cue['end'])))

    deduped_cues = []
    for cue in optimized_cues:
        if deduped_cues and deduped_cues[-1]['text'] == cue['text']:
            if _time_to_ms(cue['end']) > _time_to_ms(deduped_cues[-1]['end']):
                deduped_cues[-1]['end'] = cue['end']
            continue
        deduped_cues.append(cue)

    srt_blocks = []
    for index, cue in enumerate(deduped_cues, 1):
        srt_blocks.append(
            f"{index}\n"
            f"{cue['start'].replace('.', ',')} --> {cue['end'].replace('.', ',')}\n"
            f"{cue['text']}"
        )
    return '\n\n'.join(srt_blocks).strip()


class TaskCancelledError(Exception):
    """任务取消异常，用于中断执行流程"""


_TASK_CANCEL_FLAGS = {}
_TASK_CANCEL_LOCK = threading.Lock()
_ACTIVE_TASK_IDS = set()
_ACTIVE_TASKS_LOCK = threading.Lock()
_TASK_SCHEDULING_LOCK = threading.RLock()


def get_task_cancel_event(task_id):
    """获取任务取消事件（若不存在则创建）"""
    if not task_id:
        return None
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_FLAGS.get(task_id)
        if not event:
            event = threading.Event()
            _TASK_CANCEL_FLAGS[task_id] = event
        return event


def request_task_cancel(task_id):
    """请求取消任务，用于快速终止运行中的任务"""
    event = get_task_cancel_event(task_id)
    if event:
        event.set()
    return event


def is_task_cancelled(task_id):
    """检查任务是否已被请求取消"""
    if not task_id:
        return False
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_FLAGS.get(task_id)
        return bool(event and event.is_set())


def clear_task_cancel(task_id, clear_flag=False):
    """清理任务取消标记"""
    if not task_id or not clear_flag:
        return
    with _TASK_CANCEL_LOCK:
        _TASK_CANCEL_FLAGS.pop(task_id, None)


def _mark_task_active(task_id: str) -> bool:
    """标记任务线程已激活，返回 False 表示同任务线程已存在。"""
    if not task_id:
        return False
    with _ACTIVE_TASKS_LOCK:
        if task_id in _ACTIVE_TASK_IDS:
            return False
        _ACTIVE_TASK_IDS.add(task_id)
        return True


def _mark_task_inactive(task_id: str) -> None:
    if not task_id:
        return
    with _ACTIVE_TASKS_LOCK:
        _ACTIVE_TASK_IDS.discard(task_id)


def _is_task_active(task_id: str) -> bool:
    if not task_id:
        return False
    with _ACTIVE_TASKS_LOCK:
        return task_id in _ACTIVE_TASK_IDS


def _get_active_task_ids() -> set:
    with _ACTIVE_TASKS_LOCK:
        return set(_ACTIVE_TASK_IDS)


def _raise_if_cancelled(task_id, task_logger=None):
    if is_task_cancelled(task_id):
        if task_logger:
            task_logger.info("检测到任务取消请求，终止任务执行")
        raise TaskCancelledError("任务已取消")

# 确保目录存在
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# 任务状态定义
TASK_STATES = {
    'PENDING': 'pending',                 # 等待处理
    'DOWNLOADING': 'downloading',         # 正在下载
    'DOWNLOADED': 'downloaded',           # 下载完成
    'ASR_TRANSCRIBING': 'asr_transcribing',  # 语音转写中
    'TRANSLATING_SUBTITLE': 'translating_subtitle',  # 正在翻译字幕
    'ENCODING_VIDEO': 'encoding_video',   # 正在转码视频
    'TRANSLATING': 'translating',         # 正在翻译
    'TAGGING': 'tagging',                 # 正在生成标签
    'PARTITIONING': 'partitioning',       # 正在推荐分区
    'MODERATING': 'moderating',           # 正在内容审核
    'AWAITING_REVIEW': 'awaiting_manual_review',  # 等待人工审核
    'READY_FOR_UPLOAD': 'ready_for_upload',      # 准备上传
    'UPLOADING': 'uploading',             # 正在上传
    'COMPLETED': 'completed',             # 任务完成
    'FAILED': 'failed'                    # 任务失败
}

# 所有"处理中"状态，用于 reset_stuck_tasks 和 recover_interrupted_tasks_to_pending
PROCESSING_STATES = [
    'fetching_info',
    'info_fetched',
    TASK_STATES['TRANSLATING'],
    TASK_STATES['TAGGING'],
    TASK_STATES['PARTITIONING'],
    TASK_STATES['MODERATING'],
    TASK_STATES['DOWNLOADING'],
    TASK_STATES['DOWNLOADED'],
    TASK_STATES['ASR_TRANSCRIBING'],
    TASK_STATES['TRANSLATING_SUBTITLE'],
    TASK_STATES['ENCODING_VIDEO'],
    TASK_STATES['UPLOADING'],
]

# 任务流水线断点续跑（checkpoint）
# - 目标：进程异常退出后，重启可从“最后已完成阶段”继续。
# - 存储：tasks 表新增 pipeline_checkpoint 字段，避免 downloads 目录被清空导致断点丢失。
PIPELINE_CHECKPOINT_FIELD = 'pipeline_checkpoint'

PIPELINE_STAGE_FETCH_INFO = 'fetch_info'
PIPELINE_STAGE_TRANSLATE_CONTENT = 'translate_content'
PIPELINE_STAGE_GENERATE_TAGS = 'generate_tags'
PIPELINE_STAGE_RECOMMEND_PARTITION = 'recommend_partition'
PIPELINE_STAGE_MODERATE_CONTENT = 'moderate_content'
PIPELINE_STAGE_DOWNLOAD_VIDEO = 'download_video'
PIPELINE_STAGE_TRANSLATE_SUBTITLE = 'translate_subtitle'
PIPELINE_STAGE_UPLOAD_TO_ACFUN = 'upload_to_acfun'

PIPELINE_STAGE_ORDER = [
    PIPELINE_STAGE_FETCH_INFO,
    PIPELINE_STAGE_TRANSLATE_CONTENT,
    PIPELINE_STAGE_GENERATE_TAGS,
    PIPELINE_STAGE_RECOMMEND_PARTITION,
    PIPELINE_STAGE_MODERATE_CONTENT,
    PIPELINE_STAGE_DOWNLOAD_VIDEO,
    PIPELINE_STAGE_TRANSLATE_SUBTITLE,
    PIPELINE_STAGE_UPLOAD_TO_ACFUN,
]

UPLOAD_TARGET_ACFUN = 'acfun'
UPLOAD_TARGET_BILIBILI = 'bilibili'
UPLOAD_TARGET_BOTH = 'both'
VALID_UPLOAD_TARGETS = {UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI, UPLOAD_TARGET_BOTH}

PARTITION_FIELD_MAP = {
    UPLOAD_TARGET_ACFUN: {
        'recommended': 'recommended_partition_id_acfun',
        'selected': 'selected_partition_id_acfun',
    },
    UPLOAD_TARGET_BILIBILI: {
        'recommended': 'recommended_partition_id_bilibili',
        'selected': 'selected_partition_id_bilibili',
    },
}

_RESUME_RECOVERY_DONE = False


def normalize_upload_target(upload_target):
    """归一化任务投稿平台，默认 acfun。"""
    target = str(upload_target or '').strip().lower()
    if target not in VALID_UPLOAD_TARGETS:
        return UPLOAD_TARGET_ACFUN
    return target


def resolve_cookie_file_path(
    path_value,
    default_relative_path,
    service_name="Cookie",
    logger_obj=None,
    allow_json_txt_fallback=False
):
    """解析 Cookie 文件路径并可选在 .json/.txt 之间做兼容回退。"""
    raw_path = str(path_value or default_relative_path or '').strip()
    if not raw_path:
        return ''

    app_root = get_app_root_dir()
    resolved_path = raw_path if os.path.isabs(raw_path) else os.path.join(app_root, raw_path)
    resolved_path = os.path.normpath(resolved_path)

    if os.path.exists(resolved_path):
        return resolved_path

    if not allow_json_txt_fallback:
        return resolved_path

    base, ext = os.path.splitext(resolved_path)
    ext_lower = ext.lower()
    if ext_lower == '.json':
        alt_candidates = [base + '.txt']
    elif ext_lower == '.txt':
        alt_candidates = [base + '.json']
    else:
        alt_candidates = [resolved_path + '.json', resolved_path + '.txt']

    for alt_path in alt_candidates:
        if not os.path.exists(alt_path):
            continue
        target_logger = logger_obj if logger_obj is not None else logger
        try:
            target_logger.warning(
                f"{service_name} Cookies路径 {resolved_path} 不存在，自动回退到兼容路径: {alt_path}"
            )
        except Exception:
            pass
        return alt_path

    return resolved_path


def resolve_youtube_cookies_path(config, logger_obj=None):
    """优先解析配置路径，仅在目标缺失时兼容旧版 Cookie 存放位置。"""
    target_logger = logger_obj if logger_obj is not None else logger
    configured_path = str((config or {}).get('YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt') or '').strip()
    resolved_path = resolve_cookie_file_path(
        configured_path,
        'cookies/yt_cookies.txt',
        service_name='YouTube',
        logger_obj=target_logger,
    )
    if os.path.isfile(resolved_path):
        return resolved_path

    app_root = get_app_root_dir()
    file_name = os.path.basename(configured_path or 'yt_cookies.txt')
    legacy_candidates = [os.path.join(app_root, 'config', file_name)]
    if getattr(sys, 'frozen', False) and configured_path and not os.path.isabs(configured_path):
        legacy_candidates.append(os.path.join(app_root, '_internal', configured_path))

    normalized_target = os.path.normcase(os.path.normpath(resolved_path))
    seen = {normalized_target}
    for candidate in legacy_candidates:
        normalized_candidate = os.path.normcase(os.path.normpath(candidate))
        if normalized_candidate in seen:
            continue
        seen.add(normalized_candidate)
        if not os.path.isfile(candidate):
            continue
        target_logger.warning(
            "配置的YouTube Cookies文件不存在，临时回退到旧版路径: %s；"
            "请重新上传或同步 Cookies 以迁移到配置路径。",
            candidate,
        )
        return candidate

    target_logger.warning("指定的YouTube Cookies文件不存在: %s", resolved_path)
    return None


def _get_task_upload_target(task, fallback=UPLOAD_TARGET_ACFUN):
    if not task:
        return normalize_upload_target(fallback)
    task_target = task.get('upload_target')
    if not task_target:
        return normalize_upload_target(fallback)
    return normalize_upload_target(task_target)


def _task_has_upload_response(task, upload_target=None):
    if not task:
        return False
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    if target == UPLOAD_TARGET_BOTH:
        return bool(task.get('acfun_upload_response')) and bool(task.get('bilibili_upload_response'))
    if target == UPLOAD_TARGET_BILIBILI:
        return bool(task.get('bilibili_upload_response'))
    return bool(task.get('acfun_upload_response'))


def _task_has_platform_upload_response(task, platform):
    if not task:
        return False
    p = normalize_upload_target(platform)
    if p == UPLOAD_TARGET_BILIBILI:
        return bool(task.get('bilibili_upload_response'))
    return bool(task.get('acfun_upload_response'))


def _build_task_notification_payload(task, overrides=None) -> dict:
    merged_task = dict(task or {})
    if overrides:
        merged_task.update(overrides)
    return {
        'task_id': str(merged_task.get('id') or '').strip(),
        'youtube_url': str(merged_task.get('youtube_url') or '').strip(),
        'upload_target': normalize_upload_target(merged_task.get('upload_target')),
        'status': str(merged_task.get('status') or '').strip(),
        'video_title_original': str(merged_task.get('video_title_original') or '').strip(),
        'video_title_translated': str(merged_task.get('video_title_translated') or '').strip(),
        'error_message': str(merged_task.get('error_message') or '').strip(),
        'asr_warning_message': str(merged_task.get('asr_warning_message') or '').strip(),
        'subtitle_warning_message': str(merged_task.get('subtitle_warning_message') or '').strip(),
        'acfun_uploaded': bool(merged_task.get('acfun_upload_response')),
        'bilibili_uploaded': bool(merged_task.get('bilibili_upload_response')),
    }


def _get_upload_platforms_for_target(upload_target):
    target = normalize_upload_target(upload_target)
    if target == UPLOAD_TARGET_BOTH:
        return [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]
    if target == UPLOAD_TARGET_BILIBILI:
        return [UPLOAD_TARGET_BILIBILI]
    return [UPLOAD_TARGET_ACFUN]


def _get_effective_metadata_limits(upload_target):
    """返回当前任务应执行的标题/简介限制。

    规则：
    - both：按双平台共同最低限制执行
    - bilibili：按 bilibili 限制执行
    - acfun / 其他：按 AcFun 限制执行
    """
    target = normalize_upload_target(upload_target)
    if target == UPLOAD_TARGET_BILIBILI:
        return {
            'title_limit': 80,
            'description_limit': 2000,
        }
    return {
        'title_limit': 50,
        'description_limit': 1000,
    }


def _get_pending_upload_platforms(task, upload_target=None):
    if not task:
        return []
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    platforms = _get_upload_platforms_for_target(target)
    return [p for p in platforms if not _task_has_platform_upload_response(task, p)]


def _has_partial_upload_success(task, upload_target=None):
    if not task:
        return False
    target = normalize_upload_target(upload_target or task.get('upload_target'))
    platforms = _get_upload_platforms_for_target(target)
    if len(platforms) <= 1:
        return False
    has_success = any(_task_has_platform_upload_response(task, p) for p in platforms)
    has_pending = any(not _task_has_platform_upload_response(task, p) for p in platforms)
    return has_success and has_pending


def _get_partition_field_name(platform: str, field_type: str) -> str:
    p = normalize_upload_target(platform)
    return PARTITION_FIELD_MAP.get(p, PARTITION_FIELD_MAP[UPLOAD_TARGET_ACFUN]).get(field_type, '')


def _get_task_partition_id(task, platform, prefer_selected=True):
    if not task:
        return ''

    selected_field = _get_partition_field_name(platform, 'selected')
    recommended_field = _get_partition_field_name(platform, 'recommended')
    selected_value = str(task.get(selected_field) or '').strip()
    recommended_value = str(task.get(recommended_field) or '').strip()

    if prefer_selected:
        if selected_value:
            return selected_value
        if recommended_value:
            return recommended_value
    else:
        if recommended_value:
            return recommended_value
        if selected_value:
            return selected_value

    return ''


def _safe_json_loads(value, default):
    try:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        s = str(value).strip()
        if not s:
            return default
        return json.loads(s)
    except Exception:
        return default


def _normalize_tags_list(value):
    parsed = _safe_json_loads(value, [])
    if not isinstance(parsed, list):
        return []
    tags = []
    for tag in parsed:
        tag_text = str(tag or '').strip()
        if tag_text:
            tags.append(tag_text)
    return tags


def _as_bool(value):
    """将配置值稳健转换为布尔值（兼容 bool/int/str）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ('true', '1', 'on', 'yes')


def _as_int(value, default, minimum=None):
    """稳健地将值转换为整数，并可选限制最小值。"""
    try:
        if value is None:
            result = int(default)
        else:
            result = int(str(value).strip())
    except Exception:
        result = int(default)

    if minimum is not None:
        result = max(int(minimum), result)
    return result


def _normalize_task_text(value) -> str:
    return str(value or '').strip()


def _get_missing_required_translation_fields(task, config) -> list:
    if not task:
        return []

    missing_fields = []
    if _as_bool(config.get('TRANSLATE_TITLE', True)) and _normalize_task_text(task.get('video_title_original')):
        if not _normalize_task_text(task.get('video_title_translated')):
            missing_fields.append('title')

    if _as_bool(config.get('TRANSLATE_DESCRIPTION', True)) and _normalize_task_text(task.get('description_original')):
        if not _normalize_task_text(task.get('description_translated')):
            missing_fields.append('description')

    return missing_fields


def _build_missing_translation_review_message(field_names) -> str:
    labels = {
        'title': '标题',
        'description': '简介',
    }
    normalized_fields = [
        str(field_name).strip()
        for field_name in (field_names or [])
        if field_name is not None and str(field_name).strip()
    ]
    readable = [labels.get(field_name, field_name) for field_name in normalized_fields]
    if not readable:
        return "自动翻译未完成，任务已转入人工审核。"
    return f"自动翻译未完成：{'、'.join(readable)}仍缺少有效译文，任务已转入人工审核。"


#: ``_run_subtitle_qc`` 的返回值之一：用户**主动关闭**了字幕质检。
#: 必须与 ``None``（质检启用但没跑成：文件缺失 / 执行异常）区分开 —— 二者
#: 混用会造成「关掉质检反而更严格」的反向回归。
SUBTITLE_QC_DISABLED = 'disabled'


def _is_asr_subtitle_artifact(task_id, subtitle_path) -> bool:
    """判断某字幕文件是否来自 ASR（而非平台自带 / 人工提供）。

    质检与「移出复用范围」都只应约束 ASR 产物：它才是幻觉与时间轴错位的高
    风险来源。外部字幕走原有流程，既不增加 AI 调用成本，也不会被 ASR 的
    历史标记误拦。

    判据**只看文件名**（``asr_*.srt``）。任务级 ``subtitle_quality_state``
    描述的是「ASR 产物的来源可靠度」，它不构成平台字幕的罪名：此前该状态为
    ``degraded`` / ``failed`` 时本函数无条件返回 True，于是任务目录里一份
    干净的 ``video.zh.srt`` 也会被送去做质检，被拒后还会被改名破坏
    （``video.zh.srt`` → ``video.zh.rejected.txt``），用户既有产物直接丢失。
    这与门控层 ``_embed_guard(..., asr_artifact=False)`` 刻意让外部字幕不受
    质量结局约束的意图也自相矛盾。
    """
    try:
        name = os.path.basename(str(subtitle_path or '')).lower()
    except Exception:
        return False
    if not name:
        return False
    if name.startswith('asr_'):
        return True
    return name == f"asr_{str(task_id or '').lower()}.srt"


def _candidate_subtitle_paths(task, task_dir: str) -> list:
    """收集任务相关字幕文件路径，用于判断已烧录视频产物是否仍然新鲜。"""
    paths = []
    if isinstance(task, dict):
        for key in ('subtitle_path_original', 'subtitle_path_translated'):
            value = str(task.get(key) or '').strip()
            if value:
                paths.append(value)
    for ext in ('.srt', '.vtt'):
        paths.append(os.path.join(task_dir, f"asr_{os.path.basename(task_dir)}{ext}"))
    try:
        for name in os.listdir(task_dir):
            if isinstance(name, str) and name.lower().endswith(('.srt', '.vtt')):
                paths.append(os.path.join(task_dir, name))
    except Exception:
        pass
    seen = set()
    unique_paths = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            unique_paths.append(path)
    return unique_paths


def _is_asr_enabled(config: dict) -> bool:
    """ASR总开关。"""
    return _as_bool(config.get('SPEECH_RECOGNITION_ENABLED', False))


def _subtitle_block_reasons(
    config: dict,
    quality_state,
    qc_failed,
    *,
    qc_cleared: bool = False,
    qc_enabled=None,
) -> list:
    """返回烧录拦截原因列表；空列表表示放行。

    ``_subtitle_embed_allowed`` 与调用方的归因记录共用这套判定，避免两处
    逻辑漂移出「拦截了但说不出为什么」的日志。

    ``qc_enabled=None`` 时从 ``config['SUBTITLE_QC_ENABLED']`` 读取，因此
    「关掉质检」的效果对直接调用本函数的代码同样可见，不需要每个调用点
    自己解析开关。

    两个开关的作用域**互不重叠**，缺一不可：

    - ``ASR_FAILURE_BLOCKS_EMBED=False``（逃生口）只放宽「ASR 来源结局」
      （``failed`` / ``degraded``），**不放过**明确的质检失败结论 ——
      质检结论针对的是字幕文件内容本身，与 ASR 来源可靠度是两件事。
    - ``SUBTITLE_QC_ENABLED=False``（``qc_enabled=False``）表示用户主动放弃
      质检这道防线：此时既不再看历史 ``qc_failed``，也不再要求 ``degraded``
      素材必须通过严格质检（否则「关掉质检」会变成「更严格」，与用户意图相反）。
    """
    asr_blocks = _as_bool(config.get('ASR_FAILURE_BLOCKS_EMBED', True))
    if qc_enabled is None:
        qc_enabled = _as_bool(config.get('SUBTITLE_QC_ENABLED', True))
    qc_active = bool(qc_enabled)
    state = str(quality_state or '').strip().lower()

    reasons = []
    if state == 'failed' and asr_blocks:
        reasons.append('asr_quality_failed')
    if qc_active and _as_bool(qc_failed) and not qc_cleared:
        reasons.append('subtitle_qc_rejected')
    if state == 'degraded' and asr_blocks and qc_active and not qc_cleared:
        reasons.append('asr_quality_degraded_without_strict_qc')
    return reasons


#: 门控原因 → 落库的 ``subtitle_warning_message`` 取值。
#: UI（``templates/partials/task_row.html``、``task_card.html``）按这些取值渲染
#: tooltip，新增原因必须同步补映射，否则界面会退化成显示原始英文键名。
SUBTITLE_BLOCK_WARNING_MESSAGES = {
    'asr_quality_failed': 'asr_failed_block_embed',
    'asr_quality_degraded_without_strict_qc': 'asr_degraded_block_embed',
    'subtitle_qc_rejected': 'subtitle_qc_rejected',
    'qc_unavailable': 'qc_unavailable',
}


def _block_reason_to_warning(reason) -> str:
    """把门控原因翻译成落库用的警告文案。"""
    return SUBTITLE_BLOCK_WARNING_MESSAGES.get(str(reason or '').strip(), 'asr_failed_block_embed')


#: 表示「字幕被质量门控拒绝」的落库警告取值集合。
#: 这些取值一旦出现在任务上，就说明本轮字幕没有烧进成片，因此字幕阶段
#: **不得**被视为已完成（见 ``_is_subtitle_stage_rejected``）。
SUBTITLE_BLOCK_WARNING_VALUES = frozenset(SUBTITLE_BLOCK_WARNING_MESSAGES.values())


def _is_subtitle_stage_rejected(task) -> bool:
    """字幕阶段是否被质量门控拒绝（因此不该被 checkpoint 标为已完成）。

    判定必须覆盖三类信号，缺一不可：

    - ``subtitle_qc_failed == 1``：质检给出了明确失败结论；
    - ``subtitle_quality_state == 'failed'``：ASR/VAD 来源不可信；
    - ``subtitle_warning_message`` 落在拦截取值集合内：包含**质检不可用**
      （``qc_unavailable``）。这条尤其关键 —— 质检没跑成时不会有
      ``subtitle_qc_failed`` 结论，若只认布尔字段，该路径的拦截不会阻止
      checkpoint 把字幕阶段标记完成，用户重跑就被永久跳过。
    """
    if not task:
        return False
    if task.get('subtitle_qc_failed') == 1:
        return True
    if str(task.get('subtitle_quality_state') or '').strip().lower() == 'failed':
        return True
    warning = str(task.get('subtitle_warning_message') or '').strip().lower()
    return warning in SUBTITLE_BLOCK_WARNING_VALUES


def _subtitle_embed_allowed(
    config: dict,
    quality_state,
    qc_failed,
    *,
    qc_cleared: bool = False,
    qc_enabled=None,
    task_logger=None,
) -> bool:
    """统一烧录门控：决定当前字幕是否允许烧进成片。

    - ``quality_state == 'failed'``：ASR/VAD 来源不可信，拒绝烧录；
    - ``quality_state == 'degraded'``：字幕来源退化，必须本次通过严格质检才放行；
    - ``qc_failed`` 为真且本次未通过质检：拒绝烧录。

    这是「VAD/ASR 已报错却照样烧录」的修复点：此前 ``asr_warning_message``
    只写不读，告警与烧录决策之间没有任何连接边。
    判定细节与开关作用域见 ``_subtitle_block_reasons``。
    """
    reasons = _subtitle_block_reasons(
        config, quality_state, qc_failed, qc_cleared=qc_cleared, qc_enabled=qc_enabled
    )
    if not reasons:
        return True

    if task_logger is not None:
        try:
            task_logger.warning(
                "字幕烧录被门控拦截: quality_state=%s, qc_failed=%s, qc_cleared=%s, "
                "asr_blocks=%s, qc_enabled=%s, reasons=%s",
                str(quality_state or '').strip().lower() or 'ok',
                bool(_as_bool(qc_failed)),
                bool(qc_cleared),
                bool(_as_bool(config.get('ASR_FAILURE_BLOCKS_EMBED', True))),
                bool(_as_bool(config.get('SUBTITLE_QC_ENABLED', True))
                     if qc_enabled is None else qc_enabled),
                reasons,
            )
        except Exception:
            pass
    return False


def _qc_result_allows_embed(qc_result, config) -> bool:
    """质检三态 + 逃生口 → 是否允许烧录：三个调用点的**唯一**判定入口。

    - ``True``：本次质检通过 → 放行；
    - ``SUBTITLE_QC_DISABLED``：用户主动关闭质检 → 放行；
    - ``None``：质检启用但没跑成（文件缺失 / 执行异常）→ 只有逃生口
      ``ASR_FAILURE_BLOCKS_EMBED=False`` 打开时才放行；
    - ``False``：明确的质检失败结论 → 任何情况下都拦截（逃生口不适用）。

    为什么必须收口到一处：此前三个调用点各写一份判定，其中上传前那条路径
    （``_prepare_subtitle_for_upload``）完全不读逃生口，于是同一份 ``asr_*.srt``
    会因为「走哪条分支 / 哪个阶段」而得到相反结论 —— README 承诺该开关可放宽
    「质检没跑成」的拦截，实际上只在部分路径成立。
    """
    if qc_result is True or qc_result is SUBTITLE_QC_DISABLED:
        return True
    if qc_result is None:
        return not _as_bool((config or {}).get('ASR_FAILURE_BLOCKS_EMBED', True))
    return False


def _parse_pipeline_checkpoint(raw_value):
    data = _safe_json_loads(raw_value, {})
    if not isinstance(data, dict):
        data = {}
    completed = data.get('completed', [])
    if not isinstance(completed, list):
        completed = []
    completed = [str(x) for x in completed if x]
    # 过滤未知stage，保持向前兼容
    completed_set = {x for x in completed if x in set(PIPELINE_STAGE_ORDER)}
    return {
        'version': int(data.get('version', 1) or 1),
        'completed': sorted(completed_set, key=lambda s: PIPELINE_STAGE_ORDER.index(s)),
        'updated_at': data.get('updated_at'),
    }


def _infer_completed_stages_from_task(task):
    if not task:
        return set()

    completed = set()

    # 采集信息：有元数据路径/原标题/原始描述，基本可视为完成
    if task.get('metadata_json_path_local') or task.get('video_title_original') or task.get('description_original'):
        completed.add(PIPELINE_STAGE_FETCH_INFO)

    # 翻译标题/描述：任一翻译字段存在即可视为完成
    if task.get('video_title_translated') or task.get('description_translated'):
        completed.add(PIPELINE_STAGE_TRANSLATE_CONTENT)

    # 标签：字段存在（即使是空数组字符串）也认为已跑过
    if task.get('tags_generated') is not None:
        completed.add(PIPELINE_STAGE_GENERATE_TAGS)

    # 分区推荐：有推荐或已选分区
    if (
        task.get('recommended_partition_id_acfun')
        or task.get('selected_partition_id_acfun')
        or task.get('recommended_partition_id_bilibili')
        or task.get('selected_partition_id_bilibili')
    ):
        completed.add(PIPELINE_STAGE_RECOMMEND_PARTITION)

    # 审核：已有审核结果或进入人工审核
    if task.get('moderation_result') or task.get('status') == TASK_STATES['AWAITING_REVIEW']:
        completed.add(PIPELINE_STAGE_MODERATE_CONTENT)

    # 下载：本地视频路径存在且文件存在（优先），或状态已下载完成
    video_path = task.get('video_path_local')
    if task.get('status') == TASK_STATES['DOWNLOADED']:
        completed.add(PIPELINE_STAGE_DOWNLOAD_VIDEO)
    elif isinstance(video_path, str) and video_path and os.path.exists(video_path):
        completed.add(PIPELINE_STAGE_DOWNLOAD_VIDEO)

    # 字幕：译文路径存在才无条件认定完成。若被质量门控拒绝（质检失败 / 质检不可用
    # / ASR 质量结局 failed），不得把字幕阶段推断为已完成 —— 否则任务重跑会被
    # checkpoint 永久跳过，既拿不到新字幕，也无法重新质检。
    subtitle_rejected = _is_subtitle_stage_rejected(task)
    subtitle_keys = (
        ('subtitle_path_translated',)
        if subtitle_rejected
        else ('subtitle_path_original', 'subtitle_path_translated')
    )
    for key in subtitle_keys:
        p = task.get(key)
        if isinstance(p, str) and p and os.path.exists(p):
            completed.add(PIPELINE_STAGE_TRANSLATE_SUBTITLE)
            break

    # 上传：有上传响应或已完成
    upload_target = _get_task_upload_target(task)
    if _task_has_upload_response(task, upload_target) or task.get('status') == TASK_STATES['COMPLETED']:
        completed.add(PIPELINE_STAGE_UPLOAD_TO_ACFUN)

    return completed


def _get_completed_stages(task):
    cp = _parse_pipeline_checkpoint(task.get(PIPELINE_CHECKPOINT_FIELD) if task else None)
    completed = set(cp.get('completed', []) or [])
    completed |= _infer_completed_stages_from_task(task)
    # 差集修正：checkpoint 只能做并集，而 ``_infer_completed_stages_from_task``
    # 只能**加**阶段、无法移除 checkpoint 里已有的项。于是「上一轮已写入
    # translate_subtitle、本轮被门控拒绝」的任务仍会带着该阶段被判定完成，
    # 用户即便修好配置重跑也只会打印「跳过字幕处理（checkpoint 已完成）」。
    # 这里显式剔除；调用方会把本函数结果写回 checkpoint，因此 DB 里的陈旧项
    # 也会随之被清理，不需要额外的迁移。
    if _is_subtitle_stage_rejected(task):
        completed.discard(PIPELINE_STAGE_TRANSLATE_SUBTITLE)
    return completed


def _persist_pipeline_checkpoint(task_id, completed_stages):
    stages = [s for s in PIPELINE_STAGE_ORDER if s in set(completed_stages or set())]
    payload = {
        'version': 1,
        'completed': stages,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    update_task(task_id, silent=True, **{PIPELINE_CHECKPOINT_FIELD: json.dumps(payload, ensure_ascii=False)})


def _mark_stage_done(task_id, completed_stages, stage):
    if stage in completed_stages:
        return completed_stages
    completed_stages = set(completed_stages or set())
    completed_stages.add(stage)
    _persist_pipeline_checkpoint(task_id, completed_stages)
    return completed_stages


def recover_interrupted_tasks_to_pending():
    """将进程意外退出后卡在“处理中状态”的任务恢复为 pending，以便重启后自动续跑。"""
    processing_states = PROCESSING_STATES

    conn = get_db_connection()
    try:
        placeholders = ','.join(['?'] * len(processing_states))
        cursor = conn.execute(
            f'SELECT id, status, upload_target, acfun_upload_response, bilibili_upload_response FROM tasks WHERE status IN ({placeholders})',
            tuple(processing_states)
        )
        rows = cursor.fetchall() or []
        if not rows:
            return 0

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        recovered = 0

        for row in rows:
            task_id = row['id']
            status = row['status']
            upload_target = normalize_upload_target(row['upload_target'])
            has_acfun_resp = bool(row['acfun_upload_response'])
            has_bilibili_resp = bool(row['bilibili_upload_response'])
            if upload_target == UPLOAD_TARGET_BOTH:
                has_upload_resp = has_acfun_resp and has_bilibili_resp
                has_partial_upload_resp = has_acfun_resp != has_bilibili_resp
            elif upload_target == UPLOAD_TARGET_BILIBILI:
                has_upload_resp = has_bilibili_resp
                has_partial_upload_resp = False
            else:
                has_upload_resp = has_acfun_resp
                has_partial_upload_resp = False

            # 若上传响应已存在，直接标记为 completed（避免重复上传）
            if has_upload_resp:
                conn.execute(
                    'UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?',
                    (TASK_STATES['COMPLETED'], now_str, task_id)
                )
                recovered += 1
                continue

            # 双平台仅部分成功：恢复为 failed，让 process_task 走“失败点续传”只补失败平台
            if has_partial_upload_resp:
                conn.execute(
                    'UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?',
                    (TASK_STATES['FAILED'], now_str, task_id)
                )
                recovered += 1
                continue

            # 其他处理中状态：恢复为 pending，由流水线根据checkpoint跳过已完成阶段。
            # 同时清理上一轮的字幕质检/质量结局标记，否则这些与任务终身绑定的旧标记
            # 会让恢复后的任务永久跳过字幕生成（既拿不到新字幕，也无法重新质检）。
            conn.execute(
                '''UPDATE tasks
                   SET status = ?, updated_at = ?,
                       subtitle_qc_failed = 0,
                       subtitle_qc_reason = NULL,
                       subtitle_qc_score = NULL,
                       subtitle_qc_checked_at = NULL,
                       subtitle_quality_state = NULL,
                       asr_warning_message = NULL,
                       subtitle_warning_message = NULL
                   WHERE id = ?''',
                (TASK_STATES['PENDING'], now_str, task_id)
            )
            recovered += 1

        conn.commit()
        if recovered:
            logger.info(f"断点续跑：已恢复 {recovered} 个处理中任务为 pending")
        return recovered
    except Exception as e:
        logger.warning(f"断点续跑：恢复处理中任务失败（忽略）：{e}")
        return 0
    finally:
        conn.close()

# WebSocket实时通知功能已移除，改为使用传统页面刷新方式

# 设置日志记录器
def setup_logger(name):
    """
    设置通用日志记录器
    
    Args:
        name: 日志记录器名称
        
    Returns:
        logger: 配置好的日志记录器
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:  # 避免重复添加处理器
        logger.setLevel(logging.INFO)
        
        # 文件处理器
        log_file = os.path.join(LOGS_DIR, f'{name}.log')
        file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(file_formatter)
        console_handler.setLevel(logging.INFO)
        logger.addHandler(console_handler)
        
    return logger

# 任务管理器日志
logger = setup_logger('task_manager')

# 实时任务事件订阅管理
_TASK_EVENT_SUBSCRIBERS = set()
_TASK_EVENT_LOCK = threading.Lock()
_TASK_EVENT_QUEUE_SIZE = 128  # 增加队列容量，防止事件丢失


def register_task_updates_listener():
    """注册一个实时任务事件监听队列。"""
    listener_queue = queue.Queue(maxsize=_TASK_EVENT_QUEUE_SIZE)
    with _TASK_EVENT_LOCK:
        _TASK_EVENT_SUBSCRIBERS.add(listener_queue)
    return listener_queue


def unregister_task_updates_listener(listener_queue):
    """移除实时任务事件监听队列。"""
    with _TASK_EVENT_LOCK:
        _TASK_EVENT_SUBSCRIBERS.discard(listener_queue)


def _emit_task_event(event):
    with _TASK_EVENT_LOCK:
        subscribers = list(_TASK_EVENT_SUBSCRIBERS)

    for listener in subscribers:
        try:
            listener.put_nowait(event)
        except queue.Full:
            try:
                listener.get_nowait()
            except queue.Empty:
                pass
            try:
                listener.put_nowait(event)
            except queue.Full:
                continue


def publish_task_event(event_type='refresh', data=None):
    """向所有监听者广播任务事件。"""
    event_payload = {
        'type': event_type,
        'data': data or {},
        'timestamp': datetime.utcnow().isoformat()
    }
    _emit_task_event(event_payload)

# 任务处理日志
def setup_task_logger(task_id):
    """
    为特定任务设置日志记录器
    
    Args:
        task_id: 任务ID
        
    Returns:
        logger: 配置好的日志记录器
    """
    log_file = os.path.join(LOGS_DIR, f'task_{task_id}.log')
    logger = logging.getLogger(f'task_{task_id}')
    
    if not logger.handlers:  # 避免重复添加处理器
        logger.setLevel(logging.INFO)
        
        # 文件处理器 - 减少文件大小以降低内存使用
        file_handler = RotatingFileHandler(log_file, maxBytes=5242880, backupCount=3, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        
        # 确保消息不会传播到根日志记录器
        logger.propagate = False
    
    return logger

# 数据库操作
def init_db():
    """初始化数据库，创建tasks表"""
    conn = sqlite3.connect(DB_PATH, timeout=DB_CONNECT_TIMEOUT_SECONDS)
    try:
        conn.execute(f'PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
    except Exception:
        pass
    cursor = conn.cursor()
    
    # 创建tasks表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        youtube_url TEXT NOT NULL,
        upload_target TEXT DEFAULT 'acfun',
        status TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        video_title_original TEXT,
        video_title_translated TEXT,
        description_original TEXT,
        description_translated TEXT,
        tags_generated TEXT,  -- JSON list
        recommended_partition_id TEXT,
        selected_partition_id TEXT,
        recommended_partition_id_acfun TEXT,
        selected_partition_id_acfun TEXT,
        recommended_partition_id_bilibili TEXT,
        selected_partition_id_bilibili TEXT,
        cover_path_local TEXT,
        video_path_local TEXT,
        subtitle_path_original TEXT,  -- 原始字幕文件路径
        subtitle_path_translated TEXT,  -- 翻译后字幕文件路径
        subtitle_language_detected TEXT,  -- 检测到的字幕语言
        subtitle_qc_failed INTEGER DEFAULT 0,  -- 字幕质检是否失败（1=失败）
        subtitle_qc_reason TEXT,  -- 字幕质检失败原因（可选）
        subtitle_qc_score REAL,  -- 字幕质检评分（可选）
        subtitle_qc_checked_at TIMESTAMP,  -- 最近一次实际执行字幕质检的时间（可选）
        metadata_json_path_local TEXT,
        moderation_result TEXT,  -- JSON
        error_message TEXT,
        error_category TEXT,
        pipeline_checkpoint TEXT,  -- JSON: 断点续跑已完成阶段
        upload_progress TEXT,  -- 上传进度
        acfun_upload_response TEXT,
        bilibili_upload_response TEXT,
        asr_warning_message TEXT,  -- ASR/VAD阶段的非致命警告（如vad_low_coverage），不影响上传流程
        subtitle_warning_message TEXT,  -- 字幕处理阶段的非致命警告（如烧录失败），不影响上传流程
        subtitle_quality_state TEXT  -- ASR质量结局（ok/degraded/failed），failed时禁止烧录字幕
    )
    ''')
    
    conn.commit()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS schema_migrations (
        migration_key TEXT PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # 检查并添加新字段（用于数据库升级）
    try:
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'upload_progress' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN upload_progress TEXT")
            logger.info("数据库升级：添加upload_progress字段")
            conn.commit()

        if 'subtitle_qc_failed' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_failed INTEGER DEFAULT 0")
            logger.info("数据库升级：添加subtitle_qc_failed字段")
            conn.commit()

        if 'subtitle_qc_reason' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_reason TEXT")
            logger.info("数据库升级：添加subtitle_qc_reason字段")
            conn.commit()

        if 'subtitle_qc_score' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_score REAL")
            logger.info("数据库升级：添加subtitle_qc_score字段")
            conn.commit()

        if 'subtitle_qc_checked_at' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_qc_checked_at TIMESTAMP")
            logger.info("数据库升级：添加subtitle_qc_checked_at字段")
            conn.commit()

        if 'pipeline_checkpoint' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN pipeline_checkpoint TEXT")
            logger.info("数据库升级：添加pipeline_checkpoint字段")
            conn.commit()

        if 'error_category' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN error_category TEXT")
            cursor.execute(
                """
                UPDATE tasks
                SET error_category = ?
                WHERE status = ?
                  AND error_category IS NULL
                  AND (
                    error_message LIKE '自动翻译失败%'
                    OR error_message LIKE '自动翻译未完成%'
                  )
                """,
                (METADATA_TRANSLATION_ERROR_CATEGORY, TASK_STATES['AWAITING_REVIEW'])
            )
            logger.info("数据库升级：添加error_category字段并迁移历史翻译失败任务")
            conn.commit()

        if 'upload_target' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN upload_target TEXT DEFAULT 'acfun'")
            logger.info("数据库升级：添加upload_target字段")
            conn.commit()

        if 'bilibili_upload_response' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN bilibili_upload_response TEXT")
            logger.info("数据库升级：添加bilibili_upload_response字段")
            conn.commit()

        if 'recommended_partition_id_acfun' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN recommended_partition_id_acfun TEXT")
            logger.info("数据库升级：添加recommended_partition_id_acfun字段")
            conn.commit()

        if 'selected_partition_id_acfun' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN selected_partition_id_acfun TEXT")
            logger.info("数据库升级：添加selected_partition_id_acfun字段")
            conn.commit()

        if 'recommended_partition_id_bilibili' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN recommended_partition_id_bilibili TEXT")
            logger.info("数据库升级：添加recommended_partition_id_bilibili字段")
            conn.commit()

        if 'selected_partition_id_bilibili' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN selected_partition_id_bilibili TEXT")
            logger.info("数据库升级：添加selected_partition_id_bilibili字段")
            conn.commit()

        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]

        # 历史任务分区字段回填迁移：仅执行一次，避免重复全表扫描
        cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key = ? LIMIT 1",
            ('tasks_partition_backfill_v1',)
        )
        partition_backfill_done = cursor.fetchone() is not None

        if not partition_backfill_done:
            try:
                if 'selected_partition_id' in columns and 'selected_partition_id_acfun' in columns:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET selected_partition_id_acfun = selected_partition_id
                        WHERE (upload_target = 'acfun' OR upload_target IS NULL OR TRIM(upload_target) = '')
                          AND (selected_partition_id_acfun IS NULL OR TRIM(selected_partition_id_acfun) = '')
                          AND selected_partition_id IS NOT NULL
                          AND TRIM(selected_partition_id) <> ''
                        """
                    )

                if 'recommended_partition_id' in columns and 'recommended_partition_id_acfun' in columns:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET recommended_partition_id_acfun = recommended_partition_id
                        WHERE (upload_target = 'acfun' OR upload_target IS NULL OR TRIM(upload_target) = '')
                          AND (recommended_partition_id_acfun IS NULL OR TRIM(recommended_partition_id_acfun) = '')
                          AND recommended_partition_id IS NOT NULL
                          AND TRIM(recommended_partition_id) <> ''
                        """
                    )

                if 'selected_partition_id' in columns and 'selected_partition_id_bilibili' in columns:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET selected_partition_id_bilibili = selected_partition_id
                        WHERE upload_target = 'bilibili'
                          AND (selected_partition_id_bilibili IS NULL OR TRIM(selected_partition_id_bilibili) = '')
                          AND selected_partition_id IS NOT NULL
                          AND TRIM(selected_partition_id) <> ''
                        """
                    )

                if 'selected_partition_id' in columns:
                    if 'selected_partition_id_acfun' in columns:
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET selected_partition_id_acfun = selected_partition_id
                            WHERE upload_target = 'both'
                              AND (selected_partition_id_acfun IS NULL OR TRIM(selected_partition_id_acfun) = '')
                              AND selected_partition_id IS NOT NULL
                              AND TRIM(selected_partition_id) <> ''
                            """
                        )
                    if 'selected_partition_id_bilibili' in columns:
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET selected_partition_id_bilibili = selected_partition_id
                            WHERE upload_target = 'both'
                              AND (selected_partition_id_bilibili IS NULL OR TRIM(selected_partition_id_bilibili) = '')
                              AND selected_partition_id IS NOT NULL
                              AND TRIM(selected_partition_id) <> ''
                            """
                        )

                if 'recommended_partition_id' in columns and 'recommended_partition_id_bilibili' in columns:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET recommended_partition_id_bilibili = recommended_partition_id
                        WHERE upload_target = 'bilibili'
                          AND (recommended_partition_id_bilibili IS NULL OR TRIM(recommended_partition_id_bilibili) = '')
                          AND recommended_partition_id IS NOT NULL
                          AND TRIM(recommended_partition_id) <> ''
                        """
                    )

                if 'recommended_partition_id' in columns:
                    if 'recommended_partition_id_acfun' in columns:
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET recommended_partition_id_acfun = recommended_partition_id
                            WHERE upload_target = 'both'
                              AND (recommended_partition_id_acfun IS NULL OR TRIM(recommended_partition_id_acfun) = '')
                              AND recommended_partition_id IS NOT NULL
                              AND TRIM(recommended_partition_id) <> ''
                            """
                        )
                    if 'recommended_partition_id_bilibili' in columns:
                        cursor.execute(
                            """
                            UPDATE tasks
                            SET recommended_partition_id_bilibili = recommended_partition_id
                            WHERE upload_target = 'both'
                              AND (recommended_partition_id_bilibili IS NULL OR TRIM(recommended_partition_id_bilibili) = '')
                              AND recommended_partition_id IS NOT NULL
                              AND TRIM(recommended_partition_id) <> ''
                            """
                        )

                cursor.execute(
                    "INSERT OR IGNORE INTO schema_migrations (migration_key) VALUES (?)",
                    ('tasks_partition_backfill_v1',)
                )
                conn.commit()
                logger.info("数据库升级：历史任务分区字段回填迁移完成")
            except Exception as e2:
                conn.rollback()
                logger.warning("数据库升级：历史任务分区字段回填迁移失败，将在下次启动重试: %s", e2)
        else:
            logger.info("数据库升级：历史任务分区字段回填迁移已执行，跳过")

        if 'asr_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN asr_warning_message TEXT")
            logger.info("数据库升级：添加asr_warning_message字段")
            conn.commit()

        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'subtitle_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_warning_message TEXT")
            logger.info("数据库升级：添加subtitle_warning_message字段")
            conn.commit()

        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'subtitle_quality_state' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN subtitle_quality_state TEXT")
            logger.info("数据库升级：添加subtitle_quality_state字段")
            conn.commit()

        # 数据迁移：将 error_message 中纯 ASR/VAD 警告 token 挪至 asr_warning_message，清空 error_message
        cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_key = ? LIMIT 1",
            ('asr_warning_message_migrate_v1',)
        )
        if cursor.fetchone() is None:
            try:
                _asr_warning_tokens = (
                    'vad_low_coverage', 'vad_no_speech', 'vad_failed',
                    'asr_no_timestamps', 'asr_failed', 'vad_no_usable_window',
                )
                for _token in _asr_warning_tokens:
                    cursor.execute(
                        """
                        UPDATE tasks
                        SET asr_warning_message = TRIM(error_message),
                            error_message = NULL
                        WHERE (asr_warning_message IS NULL OR TRIM(asr_warning_message) = '')
                          AND TRIM(error_message) = ?
                        """,
                        (_token,)
                    )
                cursor.execute(
                    "INSERT OR IGNORE INTO schema_migrations (migration_key) VALUES (?)",
                    ('asr_warning_message_migrate_v1',)
                )
                conn.commit()
                logger.info("数据库升级：ASR警告字段数据迁移完成")
            except Exception as _em:
                conn.rollback()
                logger.warning("数据库升级：ASR警告字段数据迁移失败: %s", _em)

        # 兼容历史任务：空 upload_target 默认回填 acfun
        cursor.execute("UPDATE tasks SET upload_target = 'acfun' WHERE upload_target IS NULL OR TRIM(upload_target) = ''")
        conn.commit()
    except Exception as e:
        logger.warning(f"数据库升级检查失败（可能已是最新版本）: {e}")
    
    conn.close()
    
    logger.info("数据库初始化完成")

def get_db_path():
    """获取数据库文件路径"""
    return DB_PATH

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH, timeout=DB_CONNECT_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row  # 返回字典形式的结果
    try:
        conn.execute(f'PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
    except Exception as e:
        logger.debug(f"设置SQLite连接参数失败，将使用默认参数: {e}")
    return conn

def add_task(youtube_url, upload_target=None):
    """
    添加新任务到数据库
    
    Args:
        youtube_url: YouTube视频URL
        upload_target: 投稿平台(acfun|bilibili|both)，为空则使用配置默认值
        
    Returns:
        task_id: 新创建的任务ID
    """
    task_id = str(uuid.uuid4())
    normalized_target = normalize_upload_target(upload_target)
    conn = get_db_connection()
    
    try:
        if not upload_target:
            try:
                from modules.config_manager import load_config
                cfg = load_config()
                normalized_target = normalize_upload_target(cfg.get('UPLOAD_TARGET_DEFAULT', UPLOAD_TARGET_ACFUN))
            except Exception:
                normalized_target = UPLOAD_TARGET_ACFUN
        conn.execute(
            'INSERT INTO tasks (id, youtube_url, upload_target, status) VALUES (?, ?, ?, ?)',
            (task_id, youtube_url, normalized_target, TASK_STATES['PENDING'])
        )
        conn.commit()
        logger.info(f"新任务添加成功, ID: {task_id}, URL: {youtube_url}, 平台: {normalized_target}")
        
        # 新任务添加后，触发全局任务处理器检查是否需要启动任务
        try:
            from modules.config_manager import load_config
            config = load_config()
            processor = get_global_task_processor(config)
            if processor:
                # 延迟触发，确保数据库事务已提交
                import threading
                import time
                
                def delayed_trigger():
                    time.sleep(0.5)  # 等待0.5秒确保事务提交
                    processor._check_and_start_next_pending_task()
                
                threading.Thread(target=delayed_trigger, daemon=True).start()
                logger.info(f"已触发检查pending任务: {task_id}")
        except Exception as e:
            logger.warning(f"触发任务检查失败，但任务已成功添加: {str(e)}")
            
    except Exception as e:
        logger.error(f"添加任务失败: {str(e)}")
        task_id = None
    finally:
        conn.close()
    if task_id:
        publish_task_event('task_added', {'task_id': task_id})
        emit_notification_event(
            NotificationEvent(
                event_type=EVENT_TASK_ADDED,
                payload={
                    'task_id': task_id,
                    'youtube_url': youtube_url,
                    'upload_target': normalized_target,
                },
            )
        )

    return task_id

def update_task(task_id, silent=False, **kwargs):
    """
    更新任务信息
    
    Args:
        task_id: 任务ID
        silent: 是否静默更新（不记录到主日志）
        **kwargs: 要更新的字段及其值
    
    Returns:
        success: 更新是否成功
    """
    if not kwargs:
        return False
    
    # 记录状态变化
    status_changed = 'status' in kwargs
    new_status = kwargs.get('status') if status_changed else None
    
    # 添加更新时间
    kwargs['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 白名单验证：只允许更新有效的列名
    ALLOWED_COLUMNS = {
        'youtube_url': 'youtube_url = ?',
        'upload_target': 'upload_target = ?',
        'status': 'status = ?',
        'created_at': 'created_at = ?',
        'updated_at': 'updated_at = ?',
        'video_title_original': 'video_title_original = ?',
        'video_title_translated': 'video_title_translated = ?',
        'description_original': 'description_original = ?',
        'description_translated': 'description_translated = ?',
        'tags_generated': 'tags_generated = ?',
        'recommended_partition_id': 'recommended_partition_id = ?',
        'selected_partition_id': 'selected_partition_id = ?',
        'recommended_partition_id_acfun': 'recommended_partition_id_acfun = ?',
        'selected_partition_id_acfun': 'selected_partition_id_acfun = ?',
        'recommended_partition_id_bilibili': 'recommended_partition_id_bilibili = ?',
        'selected_partition_id_bilibili': 'selected_partition_id_bilibili = ?',
        'cover_path_local': 'cover_path_local = ?',
        'video_path_local': 'video_path_local = ?',
        'subtitle_path_original': 'subtitle_path_original = ?',
        'subtitle_path_translated': 'subtitle_path_translated = ?',
        'subtitle_language_detected': 'subtitle_language_detected = ?',
        'subtitle_qc_failed': 'subtitle_qc_failed = ?',
        'subtitle_qc_reason': 'subtitle_qc_reason = ?',
        'subtitle_qc_score': 'subtitle_qc_score = ?',
        'subtitle_qc_checked_at': 'subtitle_qc_checked_at = ?',
        'metadata_json_path_local': 'metadata_json_path_local = ?',
        'moderation_result': 'moderation_result = ?',
        'error_message': 'error_message = ?',
        'error_category': 'error_category = ?',
        'pipeline_checkpoint': 'pipeline_checkpoint = ?',
        'upload_progress': 'upload_progress = ?',
        'acfun_upload_response': 'acfun_upload_response = ?',
        'bilibili_upload_response': 'bilibili_upload_response = ?',
        'asr_warning_message': 'asr_warning_message = ?',
        'subtitle_warning_message': 'subtitle_warning_message = ?',
        'subtitle_quality_state': 'subtitle_quality_state = ?',
    }

    # 过滤掉不在白名单中的列
    filtered_items = [(k, v) for k, v in kwargs.items() if k in ALLOWED_COLUMNS]
    filtered_kwargs = dict(filtered_items)

    if not filtered_kwargs:
        return False

    # 构建SQL更新语句
    set_clause = ', '.join(ALLOWED_COLUMNS[key] for key, _ in filtered_items)
    values = [value for _, value in filtered_items]
    values.append(task_id)
    
    conn = get_db_connection()
    try:
        for attempt in range(1, DB_WRITE_RETRY_TIMES + 1):
            try:
                existing_row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
                existing_task = dict(existing_row) if existing_row else None
                previous_status = existing_task.get('status') if existing_task else None
                conn.execute(
                    f'UPDATE tasks SET {set_clause} WHERE id = ?',
                    values
                )
                conn.commit()

                event_type = 'task_updated'
                event_payload = {
                    'task_id': task_id,
                    'status': filtered_kwargs.get('status')
                }

                changed_fields = {key for key in filtered_kwargs.keys() if key != 'updated_at'}
                if changed_fields == {'upload_progress'}:
                    current_status = filtered_kwargs.get('status')
                    if current_status is None:
                        try:
                            row = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
                            current_status = row['status'] if row else None
                        except Exception:
                            current_status = None

                    event_type = 'task_progress'
                    event_payload = {
                        'task_id': task_id,
                        'status': current_status,
                        'upload_progress': filtered_kwargs.get('upload_progress'),
                        'updated_at': filtered_kwargs.get('updated_at')
                    }

                publish_task_event(event_type, event_payload)
                if (
                    status_changed
                    and previous_status != filtered_kwargs.get('status')
                    and filtered_kwargs.get('status') in (TASK_STATES['COMPLETED'], TASK_STATES['FAILED'])
                ):
                    merged_task = dict(existing_task or {})
                    merged_task.update(filtered_kwargs)
                    merged_task['id'] = task_id
                    emit_notification_event(
                        NotificationEvent(
                            event_type=(
                                EVENT_TASK_COMPLETED
                                if filtered_kwargs.get('status') == TASK_STATES['COMPLETED']
                                else EVENT_TASK_FAILED
                            ),
                            payload=_build_task_notification_payload(
                                merged_task,
                                overrides={'previous_status': previous_status},
                            ),
                        )
                    )
                if not silent:  # 只有非静默模式才记录到主日志
                    logger.info(f"任务 {task_id} 更新成功: {filtered_kwargs}")
                return True
            except sqlite3.OperationalError as e:
                err_msg = str(e).lower()
                is_locked = (
                    'database is locked' in err_msg
                    or 'database is busy' in err_msg
                    or 'locked' in err_msg
                    or 'busy' in err_msg
                )
                if is_locked and attempt < DB_WRITE_RETRY_TIMES:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    time.sleep(DB_WRITE_RETRY_SLEEP_SECONDS * attempt)
                    continue
                logger.error(
                    f"更新任务 {task_id} 失败 (attempt={attempt}/{DB_WRITE_RETRY_TIMES}): {str(e)}"
                )
                return False
            except Exception as e:
                logger.error(f"更新任务 {task_id} 失败: {str(e)}")
                return False
    finally:
        conn.close()

def get_task(task_id):
    """
    获取任务信息
    
    Args:
        task_id: 任务ID
    
    Returns:
        task: 任务信息字典，如果不存在则返回None
    """
    logger.debug(f"正在获取任务 {task_id}")
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        task = cursor.fetchone()
        result = dict(task) if task else None
        logger.debug(f"获取任务 {task_id} 结果: {result}")
        return result
    except Exception as e:
        logger.error(f"获取任务 {task_id} 失败: {str(e)}")
        return None
    finally:
        conn.close()

def get_all_tasks():
    """
    获取所有任务信息
    
    Returns:
        tasks: 任务信息列表
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks ORDER BY created_at DESC')
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"获取所有任务失败: {str(e)}")
        return []
    finally:
        conn.close()

def get_tasks_paginated(page=1, per_page=20):
    """
    获取分页任务信息
    
    Args:
        page (int): 页码，从1开始
        per_page (int): 每页数量，默认20
    
    Returns:
        dict: 包含tasks、total、page、per_page、total_pages等信息的字典
    """
    conn = get_db_connection()
    try:
        # 获取总数
        cursor = conn.execute('SELECT COUNT(*) FROM tasks')
        total = cursor.fetchone()[0]
        
        # 计算分页参数
        total_pages = (total + per_page - 1) // per_page  # 向上取整
        offset = (page - 1) * per_page
        
        # 获取分页数据
        cursor = conn.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?', 
                            (per_page, offset))
        tasks = [dict(row) for row in cursor.fetchall()]
        
        return {
            'tasks': tasks,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'prev_page': page - 1 if page > 1 else None,
            'next_page': page + 1 if page < total_pages else None
        }
    except Exception as e:
        logger.error(f"获取分页任务失败: {str(e)}")
        return {
            'tasks': [],
            'total': 0,
            'page': page,
            'per_page': per_page,
            'total_pages': 0,
            'has_prev': False,
            'has_next': False,
            'prev_page': None,
            'next_page': None
        }
    finally:
        conn.close()

def get_tasks_by_status(status):
    """
    获取指定状态的任务
    
    Args:
        status: 任务状态
    
    Returns:
        tasks: 任务信息列表
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC', (status,))
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"获取{status}状态任务失败: {str(e)}")
        return []
    finally:
        conn.close()

def delete_task(task_id, delete_files=True):
    """
    删除任务
    
    Args:
        task_id: 任务ID
        delete_files: 是否同时删除任务文件
    
    Returns:
        success: 删除是否成功
    """
    # 先获取任务信息，用于删除文件
    task = get_task(task_id)
    if not task:
        logger.warning(f"任务 {task_id} 不存在，无法删除")
        return False
    
    # 标记任务取消，尽快中断运行中的任务
    request_task_cancel(task_id)

    # 删除任务文件
    if delete_files:
        delete_task_files(task_id)
    
    # 删除任务记录
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        conn.commit()
        logger.info(f"任务 {task_id} 删除成功")
        publish_task_event('task_deleted', {'task_id': task_id})
        return True
    except Exception as e:
        logger.error(f"删除任务 {task_id} 失败: {str(e)}")
        return False
    finally:
        conn.close()

def clear_all_tasks(delete_files=True):
    """
    清空所有任务
    
    Args:
        delete_files: 是否同时删除任务文件
    
    Returns:
        success: 是否成功
    """
    # 标记任务取消，尽快中断运行中的任务
    tasks = get_all_tasks()
    for task in tasks:
        request_task_cancel(task['id'])

    if delete_files:
        for task in tasks:
            delete_task_files(task['id'])
    
    # 清空任务表
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM tasks')
        conn.commit()
        logger.info("所有任务已清空")
        publish_task_event('tasks_cleared', {})
        return True
    except Exception as e:
        logger.error(f"清空任务失败: {str(e)}")
        return False
    finally:
        conn.close()


def _get_task_download_dir_real(task_id):
    """返回任务下载目录的规范绝对路径，并确保其位于 downloads 根目录内。"""
    downloads_dir_real = os.path.realpath(DOWNLOADS_DIR)
    try:
        normalized_task_id = str(uuid.UUID(str(task_id or '').strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("任务ID格式无效") from exc

    safe_task_dir = safe_join(downloads_dir_real, normalized_task_id)
    if not safe_task_dir:
        raise ValueError("任务目录非法")

    task_dir_real = os.path.realpath(safe_task_dir)
    if os.path.commonpath([task_dir_real, downloads_dir_real]) != downloads_dir_real:
        raise ValueError("任务目录非法")

    return task_dir_real


def _is_upload_stage_failure(task):
    """判断失败任务是否更适合直接重试上传，而不是重跑处理流水线。"""
    if not task:
        return False

    upload_target = _get_task_upload_target(task)
    if not _get_pending_upload_platforms(task, upload_target):
        return False

    video_path = task.get('video_path_local')
    if not (isinstance(video_path, str) and video_path and os.path.exists(video_path)):
        return False

    if _task_has_upload_response(task, upload_target):
        return _has_partial_upload_success(task, upload_target)

    error_text = str(task.get('error_message') or '').lower()
    upload_markers = ('上传', 'upload', 'bilibili', 'acfun', '账号未登录', 'preupload')
    if any(marker in error_text for marker in upload_markers):
        return True

    completed = _get_completed_stages(task)
    return PIPELINE_STAGE_DOWNLOAD_VIDEO in completed and PIPELINE_STAGE_TRANSLATE_SUBTITLE in completed


def _start_background_upload_retry(task_id, config):
    """后台重试上传，避免批量重试请求阻塞 Web 响应。"""
    import threading

    def run_upload_retry():
        try:
            force_upload_task(task_id, config)
        except Exception as exc:
            logger.error(f"后台上传重试任务 {task_id} 出错: {exc}")

    threading.Thread(target=run_upload_retry, daemon=True).start()

def retry_failed_tasks(config=None):
    """重新调度所有失败的任务。"""
    failed_tasks = get_tasks_by_status(TASK_STATES['FAILED'])
    total = len(failed_tasks)
    if total == 0:
        return {
            'total': 0,
            'scheduled': 0,
            'failed_ids': []
        }

    if config is None:
        try:
            from modules.config_manager import load_config
            config = load_config()
        except Exception as e:
            logger.warning(f"加载配置失败，将使用默认配置重试失败任务: {e}")
            config = {}

    scheduled = 0
    failed_ids = []

    for task in failed_tasks:
        task_id = task['id']
        original_error = task.get('error_message')
        upload_target = _get_task_upload_target(task)

        # 失败状态兜底修复：目标平台其实都成功时，直接纠正为 completed，避免重复调度
        if _task_has_upload_response(task, upload_target):
            update_task(
                task_id,
                silent=True,
                status=TASK_STATES['COMPLETED'],
                error_message=None,
                upload_progress=None
            )
            continue

        if _is_upload_stage_failure(task):
            update_task(
                task_id,
                silent=True,
                status=TASK_STATES['READY_FOR_UPLOAD'],
                error_message=None,
                upload_progress=None
            )
            _start_background_upload_retry(task_id, config)
            scheduled += 1
            continue

        # 对“部分平台已成功”的失败任务，保留 FAILED 状态以触发 process_task 的失败点续传分支
        next_status = TASK_STATES['FAILED'] if _has_partial_upload_success(task, upload_target) else TASK_STATES['PENDING']
        update_task(
            task_id,
            silent=True,
            status=next_status,
            error_message=None,
            upload_progress=None
        )

        if start_task(task_id, config):
            scheduled += 1
        else:
            failed_ids.append(task_id)
            update_task(
                task_id,
                silent=True,
                status=TASK_STATES['FAILED'],
                error_message=original_error or '批量重试调度失败，请稍后重试。'
            )

    return {
        'total': total,
        'scheduled': scheduled,
        'failed_ids': failed_ids
    }


def is_metadata_translation_retryable(task):
    """判断人工审核任务是否由元数据自动翻译失败导致。"""
    if not task or task.get('status') != TASK_STATES['AWAITING_REVIEW']:
        return False

    return task.get('error_category') == METADATA_TRANSLATION_ERROR_CATEGORY


def get_metadata_translation_retry_block_reason(task, config) -> str | None:
    """返回当前配置下不能重新翻译的原因；可重试时返回 None。"""
    config = config or {}
    can_translate_title = (
        _as_bool(config.get('TRANSLATE_TITLE', True))
        and bool(_normalize_task_text(task.get('video_title_original')))
    )
    can_translate_description = (
        _as_bool(config.get('TRANSLATE_DESCRIPTION', True))
        and bool(_normalize_task_text(task.get('description_original')))
    )
    if not can_translate_title and not can_translate_description:
        return '当前未启用可用的标题或简介自动翻译，无法重新翻译。'
    if not _normalize_task_text(config.get('OPENAI_API_KEY')):
        return '未配置 OpenAI API Key，无法重新翻译。'
    return None


def retry_metadata_translation_task(task_id, config=None):
    """重置元数据翻译及其下游阶段，并重新调度任务。"""
    task = get_task(task_id)
    if not is_metadata_translation_retryable(task):
        return False
    if get_metadata_translation_retry_block_reason(task, config):
        return False
    if _is_task_active(task_id):
        logger.warning(f"任务 {task_id} 仍在运行，拒绝重新调度元数据翻译")
        return False

    reset_stages = {
        PIPELINE_STAGE_TRANSLATE_CONTENT,
        PIPELINE_STAGE_GENERATE_TAGS,
        PIPELINE_STAGE_RECOMMEND_PARTITION,
        PIPELINE_STAGE_MODERATE_CONTENT,
    }
    checkpoint = _parse_pipeline_checkpoint(task.get(PIPELINE_CHECKPOINT_FIELD))
    remaining_stages = [
        stage for stage in checkpoint.get('completed', [])
        if stage not in reset_stages
    ]
    reset_checkpoint = json.dumps({
        'version': checkpoint.get('version', 1),
        'completed': remaining_stages,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }, ensure_ascii=False)

    reset_values = {
        'status': TASK_STATES['PENDING'],
        'video_title_translated': '',
        'description_translated': '',
        'tags_generated': None,
        'recommended_partition_id': None,
        'recommended_partition_id_acfun': None,
        'recommended_partition_id_bilibili': None,
        'selected_partition_id': None,
        'selected_partition_id_acfun': None,
        'selected_partition_id_bilibili': None,
        'moderation_result': None,
        'error_message': None,
        'error_category': None,
        'pipeline_checkpoint': reset_checkpoint,
        'upload_progress': None,
    }
    rollback_values = {
        key: task.get(key)
        for key in reset_values
        if key != 'status'
    }
    rollback_values['status'] = task.get('status')

    if not update_task(task_id, silent=True, **reset_values):
        return False

    try:
        if start_task(task_id, config):
            return True
    except Exception as exc:
        logger.error(f"重新调度元数据翻译任务 {task_id} 出错: {exc}")

    if not update_task(task_id, silent=True, **rollback_values):
        logger.warning(f"重新调度元数据翻译任务 {task_id} 失败，且状态回滚失败")
    return False

def delete_task_files(task_id):
    """
    删除任务相关文件

    Args:
        task_id: 任务ID

    Returns:
        success: 删除是否成功
    """
    try:
        task_dir_real = _get_task_download_dir_real(task_id)
    except (ValueError, OSError) as e:
        logger.error(f"验证任务 {task_id} 路径失败: {str(e)}")
        return False

    if os.path.exists(task_dir_real):
        try:
            shutil.rmtree(task_dir_real)
            logger.info(f"任务 {task_id} 的下载目录已删除: {task_dir_real}")
        except Exception as e:
            logger.error(f"删除任务 {task_id} 的下载目录失败: {str(e)}")
            return False

    # 封面图片现在保存在downloads目录中，无需单独删除

    return True

# 全局上传队列锁
upload_queue_lock = threading.Lock()
upload_semaphore = None
task_semaphore = None

def init_upload_semaphore(max_concurrent_uploads=1):
    """初始化上传信号量"""
    global upload_semaphore
    logger.info(f"初始化上传信号量，最大并发上传数: {max_concurrent_uploads}")
    upload_semaphore = threading.Semaphore(max_concurrent_uploads)
    logger.info(f"上传信号量初始化完成: {upload_semaphore}")

def init_task_semaphore(max_concurrent_tasks=3):
    """初始化任务并发信号量"""
    global task_semaphore
    logger.info(f"初始化任务信号量，最大并发任务数: {max_concurrent_tasks}")
    task_semaphore = threading.Semaphore(max_concurrent_tasks)
    logger.info(f"任务信号量初始化完成: {task_semaphore}")

def reset_stuck_tasks(skip_active=False, cancel_active=False):
    """重置卡住的任务"""
    import time
    current_time = time.time()
    
    # 定义超时时间（30分钟）
    timeout_seconds = 30 * 60
    
    conn = get_db_connection()
    try:
        # 查找可能卡住的任务（状态为处理中但长时间未更新）
        cursor = conn.execute('''
            SELECT id, status, updated_at 
            FROM tasks 
            WHERE status IN ({}) 
            AND datetime(updated_at) < datetime('now', '-30 minutes')
        '''.format(','.join(['?'] * len(PROCESSING_STATES))), PROCESSING_STATES)
        
        stuck_tasks = cursor.fetchall()
        
        if stuck_tasks:
            logger.warning(f"发现 {len(stuck_tasks)} 个可能卡住的任务，正在重置...")
            reset_count = 0
            
            for task in stuck_tasks:
                task_id = task[0]
                old_status = task[1]
                updated_at = task[2]

                if skip_active and _is_task_active(task_id):
                    if cancel_active:
                        request_task_cancel(task_id)
                    logger.warning(
                        f"任务 {task_id[:8]}... 仍处于活动线程中，已跳过自动重置"
                    )
                    continue
                
                # 重置为失败状态；同时清理字幕质检/质量结局标记，
                # 避免重跑时被残留标记永久锁死。
                conn.execute('''
                    UPDATE tasks 
                    SET status = ?, error_message = ?, updated_at = ?,
                        subtitle_qc_failed = 0,
                        subtitle_qc_reason = NULL,
                        subtitle_qc_score = NULL,
                        subtitle_qc_checked_at = NULL,
                        subtitle_quality_state = NULL,
                        asr_warning_message = NULL,
                        subtitle_warning_message = NULL
                    WHERE id = ?
                ''', (TASK_STATES['FAILED'], 
                      f"任务超时重置 (原状态: {old_status})",
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      task_id))
                reset_count += 1
                
                logger.info(f"重置任务 {task_id[:8]}... 从 {old_status} 到 failed")
            
            conn.commit()
            return reset_count
        else:
            logger.info("没有发现卡住的任务")
            return 0
            
    except Exception as e:
        logger.error(f"重置卡住任务时出错: {str(e)}")
        return 0
    finally:
        conn.close()

def validate_cookies(cookies_path, service_name="Unknown"):
    """验证cookies文件的有效性"""
    if not cookies_path or not os.path.exists(cookies_path):
        logger.warning(f"{service_name} Cookies文件不存在: {cookies_path}")
        return False, f"Cookies文件不存在: {cookies_path}"
    
    try:
        with open(cookies_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        
        if not content:
            logger.warning(f"{service_name} Cookies文件为空")
            return False, "Cookies文件为空"
        
        # 基本格式验证
        if content.startswith('# Netscape HTTP Cookie File') or '\t' in content:
            # Netscape格式（#HttpOnly_ 前缀行是带 HttpOnly 标志的合法数据行，不算注释）
            lines = [
                line for line in content.split('\n')
                if line.strip() and (not line.startswith('#') or line.startswith('#HttpOnly_'))
            ]
            if not lines:
                return False, "Netscape格式cookies文件没有有效的cookie条目"
            logger.debug(f"{service_name} Netscape格式cookies, {len(lines)} 个条目")
        elif content.startswith('[') or content.startswith('{'):
            # JSON格式
            import json
            cookies_data = json.loads(content)
            if not cookies_data:
                return False, "JSON格式cookies文件为空数组"
            logger.debug(f"{service_name} JSON格式cookies, {len(cookies_data)} 个条目")
        else:
            # YouTube 允许 CSV / 空格分隔的文本表格格式，由后续的自动转换处理
            if service_name == "YouTube":
                try:
                    from modules.tools.convert_cookies import convert_to_netscape_lines
                    if convert_to_netscape_lines(content) is not None:
                        logger.debug(f"{service_name} 文本表格格式cookies可自动转换为Netscape")
                        return True, "Cookies文件格式正确"
                except Exception:
                    pass
            logger.warning(f"{service_name} Cookies文件格式不明")
            return False, "Cookies文件格式不明"
        
        return True, "Cookies文件格式正确"
        
    except Exception:
        logger.exception("验证%s Cookies文件时出错", service_name)
        return False, "验证cookies文件出错，请查看服务日志。"

# 任务处理逻辑
class TaskProcessor:
    """任务处理器，负责任务的执行和状态管理"""
    
    def __init__(self, config=None):
        """
        初始化任务处理器
        
        Args:
            config: 配置字典，包含各种API的配置信息
        """
        self.config = dict(config or {})
        self._current_max_concurrent_tasks = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
        self._current_max_concurrent_uploads = _as_int(self.config.get('MAX_CONCURRENT_UPLOADS', 1), 1, minimum=1)
        self._runtime_limit_refresh_pending = False
        self._last_deferred_limit_signature = None
        
        # 初始化上传信号量
        init_upload_semaphore(self._current_max_concurrent_uploads)
        # 初始化任务并发信号量
        init_task_semaphore(self._current_max_concurrent_tasks)
        
        self.scheduler = BackgroundScheduler(
            executors={
                'default': APSchedulerThreadPoolExecutor(max_workers=max(2, self._current_max_concurrent_tasks))
            },
            job_defaults={
                'coalesce': False,
                'max_instances': 1  # 每个任务只能有一个实例在运行，避免重复执行同一任务
            }
        )
        self.scheduler.start()
        logger.info(
            f"任务处理器初始化完成 - 最大并发任务: {self._current_max_concurrent_tasks}, "
            f"最大并发上传: {self._current_max_concurrent_uploads}"
        )

        # 断点续跑：仅在进程生命周期内执行一次，避免配置变更重建处理器时干扰运行中任务
        global _RESUME_RECOVERY_DONE
        if not _RESUME_RECOVERY_DONE:
            try:
                recover_interrupted_tasks_to_pending()
            finally:
                _RESUME_RECOVERY_DONE = True

        self._register_periodic_jobs()

    def _register_periodic_jobs(self):
        """注册或刷新周期性任务。"""
        try:
            scan_interval = _as_int(self.config.get('PENDING_SCAN_INTERVAL_SECONDS', 30), 30, minimum=5)
            self.scheduler.add_job(
                self._check_and_start_next_pending_task,
                'interval',
                seconds=scan_interval,
                id='pending_scanner',
                replace_existing=True
            )
            logger.info(f"已启动定时扫描pending任务：每 {scan_interval} 秒")
        except Exception as e:
            logger.warning(f"注册定时扫描pending任务失败（不影响主流程）：{e}")

        try:
            stuck_check_interval = _as_int(
                self.config.get('STUCK_TASK_CHECK_INTERVAL_SECONDS', 300),
                300,
                minimum=30,
            )
            self.scheduler.add_job(
                self._recover_stuck_tasks,
                'interval',
                seconds=stuck_check_interval,
                id='stuck_task_recovery',
                replace_existing=True
            )
            logger.info(f"已启动卡住任务扫描：每 {stuck_check_interval} 秒")
        except Exception as e:
            logger.warning(f"注册卡住任务扫描失败（不影响主流程）：{e}")

    def _refresh_runtime_limits(self, force=False):
        """按当前配置刷新并发上限；运行中有活动任务时延后生效。"""
        desired_tasks = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
        desired_uploads = _as_int(self.config.get('MAX_CONCURRENT_UPLOADS', 1), 1, minimum=1)

        if (
            desired_tasks == self._current_max_concurrent_tasks
            and desired_uploads == self._current_max_concurrent_uploads
            and not self._runtime_limit_refresh_pending
        ):
            return True

        active_task_count = len(_get_active_task_ids())
        if active_task_count > 0 and not force:
            signature = (desired_tasks, desired_uploads)
            if signature != self._last_deferred_limit_signature:
                logger.info(
                    "检测到并发配置变更，但当前仍有活动任务运行；"
                    f"新的任务/上传并发上限将延后到空闲时生效: {signature}"
                )
                self._last_deferred_limit_signature = signature
            self._runtime_limit_refresh_pending = True
            return False

        with _TASK_SCHEDULING_LOCK:
            init_task_semaphore(desired_tasks)
            init_upload_semaphore(desired_uploads)
            self._current_max_concurrent_tasks = desired_tasks
            self._current_max_concurrent_uploads = desired_uploads
            self._runtime_limit_refresh_pending = False
            self._last_deferred_limit_signature = None
        logger.info(
            f"并发配置已生效 - 最大并发任务: {desired_tasks}, 最大并发上传: {desired_uploads}"
        )
        return True

    def refresh_config(self, config=None):
        """刷新运行时配置，而不是重建处理器实例。"""
        self.config = dict(config or {})
        self._register_periodic_jobs()
        self._refresh_runtime_limits(force=False)

    def _enqueue_task_retry(self, task_id, delay_seconds=2):
        """为未抢到执行位的任务注册一次延迟重试，避免创建等待线程。"""
        try:
            self.scheduler.add_job(
                self.schedule_task,
                'date',
                run_date=datetime.now() + timedelta(seconds=max(1, int(delay_seconds))),
                id=f'queued_retry_{task_id}',
                replace_existing=True,
                args=[task_id],
            )
        except Exception as e:
            logger.debug(f"为任务 {task_id} 注册延迟重试失败，将依赖定时扫描器: {e}")

    def _recover_stuck_tasks(self):
        """周期性清理非活动的卡住任务，并对活动卡住任务发送取消请求。"""
        try:
            reset_count = reset_stuck_tasks(skip_active=True, cancel_active=True)
            if reset_count > 0:
                logger.warning(f"自动恢复了 {reset_count} 个卡住任务，准备继续调度 pending 队列")
                self._check_and_start_next_pending_task()
        except Exception as e:
            logger.warning(f"自动恢复卡住任务失败（忽略）：{e}")
    
    def shutdown(self):
        """安全关闭调度器"""
        try:
            if self.scheduler:
                # 防止对未运行的调度器调用shutdown引发异常
                self.scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            # 已经停止则忽略
            pass
        except Exception as e:
            logger.warning(f"关闭任务处理器时发生异常: {e}")
        finally:
            logger.info("任务处理器已关闭")
    
    def schedule_task(self, task_id):
        """
        调度任务处理
        
        Args:
            task_id: 任务ID
        
        Returns:
            job_id: 调度作业ID
        """
        try:
            with _TASK_SCHEDULING_LOCK:
                self._refresh_runtime_limits(force=False)

                if _is_task_active(task_id):
                    logger.warning(f"任务 {task_id} 已有活动线程，跳过重复调度")
                    return f"thread_{task_id}"

                queued_job_id = f"queued_retry_{task_id}"
                try:
                    existing_retry_job = self.scheduler.get_job(queued_job_id)
                except Exception:
                    existing_retry_job = None

                # 检查任务当前状态
                task = get_task(task_id)
                if not task:
                    logger.error(f"任务 {task_id} 不存在")
                    return None

                if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['FAILED']]:
                    logger.warning(f"任务 {task_id} 状态为 {task['status']}，不能调度")
                    return None

                global task_semaphore
                if task_semaphore is None:
                    init_task_semaphore(self._current_max_concurrent_tasks)
                assert task_semaphore is not None, "task_semaphore 应该已经初始化"
                local_task_semaphore = task_semaphore

                slot_acquired = local_task_semaphore.acquire(blocking=False)
                if not slot_acquired:
                    if existing_retry_job is None:
                        self._enqueue_task_retry(task_id, delay_seconds=2)
                    logger.info(
                        f"任务 {task_id} 当前达到并发上限，保留原状态等待后续调度，不再创建等待线程"
                    )
                    return queued_job_id

                import threading
                release_slot_on_failure = True
                task_marked_active = False

                def run_task_wrapper():
                    try:
                        logger.info(f"任务 {task_id} 开始在线程中执行")
                        self.process_task(
                            task_id,
                            slot_already_acquired=True,
                            acquired_task_semaphore=local_task_semaphore,
                        )
                    except Exception as e:
                        logger.error(f"任务 {task_id} 执行出错: {str(e)}")
                        import traceback
                        logger.error(traceback.format_exc())
                        update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"执行出错: {str(e)}")
                    finally:
                        _mark_task_inactive(task_id)

                try:
                    if not _mark_task_active(task_id):
                        logger.warning(f"任务 {task_id} 在线程启动前检测到重复调度，已跳过")
                        return f"thread_{task_id}"
                    task_marked_active = True

                    thread = threading.Thread(
                        target=run_task_wrapper,
                        name=f"task_{task_id}",
                        daemon=True
                    )
                    thread.start()
                    release_slot_on_failure = False
                except Exception:
                    raise
                finally:
                    if release_slot_on_failure:
                        if task_marked_active:
                            _mark_task_inactive(task_id)
                        local_task_semaphore.release()

                logger.info(f"任务 {task_id} 已在后台线程启动")
                return f"thread_{task_id}"
            
        except Exception as e:
            logger.error(f"调度任务 {task_id} 失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            # 更新任务状态为失败
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"调度失败: {str(e)}")
            return None
    
    def process_task(self, task_id, slot_already_acquired=False, acquired_task_semaphore=None):
        """
        处理任务，包括采集信息、内容审核、下载、上传等步骤
        """
        task_logger = setup_task_logger(task_id)
        task_logger.info(f"开始处理任务 {task_id}")
        slot_acquired = bool(slot_already_acquired)
        active_task_semaphore = acquired_task_semaphore
        if slot_already_acquired and active_task_semaphore is None:
            task_logger.warning("slot_already_acquired=True 但未提供信号量实例，改为重新获取并发配额")
            slot_acquired = False
        if active_task_semaphore is None:
            global task_semaphore
            with _TASK_SCHEDULING_LOCK:
                active_task_semaphore = task_semaphore
                if active_task_semaphore is None:
                    # 兜底：根据当前配置重新初始化
                    task_logger.warning("task_semaphore 为 None，正在重新初始化...")
                    init_task_semaphore(self._current_max_concurrent_tasks)
                    active_task_semaphore = task_semaphore
            task_logger.info(f"task_semaphore 当前值: {active_task_semaphore}")
            if active_task_semaphore is None:
                task_logger.error("task_semaphore 初始化失败，无法继续执行任务")
                return
        try:
            task = get_task(task_id)
            if not task:
                logger.error(f"任务 {task_id} 不存在")
                return

            if not slot_acquired:
                task_logger.info("等待获取任务并发配额...")
                try:
                    while True:
                        if is_task_cancelled(task_id):
                            raise TaskCancelledError("任务已取消")
                        if active_task_semaphore.acquire(timeout=0.5):
                            slot_acquired = True
                            break
                    task_logger.info("获得任务并发配额，开始执行任务")
                except TaskCancelledError:
                    task_logger.info("任务在等待并发配额时已取消")
                    update_task(task_id, status=TASK_STATES['FAILED'], error_message="任务已取消")
                    return
                except Exception as _e:
                    task_logger.error(f"获取任务并发配额失败: {_e}")
                    return
            else:
                task_logger.info("任务已在调度阶段获得任务并发配额")

            _raise_if_cancelled(task_id, task_logger)
            # 断点续跑：读取checkpoint + 根据现有任务字段推断已完成阶段
            completed_stages = _get_completed_stages(task)
            # 将推断结果写回checkpoint（幂等），使后续重启更稳定
            try:
                _persist_pipeline_checkpoint(task_id, completed_stages)
            except Exception:
                pass

            # 重新获取任务，避免排队等待期间状态变化导致基于旧快照决策
            task = get_task(task_id) or task
            if task is None:
                task_logger.error("任务对象为None，终止任务处理")
                return

            # 上传失败恢复优化：若目标平台已全部成功则直接完成；若仅部分成功则仅重试失败平台
            upload_target = _get_task_upload_target(task)
            pending_platforms = _get_pending_upload_platforms(task, upload_target)
            if task.get('status') == TASK_STATES['FAILED'] and not pending_platforms and _task_has_upload_response(task, upload_target):
                task_logger.info("检测到目标平台已全部上传成功，跳过重复流程并标记完成")
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
                return

            if task.get('status') == TASK_STATES['FAILED'] and _has_partial_upload_success(task, upload_target):
                done_platforms = [
                    p for p in _get_upload_platforms_for_target(upload_target)
                    if _task_has_platform_upload_response(task, p)
                ]
                task_logger.info(
                    "检测到部分平台已上传成功，进入失败点续传模式。"
                    f" 已完成平台: {done_platforms}，待重试平台: {pending_platforms}"
                )
                self._upload_to_target(task_id, task_logger)
                task = get_task(task_id)
                if task and _task_has_upload_response(task, upload_target):
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                    if task.get('status') != TASK_STATES['COMPLETED']:
                        update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
                        task_logger.info("失败平台重试成功，任务已标记为完成")
                return

            # 1. 采集视频信息（只获取元数据和封面，不下载视频文件）
            task_logger.info(f"开始处理任务，当前task对象: {task}")
            if task is None:
                task_logger.error("任务对象为None，终止任务处理")
                return

            if PIPELINE_STAGE_FETCH_INFO in completed_stages:
                task_logger.info("跳过采集视频信息（checkpoint已完成）")
            else:
                self._fetch_video_info(task_id, task['youtube_url'], task_logger)
                _raise_if_cancelled(task_id, task_logger)
                task = get_task(task_id)
                task_logger.info(f"重新获取任务对象: {task}")
                if task is None:
                    task_logger.error("重新获取任务对象为None，终止任务处理")
                    return
                if task['status'] == TASK_STATES['FAILED']:
                    task_logger.error("采集视频信息失败，终止任务处理")
                    return
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_FETCH_INFO)

            # 2. 翻译/标签/分区推荐（如有需要）
            if self.config.get('TRANSLATE_TITLE', True) or self.config.get('TRANSLATE_DESCRIPTION', True):
                if PIPELINE_STAGE_TRANSLATE_CONTENT in completed_stages:
                    task_logger.info("跳过标题/描述翻译（checkpoint已完成）")
                else:
                    ok = self._translate_content(task_id, task_logger)
                    task = get_task(task_id)
                    if ok:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_TRANSLATE_CONTENT)
                    elif task is not None and task['status'] == TASK_STATES['AWAITING_REVIEW']:
                        task_logger.info("元数据翻译失败，任务已转入人工审核")
                        return
                    elif task is not None and task['status'] == TASK_STATES['FAILED']:
                        task_logger.error("元数据翻译失败，终止任务处理")
                        return
                _raise_if_cancelled(task_id, task_logger)

            if self.config.get('GENERATE_TAGS', True):
                if PIPELINE_STAGE_GENERATE_TAGS in completed_stages:
                    task_logger.info("跳过标签生成（checkpoint已完成）")
                else:
                    ok = self._generate_tags(task_id, task_logger)
                    if ok:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
                _raise_if_cancelled(task_id, task_logger)

            if self.config.get('RECOMMEND_PARTITION', False):
                if PIPELINE_STAGE_RECOMMEND_PARTITION in completed_stages:
                    task_logger.info("跳过分区推荐（checkpoint已完成）")
                else:
                    ok = self._recommend_partition(task_id, task_logger)
                    if ok:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_RECOMMEND_PARTITION)

                task = get_task(task_id)
                if task is None:
                    task_logger.warning("任务对象为 None，跳过分区自动选择")
                _raise_if_cancelled(task_id, task_logger)

            # 3. 内容审核（如启用）
            if self.config.get('CONTENT_MODERATION_ENABLED', False):
                if PIPELINE_STAGE_MODERATE_CONTENT in completed_stages:
                    task_logger.info("跳过内容审核（checkpoint已完成）")
                else:
                    self._moderate_content(task_id, task_logger)
                    task = get_task(task_id)
                    if task is not None and task['status'] == TASK_STATES['AWAITING_REVIEW']:
                        # 审核不通过，进入人工审核；该阶段也视为已完成
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)
                        task_logger.info("内容需要人工审核，暂停任务处理")
                        return
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)
                _raise_if_cancelled(task_id, task_logger)

            # 4. 审核通过后才下载视频文件
            task = get_task(task_id)
            if task is None:
                task_logger.error("任务对象为 None，无法下载视频文件")
                return

            if PIPELINE_STAGE_DOWNLOAD_VIDEO in completed_stages:
                task_logger.info("跳过下载视频文件（checkpoint已完成）")
            else:
                self._download_video_file(task_id, task['youtube_url'], task_logger)
                _raise_if_cancelled(task_id, task_logger)
                task = get_task(task_id)
                if task is not None and task['status'] == TASK_STATES['FAILED']:
                    task_logger.error("下载视频文件失败，终止任务处理")
                    return
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_DOWNLOAD_VIDEO)

            # 5. 字幕处理（翻译或烧录启用时）
            subtitle_translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
            subtitle_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
            if subtitle_translation_enabled or subtitle_embed_enabled:
                if PIPELINE_STAGE_TRANSLATE_SUBTITLE in completed_stages:
                    task_logger.info("跳过字幕处理（checkpoint已完成）")
                else:
                    ok = self._translate_subtitle(task_id, task_logger)
                    task = get_task(task_id)
                    if ok:
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_TRANSLATE_SUBTITLE)
                    if task is not None and task['status'] == TASK_STATES['FAILED']:
                        task_logger.error("字幕处理失败，继续执行后续步骤")
                _raise_if_cancelled(task_id, task_logger)

            # 6. 上传
            if self.config.get('AUTO_MODE_ENABLED', False):
                # 若已有上传响应，避免重复上传
                task = get_task(task_id)
                upload_target = _get_task_upload_target(task)
                if task and _task_has_upload_response(task, upload_target):
                    task_logger.info("检测到已有上传响应，跳过上传（避免重复上传）")
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)
                elif PIPELINE_STAGE_UPLOAD_TO_ACFUN in completed_stages:
                    task_logger.info("跳过上传（checkpoint已完成）")
                else:
                    self._upload_to_target(task_id, task_logger)
                    _raise_if_cancelled(task_id, task_logger)
                    task = get_task(task_id)
                    upload_target = _get_task_upload_target(task)
                    if task and task.get('status') == TASK_STATES['AWAITING_REVIEW']:
                        task_logger.info("上传前校验发现缺失译文，任务已转入人工审核")
                        return
                    if task and (_task_has_upload_response(task, upload_target) or task.get('status') == TASK_STATES['COMPLETED']):
                        completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_UPLOAD_TO_ACFUN)

            # 任务处理完成后，根据是否已上传到目标平台决定状态
            task = get_task(task_id)
            if task is not None:
                upload_target = _get_task_upload_target(task)
                if task['status'] == TASK_STATES['AWAITING_REVIEW']:
                    task_logger.info("任务当前处于人工审核状态，保留待审核状态")
                elif task['status'] != TASK_STATES['COMPLETED'] and task['status'] != TASK_STATES['FAILED']:
                    # 如果没有开启自动上传或者上传失败，则标记为"准备上传"
                    if not self.config.get('AUTO_MODE_ENABLED', False) or not _task_has_upload_response(task, upload_target):
                        update_task(task_id, status=TASK_STATES['READY_FOR_UPLOAD'])
                        task_logger.info("任务处理完成，标记为准备上传")
                    else:
                        # 只有成功上传到目标平台的视频才会被标记为"已完成"
                        update_task(task_id, status=TASK_STATES['COMPLETED'])
                        task_logger.info("任务处理并上传完成")
            else:
                task_logger.warning("任务对象为 None，无法确定最终状态")
                update_task(task_id, status=TASK_STATES['READY_FOR_UPLOAD'])
                task_logger.info("任务处理完成，标记为准备上传")
        except TaskCancelledError:
            task_logger.info("任务已取消，停止后续处理")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message="任务已取消")
        except Exception as e:
            task_logger.error(f"任务处理过程中发生错误: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=str(e))
        finally:
            clear_task_cancel(task_id, clear_flag=True)
            # 释放并发配额
            if slot_acquired:
                try:
                    active_task_semaphore.release()
                    task_logger.info("已释放任务并发配额")
                except Exception:
                    pass
            
            # 主动清理内存以降低系统资源占用
            try:
                gc.collect()
                task_logger.debug("已执行垃圾回收以优化内存使用")
            except Exception:
                pass
                
            # 任务完成后，检查是否有其他pending任务需要启动
            task_logger.info("任务处理完成，检查是否有其他pending任务...")
            # 延迟1秒后检查，确保数据库状态更新完成
            import threading
            import time
            
            def delayed_check():
                time.sleep(1)  # 等待1秒确保状态已更新
                self._check_and_start_next_pending_task()
            
            threading.Thread(target=delayed_check, daemon=True).start()
    
    def _check_and_start_next_pending_task(self):
        """检查并启动下一个pending任务"""
        try:
            self._refresh_runtime_limits(force=False)
            # 获取所有pending任务
            pending_tasks = get_tasks_by_status(TASK_STATES['PENDING'])
            active_task_ids = _get_active_task_ids()
            pending_tasks = [task for task in pending_tasks if task.get('id') not in active_task_ids]
            
            if not pending_tasks:
                if active_task_ids:
                    logger.info(f"当前没有可启动的pending任务（{len(active_task_ids)} 个任务线程处于活动状态）")
                else:
                    logger.info("没有pending任务需要启动")
                return
            
            # 检查当前是否有正在运行的任务
            processing_states = [
                'fetching_info',
                'info_fetched', 
                TASK_STATES['TRANSLATING'], 
                TASK_STATES['TAGGING'],
                TASK_STATES['PARTITIONING'],
                TASK_STATES['MODERATING'],
                TASK_STATES['DOWNLOADING'], 
                TASK_STATES['DOWNLOADED'],
                TASK_STATES['ASR_TRANSCRIBING'],
                TASK_STATES['TRANSLATING_SUBTITLE'],
                TASK_STATES['ENCODING_VIDEO'],
                TASK_STATES['UPLOADING']
            ]
            
            running_tasks = []
            for state in processing_states:
                running_tasks.extend(get_tasks_by_status(state))
            running_task_ids = {task.get('id') for task in running_tasks if task.get('id')}
            effective_running_count = len(running_task_ids.union(active_task_ids))
            
            # 如果有任务正在运行且并发限制为1，则不启动新任务
            # 兼容字符串配置，安全转换为整数
            max_concurrent = _as_int(self.config.get('MAX_CONCURRENT_TASKS', 2), 2, minimum=1)
            
            # 内存感知并发控制：如果内存使用过高，降低并发数
            if _should_reduce_concurrency():
                max_concurrent = max(1, max_concurrent // 2)
                logger.info(f"检测到高内存使用，降低并发数至 {max_concurrent}")
                
            if effective_running_count >= max_concurrent:
                logger.info(
                    f"当前有效运行任务数 {effective_running_count}（DB运行中 {len(running_task_ids)}，活动线程 {len(active_task_ids)}），"
                    f"达到并发限制 {max_concurrent}，暂不启动新任务"
                )
                return
            
            # 按创建时间排序，启动最早的pending任务
            pending_tasks.sort(key=lambda x: x['created_at'])
            next_task = pending_tasks[0]
            
            logger.info(f"发现pending任务，准备启动: {next_task['id'][:8]}... ({next_task.get('youtube_url', 'Unknown URL')[-30:]})")
            
            # 调度下一个任务
            job_id = self.schedule_task(next_task['id'])
            if job_id:
                logger.info(f"下一个pending任务已自动启动: {next_task['id'][:8]}...")
            else:
                logger.error(f"启动下一个pending任务失败: {next_task['id'][:8]}...")
                
        except Exception as e:
            logger.error(f"检查和启动下一个pending任务时出错: {str(e)}")
    
    def _fetch_video_info(self, task_id, youtube_url, task_logger):
        """只采集视频元数据和封面，不下载视频文件"""
        from modules.youtube_handler import download_video_data
        task_logger.info(f"采集视频信息: {youtube_url}")
        update_task(task_id, status='fetching_info')

        _raise_if_cancelled(task_id, task_logger)
        
        cookies_path = resolve_youtube_cookies_path(self.config, task_logger)
        
        # 验证cookies文件
        if cookies_path:
            is_valid, error_msg = validate_cookies(cookies_path, "YouTube")
            if not is_valid:
                task_logger.error(f"YouTube Cookies验证失败: {error_msg}")
                # 尝试不使用cookies继续
                task_logger.info("尝试不使用cookies继续采集信息...")
                cookies_path = None
                
        # 只采集信息
        try:
            cancel_event = get_task_cancel_event(task_id)
            success, result = download_video_data(
                youtube_url,
                task_id,
                cookies_path,
                skip_download=True,
                cancel_event=cancel_event
            )
        except Exception as e:
            if is_task_cancelled(task_id):
                task_logger.info("采集信息过程中检测到任务取消请求")
                raise TaskCancelledError("任务已取消")
            task_logger.error(f"采集视频信息时发生异常: {str(e)}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"采集信息异常: {str(e)}")
            return
            
        if success:
            task_logger.info("视频信息采集成功")
            metadata_path = result.get('metadata_path')
            video_title = ""
            video_description = ""
            if metadata_path and os.path.exists(metadata_path):
                try:
                    with open(metadata_path, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                        video_title = metadata.get('title', '')
                        video_description = metadata.get('description', '')
                except Exception as e:
                    task_logger.error(f"读取视频元数据失败: {str(e)}")
            update_task(
                task_id,
                status='info_fetched',
                video_title_original=video_title,
                description_original=video_description,
                cover_path_local=result.get('cover_path', ''),
                metadata_json_path_local=metadata_path
            )
        else:
            task_logger.error(f"视频信息采集失败: {result}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"采集信息失败: {result}")

    def _download_video_file(self, task_id, youtube_url, task_logger):
        """审核通过后下载视频文件"""
        from modules.youtube_handler import download_video_data
        task_logger.info(f"审核通过，开始下载视频文件: {youtube_url}")
        update_task(task_id, status=TASK_STATES['DOWNLOADING'])

        _raise_if_cancelled(task_id, task_logger)
        
        cookies_path = resolve_youtube_cookies_path(self.config, task_logger)
        
        # 验证cookies文件
        if cookies_path:
            is_valid, error_msg = validate_cookies(cookies_path, "YouTube")
            if not is_valid:
                task_logger.error(f"YouTube Cookies验证失败: {error_msg}")
                # 尝试不使用cookies继续
                task_logger.info("尝试不使用cookies继续下载...")
                cookies_path = None
                
        # 定义进度回调函数
        def progress_callback(progress_info):
            percent = progress_info.get('percent', 0)
            file_size = progress_info.get('file_size', '')
            speed = progress_info.get('speed', '')
            eta = progress_info.get('eta', '')
            
            # 只显示百分比，简洁明了
            progress_msg = f"{percent:.1f}%"
            
            # 详细信息记录到日志
            detailed_msg = progress_msg
            if file_size:
                detailed_msg += f" / {file_size}"
            if speed:
                detailed_msg += f" @ {speed}"
            if eta:
                detailed_msg += f" ETA {eta}"
            
            # 不再把每次下载进度记录为 INFO 到文件，以减少日志噪声；保留网页进度显示
            task_logger.debug(f"下载进度: {detailed_msg}")
            # 更新任务的上传进度字段用于显示（只显示百分比）
            update_task(task_id, upload_progress=progress_msg, silent=True)
        
        # 只下载视频文件
        try:
            cancel_event = get_task_cancel_event(task_id)
            success, result = download_video_data(
                youtube_url,
                task_id,
                cookies_path,
                only_video=True,
                progress_callback=progress_callback,
                cancel_event=cancel_event
            )
        except Exception as e:
            if is_task_cancelled(task_id):
                task_logger.info("下载过程中检测到任务取消请求")
                raise TaskCancelledError("任务已取消")
            task_logger.error(f"下载视频文件时发生异常: {str(e)}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"下载异常: {str(e)}")
            return
        if is_task_cancelled(task_id):
            task_logger.info("下载完成前检测到任务取消请求")
            raise TaskCancelledError("任务已取消")
        if success:
            task_logger.info("视频文件下载成功")
            
            # 获取当前任务信息
            task = get_task(task_id)
            
            update_data = {
                'status': TASK_STATES['DOWNLOADED'],
                'video_path_local': result.get('video_path', ''),
                'upload_progress': None  # 清除进度显示
            }
            
            # 如果结果中包含元数据和封面信息，保存这些信息
            # 这是因为我们修改了download_video_data函数，使其在only_video=True时也能返回之前保存的元数据和封面
            if task is not None:
                if result.get('metadata_path') and not task.get('metadata_json_path_local'):
                    update_data['metadata_json_path_local'] = result.get('metadata_path')
                    
                if result.get('cover_path') and not task.get('cover_path_local'):
                    update_data['cover_path_local'] = result.get('cover_path')
            else:
                task_logger.warning("任务对象为 None，无法更新元数据和封面信息")
                
            update_task(task_id, **update_data)
        else:
            task_logger.error(f"视频文件下载失败: {result}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"下载视频失败: {result}")

    def _recover_cover_path(self, task_id, cover_path, task_logger):
        """尝试恢复缺失的封面文件路径。

        当封面路径为空或文件不存在时（例如直播预告被监控捕获后初始下载失败），
        先在任务目录中搜索已有的封面文件，若仍未找到则从 YouTube 重新采集封面。

        Returns:
            恢复后的封面文件路径，或空字符串（恢复失败时）。
        """
        # 目录边界校验：防止符号链接/路径遍历将 cover_path_local 指向 downloads 目录外
        downloads_dir_real = os.path.realpath(DOWNLOADS_DIR)

        def _is_within_downloads(path):
            path_real = os.path.realpath(path)
            try:
                return os.path.commonpath([downloads_dir_real, path_real]) == downloads_dir_real, path_real
            except ValueError:
                return False, path_real

        if cover_path:
            cover_ok, cover_real = _is_within_downloads(cover_path)
            if cover_ok and os.path.isfile(cover_real):
                return cover_real

        task_logger.info("检测到封面文件缺失，尝试恢复封面...")

        task_dir_ok, task_dir = _is_within_downloads(os.path.join(DOWNLOADS_DIR, task_id))
        if not task_dir_ok:
            task_logger.warning(f"任务目录越界，跳过本地封面恢复: {task_id}")
            task_dir = ''

        # 1) 尝试在任务目录中搜索已有的封面文件
        if task_dir and os.path.isdir(task_dir):
            cover_candidates = [
                'cover.jpg', 'cover.png', 'cover.webp',
                'thumbnail.jpg', 'thumbnail.png', 'thumbnail.webp',
            ]
            for name in cover_candidates:
                candidate_ok, candidate = _is_within_downloads(os.path.join(task_dir, name))
                if not candidate_ok:
                    task_logger.warning(f"检测到越界封面候选路径，已跳过: {name}")
                    continue
                if os.path.isfile(candidate):
                    task_logger.info(f"在任务目录中找到封面文件: {name}")
                    update_task(task_id, cover_path_local=candidate, silent=True)
                    return candidate
            # 搜索任意图片文件
            try:
                with os.scandir(task_dir) as entries:
                    for entry in entries:
                        entry_ok, entry_path = _is_within_downloads(entry.path)
                        if not entry_ok:
                            task_logger.warning(f"检测到越界图片路径，已跳过: {entry.name}")
                            continue
                        if entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')) and os.path.isfile(entry_path):
                            task_logger.info(f"在任务目录中找到图片文件作为封面: {entry.name}")
                            update_task(task_id, cover_path_local=entry_path, silent=True)
                            return entry_path
            except OSError as e:
                task_logger.warning(f"扫描任务目录封面失败，将尝试从 YouTube 重新采集: {task_dir}, error: {e}")

        # 2) 从 YouTube 重新采集封面（仅下载缩略图，不清空任务目录）
        task = get_task(task_id)
        youtube_url = task.get('youtube_url', '') if task else ''
        if not youtube_url:
            task_logger.warning("无法获取YouTube URL，无法重新采集封面")
            return ''

        task_logger.info(f"从 YouTube 重新采集封面: {youtube_url}")
        try:
            from modules.youtube_handler import _find_yt_dlp_command, build_proxy_url, \
                _append_yt_dlp_network_args, _resolve_safe_cookies_path

            if task_dir:
                os.makedirs(task_dir, exist_ok=True)
            else:
                task_logger.warning("任务目录越界，跳过从 YouTube 重新采集封面")
                return ''

            yt_dlp_cmd = _find_yt_dlp_command(task_logger)
            cmd = yt_dlp_cmd + [
                '-o', os.path.join(task_dir, 'video.%(ext)s'),
                '--write-thumbnail',
                '--skip-download',
                '--no-write-info-json',
                '--no-playlist',
                '--ignore-no-formats-error',
                youtube_url,
            ]

            cookies_path = resolve_youtube_cookies_path(self.config, task_logger)

            if cookies_path:
                cookies_path = _resolve_safe_cookies_path(cookies_path, task_logger)

            config = self.config
            proxy_url = build_proxy_url(config)
            _append_yt_dlp_network_args(cmd, proxy_url=proxy_url, cookies_path=cookies_path)

            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                encoding='utf-8', errors='replace',
            )
            task_logger.debug(f"yt-dlp 封面采集输出: {proc.stdout}")
            if proc.returncode != 0:
                task_logger.warning(f"yt-dlp 封面采集返回非零状态: {proc.returncode}, stderr: {proc.stderr}")

            # yt-dlp 使用 -o video.%(ext)s 模板，但缩略图扩展名/文件名并不一定严格固定。
            # 重新扫描任务目录，优先匹配 video*.{jpg,jpeg,png,webp}，再回退到目录内其他图片文件。
            if os.path.isdir(task_dir):
                allowed_exts = {'.jpg', '.jpeg', '.png', '.webp'}
                try:
                    dir_entries = sorted(os.listdir(task_dir))
                except OSError as scan_error:
                    task_logger.warning(f"扫描任务目录中的封面文件失败: {scan_error}")
                    dir_entries = []

                preferred_names = []
                fallback_names = []
                for name in dir_entries:
                    candidate_ok, candidate = _is_within_downloads(os.path.join(task_dir, name))
                    if not candidate_ok or not os.path.isfile(candidate):
                        continue
                    _, ext = os.path.splitext(name)
                    if ext.lower() not in allowed_exts:
                        continue
                    if os.path.splitext(name)[0].lower().startswith('video'):
                        preferred_names.append(name)
                    else:
                        fallback_names.append(name)

                for name in preferred_names + fallback_names:
                    candidate_ok, candidate = _is_within_downloads(os.path.join(task_dir, name))
                    if not candidate_ok:
                        continue
                    if os.path.isfile(candidate):
                        task_logger.info(f"找到封面文件 {name}，直接作为封面使用: {candidate}")
                        update_task(task_id, cover_path_local=candidate, silent=True)
                        return candidate

            task_logger.warning("从 YouTube 重新采集封面未获取到文件")
        except Exception as e:
            task_logger.warning(f"重新采集封面时发生异常: {e}")
        return ''

    def _translate_content(self, task_id, task_logger):
        """翻译视频标题和描述"""
        from modules.ai_enhancer import translate_video_metadata
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始翻译视频标题和描述")
        update_task(task_id, status=TASK_STATES['TRANSLATING'])
        upload_target = _get_task_upload_target(task)
        effective_limits = _get_effective_metadata_limits(upload_target)
        
        # 构建OpenAI配置
        openai_config = {
            'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''),
            'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''),
            'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
            'OPENAI_THINKING_ENABLED': self.config.get('OPENAI_THINKING_ENABLED', False),
            # 兜底端点：显式透传，确保 get_ai_client 能拿到（统一客户端也会从全局配置补齐）
            'FALLBACK_OPENAI_API_KEY': self.config.get('FALLBACK_OPENAI_API_KEY', ''),
            'FALLBACK_OPENAI_BASE_URL': self.config.get('FALLBACK_OPENAI_BASE_URL', ''),
            'FALLBACK_OPENAI_MODEL_NAME': self.config.get('FALLBACK_OPENAI_MODEL_NAME', ''),
            # 可选：允许用户配置固定分区ID，确保一次命中
            'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', ''),
        }
        # 传递 Prompt 中心配置（元数据翻译）
        for prompt_key in (
            'METADATA_TRANSLATE_MODE', 'METADATA_TRANSLATE_TEXT',
            'METADATA_DESC_RETRY_MODE', 'METADATA_DESC_RETRY_TEXT',
        ):
            if prompt_key in self.config:
                openai_config[prompt_key] = self.config[prompt_key]
        
        translate_title = bool(self.config.get('TRANSLATE_TITLE', True) and task.get('video_title_original'))
        translate_description = bool(self.config.get('TRANSLATE_DESCRIPTION', True) and task.get('description_original'))

        if not translate_title and not translate_description:
            task_logger.info("标题和描述翻译均已禁用或缺少原文，跳过")
            return True

        translated = translate_video_metadata(
            task.get('video_title_original', ''),
            task.get('description_original', ''),
            target_language="zh-CN",
            openai_config=openai_config,
            task_id=task_id,
            translate_title=translate_title,
            translate_description=translate_description,
            title_limit=effective_limits['title_limit'],
            description_limit=effective_limits['description_limit'],
        )

        updates = {}
        requested_fields = set(translated.get('requested_fields') or [])
        if translate_title and 'title' in requested_fields:
            updates['video_title_translated'] = translated.get('title', '')
        if translate_description and 'description' in requested_fields:
            updates['description_translated'] = translated.get('description', '')

        if translated.get('success'):
            if updates:
                updates['error_message'] = None
                updates['error_category'] = None
                update_task(task_id, **updates)
            task_logger.info("翻译完成")
            return True

        review_message = translated.get('error_message') or _build_missing_translation_review_message(requested_fields)
        updates.update({
            'status': TASK_STATES['AWAITING_REVIEW'],
            'error_message': review_message,
            'error_category': METADATA_TRANSLATION_ERROR_CATEGORY,
        })
        update_task(task_id, **updates)
        task_logger.warning(review_message)
        return False

    def _ensure_required_translations_ready(self, task_id, task, task_logger, allow_missing_translations=False):
        missing_fields = _get_missing_required_translation_fields(task, self.config)
        if not missing_fields:
            return True

        missing_message = _build_missing_translation_review_message(missing_fields)
        if allow_missing_translations:
            task_logger.warning(
                f"{missing_message} 当前为强制上传路径，将继续回退原文执行上传。"
            )
            return True

        task_logger.warning(missing_message)
        update_task(
            task_id,
            status=TASK_STATES['AWAITING_REVIEW'],
            error_message=missing_message,
            error_category=METADATA_TRANSLATION_ERROR_CATEGORY,
            upload_progress=None,
        )
        return False
    
    def _translate_subtitle(self, task_id, task_logger, embed_in_video_override=None):
        """翻译字幕文件"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return False

        translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
        config_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
        if embed_in_video_override is None:
            should_embed_subtitle = config_embed_enabled
        else:
            should_embed_subtitle = bool(embed_in_video_override)

        # 烧录门控状态。质检失败不再直接短路整个字幕流程，否则任务重跑会被
        # 旧标记永久锁死、永远拿不到新字幕；改为在每次烧录前统一裁决。
        embed_quality_state = str(task.get('subtitle_quality_state') or '').strip().lower() or 'ok'
        qc_failed = task.get('subtitle_qc_failed') == 1
        qc_cleared = False
        # ASR_FAILURE_BLOCKS_EMBED=False 时置真：本轮 ASR 产物按旧行为放行烧录。
        escape_hatch_active = False
        # 首个烧录拦截归因。质检闸门写入的 qc_unavailable / subtitle_qc_rejected
        # 必须比外层兜底的 asr_failed_block_embed 更精确，不能被覆盖。
        block_reason = None

        def _embed_guard(subtitle_file, *, asr_artifact=True):
            """烧录前统一门控。

            ``subtitle_quality_state`` 描述的是**ASR 产物**的来源可靠度，
            因此只看 ASR 生成的字幕；已有外部字幕（如平台自带 SRT）不受它约束，
            否则一次历史 ASR 失败会永久阻断正常字幕的烧录。
            ``subtitle_qc_failed`` 始终生效 —— 它针对的是具体字幕文件的质检结论。

            被拒时记录**首个**原因，供外层写入精确的 ``subtitle_warning_message``。
            """
            nonlocal block_reason
            state = embed_quality_state if asr_artifact else 'ok'
            allowed = _subtitle_embed_allowed(
                self.config,
                state,
                qc_failed,
                qc_cleared=qc_cleared,
                task_logger=task_logger,
            )
            if not allowed and block_reason is None:
                reasons = _subtitle_block_reasons(
                    self.config, state, qc_failed, qc_cleared=qc_cleared
                )
                if reasons:
                    block_reason = reasons[0]
            return allowed

        def _ensure_asr_qc(subtitle_file):
            """确保 ASR 产物在烧录前通过质检。

            此前质检只在「本次 ASR 现场生成」的分支里执行；已有 ``asr_*.srt``
            被复用时直接烧录，完全绕过质检 —— 这是「字幕质量差却照样烧录」的
            另一条真实路径。此处按文件来源补齐：ASR 产物必须过质检，
            外部字幕（平台自带/人工提供）保持原行为，不额外增加 AI 调用。
            """
            nonlocal qc_cleared, qc_failed, block_reason
            if qc_cleared:
                return True
            allowed, cleared, reason = self._ensure_asr_subtitle_qc(
                task_id, subtitle_file, task_logger, embed_quality_state
            )
            if cleared:
                qc_cleared = True
                qc_failed = False
            if not allowed and block_reason is None:
                block_reason = reason or 'subtitle_qc_rejected'
            return allowed

        def _quarantine_if_asr_artifact(subtitle_file):
            """被门控拦截的 ASR 产物移出扫描范围（外部字幕保持原地不动）。

            判定已下沉到 ``_quarantine_rejected_subtitle`` 内部，这里保留一层
            显式意图声明，并让「不满足条件直接跳过」在调用点可读。
            """
            if _is_asr_subtitle_artifact(task_id, subtitle_file):
                self._quarantine_rejected_subtitle(subtitle_file, task_logger)

        def _block_warning(fallback='asr_failed_block_embed'):
            """保留首个拦截归因，不被外层兜底文案覆盖。"""
            return _block_reason_to_warning(block_reason) if block_reason else fallback

        if translation_enabled:
            task_logger.info("开始字幕翻译")
        elif should_embed_subtitle:
            task_logger.info("开始字幕烧录")
        else:
            task_logger.info("开始字幕处理")
        update_task(task_id, status=TASK_STATES['TRANSLATING_SUBTITLE'])
        
        try:
            task_upload_target = _get_task_upload_target(task)
            asr_generated = False
            # 显式初始化：读取 ASR 质量结局的日志路径引用它，避免「未启用 ASR
            # 且已有外部字幕」时出现未绑定变量。
            recognizer = None
            # 查找字幕文件（大小写无关）
            task_dir = os.path.join(DOWNLOADS_DIR, task_id)
            subtitle_files = []
            try:
                for name in os.listdir(task_dir):
                    if not isinstance(name, str):
                        continue
                    lower = name.lower()
                    if lower.endswith('.srt'):
                        subtitle_files.append(os.path.join(task_dir, name))
                    elif lower.endswith('.vtt'):
                        # 自动将 VTT 转换为 SRT
                        vtt_path = os.path.join(task_dir, name)
                        srt_path = self._convert_vtt_to_srt(vtt_path, task_logger)
                        if srt_path and os.path.exists(srt_path):
                            subtitle_files.append(srt_path)
                            task_logger.info(f"已将 VTT 转换为 SRT: {name} -> {os.path.basename(srt_path)}")
                        else:
                            # 转换失败，仍保留 VTT 以防万一
                            subtitle_files.append(vtt_path)
            except Exception:
                pass
            
            if not subtitle_files:
                task_logger.info("未找到字幕文件，尝试语音识别生成字幕（如已启用）")
                # 若启用了语音识别，使用Whisper兼容API从视频生成字幕
                if _is_asr_enabled(self.config):
                    out_path = None
                    try:
                        from modules.speech_recognition import create_speech_recognizer_from_config
                        recognizer = create_speech_recognizer_from_config(self.config, task_id)
                    except Exception as e:
                        task_logger.error(f"创建语音识别器失败: {e}")
                        recognizer = None
                    if recognizer and task is not None:
                        video_path = task.get('video_path_local')
                        if video_path and os.path.exists(video_path):
                            # 确保在任何情况下都有默认的 prev_status，避免未绑定警告
                            prev_status = TASK_STATES['TRANSLATING_SUBTITLE']
                            try:
                                # 显示ASR状态
                                _t = get_task(task_id)
                                prev_status = _t['status'] if _t else prev_status
                                update_task(task_id, status=TASK_STATES['ASR_TRANSCRIBING'])
                                # 输出字幕路径（强制使用 SRT）
                                asr_ext = '.srt'
                                asr_subtitle_path = os.path.join(task_dir, f"asr_{task_id}{asr_ext}")
                                out_path = None
                                # 新一轮 ASR 前复位上一轮的质检/质量结局标记，
                                # 否则旧标记会让任务永久跳过字幕生成。
                                update_task(
                                    task_id,
                                    asr_warning_message=None,
                                    subtitle_qc_failed=0,
                                    subtitle_qc_reason=None,
                                    subtitle_qc_score=None,
                                    subtitle_qc_checked_at=None,
                                    subtitle_warning_message=None,
                                    subtitle_quality_state=None,
                                )
                                qc_failed = False
                                qc_cleared = False
                                embed_quality_state = 'ok'
                                out_path = recognizer.transcribe_video_to_subtitles(video_path, asr_subtitle_path)
                                # 恢复到字幕翻译状态
                                update_task(task_id, status=prev_status)
                            except Exception as e:
                                # 捕获语音识别过程中的所有异常
                                task_logger.error(f"语音识别过程中发生错误: {e}")
                                import traceback
                                task_logger.error(f"详细错误信息:\n{traceback.format_exc()}")
                                # 恢复任务状态
                                update_task(task_id, status=prev_status if 'prev_status' in locals() else TASK_STATES['TRANSLATING_SUBTITLE'])
                                out_path = None
                        if out_path and os.path.exists(out_path):
                            subtitle_files = [out_path]
                            asr_generated = True
                            embed_quality_state = (
                                str(getattr(recognizer, 'last_quality_state', '') or 'ok').strip().lower() or 'ok'
                            )
                            task_logger.info(f"语音识别生成字幕成功: {os.path.basename(out_path)}")
                            update_task(
                                task_id,
                                asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None),
                                subtitle_quality_state=embed_quality_state,
                            )
                        else:
                            update_task(
                                task_id,
                                asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None),
                                subtitle_quality_state=(
                                    str(getattr(recognizer, 'last_quality_state', '') or 'failed').strip().lower()
                                    or 'failed'
                                ),
                            )
                            if getattr(recognizer, 'last_error_message', ''):
                                _t = get_task(task_id)
                                prev_error = _t.get('error_message') if _t else None
                                merged_error = (
                                    f"{prev_error}\n{recognizer.last_error_message}" if prev_error else recognizer.last_error_message
                                )
                                update_task(task_id, error_message=merged_error)
                            self._mark_subtitle_issue(task_id, 'asr_no_subtitle')
                            task_logger.warning("语音识别未能生成字幕，跳过字幕流程")
                            return True
                    else:
                        task_logger.warning("语音识别未启用或视频文件缺失，跳过字幕流程")
                        return True
                else:
                    task_logger.info("未启用语音识别，跳过字幕流程")
                    return True

            # ASR/VAD 质量结局与质检门控：
            # failed  → 直接拒绝烧录（来源不可信）；
            # degraded→ 必须通过严格质检（含时间轴维度）才允许烧录；
            # ok      → 走常规质检。
            if asr_generated:
                asr_subtitle_path = subtitle_files[0] if subtitle_files else None
                detected_lang = self._detect_subtitle_language(asr_subtitle_path) if asr_subtitle_path else None

                if embed_quality_state == 'failed':
                    block_embed = _as_bool(self.config.get('ASR_FAILURE_BLOCKS_EMBED', True))
                    log_fn = task_logger.error if block_embed else task_logger.warning
                    log_fn(
                        "ASR/VAD 质量结局为 failed（%s），%s：继续上传%s",
                        '/'.join(str(item) for item in (getattr(recognizer, 'last_degraded_reasons', []) or []))
                        or 'unknown',
                        '拒绝烧录字幕' if block_embed else '按 ASR_FAILURE_BLOCKS_EMBED=False 放行烧录',
                        '原视频' if block_embed else '本次产物',
                    )
                    if block_embed:
                        self._quarantine_rejected_subtitle(asr_subtitle_path, task_logger)
                        update_task(
                            task_id,
                            subtitle_language_detected=detected_lang,
                            subtitle_warning_message='asr_failed_block_embed',
                            upload_progress=None,
                        )
                        return True
                    # 逃生口：DB 中仍保留 failed 结局供展示，但本轮 ASR 来源
                    # 结局不再拦烧录（这就是 ASR_FAILURE_BLOCKS_EMBED=False 的
                    # 旧行为语义）。注意这里**不改写** embed_quality_state：
                    # 质检的严格度与归因仍需要真实的结局，放行交由
                    # ``_subtitle_embed_allowed`` 内部的 asr_blocks 决定。
                    escape_hatch_active = True

                if asr_subtitle_path and os.path.exists(asr_subtitle_path):
                    strict_qc = embed_quality_state == 'degraded'
                    qc_result = self._run_subtitle_qc(
                        task_id, asr_subtitle_path, task_logger, strict=strict_qc
                    )
                    if qc_result is SUBTITLE_QC_DISABLED:
                        # 用户主动关闭质检：没有任何质检结论可依赖，按配置放行，
                        # 同时清掉内存中的历史 qc_failed —— 否则「关掉质检」会比
                        # 开着更严格。``qc_cleared`` 在此表示「本轮质检门控已裁决
                        # 完毕」，不等于「质检结论为通过」；degraded 的严格质检
                        # 要求同样随质检开关关闭（见 ``_subtitle_block_reasons``）。
                        task_logger.info("字幕质检已关闭，跳过本次烧录前质检裁决")
                        qc_cleared = True
                        qc_failed = False
                    else:
                        # 逃生口（ASR_FAILURE_BLOCKS_EMBED=False）生效时，「质检没跑成」
                        # 也不应阻断烧录，但明确的质检失败结论（False）不受它影响。
                        # 判定统一由 ``_qc_result_allows_embed`` 给出，避免各调用点漂移。
                        if not _qc_result_allows_embed(qc_result, self.config):
                            reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
                            task_logger.warning(
                                "转录字幕质检未通过（%s, strict=%s, escape_hatch=%s）：跳过字幕翻译/烧录，"
                                "保留字幕文件并继续上传原视频",
                                reason_text,
                                strict_qc,
                                escape_hatch_active,
                            )
                            self._quarantine_rejected_subtitle(asr_subtitle_path, task_logger)
                            update_task(
                                task_id,
                                subtitle_language_detected=detected_lang,
                                subtitle_warning_message=reason_text,
                                upload_progress=None,
                            )
                            return True
                        qc_cleared = True
                        qc_failed = False
            
            # 优化选择策略：若有中文字幕则直接烧录；否则优先选英文字幕进行翻译
            detected_list = []
            for f in subtitle_files:
                lang = self._detect_subtitle_language(f)
                detected_list.append((f, lang))
            zh_candidates = [f for f, lang in detected_list if str(lang).lower().startswith('zh')]
            en_candidates = [f for f, lang in detected_list if str(lang).lower().startswith('en')]
            
            if zh_candidates:
                # 直接使用中文字幕，不进行翻译
                subtitle_file = zh_candidates[0]
                subtitle_lang = 'zh'
                task_logger.info(f"检测到中文字幕，直接烧录，无需翻译: {os.path.basename(subtitle_file)}")

                if should_embed_subtitle and _embed_guard(subtitle_file, asr_artifact=asr_generated) and _ensure_asr_qc(subtitle_file):
                    embedded_video_path = self._embed_subtitle_in_video(
                        task_id, task['video_path_local'], subtitle_file, task_logger
                    )
                    if embedded_video_path:
                        update_task(
                            task_id,
                            video_path_local=embedded_video_path,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=None,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message=None,
                        )
                        task_logger.info("中文字幕烧录完成")
                        return True
                    else:
                        task_logger.warning("中文字幕烧录失败，保留原视频")
                        update_task(
                            task_id,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=None,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message='subtitle_embed_failed',
                        )
                        return False
                elif should_embed_subtitle:
                    task_logger.warning("中文字幕被烧录门控拦截，保留原视频并继续上传")
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_lang,
                        subtitle_warning_message=_block_warning(),
                    )
                    return True
                else:
                    # 不嵌入字幕，只保存信息
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_lang
                    )
                    task_logger.info("已检测到中文字幕，但未开启烧录，跳过翻译")
                    return True
            
            if not translation_enabled:
                # 只烧录模式：不创建翻译器，直接使用最合适的已有/ASR字幕。
                subtitle_file = zh_candidates[0] if zh_candidates else (en_candidates[0] if en_candidates else subtitle_files[0])
                subtitle_lang = self._detect_subtitle_language(subtitle_file)
                task_logger.info(f"字幕翻译未启用，直接使用原字幕进行烧录: {os.path.basename(subtitle_file)}")

                if should_embed_subtitle and _embed_guard(subtitle_file, asr_artifact=asr_generated) and _ensure_asr_qc(subtitle_file):
                    embedded_video_path = self._embed_subtitle_in_video(
                        task_id, task['video_path_local'], subtitle_file, task_logger
                    )
                    if embedded_video_path:
                        update_task(
                            task_id,
                            video_path_local=embedded_video_path,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=None,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message=None,
                        )
                        task_logger.info("原字幕烧录完成")
                        return True

                    task_logger.warning("原字幕烧录失败，保留原视频")
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_lang,
                        subtitle_warning_message='subtitle_embed_failed',
                    )
                    return False
                elif should_embed_subtitle:
                    task_logger.warning("原字幕被烧录门控拦截，保留原视频并继续上传")
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_lang,
                        subtitle_warning_message=_block_warning(),
                    )
                    return True

                update_task(
                    task_id,
                    subtitle_path_original=subtitle_file,
                    subtitle_path_translated=None,
                    subtitle_language_detected=subtitle_lang,
                )
                task_logger.info("字幕翻译和烧录均未启用，仅记录字幕文件")
                return True
            
            # 没有中文：优先选择英文，否则退回第一个文件
            subtitle_file = en_candidates[0] if en_candidates else subtitle_files[0]
            task_logger.info(f"找到字幕文件: {os.path.basename(subtitle_file)}")
            subtitle_lang = self._detect_subtitle_language(subtitle_file)
            task_logger.info(f"检测到字幕语言: {subtitle_lang}")
            if en_candidates and subtitle_file in en_candidates:
                task_logger.info("优先使用英文字幕进行翻译")

            # 创建翻译器（此时需要翻译为中文）
            from modules.subtitle_translator import create_translator_from_config
            translator = create_translator_from_config(self.config, task_id)
            if not translator:
                task_logger.error("无法创建字幕翻译器，请检查API配置")
                update_task(
                    task_id,
                    subtitle_path_original=subtitle_file,
                    subtitle_path_translated=None,
                    subtitle_language_detected=subtitle_lang,
                    upload_progress=None,
                    silent=True,
                )
                return False
            
            # 生成翻译后的文件路径
            # 强制使用 .srt 格式
            translated_subtitle_path = os.path.join(
                task_dir, 
                f"translated_{task_id}.srt"
            )
            
            # 定义进度回调函数
            def progress_callback(progress, current, total):
                if is_task_cancelled(task_id):
                    raise TaskCancelledError("任务已取消")
                # 不再将每次字幕翻译进度记录为 INFO 到文件，减少日志噪声
                task_logger.debug(f"字幕翻译进度: {progress:.1f}% ({current}/{total})")
                # 更新任务进度显示到网页
                update_task(task_id, upload_progress=f"{progress:.1f}%", silent=True)
            
            # 执行翻译
            cancel_event = get_task_cancel_event(task_id)
            success = translator.translate_file(
                subtitle_file, 
                translated_subtitle_path,
                progress_callback=progress_callback,
                cancel_event=cancel_event
            )
            
            if success:
                task_logger.info("字幕翻译完成")
                # 清除翻译进度显示
                update_task(task_id, upload_progress=None, silent=True)
                
                # 如果配置了将字幕嵌入视频
                # 双闸门：门控看译文产物，质检看**源**字幕 —— 译文由源字幕翻译
                # 而来，源头不合格就没有烧录价值；此前翻译分支只有单闸门，
                # 复用已有 asr_*.srt 时会整条绕过质检。
                if (
                    should_embed_subtitle
                    and _embed_guard(translated_subtitle_path, asr_artifact=asr_generated)
                    and _ensure_asr_qc(subtitle_file)
                ):
                    embedded_video_path = self._embed_subtitle_in_video(
                        task_id, task['video_path_local'], 
                        translated_subtitle_path, task_logger
                    )
                    if embedded_video_path:
                        # 更新视频路径为嵌入字幕的版本
                        update_task(
                            task_id,
                            video_path_local=embedded_video_path,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=translated_subtitle_path,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message=None,
                        )
                    else:
                        task_logger.warning("字幕嵌入失败，保留原视频和字幕文件")
                        update_task(
                            task_id,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=translated_subtitle_path,
                            subtitle_language_detected=subtitle_lang,
                            subtitle_warning_message='subtitle_embed_failed',
                        )
                elif should_embed_subtitle:
                    task_logger.warning("翻译字幕被烧录门控拦截，保留原视频和字幕文件")
                    # 拦截的若是 ASR 产物，必须移出扫描范围，否则下轮重跑会复用
                    # 这条不合格字幕、永远拿不到新字幕。
                    _quarantine_if_asr_artifact(subtitle_file)
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=translated_subtitle_path,
                        subtitle_language_detected=subtitle_lang,
                        subtitle_warning_message=_block_warning(),
                    )
                else:
                    # 不嵌入字幕，只保存字幕文件信息
                    update_task(
                        task_id,
                        subtitle_path_original=subtitle_file,
                        subtitle_path_translated=translated_subtitle_path,
                        subtitle_language_detected=subtitle_lang
                    )
                
                task_logger.info("字幕翻译处理完成")
                return True
            else:
                task_logger.warning("字幕翻译失败，按策略跳过字幕产物并继续后续上传")
                # 清除进度显示
                update_task(
                    task_id,
                    subtitle_path_original=subtitle_file,
                    subtitle_path_translated=None,
                    subtitle_language_detected=subtitle_lang,
                    upload_progress=None,
                    silent=True,
                )
                return False
                
        except Exception as e:
            task_logger.error(f"字幕翻译过程中发生错误: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            # 清除进度显示
            update_task(
                task_id,
                subtitle_path_translated=None,
                upload_progress=None,
                silent=True,
            )
            return False

    def _ensure_asr_subtitle_qc(self, task_id, subtitle_file, task_logger, quality_state, *, force_asr=None):
        """ASR 产物烧录前的质检闸门，返回 ``(allowed, qc_cleared, reason)``。

        非 ASR 产物（平台自带 / 人工提供字幕）直接放行且不标记 ``qc_cleared``
        —— 它们不是幻觉与时间轴错位的高风险来源，也不该被 ASR 的历史标记误拦。
        判定默认只看文件名（见 ``_is_asr_subtitle_artifact``）；``force_asr``
        用于「译文的来源是 ASR 字幕」这类**名字上继承不到**的情形：译文写入
        ``translated_*.srt``，不匹配 ``asr_*`` 前缀，若不显式继承来源就会被判为
        非 ASR 产物而**永远放行**（注释声称「至少让闸门有机会拒绝」，实际恒真）。
        ``quality_state`` 仅用于决定质检的**严格度**，不参与「是否质检」的判定。

        ``reason`` 只在与 ``allowed=False`` 搭配时有意义，供调用方在
        ``subtitle_warning_message`` 中保留**首个**拦截归因（此前外层分支会
        用 ``asr_failed_block_embed`` 覆盖掉这里更精确的原因）。
        """
        is_asr_artifact = _is_asr_subtitle_artifact(task_id, subtitle_file)
        if force_asr is not None:
            is_asr_artifact = bool(force_asr)
        if not is_asr_artifact:
            return True, False, ''
        qc_result = self._run_subtitle_qc(
            task_id,
            subtitle_file,
            task_logger,
            strict=(str(quality_state or '').strip().lower() == 'degraded'),
        )
        if qc_result is True:
            return True, True, ''
        if qc_result is SUBTITLE_QC_DISABLED:
            # 用户主动关闭质检：放行，但不标记 qc_cleared（不该借机改写历史
            # 质检结论，也不该让门控以为「本次已通过质检」）。
            task_logger.info("字幕质检已被配置关闭，ASR 字幕按用户配置放行烧录")
            return True, False, ''
        if _qc_result_allows_embed(qc_result, self.config):
            # 逃生口对「质检没跑成」同样生效。本方法被翻译分支与上传前阶段复用，
            # 而 ``_translate_subtitle`` 内那份 ``escape_hatch_active`` 是函数级
            # 局部变量、覆盖不到这里；若不在本方法内读同一个配置键，同一份
            # asr_*.srt 会因为走哪条分支而得到相反结论（实测两条路径相反）。
            # 明确的质检失败结论（False）仍不受逃生口影响。
            task_logger.info(
                "ASR_FAILURE_BLOCKS_EMBED=False：质检不可用不阻断烧录，按配置放行"
            )
            return True, False, ''
        reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
        task_logger.warning(
            "ASR 字幕质检未通过（%s）：跳过烧录，保留字幕文件并继续上传原视频",
            reason_text,
        )
        # 被拒对象是 ASR 来源的字幕（含 force_asr 继承来的译文），必须移出复用范围：
        # 留在 task_dir 里会让下一轮扫描直接复用它，用户重跑永远拿不到新字幕。
        self._quarantine_rejected_subtitle(
            subtitle_file, task_logger, force=bool(force_asr)
        )
        update_task(task_id, subtitle_warning_message=reason_text, upload_progress=None)
        return False, False, reason_text

    def _quarantine_rejected_subtitle(self, subtitle_path, task_logger=None, *, force=False) -> str:
        """把被质检/质量门控拒绝的 ASR 字幕移出扫描范围。

        为什么必须移动：被拒绝的字幕文件留在 task_dir 里会让下一轮
        ``_translate_subtitle`` 的 ``os.listdir`` 扫描命中它，从而**跳过 ASR
        重新生成**、直接复用这条已被判定不合格的字幕 —— 用户重跑任务永远
        拿不到新字幕。改名为 ``.rejected.txt`` 后内容仍保留供人工检查，但不再
        匹配 ``.srt/.vtt`` 过滤条件，因此既不会被复用也不会被上传。

        **默认只处理 ASR 产物**：判据在方法内部（只看文件名），而不是依赖每个调用点
        自己先判定。此前防护只加在 ``_quarantine_if_asr_artifact`` 这层包装上，
        而 ``_ensure_asr_subtitle_qc`` 内部那条路径直接调用本方法，导致平台自带
        字幕 ``video.zh.srt`` 在任务级 ``degraded`` 时被改名成
        ``video.zh.rejected.txt`` —— 破坏用户既有产物，且改名后
        ``subtitle_path_original`` 仍指向已不存在的路径。

        ``force=True`` 仅供「译文由 ASR 源字幕翻译而来」这一处使用：文件名
        （``translated_*.srt``）继承不到 ``asr_`` 前缀，来源判定由调用方给出。
        """
        if not force and not _is_asr_subtitle_artifact('', subtitle_path):
            return ''
        try:
            source = str(subtitle_path or '').strip()
            if not source or not os.path.exists(source):
                return ''
            target = f"{os.path.splitext(source)[0]}.rejected.txt"
            index = 1
            while os.path.exists(target):
                target = f"{os.path.splitext(source)[0]}.rejected.{index}.txt"
                index += 1
            os.replace(source, target)
            if task_logger is not None:
                task_logger.info(
                    "已被拒绝的 ASR 字幕已移出复用范围（保留供人工检查）：%s",
                    os.path.basename(target),
                )
            return target
        except Exception as exc:
            if task_logger is not None:
                try:
                    task_logger.warning(f"移动被拒绝的 ASR 字幕失败（不影响上传）: {exc}")
                except Exception:
                    pass
            return ''

    def _mark_subtitle_qc_unavailable(self, task_id, task_logger=None, detail='', blocks=None):
        """把「质检没跑成」显式落库，返回落库成功与否。

        为什么必须落库：``_run_subtitle_qc`` 返回 ``None``（字幕文件缺失 /
        质检执行异常）时此前不写任何字段，``subtitle_qc_failed`` 仍停留在 0。
        后果是一条跨阶段失效链：本阶段拒绝烧录并隔离了源字幕、只落了
        ``subtitle_warning_message='qc_unavailable'``；下一阶段（上传前
        ``_prepare_subtitle_for_upload``）重新读取门控状态时拿到的是
        ``('ok', 0)`` → 放行 → 命中「既有带字幕视频可复用」直接 return。
        而源字幕已被隔离，``_get_embedded_video_candidate`` 的新鲜度检查
        看不到它，就把**上一轮烧录的旧成片**判为新鲜并复用 —— 用户界面显示
        「质检未通过、已跳过烧录」，上传的成片却带着那条被拒字幕。

        落一个显式不可用标记后，``_is_subtitle_stage_rejected`` 与
        ``_subtitle_block_reasons`` 都能看到它，门控在后续阶段保持一致结论。

        ``blocks`` 表示本次「质检不可用」是否按门控拦截烧录（默认取逃生口
        ``ASR_FAILURE_BLOCKS_EMBED``）。逃生口打开时它**不**拦截，此时不得落
        ``subtitle_qc_failed=1`` —— 否则同一状态在不同阶段得到相反结论：
        ``_ensure_asr_subtitle_qc`` 按逃生口放行，而上传前阶段读到库里的
        ``qc_failed=1`` 又把烧录拦下、并把字幕阶段永久判为「未完成」，
        用户显式配置的宽松策略失效且每轮重跑都重做 ASR。
        """
        if blocks is None:
            blocks = _as_bool(self.config.get('ASR_FAILURE_BLOCKS_EMBED', True))
        try:
            fields = {
                'subtitle_qc_reason': 'qc_unavailable',
                'subtitle_qc_checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'upload_progress': None,
            }
            if blocks:
                fields['subtitle_qc_failed'] = 1
            else:
                # 只更新「没跑成」这一事实，保留既有结论：逃生口只放宽
                # 「质检没跑成」，不得顺手把历史上的明确质检失败结论洗白。
                current = get_task(task_id) or {}
                fields['subtitle_qc_failed'] = 1 if current.get('subtitle_qc_failed') == 1 else 0
            update_task(task_id, **fields)
            return True
        except Exception as exc:
            if task_logger is not None:
                try:
                    task_logger.warning(f"写入质检不可用标记失败（不影响本轮拦截决策）: {exc}")
                except Exception:
                    pass
            return False

    def _persist_qc_unavailable(self, task_id, task_logger=None, detail=''):
        """落库「质检没跑成」标记，并**消费**落库结果。

        ``_mark_subtitle_qc_unavailable`` 的返回值此前被两个调用点直接丢弃：
        写入失败时它只记一条 warning，于是「结论不落库」的失效链（后续阶段
        重新读库拿到 ``qc_failed=0`` 而放行复用旧成片）会在 DB 写失败这一情形下
        原样重现。这里显式消费返回值并留下可排查的告警，不再静默降级。
        """
        persisted = self._mark_subtitle_qc_unavailable(task_id, task_logger, detail=detail)
        if not persisted and task_logger is not None:
            try:
                task_logger.warning(
                    "质检不可用标记未能落库（detail=%s）：后续阶段可能看不到本次"
                    "拦截结论，若复用既有成片请人工确认其字幕是否合格",
                    detail or 'n/a',
                )
            except Exception:
                pass
        return persisted

    def _run_subtitle_qc(self, task_id: str, srt_path: str, task_logger, strict: bool = False):
        """对 ASR 生成字幕执行预检，每次都重新计算结果。

        返回三态：``True`` 通过、``False`` 未通过、``SUBTITLE_QC_DISABLED``
        用户主动关闭质检、``None`` 质检启用但不可用（文件缺失 / 执行异常）。
        调用方必须区分「通过」与「没跑成」—— 此前两种情况都返回 ``True``，
        导致质检形同虚设；而「主动关闭」与「没跑成」混用又会造成关闭质检
        反而更严格的反向回归。
        两条「不可用」分支都会落库显式标记（见 ``_mark_subtitle_qc_unavailable``），
        避免拦截结论不持久化导致后续阶段复用旧成片。
        ``strict=True`` 时对 ASR 来源退化的字幕启用更严格判定。
        """
        enabled_raw = self.config.get('SUBTITLE_QC_ENABLED', True)
        enabled = _as_bool(enabled_raw)
        if not enabled:
            task_logger.info("字幕质检未启用，按配置跳过质检并放行烧录")
            return SUBTITLE_QC_DISABLED

        if not srt_path or not os.path.exists(srt_path):
            try:
                task_logger.warning(f"字幕质检不可用：字幕文件缺失 {srt_path}")
            except Exception:
                pass
            self._persist_qc_unavailable(task_id, task_logger, detail='missing_file')
            return None

        try:
            from modules.subtitle_qc import run_subtitle_qc

            result = run_subtitle_qc(
                srt_path,
                self.config,
                total_duration_s=self._resolve_qc_total_duration(task_id, task_logger),
                strict=bool(strict),
            )
            checked_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            update_task(
                task_id,
                subtitle_qc_failed=0 if result.passed else 1,
                subtitle_qc_reason=result.reason,
                subtitle_qc_score=float(result.score),
                subtitle_qc_checked_at=checked_at,
            )

            if result.passed:
                raw_ai = result.raw_ai if isinstance(result.raw_ai, dict) else {}
                task_logger.info(
                    "字幕质检通过: decision=%s, reason=%s, final_score=%.3f, rule_score=%.3f, ai_score=%s, ai_mode=%s, ai_override=%s, sample_items=%s, sample_chars=%s, timeline=%s",
                    result.decision or 'unknown',
                    result.reason,
                    float(result.score),
                    float(result.rule_score),
                    f"{float(result.ai_score):.3f}" if result.ai_score is not None else 'n/a',
                    str(raw_ai.get('ai_mode') or 'n/a'),
                    bool(raw_ai.get('ai_override')),
                    result.sample_items,
                    result.sample_chars,
                    raw_ai.get('timeline_metrics') or 'n/a',
                )
                return True

            raw_ai = result.raw_ai if isinstance(result.raw_ai, dict) else {}
            task_logger.warning(
                "字幕质检失败: decision=%s, reason=%s, final_score=%.3f, rule_score=%.3f, ai_score=%s, ai_mode=%s, ai_override=%s, sample_items=%s, sample_chars=%s, timeline=%s",
                result.decision or 'unknown',
                result.reason,
                float(result.score),
                float(result.rule_score),
                f"{float(result.ai_score):.3f}" if result.ai_score is not None else 'n/a',
                str(raw_ai.get('ai_mode') or 'n/a'),
                bool(raw_ai.get('ai_override')),
                result.sample_items,
                result.sample_chars,
                raw_ai.get('timeline_metrics') or 'n/a',
            )
            return False
        except Exception as e:
            # 质检执行异常不再默认放行：返回 None 表示「不可用」，由调用方拒绝烧录。
            try:
                task_logger.warning(f"字幕质检执行异常，判定为质检不可用，将不烧录字幕: {e}")
            except Exception:
                pass
            self._persist_qc_unavailable(task_id, task_logger, detail=f'exception: {e}')
            return None

    def _resolve_qc_total_duration(self, task_id: str, task_logger=None):
        """取视频总时长用于质检的时间轴维度；取不到时返回 None（跳过该维度）。"""
        try:
            task = get_task(task_id)
            video_path = str(task.get('video_path_local') or '') if task else ''
            if not video_path or not os.path.exists(video_path):
                return None
            # _get_video_duration 要求有效 logger（内部直接调用 .info/.warning）。
            duration = self._get_video_duration(video_path, task_logger or logger)
            duration_value = float(duration or 0.0)
            return duration_value if duration_value > 0 else None
        except Exception:
            return None

    def _mark_subtitle_issue(self, task_id: str, reason: str, score=None):
        """将字幕异常写入任务表，复用前端现有“字幕异常”展示。"""
        update_task(
            task_id,
            subtitle_qc_failed=1,
            subtitle_qc_reason=reason,
            subtitle_qc_score=score,
            subtitle_qc_checked_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )
    
    def _detect_subtitle_language(self, subtitle_path):
        """从字幕文件名提取语言代码（如 video.ja.srt -> ja）"""
        try:
            filename = os.path.basename(subtitle_path)
            name_without_ext = os.path.splitext(filename)[0]
            parts = name_without_ext.split('.')
            if len(parts) >= 2:
                lang_code = parts[-1].lower()
                if '-' in lang_code:
                    lang_code = lang_code.split('-')[0]
                return lang_code
            return "auto"
        except Exception:
            return "auto"
    
    _KNOWN_HW_ENCODER_ERROR_PATTERNS = (
        # 软件编码器：libx265 不一定被编进用户的 FFmpeg（自备构建常见
        # --disable-libx265）。此时没有任何 GPU 相关的错误文本，若不登记，
        # 失败会既不属于已知错误、也拿不到任何降级阶段，整任务直接失败。
        'Unknown encoder "libx265"',
        "Unknown encoder 'libx265'",
        # NVENC (NVIDIA)
        'Unknown encoder "h264_nvenc"',
        'Unknown encoder "hevc_nvenc"',
        "Unknown encoder 'h264_nvenc'",
        "Unknown encoder 'hevc_nvenc'",
        'No NVENC capable devices found',
        'Device not present',
        'No such filter: "subtitles"',
        "No such filter: 'subtitles'",
        'Cannot load libnvidia-encode',
        'Failed to query maximum',
        'Cannot find a CUDA capable',
        'CUDA_ERROR_',
        'nvcuda.dll',
        'libnvcuvid',
        'nvenc_load_functions',
        'Could not initialize',
        'OpenEncodeSession failed',
        'out of memory',
        'InitializeEncoder failed',
        'GPU is not in the list',
        # QSV (Intel)
        'Unknown encoder "h264_qsv"',
        'Unknown encoder "hevc_qsv"',
        "Unknown encoder 'h264_qsv'",
        "Unknown encoder 'hevc_qsv'",
        'MFXInit',
        'Error initializing an internal MFX session',
        'Error creating a MFX session',
        'Error initializing the MFX video session',
        'Unable to find',
        'No device',
        'Device creation failed',
        'iHD_drv_video.so',
        'i965_drv_video.so',
        # AMF (AMD Windows)
        'Unknown encoder "h264_amf"',
        'Unknown encoder "hevc_amf"',
        "Unknown encoder 'h264_amf'",
        "Unknown encoder 'hevc_amf'",
        'AMF initialization failed',
        'AMFContext',
        'AMF failed',
        'amfrt64.dll',
        # VAAPI (AMD/Intel Linux)
        'Unknown encoder "h264_vaapi"',
        'Unknown encoder "hevc_vaapi"',
        "Unknown encoder 'h264_vaapi'",
        "Unknown encoder 'hevc_vaapi'",
        'Error creating a VAAPI device',
        'Failed to create VAAPI device',
        'Failed to initialise VAAPI connection',
        'No VA display found for device',
        'vaInitialize',
        'vaDeriveImage',
        '/dev/dri/renderD128',
        'libva',
        'hwupload',
        'A hardware device reference is required to upload frames to.',
        'Failed to configure output pad on Parsed_hwupload',
        'Driver does not support the required nvenc API version',
        'The minimum required Nvidia driver for nvenc is',
        'libamfrt64.so',
        "failed to load library 'libX11.so.6'",
        'cannot open shared object file',
        'Assertion in generated code',
        # 编码器不接受某个选项：多见于老版本 FFmpeg 或老驱动不支持
        # 新引入的质量增强参数（AQ / 前瞻 / 多遍 / blbrc 等）。
        # 命中后走「关闭质量增强 → CPU」的分级降级，而不是直接掉到 CPU。
        'Unrecognized option',
        'Option not found',
        'Error setting option',
        'Error applying encoder options',
        'Error parsing options',
        'Invalid option',
    )

    # 「参数类」错误的子集：只有关闭质量增强才可能治好。
    # 其余已知硬件错误属于设备/驱动类（设备被占用、驱动崩溃、显存不足、
    # 编码器不存在等），关闭增强不会改变任何东西 —— 必须先按同一硬编器重跑
    # 一遍整部视频再轮到 CPU，纯属白跑（长视频代价极高）。
    _HW_OPTION_ERROR_PATTERNS = (
        'Unrecognized option',
        'Option not found',
        'Error setting option',
        'Error applying encoder options',
        'Error parsing options',
        'Invalid option',
    )

    _HW_PROBE_DEFAULT_SIZE = '640x480'
    _HW_PROBE_RETRY_SIZE = '1280x720'
    _HW_PROBE_DURATION = '0.1'
    _HW_PROBE_DIMENSION_ERROR_PATTERNS = (
        'frame dimension less than the minimum supported value',
        'invalid param (8)',
    )
    _ASS_PLAY_RES_X = 1920
    _ASS_PLAY_RES_Y = 1080
    #: force_style 比生成 ASS 文档多需要的键：生成路径把这些值写进 [V4+ Styles]
    #: 的 Style 行，而 ASS/SSA 输入路径必须靠 force_style 逐键覆盖源样式。
    _FORCE_STYLE_APPEARANCE_KEYS = (
        'PrimaryColour',
        'OutlineColour',
        'BackColour',
        'BorderStyle',
        'Bold',
    )

    _ASS_STYLE_BASE = {
        'FontSize': 56.0,
        'Outline': 2.0,
        'Shadow': 1.0,
        'MarginV': 40.0,
        'MarginL': 96.0,
        'MarginR': 96.0,
        'Alignment': 2,
        # BorderStyle=4 gives a modern rounded-rectangle background box
        # (supported by libass/FFmpeg), which is the dominant look for
        # streaming/online-video captions.
        # BorderStyle=1 with a thick, dark semi-transparent outline.
        # This gives the clean "no-box" streaming caption look while keeping
        # text readable on bright/complex backgrounds.
        'BorderStyle': 1,
        'Bold': 1,
        'PrimaryColour': '&H00FFFFFF',
        'SecondaryColour': '&H00FFFFFF',
        'OutlineColour': '&HB2000000',
        'BackColour': '&H00000000',
    }
    _ASS_LANDSCAPE_FONT_SIZE_ANCHORS = (
        (720.0, 48.0),
        (1080.0, 54.0),
        (1440.0, 68.0),
        (2160.0, 102.0),
    )
    _ASS_PORTRAIT_FONT_SIZE_ANCHORS = (
        (720.0, 44.0),
        (1280.0, 54.0),
        (1920.0, 68.0),
        (2560.0, 76.0),
    )
    _ASS_LANDSCAPE_MARGIN_V_ANCHORS = (
        (720.0, 56.0),
        (1080.0, 62.0),
        (1440.0, 82.0),
        (2160.0, 124.0),
    )
    _ASS_PORTRAIT_MARGIN_V_ANCHORS = (
        (720.0, 120.0),
        (1280.0, 156.0),
        (1920.0, 220.0),
        (2560.0, 292.0),
    )
    # Landscape captions use generous side margins to avoid crowding the
    # edges, but still keep enough width to stay single-line after scaling.
    _ASS_LANDSCAPE_SIDE_MARGIN_RATIO = 0.025
    _ASS_LANDSCAPE_SIDE_MARGIN_MIN = 32.0
    _ASS_LANDSCAPE_SIDE_MARGIN_MAX = 80.0
    _ASS_PORTRAIT_SIDE_MARGIN_RATIO = 0.095
    _ASS_PORTRAIT_SIDE_MARGIN_MIN = 82.0
    _ASS_PORTRAIT_SIDE_MARGIN_MAX = 156.0
    _ASS_LANDSCAPE_LAYOUT_DENSITY = 0.93
    # 单行优先模式允许密度略 >1.0：font_size 按等宽近似估算偏保守，略微放宽
    # 以更充分地利用 _ASS_SAFE_WIDTH_RATIO(0.98) 已留出的安全宽度。实际单行
    # 字符上限由下游 _clamp(18, 28) 钳制，不会真正溢出可用宽度。
    _ASS_LANDSCAPE_SINGLE_LINE_DENSITY = 1.04
    _ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MIN = 28.0
    _ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX = 38.0
    _ASS_PORTRAIT_LAYOUT_DENSITY = 0.92
    # Allow text to use almost the full usable width. A small safety margin
    # remains so descenders/outlines do not touch the screen edges.
    _ASS_SAFE_WIDTH_RATIO = 0.98
    _ASS_OVERRIDE_FONT_SIZE_RATIO_MIN = 0.60
    # Allow aggressive down-scaling for single-line priority. Landscape
    # captions are required to stay on one line, so we shrink the font as
    # far as the global override minimum permits before accepting overflow.
    _ASS_SINGLE_LINE_FONT_SCALE_MIN = 0.60
    _ASS_OVERRIDE_FONT_SIZE_MIN = 32.0
    _ASS_HARD_WRAP_MIN_LINE_LENGTH = 8
    _ASS_PORTRAIT_RESCUE_LINE_LENGTH_MAX = 16.0
    # Thicker outline for the BorderStyle=4 rounded box, plus a soft shadow.
    _ASS_OUTLINE_RATIO = 0.075
    _ASS_OUTLINE_MIN = 2.2
    _ASS_OUTLINE_MAX = 5.5
    _ASS_SHADOW_RATIO = 0.025
    _ASS_SHADOW_MIN = 1.0
    _ASS_SHADOW_MAX = 2.2
    _ASS_OVERRIDE_OUTLINE_RATIO = 0.045
    _ASS_OVERRIDE_OUTLINE_MIN = 1.5
    _ASS_OVERRIDE_OUTLINE_MAX = 3.0
    _STREAMING_SRT_TEMPLATE_HEIGHTS = (720, 1080, 1440, 2160)
    _STREAMING_SRT_STYLE_TEMPLATES = {
        720: {
            'FontSize': 18.0,
            'Outline': 0.4,
            'Shadow': 0.6,
            'MarginL': 42,
            'MarginR': 42,
            'MarginV': 16,
        },
        1080: {
            'FontSize': 18.0,
            'Outline': 0.4,
            'Shadow': 0.6,
            'MarginL': 42,
            'MarginR': 42,
            'MarginV': 16,
        },
        1440: {
            'FontSize': 18.0,
            'Outline': 0.4,
            'Shadow': 0.6,
            'MarginL': 42,
            'MarginR': 42,
            'MarginV': 16,
        },
        2160: {
            'FontSize': 18.0,
            'Outline': 0.4,
            'Shadow': 0.6,
            'MarginL': 42,
            'MarginR': 42,
            'MarginV': 16,
        },
    }
    _STREAMING_SRT_PORTRAIT_FONT_SIZE_ANCHORS = (
        (1280.0, 12.5),
        (1920.0, 12.5),
        (2560.0, 13.5),
    )
    _STREAMING_SRT_PORTRAIT_MARGIN_V_ANCHORS = (
        (1280.0, 18.0),
        (1920.0, 28.0),
        (2560.0, 40.0),
    )
    _STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO = 0.08
    _STREAMING_SRT_LANDSCAPE_LAYOUT_DENSITY = 0.72
    _STREAMING_SRT_PORTRAIT_LAYOUT_DENSITY = 0.96
    _BUNDLED_FONT_EXTENSIONS = ('.otf', '.ttf', '.ttc', '.otc')

    @staticmethod
    def _short_error_text(error_output, limit=240):
        text = str(error_output or '').replace('\n', ' ').strip()
        if not text:
            return ''
        return text[:limit]

    @staticmethod
    def _summarize_cmd(cmd, limit=300):
        summary = ' '.join(str(x) for x in (cmd or []))
        if len(summary) <= limit:
            return summary
        return summary[:limit]

    @classmethod
    def _is_probe_dimension_error(cls, error_output):
        if not error_output:
            return False
        text = str(error_output).lower()
        return any(pattern in text for pattern in cls._HW_PROBE_DIMENSION_ERROR_PATTERNS)

    @classmethod
    def _should_keep_nvidia_preference_on_probe_failure(cls, nvenc_listed, nvidia_device_visible, detect_error):
        return bool(
            nvenc_listed
            and nvidia_device_visible
            and cls._is_probe_dimension_error(detect_error)
        )

    @classmethod
    def _build_hw_probe_cmd(cls, ffmpeg_bin, encoder_name, probe_size=None):
        size = probe_size or cls._HW_PROBE_DEFAULT_SIZE
        color_src = f"color=c=black:s={size}:d={cls._HW_PROBE_DURATION}"
        encoder_name_lower = str(encoder_name or '').lower().strip()
        test_cmd = [ffmpeg_bin, '-hide_banner', '-loglevel', 'error']

        if encoder_name_lower == 'h264_vaapi':
            test_cmd.extend([
                '-vaapi_device', '/dev/dri/renderD128',
                '-f', 'lavfi', '-i', color_src,
                '-vf', 'format=nv12,hwupload',
                '-frames:v', '1',
                '-c:v', 'h264_vaapi',
                '-qp', '23',
            ])
        elif encoder_name_lower == 'hevc_vaapi':
            test_cmd.extend([
                '-vaapi_device', '/dev/dri/renderD128',
                '-f', 'lavfi', '-i', color_src,
                '-vf', 'format=nv12,hwupload',
                '-frames:v', '1',
                '-c:v', 'hevc_vaapi',
                '-qp', '23',
                '-profile:v', 'main',
            ])
        elif encoder_name_lower == 'h264_qsv':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', 'h264_qsv',
                '-global_quality', '23',
                '-look_ahead', '0',
                '-pix_fmt', 'nv12',
            ])
        elif encoder_name_lower == 'hevc_qsv':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', 'hevc_qsv',
                '-global_quality', '23',
                '-look_ahead', '0',
                '-profile:v', 'main',
                '-pix_fmt', 'nv12',
            ])
        elif encoder_name_lower == 'h264_amf':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
                '-usage', 'transcoding',
                '-quality', 'balanced',
                '-rc', 'cqp',
                '-qp_i', '23',
                '-qp_p', '23',
                '-qp_b', '23',
            ])
        elif encoder_name_lower == 'hevc_amf':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
                '-usage', 'transcoding',
                '-quality', 'balanced',
                '-rc', 'qvbr',
                '-qvbr_quality_level', '24',
                '-profile:v', 'main',
            ])
        elif encoder_name_lower == 'hevc_nvenc':
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
                '-preset', 'p4',
                '-rc:v', 'vbr',
                '-cq:v', '23',
                '-profile:v', 'main',
            ])
        elif 'nvenc' in encoder_name_lower:
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
                '-preset', 'p4',
                '-rc:v', 'vbr',
                '-cq:v', '23',
            ])
        else:
            test_cmd.extend([
                '-f', 'lavfi', '-i', color_src,
                '-frames:v', '1',
                '-c:v', encoder_name,
            ])

        test_cmd.extend(['-f', 'null', '-'])
        return test_cmd

    @classmethod
    def _probe_hw_encoder_availability(cls, ffmpeg_bin, encoder_name):
        attempts = [cls._HW_PROBE_DEFAULT_SIZE]
        last_meta = None
        last_error = ''

        for idx, probe_size in enumerate(attempts):
            probe_retry = idx > 0
            test_cmd = cls._build_hw_probe_cmd(
                ffmpeg_bin=ffmpeg_bin,
                encoder_name=encoder_name,
                probe_size=probe_size,
            )
            test_result = subprocess.run(
                test_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=15
            )
            err_msg = (test_result.stderr or test_result.stdout or '').strip()
            if not err_msg and test_result.returncode != 0:
                err_msg = '未知错误'
            last_error = err_msg
            probe_error_short = cls._short_error_text(err_msg)
            last_meta = {
                'probe_size': probe_size,
                'probe_retry': probe_retry,
                'probe_returncode': test_result.returncode,
                'probe_error_short': probe_error_short,
                'probe_cmd_summary': cls._summarize_cmd(test_cmd),
            }

            if test_result.returncode == 0:
                return True, '', last_meta

            if idx == 0 and cls._is_probe_dimension_error(err_msg):
                attempts.append(cls._HW_PROBE_RETRY_SIZE)

        if not last_meta:
            last_meta = {
                'probe_size': cls._HW_PROBE_DEFAULT_SIZE,
                'probe_retry': False,
                'probe_returncode': -1,
                'probe_error_short': '未知错误',
                'probe_cmd_summary': '',
            }
        return False, last_error or '未知错误', last_meta

    def _parse_custom_video_params(self, task_logger):
        """解析自定义视频参数，解析失败时回退默认策略。"""
        enabled = _as_bool(self.config.get('VIDEO_CUSTOM_PARAMS_ENABLED', False))
        custom_params = str(self.config.get('VIDEO_CUSTOM_PARAMS', '')).strip()
        if not enabled or not custom_params:
            return None
        try:
            params = shlex.split(custom_params)
            if not params:
                return None
            return params
        except ValueError as e:
            task_logger.warning(f"自定义视频参数解析失败，回退默认编码参数: {e}")
            return None

    @staticmethod
    def _coerce_int(value):
        try:
            if value is None or value == '':
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _select_audio_target_bitrate(cls, input_bit_rate):
        bit_rate = cls._coerce_int(input_bit_rate)
        if bit_rate is None or bit_rate <= 0:
            return '128k'
        if bit_rate >= 192000:
            return '192k'
        if bit_rate >= 160000:
            return '160k'
        if bit_rate >= 128000:
            return '128k'
        if bit_rate >= 96000:
            return '96k'
        return '64k'

    @classmethod
    def _build_audio_transcode_params(cls, audio_info):
        """统一音频转码参数，AAC 优先直拷，其他编码按源码率上限转 AAC。

        实现统一收敛到 video_encoder_params.build_audio_params，保留本方法作为
        类内兼容入口（既有测试与调用方签名不变）。
        """
        return build_audio_params(audio_info)

    @staticmethod
    def _estimate_embed_timeout(video_duration, cpu_codec=None):
        """根据视频时长估算 FFmpeg 超时时间（秒）。

        x264 路径的估算与历史完全一致（时长 * 3，超过 30 分钟则 * 2，下限 1800，
        上限 10800）。x265 路径按 EMBED_TIMEOUT_X265_FACTOR 放大估算与上限：
        libx265 实测比同 preset 的 libx264 慢约 5 倍，沿用 x264 的预算会让长视频
        在编码中途被强杀；而且首轮就把 overall_deadline 用完时，后续降级阶段会被
        「预算已耗尽」直接跳过 —— 连 x265 -> x264 那一级都轮不到。
        """
        if video_duration:
            rate = 3 if video_duration < 1800 else 2
            estimated_time = video_duration * rate
        else:
            estimated_time = 3600

        if normalize_cpu_codec(cpu_codec) == 'x265':
            estimated_time *= EMBED_TIMEOUT_X265_FACTOR
            ceiling = EMBED_TIMEOUT_MAX_SECONDS_X265
        else:
            ceiling = EMBED_TIMEOUT_MAX_SECONDS

        return int(max(EMBED_TIMEOUT_MIN_SECONDS, min(estimated_time, ceiling)))

    @staticmethod
    def _build_embed_ffmpeg_cmd(
        ffmpeg_bin,
        input_video,
        vf_filter,
        vparams,
        aparams,
        output_video,
        color_params=None,
    ):
        """构建统一的 FFmpeg 转码命令。"""
        return [
            ffmpeg_bin, '-y',
            '-i', input_video,
            '-vf', vf_filter,
            *vparams,
            # 色彩元数据必须显式写出，否则播放器会按默认色域解释导致偏色
            *(color_params or []),
            *aparams,
            '-movflags', '+faststart',
            '-progress', 'pipe:1',
            output_video
        ]

    @classmethod
    def _is_known_hw_encoder_error(cls, error_output):
        """判断是否命中已知硬编/滤镜错误，触发 CPU 回退。"""
        if not error_output:
            return False
        error_lower = error_output.lower()
        return any(err.lower() in error_lower for err in cls._KNOWN_HW_ENCODER_ERROR_PATTERNS)

    @classmethod
    def _is_hw_option_error(cls, error_output):
        """判断失败是否属于「编码器不接受某个选项」这一类。

        这一类才值得先关掉质量增强、用同一硬编器重试；设备/驱动类错误关掉增强
        没有任何作用，应先走 CPU（见 ``_resolve_embed_retry_stages``）。
        """
        if not error_output:
            return False
        error_lower = error_output.lower()
        return any(err.lower() in error_lower for err in cls._HW_OPTION_ERROR_PATTERNS)

    @staticmethod
    def _resolve_embed_retry_stages(actual_encoder, hw_quality_boost, hw_error_detected,
                                    hw_option_error=None, cpu_codec=None):
        """决定字幕烧录失败后的降级阶段顺序。

        硬件编码器失败时先尝试「关闭质量增强后用同一编码器重试」，保住硬件加速；
        只有在仍失败时才退回 CPU。若当前已经是 CPU 编码，重跑同样的命令必然
        同样失败（失败原因在滤镜或输入而不在编码器），因此不再重试 —— 这能避免
        为一个失败的长视频白跑一遍完整转码。

        ``hw_quality_boost``（是否开了质量增强）与 ``hw_option_error``（失败是否
        属于「编码器不接受某个选项」）共同决定是否需要 ``hw_no_boost`` 这一级：

        - 没开增强 → 没有可关的东西，直接 CPU；
        - 开了增强，但错误是设备/驱动类（设备被占用、驱动崩溃、显存不足、
          编码器不存在）→ 关掉增强也治不好，直接 CPU，避免按同一硬编器把整部
          视频白跑一遍；
        - 开了增强且错误是参数类，或错误文本完全未知 → 先关增强用同一硬编器
          重试，保留硬件加速。

        ``hw_option_error=None`` 时按旧语义处理（已知硬件错误即不值得关增强重试），
        便于既有调用点与测试渐进迁移。

        ``cpu_codec`` 是 CPU 软编码器取值（'x264' / 'x265'）。当用户选的软编码器是
        x265 时，CPU 路径仍有一级可降：改用 libx264 重跑。libx265 不一定被编进
        用户的 FFmpeg，切换实现时也可能带上不被接受的参数，而 libx264 在
        --enable-gpl 构建里几乎是必然存在的。x264 已是最底层，没有更下一级。

        返回 'hw_no_boost' / 'cpu' / 'cpu_x264' 组成的列表，按执行顺序排列。
        """
        encoder = str(actual_encoder or '').strip().lower()
        if encoder not in ('nvidia', 'intel', 'amd'):
            if encoder == 'cpu' and normalize_cpu_codec(cpu_codec) == 'x265':
                return ['cpu_x264']
            return []
        stages = []
        if hw_quality_boost:
            if hw_option_error is None:
                # 旧语义：不知道细分类型，按「已知硬件错误即不值得关增强」处理
                worth_disabling_boost = not bool(hw_error_detected)
            else:
                # 三种情形必须区分：
                #   参数类错误（hw_option_error）→ 关增强有可能治好，值得一试；
                #   已知设备/驱动类错误（hw_error_detected 且非参数类）→ 关增强无效；
                #   完全未知的错误文本（两者皆假）→ 无法判断，仍先试关增强，
                #     避免把「只是不认识某个新参数」误判成设备故障而直接掉到 CPU。
                worth_disabling_boost = bool(hw_option_error) or not bool(hw_error_detected)
            if worth_disabling_boost:
                stages.append('hw_no_boost')
        stages.append('cpu')
        return stages

    @staticmethod
    def _drop_same_encoder_stage_after_timeout(retry_stages):
        """超时失败后剔除「关增强、同编码器重试」这一级。

        超时的含义是「这份活在这个编码器上跑不完」，同编码器关增强重试对长视频
        通常只是把整片时间再花一次（最现实的失败类型反而拿到最差的降级策略）。
        因此超时只保留「换编码器」的那一级（CPU）。
        """
        try:
            return [str(stage) for stage in (retry_stages or []) if str(stage) != 'hw_no_boost']
        except Exception:
            return []

    @staticmethod
    def _describe_hw_failure(actual_encoder, *, timed_out=False, hw_option_error=False,
                             hw_error_detected=False, boost_enabled=True, returncode=None):
        """硬编失败的降级归因，供日志与排查使用（纯函数）。

        返回值是稳定的语义键：``timeout`` / ``option_error`` /
        ``option_error_no_boost`` / ``device_error`` / ``unknown``。

        ``option_error_no_boost`` 存在的意义：质量增强本来就没开时，
        「先关闭质量增强后用同一编码器重试」是空动作，照搬那句话会把人带偏。
        """
        if timed_out:
            return 'timeout'
        if hw_option_error:
            return 'option_error' if boost_enabled else 'option_error_no_boost'
        if hw_error_detected:
            return 'device_error'
        return 'unknown'

    @staticmethod
    def _finalize_embedded_video_output(temp_output_path, final_output_path):
        """优先原子替换输出文件，失败时回退复制。"""
        try:
            os.replace(temp_output_path, final_output_path)
        except OSError:
            shutil.copy2(temp_output_path, final_output_path)
            try:
                os.remove(temp_output_path)
            except Exception:
                pass

    def _convert_vtt_to_srt(self, vtt_path, task_logger):
        """将VTT字幕文件转换为SRT格式（FFmpeg对SRT支持更好）"""
        try:
            import os
            
            # 生成SRT文件路径
            base_path, _ = os.path.splitext(vtt_path)
            srt_path = f"{base_path}.srt"
            
            with open(vtt_path, 'r', encoding='utf-8') as vtt_file:
                vtt_content = vtt_file.read()
            srt_content = _convert_vtt_text_to_srt_text(vtt_content)
            if not srt_content:
                task_logger.error(f"VTT未解析出有效字幕: {vtt_path}")
                return None
            
            # 写入SRT文件
            with open(srt_path, 'w', encoding='utf-8') as srt_file:
                srt_file.write(srt_content)
            
            task_logger.info(f"VTT转换为SRT成功: {srt_path}")
            return srt_path
            
        except Exception as e:
            task_logger.error(f"VTT转SRT转换失败: {str(e)}")
            return None

    @staticmethod
    def _clamp(value, minimum, maximum):
        return max(minimum, min(maximum, value))

    @staticmethod
    def _interpolate_anchor_value(position, anchors):
        normalized_anchors = []
        for anchor_position, anchor_value in anchors or ():
            try:
                normalized_anchors.append((float(anchor_position), float(anchor_value)))
            except Exception:
                continue

        if not normalized_anchors:
            return 0.0

        normalized_anchors.sort(key=lambda item: item[0])
        if position <= normalized_anchors[0][0]:
            return normalized_anchors[0][1]

        for idx in range(1, len(normalized_anchors)):
            left_position, left_value = normalized_anchors[idx - 1]
            right_position, right_value = normalized_anchors[idx]
            if position <= right_position:
                if right_position <= left_position:
                    return right_value
                ratio = (position - left_position) / (right_position - left_position)
                return left_value + (right_value - left_value) * ratio

        return normalized_anchors[-1][1]

    @classmethod
    def _resolve_ass_dimensions(cls, video_width, video_height):
        try:
            width = int(video_width)
        except Exception:
            width = 0
        try:
            height = int(video_height)
        except Exception:
            height = 0

        if width <= 0:
            width = cls._ASS_PLAY_RES_X
        if height <= 0:
            height = cls._ASS_PLAY_RES_Y
        return width, height

    @staticmethod
    def _format_ass_number(value):
        try:
            value_f = float(value)
        except Exception:
            return str(value)
        if value_f.is_integer():
            return str(int(value_f))
        return f"{value_f:.2f}".rstrip('0').rstrip('.')

    @staticmethod
    def _seconds_to_ass_timestamp(seconds):
        try:
            total_cs = max(0, int(round(float(seconds) * 100)))
        except Exception:
            total_cs = 0
        hours = total_cs // 360000
        total_cs %= 360000
        minutes = total_cs // 6000
        total_cs %= 6000
        secs = total_cs // 100
        centis = total_cs % 100
        return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"

    @staticmethod
    def _escape_ass_text_line(text):
        return str(text or '').replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}')

    @staticmethod
    def _escape_ass_text(text):
        normalized = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
        escaped_lines = []
        for line in normalized.split('\n'):
            escaped_lines.append(TaskProcessor._escape_ass_text_line(line))
        return r'\N'.join(escaped_lines)

    @classmethod
    def _compose_ass_dialogue_text(cls, lines, override_font_size=None):
        escaped_lines = [
            cls._escape_ass_text_line(line)
            for line in (lines or [])
            if str(line or '').strip()
        ]
        if not escaped_lines:
            return ''
        payload = r'\N'.join(escaped_lines)
        if override_font_size is None:
            return payload
        try:
            override_font_size = int(round(float(override_font_size)))
        except Exception:
            override_font_size = 0
        if override_font_size <= 0:
            return payload
        return f"{{\\fs{override_font_size}}}{payload}"

    @classmethod
    def _build_streaming_ass_style(cls, video_width, video_height, style_overrides=None):
        width, height = cls._resolve_ass_dimensions(video_width, video_height)

        style = dict(cls._ASS_STYLE_BASE)
        style.update({
            'PlayResX': width,
            'PlayResY': height,
        })

        is_portrait = height > width
        if is_portrait:
            font_size = cls._clamp(
                cls._interpolate_anchor_value(height, cls._ASS_PORTRAIT_FONT_SIZE_ANCHORS),
                58.0,
                80.0,
            )
            margin_v = cls._clamp(
                cls._interpolate_anchor_value(height, cls._ASS_PORTRAIT_MARGIN_V_ANCHORS),
                150.0,
                320.0,
            )
            side_margin = cls._clamp(
                width * cls._ASS_PORTRAIT_SIDE_MARGIN_RATIO,
                cls._ASS_PORTRAIT_SIDE_MARGIN_MIN,
                cls._ASS_PORTRAIT_SIDE_MARGIN_MAX,
            )
        else:
            font_size = cls._clamp(
                cls._interpolate_anchor_value(height, cls._ASS_LANDSCAPE_FONT_SIZE_ANCHORS),
                46.0,
                132.0,
            )
            margin_v = cls._clamp(
                cls._interpolate_anchor_value(height, cls._ASS_LANDSCAPE_MARGIN_V_ANCHORS),
                46.0,
                156.0,
            )
            side_margin = cls._clamp(
                width * cls._ASS_LANDSCAPE_SIDE_MARGIN_RATIO,
                cls._ASS_LANDSCAPE_SIDE_MARGIN_MIN,
                cls._ASS_LANDSCAPE_SIDE_MARGIN_MAX,
            )

        style.update({
            'FontSize': font_size,
            'Outline': cls._clamp(
                font_size * cls._ASS_OUTLINE_RATIO,
                cls._ASS_OUTLINE_MIN,
                cls._ASS_OUTLINE_MAX,
            ),
            'Shadow': cls._clamp(
                font_size * cls._ASS_SHADOW_RATIO,
                cls._ASS_SHADOW_MIN,
                cls._ASS_SHADOW_MAX,
            ),
            'MarginV': margin_v,
            'MarginL': side_margin,
            'MarginR': side_margin,
        })
        if not style_overrides:
            # 未提供覆盖项时返回原样式，保证默认渲染行为与历史版本逐字段一致。
            return style
        return apply_style_overrides(style, style_overrides)

    @staticmethod
    def _sanitize_ass_font_name(font_family):
        return str(font_family or 'Arial').replace('\r', ' ').replace('\n', ' ').strip() or 'Arial'

    @classmethod
    def _resolve_streaming_srt_template_height(cls, video_width, video_height):
        _, height = cls._resolve_ass_dimensions(video_width, video_height)
        for template_height in cls._STREAMING_SRT_TEMPLATE_HEIGHTS:
            if height <= template_height:
                return template_height
        return cls._STREAMING_SRT_TEMPLATE_HEIGHTS[-1]

    @classmethod
    def _build_streaming_srt_style_description(cls, font_family, video_width, video_height):
        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        template_height = cls._resolve_streaming_srt_template_height(width, height)
        template = dict(cls._STREAMING_SRT_STYLE_TEMPLATES[template_height])
        is_portrait = height > width
        if is_portrait:
            template.update({
                'FontSize': cls._clamp(
                    cls._interpolate_anchor_value(height, cls._STREAMING_SRT_PORTRAIT_FONT_SIZE_ANCHORS),
                    12.5,
                    13.5,
                ),
                'MarginL': int(round(cls._clamp(width * cls._STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO, 40.0, 88.0))),
                'MarginR': int(round(cls._clamp(width * cls._STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO, 40.0, 88.0))),
                'MarginV': int(round(cls._clamp(
                    cls._interpolate_anchor_value(height, cls._STREAMING_SRT_PORTRAIT_MARGIN_V_ANCHORS),
                    16.0,
                    44.0,
                ))),
            })
        template.update({
            'FontName': cls._sanitize_ass_font_name(font_family),
            'Alignment': 2,
            'BorderStyle': 1,
            'OriginalSize': f"{width}x{height}",
            'TemplateHeight': template_height,
        })
        return template

    @classmethod
    def _build_streaming_srt_force_style(cls, font_family, video_width, video_height):
        style = cls._build_streaming_srt_style_description(font_family, video_width, video_height)
        entries = [
            f"FontName={style['FontName']}",
            f"FontSize={cls._format_ass_number(style['FontSize'])}",
            f"Outline={cls._format_ass_number(style['Outline'])}",
            f"Shadow={cls._format_ass_number(style['Shadow'])}",
            f"MarginL={int(style['MarginL'])}",
            f"MarginR={int(style['MarginR'])}",
            f"MarginV={int(style['MarginV'])}",
            f"Alignment={style['Alignment']}",
            f"BorderStyle={style['BorderStyle']}",
        ]
        payload = ','.join(entries).replace("'", r"\'")
        return f"force_style='{payload}'"

    @classmethod
    def _build_streaming_srt_filter(cls, render_subtitle_name, font_family, video_width, video_height):
        style = cls._build_streaming_srt_style_description(font_family, video_width, video_height)
        filter_segments = [
            f"subtitles={render_subtitle_name}",
            f"original_size={style['OriginalSize']}",
            "wrap_unicode=1",
            "fontsdir=fonts",
            "charenc=UTF-8",
            cls._build_streaming_srt_force_style(font_family, video_width, video_height),
        ]
        return ':'.join(filter_segments)

    @classmethod
    def _estimate_streaming_srt_layout_limits(cls, video_width, video_height):
        style = cls._build_streaming_srt_style_description('Arial', video_width, video_height)
        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        is_portrait = height > width
        usable_width = max(
            120.0,
            float(width) - float(style['MarginL']) - float(style['MarginR']),
        )
        font_size = max(1.0, float(style['FontSize']))
        density = (
            cls._STREAMING_SRT_PORTRAIT_LAYOUT_DENSITY
            if is_portrait
            else cls._STREAMING_SRT_LANDSCAPE_LAYOUT_DENSITY
        )
        max_line_length = int(round(usable_width / font_size * density))
        if is_portrait:
            max_line_length = int(cls._clamp(max_line_length, 12.0, 18.0))
            max_lines = 3
        else:
            # Hard-coded single-line target for streaming SRT output.
            max_line_length = int(cls._clamp(max_line_length, 20.0, 26.0))
            max_lines = 1
        return max_line_length, max_lines

    @classmethod
    def _create_streaming_srt_engine(cls, video_width=None, video_height=None):
        from .srt_transform_engine import SrtTransformConfig, SrtTransformEngine
        max_line_length, max_lines = cls._estimate_streaming_srt_layout_limits(
            video_width,
            video_height,
        )

        return SrtTransformEngine(
            SrtTransformConfig(
                max_line_length=max_line_length,
                max_lines=max_lines,
                split_long_cues=False,
                preserve_line_breaks=False,
                normalize_punctuation=False,
                filter_filler_words=False,
            ),
            logger=logger,
        )

    @classmethod
    def _wrap_streaming_srt_text(cls, text, video_width, video_height):
        normalized = cls._merge_subtitle_text_parts(
            str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
        )
        if not normalized:
            return ''

        width, height = cls._resolve_ass_dimensions(video_width, video_height)
        is_portrait = height > width
        style = cls._build_streaming_srt_style_description('Arial', width, height)
        max_line_length, max_lines = cls._estimate_streaming_srt_layout_limits(width, height)
        usable_width = max(
            120.0,
            float(width) - float(style['MarginL']) - float(style['MarginR']),
        )
        font_size = max(1.0, float(style['FontSize']))
        outline = float(style['Outline'])
        shadow = float(style['Shadow'])
        single_line_limit = int(cls._clamp(
            round(usable_width / font_size * cls._ASS_LANDSCAPE_SINGLE_LINE_DENSITY),
            18.0,
            28.0,
        ))
        wrapped_lines = cls._build_wrapped_lines_for_ass(
            normalized,
            is_portrait=is_portrait,
            max_line_length=max_line_length,
            max_lines=max_lines,
            single_line_limit=single_line_limit,
            aggressive=False,
        )
        fits, _, _, _ = cls._check_ass_lines_width_safety(
            wrapped_lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if not fits:
            hard_wrap_lines, _ = cls._find_safe_hard_wrap_lines(
                normalized,
                max_line_length=max_line_length,
                max_lines=max_lines,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
            )
            if hard_wrap_lines:
                wrapped_lines = hard_wrap_lines
        return '\n'.join(line for line in wrapped_lines if line).strip() or normalized

    @classmethod
    def _prepare_streaming_srt_cues(cls, subtitle_text, video_width=None, video_height=None):
        engine = cls._create_streaming_srt_engine(video_width, video_height)
        cues = engine.parse_srt(subtitle_text or '')
        if not cues:
            return []
        total_duration = max(float(cue.get('end', 0.0) or 0.0) for cue in cues)
        # Rendering should preserve validated subtitle content. Hallucination cleanup
        # is part of the ASR generation pipeline and is too destructive here,
        # especially for dense translated Chinese cues that are still legitimate.
        cues = engine.resolve_overlaps(cues, total_duration)
        cues = engine.apply_text_processing(cues)
        cues = engine.finalize_cues(cues, total_duration)
        for cue in cues:
            cue['text'] = cls._wrap_streaming_srt_text(
                cue.get('text', ''),
                video_width,
                video_height,
            )
        return cues

    def _build_streaming_srt_file(
        self,
        subtitle_path,
        srt_output_path,
        task_logger,
        *,
        video_width=None,
        video_height=None,
    ):
        try:
            source_path = subtitle_path
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()

            if subtitle_ext == '.vtt':
                converted_srt = self._convert_vtt_to_srt(subtitle_path, task_logger)
                if not converted_srt or not os.path.exists(converted_srt):
                    task_logger.error("VTT转SRT失败，无法生成流媒体SRT字幕")
                    return False
                source_path = converted_srt

            with open(source_path, 'r', encoding='utf-8-sig', errors='replace') as subtitle_file:
                subtitle_content = subtitle_file.read()

            cues = self._prepare_streaming_srt_cues(
                subtitle_content,
                video_width=video_width,
                video_height=video_height,
            )
            if not cues:
                task_logger.error(f"未解析出有效字幕条目，无法生成SRT: {os.path.basename(subtitle_path)}")
                return False

            srt_text = self._create_streaming_srt_engine(
                video_width,
                video_height,
            ).render_srt(cues)
            if not srt_text:
                task_logger.error(f"字幕渲染为SRT失败: {os.path.basename(subtitle_path)}")
                return False

            with open(srt_output_path, 'w', encoding='utf-8') as srt_file:
                srt_file.write(srt_text)

            task_logger.info(f"字幕转换为流媒体SRT成功: {srt_output_path}")
            return True
        except Exception as e:
            task_logger.error(f"生成流媒体SRT失败: {str(e)}")
            return False

    @classmethod
    def _build_subtitle_style_description(cls, font_family, video_width, video_height, style_overrides=None):
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        font_name = cls._sanitize_ass_font_name(font_family)
        force_style = {
            'FontName': font_name,
            'FontSize': cls._format_ass_number(style['FontSize']),
            'Outline': cls._format_ass_number(style['Outline']),
            'Shadow': cls._format_ass_number(style['Shadow']),
            'MarginL': str(int(round(style['MarginL']))),
            'MarginR': str(int(round(style['MarginR']))),
            'MarginV': str(int(round(style['MarginV']))),
            'Alignment': str(style['Alignment']),
        }
        # 颜色 / 粗体 / 底板必须一并写进 force_style：libass 的 force_style 是
        # **逐键覆盖**，只给字体与边距的话，ASS/SSA 源字幕的字色、描边色、粗体与
        # 半透明底板仍旧来自创作者样式 —— 用户改了「字体颜色」画面上毫无变化，
        # 而日志照样打印「将以 force_style 覆盖源样式」（实测：同一份配置喂 .srt
        # 是红字、喂 .ass 仍是白字）。取值已由 ``apply_style_overrides`` 算好，
        # 这里只做搬运，因此默认配置下写入的仍是历史默认值。
        for key in cls._FORCE_STYLE_APPEARANCE_KEYS:
            if key in style:
                force_style[key] = cls._format_ass_number(style[key])
        return style, force_style

    @classmethod
    def _build_subtitle_force_style(cls, font_family, video_width, video_height, style_overrides=None):
        _, force_style = cls._build_subtitle_style_description(
            font_family,
            video_width,
            video_height,
            style_overrides,
        )
        entries = [f"{key}={value}" for key, value in force_style.items()]
        payload = ','.join(entries).replace("'", r"\'")
        return f"force_style='{payload}'"

    @classmethod
    def _estimate_subtitle_layout_limits(cls, video_width, video_height, style_overrides=None):
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        is_portrait = float(style['PlayResY']) > float(style['PlayResX'])
        usable_width = max(
            120.0,
            float(style['PlayResX']) - float(style['MarginL']) - float(style['MarginR']),
        )
        font_size = max(1.0, float(style['FontSize']))
        density = cls._ASS_PORTRAIT_LAYOUT_DENSITY if is_portrait else cls._ASS_LANDSCAPE_LAYOUT_DENSITY
        max_line_length = int(round(usable_width / font_size * density))
        if is_portrait:
            # Portrait lines are capped at 14 visual units so that 5 balanced
            # lines can absorb medium-length cues without immediately
            # triggering overflow warnings, while still keeping the text narrow.
            max_line_length = int(cls._clamp(max_line_length, 7.0, 14.0))
            # Keep portrait subtitles compact: 5 lines is the hard ceiling,
            # but the partitioner still prefers fewer balanced lines.
            max_lines = 5
        else:
            # Hard-coded to 1 line for landscape captions. The single-line
            # priority logic will scale the font down when needed; only if the
            # text still does not fit will an overflow warning be emitted.
            max_line_length = int(cls._clamp(max_line_length, 18.0, 22.0))
            max_lines = 1
        return max_line_length, max_lines

    @staticmethod
    def _is_cjk_like_char(char):
        if not char:
            return False
        return unicodedata.east_asian_width(char) in {'W', 'F'}

    @classmethod
    def _merge_subtitle_text_parts(cls, parts):
        merged = ''
        trailing_no_space = '([{\u3008\u300a\u300c\u300e\u3010'
        leading_no_space = '.,!?;:)]}，。！？；：、…】）》」』'

        for part in parts or []:
            piece = str(part or '').strip()
            if not piece:
                continue
            if not merged:
                merged = piece
                continue

            prev_char = merged[-1]
            curr_char = piece[0]
            if (
                prev_char.isspace()
                or curr_char.isspace()
                or prev_char in trailing_no_space
                or curr_char in leading_no_space
                or (cls._is_cjk_like_char(prev_char) and cls._is_cjk_like_char(curr_char))
            ):
                merged += piece
            else:
                merged += f" {piece}"

        return merged.strip()

    @classmethod
    def _limit_wrapped_lines(cls, lines, max_lines):
        filtered_lines = [str(line or '').strip() for line in (lines or []) if str(line or '').strip()]
        if len(filtered_lines) <= max_lines:
            return filtered_lines

        if max_lines >= 4:
            visible_lines = filtered_lines[:max_lines - 1]
            remainder = cls._merge_subtitle_text_parts(filtered_lines[max_lines - 1:])
            if remainder:
                visible_lines.append(remainder)
            return [line for line in visible_lines if line]

        merged_text = cls._merge_subtitle_text_parts(filtered_lines)
        if not merged_text or max_lines <= 1:
            return filtered_lines[:max_lines]

        rebalanced_lines = []
        remaining_text = merged_text
        remaining_slots = int(max_lines)

        while remaining_slots > 1 and remaining_text:
            remaining_units = cls._estimate_subtitle_text_units(remaining_text)
            target_line_length = max(6.0, remaining_units / remaining_slots)
            split_index = cls._find_balanced_wrap_index(remaining_text, target_line_length)
            if split_index <= 0:
                fallback_lines = cls._wrap_subtitle_segment_greedily(remaining_text, target_line_length)
                if len(fallback_lines) <= 1:
                    break
                current_line = str(fallback_lines[0] or '').strip()
                next_text = cls._merge_subtitle_text_parts(fallback_lines[1:])
            else:
                current_line = remaining_text[:split_index].strip()
                next_text = remaining_text[split_index:].strip()

            if not current_line or not next_text:
                break

            rebalanced_lines.append(current_line)
            remaining_text = next_text
            remaining_slots -= 1

        if remaining_text:
            rebalanced_lines.append(remaining_text.strip())
        return [line for line in rebalanced_lines[:max_lines] if line]

    @classmethod
    def _build_wrapped_lines_for_ass(
        cls,
        normalized,
        *,
        is_portrait,
        max_line_length,
        max_lines,
        single_line_limit,
        aggressive=False,
    ):
        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        wrapped_lines = []

        for segment in raw_segments:
            if is_portrait or aggressive:
                max_lines = max(1, int(max_lines))
                candidate_lines = cls._build_optimal_multiline_partition(
                    segment,
                    max_line_length=max_line_length,
                    min_lines=1,
                    max_lines=max_lines,
                )
                if candidate_lines:
                    wrapped_lines.extend(candidate_lines)
                else:
                    wrapped_lines.extend(cls._wrap_subtitle_segment_greedily(segment, max_line_length))
            elif int(max_lines) <= 1:
                # Hard-coded single-line mode: keep the segment intact so the
                # caller can scale the font or emit an overflow warning instead
                # of wrapping.
                wrapped_lines.append(segment)
            else:
                wrapped_lines.extend(
                    cls._wrap_landscape_segment_for_ass(
                        segment,
                        single_line_limit=single_line_limit,
                        max_line_length=max_line_length,
                    )
                )

        return cls._limit_wrapped_lines(wrapped_lines, max_lines)

    @classmethod
    def _estimate_ass_line_render_width(cls, line, font_size, outline, shadow):
        text_units = cls._estimate_subtitle_text_units(str(line or ''))
        if text_units <= 0:
            return 0.0
        # Padding accounts for the rounded box (BorderStyle=4) or outline,
        # the shadow offset and a small safety margin. The coefficients were
        # calibrated against Source Han Sans HW SC rendered at 1080p.
        padding = max(
            8.0,
            float(outline) * 2.8 + float(shadow) * 1.8 + float(font_size) * 0.14,
        )
        return text_units * float(font_size) + padding

    @classmethod
    def _check_ass_lines_width_safety(cls, lines, usable_width, font_size, outline, shadow):
        safe_width = max(1.0, float(usable_width) * cls._ASS_SAFE_WIDTH_RATIO)
        line_widths = [
            cls._estimate_ass_line_render_width(line, font_size, outline, shadow)
            for line in (lines or [])
            if str(line or '').strip()
        ]
        if not line_widths:
            return True, 0.0, safe_width, []
        max_width = max(line_widths)
        return max_width <= safe_width, max_width, safe_width, line_widths

    @classmethod
    def _estimate_safe_line_units(cls, usable_width, font_size, outline, shadow):
        safe_width = max(1.0, float(usable_width) * cls._ASS_SAFE_WIDTH_RATIO)
        padding = max(6.0, float(outline) * 4.0 + float(shadow) * 2.0 + float(font_size) * 0.08)
        return max(
            float(cls._ASS_HARD_WRAP_MIN_LINE_LENGTH),
            (safe_width - padding) / max(1.0, float(font_size)),
        )

    @classmethod
    def _can_lines_fit_with_font_override(cls, lines, usable_width, font_size, outline, shadow):
        fits, _, _, _ = cls._check_ass_lines_width_safety(
            lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if fits:
            return True

        _, override_fits = cls._resolve_safe_override_font_size(
            lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        return override_fits

    @classmethod
    def _score_partition_line(cls, line, target_units, max_line_length, *, is_last):
        stripped = str(line or '').strip()
        if not stripped:
            return float('inf')

        units = cls._estimate_subtitle_text_units(stripped)
        # Soft length constraint with a generous tolerance: hard-reject only
        # when a line is extremely long, otherwise apply a strong quadratic
        # penalty.  This keeps the DP out of the greedy fallback for portrait
        # cues where perfect boundary-aligned partitions would otherwise be
        # impossible within a tight budget.
        hard_limit = max(float(max_line_length) * 1.5, float(max_line_length) + 8.0)
        if units > hard_limit:
            return float('inf')

        score = (abs(units - float(target_units)) ** 2) * 1.3
        if units > float(max_line_length):
            score += ((units - float(max_line_length)) ** 2) * 36.0

        minimum_units = max(4.5, float(target_units) * (0.60 if is_last else 0.72))
        if units < minimum_units:
            score += (minimum_units - units) ** 2 * (8.0 if is_last else 16.0)

        if stripped[0] in '.,!?;:，。！？；：、)]}】）》」』':
            score += 25.0
        if stripped[-1] in '([{【（《“‘':
            score += 25.0
        if not is_last and cls._is_short_orphan_tail(stripped):
            score += 10.0
        return score

    @classmethod
    def _build_optimal_multiline_partition(
        cls,
        normalized,
        *,
        max_line_length,
        min_lines,
        max_lines,
    ):
        from functools import lru_cache

        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        merged_text = cls._merge_subtitle_text_parts(raw_segments)
        if not merged_text:
            return []

        minimum_lines = max(1, int(min_lines))
        maximum_lines = max(minimum_lines, int(max_lines))

        preferred_points = cls._collect_candidate_wrap_indices(merged_text, include_fallback=False)
        all_points = cls._collect_candidate_wrap_indices(merged_text, include_fallback=True)

        total_units = cls._estimate_subtitle_text_units(merged_text)
        best_score = None
        best_lines = []

        for point_source in (preferred_points, all_points):
            candidate_points = point_source
            if not candidate_points:
                continue

            points = [0] + sorted(set(idx for idx in candidate_points if 0 < idx < len(merged_text))) + [len(merged_text)]
            if len(points) < minimum_lines + 1:
                continue

            if len(points) < 2:
                return [merged_text]

            for line_count in range(minimum_lines, maximum_lines + 1):
                target_units = max(6.0, total_units / max(1, line_count))

                @lru_cache(maxsize=None)
                def solve(start_idx, lines_left):
                    remaining_text = merged_text[points[start_idx]:].strip()
                    if not remaining_text:
                        return float('inf'), tuple()

                    if lines_left == 1:
                        line_score = cls._score_partition_line(
                            remaining_text,
                            target_units,
                            max_line_length,
                            is_last=True,
                        )
                        if line_score == float('inf'):
                            return float('inf'), tuple()
                        return line_score, (remaining_text,)

                    best_local = float('inf'), tuple()
                    max_next_index = len(points) - lines_left
                    for next_idx in range(start_idx + 1, max_next_index + 1):
                        line = merged_text[points[start_idx]:points[next_idx]].strip()
                        if not line:
                            continue

                        current_score = cls._score_partition_line(
                            line,
                            target_units,
                            max_line_length,
                            is_last=False,
                        )
                        if current_score == float('inf'):
                            continue

                        tail_score, tail_lines = solve(next_idx, lines_left - 1)
                        total_score = current_score + tail_score
                        if total_score < best_local[0]:
                            best_local = total_score, (line,) + tail_lines

                    return best_local

                score, lines = solve(0, line_count)
                if not lines:
                    continue

                score += max(0, line_count - minimum_lines) * 3.0
                if best_score is None or score < best_score:
                    best_score = score
                    best_lines = list(lines)

            if best_lines:
                break

        return best_lines

    @classmethod
    def _find_portrait_rescue_lines(
        cls,
        normalized,
        *,
        max_line_length,
        usable_width,
        font_size,
        outline,
        shadow,
    ):
        rescue_font_size = max(
            float(cls._ASS_OVERRIDE_FONT_SIZE_MIN),
            float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN),
        )
        rescue_outline = cls._clamp(
            rescue_font_size * cls._ASS_OVERRIDE_OUTLINE_RATIO,
            cls._ASS_OVERRIDE_OUTLINE_MIN,
            cls._ASS_OVERRIDE_OUTLINE_MAX,
        )
        rescue_shadow = cls._clamp(rescue_font_size * 0.016, 0.7, 1.35)
        rescue_line_length = int(round(
            cls._estimate_safe_line_units(
                usable_width,
                rescue_font_size,
                rescue_outline,
                rescue_shadow,
            )
        ))
        rescue_line_length = int(cls._clamp(
            rescue_line_length,
            max_line_length + 1,
            cls._ASS_PORTRAIT_RESCUE_LINE_LENGTH_MAX,
        ))

        best_lines = []
        for line_count in (4, 5):
            candidate_lines = cls._build_optimal_multiline_partition(
                normalized,
                max_line_length=rescue_line_length,
                min_lines=line_count,
                max_lines=line_count,
            )
            if not candidate_lines:
                continue

            if cls._can_lines_fit_with_font_override(
                candidate_lines,
                usable_width,
                font_size,
                outline,
                shadow,
            ):
                return candidate_lines, True

            if not best_lines:
                best_lines = candidate_lines

        return best_lines, False

    @classmethod
    def _find_landscape_rescue_lines(
        cls,
        normalized,
        *,
        max_line_length,
        usable_width,
        font_size,
        outline,
        shadow,
    ):
        rescue_line_length = int(round(
            cls._estimate_safe_line_units(
                usable_width,
                font_size,
                outline,
                shadow,
            )
        ))
        rescue_line_length = int(cls._clamp(
            rescue_line_length,
            max_line_length,
            cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX,
        ))

        best_lines = []
        for line_count in (3, 4):
            candidate_lines = cls._build_optimal_multiline_partition(
                normalized,
                max_line_length=rescue_line_length,
                min_lines=line_count,
                max_lines=line_count,
            )
            if not candidate_lines:
                continue

            if cls._can_lines_fit_with_font_override(
                candidate_lines,
                usable_width,
                font_size,
                outline,
                shadow,
            ):
                return candidate_lines, True

            if not best_lines:
                best_lines = candidate_lines

        return best_lines, False

    @classmethod
    def _find_safe_hard_wrap_lines(
        cls,
        normalized,
        *,
        max_line_length,
        max_lines,
        usable_width,
        font_size,
        outline,
        shadow,
    ):
        best_lines = []
        best_width = None

        for line_limit in range(
            int(max_line_length),
            int(cls._ASS_HARD_WRAP_MIN_LINE_LENGTH) - 1,
            -1,
        ):
            candidate_lines = cls._build_aggressive_two_line_candidate(
                normalized,
                max_line_length=line_limit,
                max_lines=max_lines,
            )
            fits, max_width, _, _ = cls._check_ass_lines_width_safety(
                candidate_lines,
                usable_width,
                font_size,
                outline,
                shadow,
            )
            if best_width is None or max_width < best_width:
                best_lines = candidate_lines
                best_width = max_width
            if fits:
                return candidate_lines, True

        return best_lines, False

    @classmethod
    def _build_aggressive_two_line_candidate(cls, normalized, *, max_line_length, max_lines):
        raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
        merged_text = cls._merge_subtitle_text_parts(raw_segments)
        if not merged_text:
            return []
        if max_lines <= 1 or cls._estimate_subtitle_text_units(merged_text) <= max_line_length:
            return [merged_text]

        if max_lines > 2:
            return cls._limit_wrapped_lines(
                cls._wrap_subtitle_segment_greedily(merged_text, max_line_length),
                max_lines,
            )

        split_index = cls._find_balanced_wrap_index(merged_text, max_line_length)
        if split_index > 0:
            return cls._limit_wrapped_lines(
                [merged_text[:split_index].strip(), merged_text[split_index:].strip()],
                max_lines,
            )

        return cls._limit_wrapped_lines(
            cls._wrap_subtitle_segment_greedily(merged_text, max_line_length),
            max_lines,
        )

    @classmethod
    def _resolve_safe_override_font_size(cls, lines, usable_width, font_size, outline, shadow):
        fits, max_width, safe_width, _ = cls._check_ass_lines_width_safety(
            lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if fits or max_width <= 0:
            return None, True

        min_font_size = max(
            float(cls._ASS_OVERRIDE_FONT_SIZE_MIN),
            float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN),
        )
        target_font_size = max(
            min_font_size,
            float(font_size) * (safe_width / max_width),
        )
        target_font_size = min(float(font_size), target_font_size)
        target_font_size = float(int(max(1, round(target_font_size))))
        if target_font_size >= float(font_size):
            return None, False

        adjusted_outline = cls._clamp(
            target_font_size * cls._ASS_OVERRIDE_OUTLINE_RATIO,
            cls._ASS_OVERRIDE_OUTLINE_MIN,
            cls._ASS_OVERRIDE_OUTLINE_MAX,
        )
        adjusted_shadow = cls._clamp(target_font_size * 0.016, 0.7, 1.35)
        adjusted_fits, _, _, _ = cls._check_ass_lines_width_safety(
            lines,
            usable_width,
            target_font_size,
            adjusted_outline,
            adjusted_shadow,
        )
        return target_font_size, adjusted_fits

    @classmethod
    def _resolve_single_line_scaled_font(
        cls,
        text,
        usable_width,
        font_size,
        outline,
        shadow,
        *,
        min_scale,
    ):
        """Find the largest font size >= min_scale*font_size that keeps *text* on one line.

        Uses binary search so the scale reduction is as small as possible,
        keeping the subtitle single-line without an aggressive visual shrink.

        Returns (scaled_font_size, scaled_outline, scaled_shadow) if a fit is found,
        otherwise (None, None, None).
        """
        single_line = [str(text or '').strip()]
        if not single_line[0]:
            return None, None, None

        min_allowed_font = max(
            float(cls._ASS_OVERRIDE_FONT_SIZE_MIN),
            float(font_size) * float(cls._ASS_OVERRIDE_FONT_SIZE_RATIO_MIN),
            float(font_size) * float(min_scale),
        )
        if min_allowed_font >= float(font_size):
            return None, None, None

        def _fits_at_size(size):
            test_outline = cls._clamp(
                size * cls._ASS_OUTLINE_RATIO,
                cls._ASS_OUTLINE_MIN,
                cls._ASS_OUTLINE_MAX,
            )
            test_shadow = cls._clamp(
                size * cls._ASS_SHADOW_RATIO,
                cls._ASS_SHADOW_MIN,
                cls._ASS_SHADOW_MAX,
            )
            fits, _, _, _ = cls._check_ass_lines_width_safety(
                single_line,
                usable_width,
                size,
                test_outline,
                test_shadow,
            )
            return fits

        lo = min_allowed_font
        hi = float(font_size)
        if _fits_at_size(hi):
            return None, None, None
        if not _fits_at_size(lo):
            return None, None, None

        best_size = lo
        for _ in range(12):
            mid = (lo + hi) / 2.0
            if _fits_at_size(mid):
                best_size = mid
                lo = mid
            else:
                hi = mid

        best_size = float(int(round(best_size)))
        best_size = min(best_size, font_size - 1.0)
        if best_size < min_allowed_font:
            return None, None, None

        best_outline = cls._clamp(
            best_size * cls._ASS_OUTLINE_RATIO,
            cls._ASS_OUTLINE_MIN,
            cls._ASS_OUTLINE_MAX,
        )
        best_shadow = cls._clamp(
            best_size * cls._ASS_SHADOW_RATIO,
            cls._ASS_SHADOW_MIN,
            cls._ASS_SHADOW_MAX,
        )
        return best_size, best_outline, best_shadow

    @staticmethod
    def _is_preferred_wrap_boundary(char):
        if not char:
            return False
        if char.isspace():
            return True
        return char in '.,!?;:，。！？；：、)]}】）》」』'

    @staticmethod
    def _is_latin_word_char(char):
        if not char:
            return False
        return char.isascii() and (char.isalnum() or char in "'#&+_./-")

    @classmethod
    def _is_disallowed_wrap_pair(cls, left_char, right_char):
        if not left_char or not right_char:
            return False
        if cls._is_latin_word_char(left_char) and cls._is_latin_word_char(right_char):
            return True
        if left_char in '([{【（《“‘' or right_char in ')]}】）》」』”’':
            return True
        if left_char.isdigit() and right_char.isascii() and right_char.isalpha():
            return True
        if cls._is_latin_word_char(left_char) and right_char == '%':
            return True
        return False

    @classmethod
    def _is_cjk_cjk_split(cls, text, split_index):
        """Return True if *split_index* falls between two CJK-like characters.

        Splitting mid-CJK-run is generally undesirable because most CJK
        words are multi-character compounds.  The caller should penalise
        these positions so the wrapper prefers punctuation, spaces and
        script boundaries instead.
        """
        segment = str(text or '')
        if split_index <= 0 or split_index >= len(segment):
            return False
        return (
            cls._is_cjk_like_char(segment[split_index - 1])
            and cls._is_cjk_like_char(segment[split_index])
        )

    @classmethod
    def _should_keep_ascii_phrase_together(cls, text, split_index):
        segment = str(text or '')
        if split_index <= 0 or split_index >= len(segment):
            return False
        if not segment[split_index - 1].isspace():
            return False

        left_end = split_index - 1
        left_start = left_end - 1
        while left_start >= 0 and cls._is_latin_word_char(segment[left_start]):
            left_start -= 1
        left_token = segment[left_start + 1:left_end].strip()

        right_end = split_index
        while right_end < len(segment) and cls._is_latin_word_char(segment[right_end]):
            right_end += 1
        right_token = segment[split_index:right_end].strip()

        if not left_token or not right_token:
            return False

        combined_length = len(left_token) + len(right_token) + 1
        if combined_length <= 15:
            return True
        if left_token.isupper() and len(left_token) <= 4:
            return True
        return False

    @classmethod
    def _collect_candidate_wrap_indices(cls, segment, include_fallback=True):
        preferred = []
        fallback = []
        for idx in range(1, len(segment)):
            left_char = segment[idx - 1]
            right_char = segment[idx]
            if cls._is_disallowed_wrap_pair(left_char, right_char):
                continue
            if left_char.isspace() and cls._should_keep_ascii_phrase_together(segment, idx):
                continue
            right_text = segment[idx:].strip()
            if cls._is_preferred_wrap_boundary(left_char) or cls._semantic_wrap_bonus(right_text) > 0.0:
                preferred.append(idx)
            else:
                fallback.append(idx)
        if not include_fallback:
            return preferred
        return preferred + fallback

    @classmethod
    def _find_wrap_boundary(cls, chars):
        joined_chars = ''.join(chars)
        for idx in range(len(chars) - 1, -1, -1):
            right_char = chars[idx + 1] if idx + 1 < len(chars) else ''
            if chars[idx].isspace() and cls._should_keep_ascii_phrase_together(joined_chars, idx + 1):
                continue
            if cls._is_preferred_wrap_boundary(chars[idx]) and not cls._is_disallowed_wrap_pair(chars[idx], right_char):
                return idx
        return -1

    @staticmethod
    def _estimate_subtitle_char_units(char):
        if not char:
            return 0.0
        if char.isspace():
            return 0.35
        if unicodedata.east_asian_width(char) in {'W', 'F'}:
            if unicodedata.category(char).startswith('P'):
                return 0.7
            return 1.0
        if char.isascii():
            if char.isalnum():
                return 0.6
            return 0.45
        if unicodedata.category(char).startswith('P'):
            return 0.6
        return 0.8

    @classmethod
    def _estimate_subtitle_text_units(cls, text):
        return sum(cls._estimate_subtitle_char_units(char) for char in str(text or ''))

    @staticmethod
    def _is_punctuation_only_text(text):
        stripped = str(text or '').strip()
        if not stripped:
            return False
        return all(
            char.isspace() or unicodedata.category(char).startswith('P')
            for char in stripped
        )

    @classmethod
    def _is_short_orphan_tail(cls, text):
        stripped = str(text or '').strip()
        if not stripped:
            return False
        if cls._is_punctuation_only_text(stripped):
            return True

        tail_core = stripped.rstrip('.,!?;:，。！？；：、… ')
        if not tail_core:
            return True

        tail_units = cls._estimate_subtitle_text_units(tail_core)
        if tail_units <= 3.0:
            return True

        return tail_core in {'吗', '呢', '啊', '吧', '呀', '了', '嘛', '么', '呗', '哇', '哦', '喔'}

    @classmethod
    def _semantic_wrap_bonus(cls, text):
        stripped = str(text or '').strip()
        if not stripped:
            return 0.0

        strong_prefixes = (
            '而不是', '而非', '并不是', '不是', '但是', '不过', '然而', '因此', '所以',
            '因为', '如果', '虽然', '并且', '或者', '还是', '以及', '然后', '兼顾',
            '保持', '避免', '确保', '否则',
            'rather than', 'instead of', 'because', 'however', 'therefore', 'although',
        )
        weak_prefixes = (
            '而', '但', '却', '并', '或',
            'and', 'but', 'or', 'if', 'when', 'while', 'that', 'which',
        )

        lowered = stripped.lower()
        if lowered.startswith(strong_prefixes):
            return 18.0
        if lowered.startswith(weak_prefixes):
            return 8.0
        return 0.0

    @classmethod
    def _is_broken_compound_wrap(cls, left_text, right_text):
        left = str(left_text or '').strip().lower()
        right = str(right_text or '').strip().lower()
        if not left or not right:
            return False

        broken_pairs = (
            ('而', '不是'),
            ('并', '不是'),
            ('rather', 'than'),
            ('instead', 'of'),
        )
        return any(left.endswith(prefix) and right.startswith(suffix) for prefix, suffix in broken_pairs)

    @classmethod
    def _wrap_subtitle_segment_greedily(cls, segment, max_line_length):
        wrapped_lines = []
        chars = []
        current_units = 0.0
        idx = 0

        while idx < len(segment):
            char = segment[idx]
            char_units = cls._estimate_subtitle_char_units(char)
            if chars and current_units + char_units > max_line_length:
                break_at = cls._find_wrap_boundary(chars)
                if break_at >= 0:
                    line_chars = chars[:break_at + 1]
                    remainder = ''.join(chars[break_at + 1:]).strip()
                else:
                    tail_start = len(chars)
                    while tail_start > 0 and cls._is_latin_word_char(chars[tail_start - 1]):
                        tail_start -= 1
                    if 0 < tail_start < len(chars):
                        line_chars = chars[:tail_start]
                        remainder = ''.join(chars[tail_start:]).strip()
                    elif tail_start == 0 and all(cls._is_latin_word_char(c) for c in chars):
                        # The accumulated buffer is one unbreakable Latin token that
                        # already exceeds the line length. Keep it intact so the
                        # downstream overflow guard can scale the font or wrap via
                        # rescue logic, rather than splitting the word mid-character.
                        line_chars = chars
                        remainder = ''
                    else:
                        line_chars = chars
                        remainder = ''

                line = ''.join(line_chars).strip()
                if line:
                    wrapped_lines.append(line)
                chars = list(remainder)
                current_units = cls._estimate_subtitle_text_units(remainder)
                continue

            chars.append(char)
            current_units += char_units
            idx += 1

        line = ''.join(chars).strip()
        if line:
            wrapped_lines.append(line)
        return wrapped_lines

    @classmethod
    def _find_balanced_wrap_index(cls, segment, max_line_length):
        best_index = -1
        best_score = None

        candidate_indices = cls._collect_candidate_wrap_indices(segment)
        if not candidate_indices:
            return -1

        for idx in candidate_indices:
            left = segment[:idx].strip()
            right = segment[idx:].strip()
            if not left or not right:
                continue

            left_units = cls._estimate_subtitle_text_units(left)
            right_units = cls._estimate_subtitle_text_units(right)
            boundary_char = segment[idx - 1]
            overflow = max(0.0, left_units - max_line_length) + max(0.0, right_units - max_line_length)
            score = abs(left_units - right_units) + overflow * 8.0

            if not cls._is_preferred_wrap_boundary(boundary_char):
                score += 4.5
            elif boundary_char.isspace():
                score -= 1.5
            else:
                score -= 4.0
            if cls._is_short_orphan_tail(right):
                score += 15.0
            if left_units <= 3.0:
                score += 8.0
            # Penalise severely unbalanced splits (ratio > 3:1)
            shorter = min(left_units, right_units)
            longer = max(left_units, right_units)
            if shorter > 0 and longer / shorter > 3.0:
                score += 6.0
            score -= cls._semantic_wrap_bonus(right)
            if cls._is_broken_compound_wrap(left, right):
                score += 12.0
            if cls._is_cjk_cjk_split(segment, idx):
                score += 4.0

            if best_score is None or score < best_score:
                best_score = score
                best_index = idx

        return best_index

    # Tolerance (in visual units) above single_line_limit where we still
    # prefer keeping the text on one line rather than splitting.
    _SINGLE_LINE_TOLERANCE = 3.5

    @classmethod
    def _wrap_landscape_segment_for_ass(cls, segment, single_line_limit, max_line_length):
        total_units = cls._estimate_subtitle_text_units(segment)
        if total_units <= single_line_limit:
            return [segment]

        tolerance = cls._SINGLE_LINE_TOLERANCE

        split_index = cls._find_balanced_wrap_index(segment, max_line_length)
        if split_index <= 0:
            # No viable balanced split – keep single if within tolerance
            if total_units <= single_line_limit + tolerance:
                return [segment]
            return cls._wrap_subtitle_segment_greedily(segment, max_line_length)

        first_line = segment[:split_index].strip()
        second_line = segment[split_index:].strip()
        if not first_line or not second_line:
            if total_units <= single_line_limit + tolerance:
                return [segment]
            return cls._wrap_subtitle_segment_greedily(segment, max_line_length)

        # Orphan tail: second line is too short to justify a split
        if cls._is_short_orphan_tail(second_line) and total_units <= single_line_limit + tolerance:
            return [segment]

        first_units = cls._estimate_subtitle_text_units(first_line)
        second_units = cls._estimate_subtitle_text_units(second_line)

        # Severely unbalanced split (ratio > 3:1): prefer single line if within tolerance
        shorter = min(first_units, second_units)
        longer = max(first_units, second_units)
        if shorter > 0 and longer / shorter > 3.0 and total_units <= single_line_limit + tolerance:
            return [segment]

        if first_units > max_line_length * 1.35 or second_units > max_line_length * 1.35:
            fallback_lines = cls._wrap_subtitle_segment_greedily(segment, max_line_length)
            if len(fallback_lines) == 2 and not cls._is_short_orphan_tail(fallback_lines[1]):
                return fallback_lines
            if total_units <= single_line_limit + tolerance:
                return [segment]
            return fallback_lines

        return [first_line, second_line]

    @classmethod
    def _wrap_subtitle_text_for_ass(
        cls,
        text,
        video_width,
        video_height,
        return_meta=False,
        *,
        prefer_single_line=True,
        single_line_min_font_scale=None,
        style_overrides=None,
    ):
        # Normalize internal line breaks so that a single SRT cue is always
        # treated as one logical line. The ASS burn-in stage decides whether
        # to scale the font or emit an overflow warning; it never falls back
        # to multi-line wrapping for landscape captions.
        normalized = cls._merge_subtitle_text_parts(
            str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
        )
        wrap_meta = {
            'forced_wrap': False,
            'font_override': None,
            'overflow_warning': False,
        }
        if not normalized:
            return ('', wrap_meta) if return_meta else ''

        max_line_length, max_lines = cls._estimate_subtitle_layout_limits(
            video_width, video_height, style_overrides
        )
        style = cls._build_streaming_ass_style(video_width, video_height, style_overrides)
        is_portrait = float(style['PlayResY']) > float(style['PlayResX'])
        usable_width = max(
            120.0,
            float(style['PlayResX']) - float(style['MarginL']) - float(style['MarginR']),
        )
        font_size = max(1.0, float(style['FontSize']))
        outline = float(style['Outline'])
        shadow = float(style['Shadow'])
        single_line_limit = int(cls._clamp(
            round(usable_width / font_size * cls._ASS_LANDSCAPE_SINGLE_LINE_DENSITY),
            cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MIN,
            cls._ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX,
        ))

        # Single-line priority: keep the whole cue on one line if it fits,
        # optionally scaling the font down within the configured range.
        if prefer_single_line:
            min_scale = max(
                cls._ASS_SINGLE_LINE_FONT_SCALE_MIN,
                float(single_line_min_font_scale or cls._ASS_SINGLE_LINE_FONT_SCALE_MIN),
            )
            single_line = [normalized]
            single_fits, _, _, _ = cls._check_ass_lines_width_safety(
                single_line,
                usable_width,
                font_size,
                outline,
                shadow,
            )
            if single_fits:
                wrapped_lines = single_line
            else:
                scaled_font_size, scaled_outline, scaled_shadow = cls._resolve_single_line_scaled_font(
                    normalized,
                    usable_width,
                    font_size,
                    outline,
                    shadow,
                    min_scale=min_scale,
                )
                if scaled_font_size is not None and scaled_font_size < font_size:
                    wrapped_lines = single_line
                    wrap_meta['font_override'] = int(round(scaled_font_size))
                    ass_text = cls._compose_ass_dialogue_text(
                        wrapped_lines,
                        override_font_size=wrap_meta['font_override'],
                    )
                    return (ass_text, wrap_meta) if return_meta else ass_text
                wrapped_lines = cls._build_wrapped_lines_for_ass(
                    normalized,
                    is_portrait=is_portrait,
                    max_line_length=max_line_length,
                    max_lines=max_lines,
                    single_line_limit=single_line_limit,
                    aggressive=False,
                )
        else:
            wrapped_lines = cls._build_wrapped_lines_for_ass(
                normalized,
                is_portrait=is_portrait,
                max_line_length=max_line_length,
                max_lines=max_lines,
                single_line_limit=single_line_limit,
                aggressive=False,
            )

        fits, _, _, _ = cls._check_ass_lines_width_safety(
            wrapped_lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        candidate_lines = wrapped_lines
        if not fits:
            hard_wrap_lines, hard_wrap_fits = cls._find_safe_hard_wrap_lines(
                normalized,
                max_line_length=max_line_length,
                max_lines=max_lines,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
            )
            if hard_wrap_lines:
                candidate_lines = hard_wrap_lines
                wrap_meta['forced_wrap'] = candidate_lines != wrapped_lines
            fits = hard_wrap_fits

        if not fits and is_portrait and max_lines < 5:
            portrait_rescue_lines, portrait_rescue_fits = cls._find_portrait_rescue_lines(
                normalized,
                max_line_length=max_line_length,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
            )
            if portrait_rescue_lines:
                candidate_lines = portrait_rescue_lines
                wrap_meta['forced_wrap'] = True
                fits = portrait_rescue_fits
            elif not portrait_rescue_lines:
                portrait_fallback_lines, portrait_fallback_fits = cls._find_safe_hard_wrap_lines(
                    normalized,
                    max_line_length=max(max_line_length, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH),
                    max_lines=5,
                    usable_width=usable_width,
                    font_size=font_size,
                    outline=outline,
                    shadow=shadow,
                )
                if portrait_fallback_lines:
                    candidate_lines = portrait_fallback_lines
                    wrap_meta['forced_wrap'] = True
                    fits = portrait_fallback_fits

        # Landscape captions are hard-coded to a single line. Only attempt
        # multi-line rescue fallbacks when the layout explicitly allows more
        # than one line (e.g. caller passed prefer_single_line=False).
        if not fits and not is_portrait and 1 < max_lines < 4:
            landscape_rescue_lines, landscape_rescue_fits = cls._find_landscape_rescue_lines(
                normalized,
                max_line_length=max_line_length,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
            )
            if landscape_rescue_lines:
                candidate_lines = landscape_rescue_lines
                wrap_meta['forced_wrap'] = True
                fits = landscape_rescue_fits
            elif not landscape_rescue_lines:
                rescue_lines, rescue_fits = cls._find_safe_hard_wrap_lines(
                    normalized,
                    max_line_length=max(max_line_length - 1, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH),
                    max_lines=3,
                    usable_width=usable_width,
                    font_size=font_size,
                    outline=outline,
                    shadow=shadow,
                )
                if rescue_lines:
                    candidate_lines = rescue_lines
                    wrap_meta['forced_wrap'] = True
                    fits = rescue_fits

            if not fits and not landscape_rescue_lines:
                deep_rescue_lines, deep_rescue_fits = cls._find_safe_hard_wrap_lines(
                    normalized,
                    max_line_length=max(max_line_length - 2, cls._ASS_HARD_WRAP_MIN_LINE_LENGTH),
                    max_lines=4,
                    usable_width=usable_width,
                    font_size=font_size,
                    outline=outline,
                    shadow=shadow,
                )
                if deep_rescue_lines:
                    candidate_lines = deep_rescue_lines
                    wrap_meta['forced_wrap'] = True
                    fits = deep_rescue_fits

        override_font_size = None
        if not fits and candidate_lines:
            override_font_size, override_fits = cls._resolve_safe_override_font_size(
                candidate_lines,
                usable_width,
                font_size,
                outline,
                shadow,
            )
            if override_font_size is not None:
                wrap_meta['font_override'] = int(round(override_font_size))
                fits = override_fits

        if not fits and candidate_lines:
            wrap_meta['overflow_warning'] = True

        ass_text = cls._compose_ass_dialogue_text(candidate_lines, override_font_size=override_font_size)
        return (ass_text, wrap_meta) if return_meta else ass_text

    @classmethod
    def _rebalance_split_cue_durations(cls, cues):
        if not cues:
            return []

        fixed_cues = [dict(cue or {}) for cue in cues]
        minimum_visible = 0.35
        preferred_duration = 0.6

        for idx, cue in enumerate(fixed_cues):
            start = float(cue.get('start', 0.0) or 0.0)
            end = float(cue.get('end', 0.0) or 0.0)
            if end - start >= 0.05:
                continue

            if idx > 0:
                prev = fixed_cues[idx - 1]
                prev_start = float(prev.get('start', 0.0) or 0.0)
                prev_end = float(prev.get('end', 0.0) or 0.0)
                target_start = max(prev_start + minimum_visible, end - preferred_duration)
                if target_start < end:
                    prev['end'] = target_start
                    cue['start'] = target_start
                    cue['end'] = end
                    continue

            if idx + 1 < len(fixed_cues):
                nxt = fixed_cues[idx + 1]
                next_start = float(nxt.get('start', 0.0) or 0.0)
                next_end = float(nxt.get('end', 0.0) or 0.0)
                target_end = min(next_end - minimum_visible, start + preferred_duration)
                if target_end > start:
                    cue['start'] = start
                    cue['end'] = target_end
                    nxt['start'] = target_end

        return fixed_cues

    @classmethod
    def _parse_subtitle_text_to_cues(cls, subtitle_text, video_width=None, video_height=None):
        from .srt_transform_engine import SrtTransformConfig, SrtTransformEngine

        # Hard-coded large limits for SRT parsing: never split an incoming SRT
        # cue here. The ASS burn-in stage (_wrap_subtitle_text_for_ass) decides
        # later whether to scale the font or wrap, based on the real video
        # dimensions and the single-line priority settings.
        engine = SrtTransformEngine(
            SrtTransformConfig(
                max_line_length=999,
                max_lines=99,
                normalize_punctuation=False,
                filter_filler_words=False,
            )
        )
        cues = engine.parse_srt(subtitle_text or '')
        if not cues:
            return []
        processed = engine.apply_text_processing(cues)
        return cls._rebalance_split_cue_durations(processed)

    @staticmethod
    def _normalize_font_match_name(font_name):
        return ' '.join(str(font_name or '').strip().split()).casefold()

    @staticmethod
    def _normalize_font_filename(font_name):
        return os.path.basename(str(font_name or '').strip()).casefold()

    @classmethod
    def _iter_bundled_font_paths(cls):
        fonts_dir = get_app_subdir('fonts')
        if not os.path.isdir(fonts_dir):
            return []
        font_paths = []
        for entry in os.listdir(fonts_dir):
            ext = os.path.splitext(entry)[1].lower()
            if ext in cls._BUNDLED_FONT_EXTENSIONS:
                font_paths.append(os.path.join(fonts_dir, entry))
        return sorted(font_paths)

    @classmethod
    def _read_font_display_names(cls, font_path):
        from PIL import ImageFont

        pil_font = ImageFont.truetype(font_path, size=18)
        family_name, style_name = pil_font.getname()
        family_name = str(family_name or '').strip()
        style_name = str(style_name or '').strip()

        candidates = []
        if family_name and style_name:
            candidates.append(f"{family_name} {style_name}")
        if family_name:
            candidates.append(family_name)
        return tuple(dict.fromkeys(name for name in candidates if name))

    @classmethod
    def _find_bundled_font_by_name(cls, font_name, task_logger=None):
        normalized_target = cls._normalize_font_match_name(font_name)
        normalized_filename = cls._normalize_font_filename(font_name)
        if not normalized_target:
            return None

        for font_path in cls._iter_bundled_font_paths():
            if normalized_filename and os.path.basename(font_path).casefold() == normalized_filename:
                try:
                    display_names = cls._read_font_display_names(font_path)
                except Exception as exc:
                    if task_logger:
                        task_logger.warning(f"读取内置字体信息失败，已跳过 {os.path.basename(font_path)}: {exc}")
                    continue
                primary_name = display_names[0] if display_names else os.path.basename(font_path)
                family_name = display_names[1] if len(display_names) > 1 else primary_name
                return {
                    'font_path': font_path,
                    'font_name': primary_name,
                    'font_family': family_name,
                }

            try:
                display_names = cls._read_font_display_names(font_path)
            except Exception as exc:
                if task_logger:
                    task_logger.warning(f"读取内置字体信息失败，已跳过 {os.path.basename(font_path)}: {exc}")
                continue

            for display_name in display_names:
                if cls._normalize_font_match_name(display_name) == normalized_target:
                    primary_name = display_names[0] if display_names else str(font_name).strip()
                    family_name = display_names[1] if len(display_names) > 1 else primary_name
                    return {
                        'font_path': font_path,
                        'font_name': primary_name,
                        'font_family': family_name,
                    }
        return None

    def _resolve_subtitle_font(self, task_logger, temp_fonts_dir):
        config = getattr(self, 'config', {}) or {}
        configured_font_name = str(
            config.get('SUBTITLE_FONT_NAME') or 'NotoSansCJKsc-Regular.otf'
        ).strip()
        if not configured_font_name:
            configured_font_name = 'NotoSansCJKsc-Regular.otf'

        resolved_font = {
            'configured_font_name': configured_font_name,
            'font_name': configured_font_name,
            'font_family': configured_font_name,
            'matched_font_name': None,
            'font_path': None,
            'temp_font_path': None,
        }

        matched_font = self._find_bundled_font_by_name(configured_font_name, task_logger)
        if not matched_font:
            for fallback_path in self._iter_bundled_font_paths():
                try:
                    display_names = self._read_font_display_names(fallback_path)
                except Exception as exc:
                    if task_logger:
                        task_logger.warning(f"读取内置字体信息失败，已跳过 {os.path.basename(fallback_path)}: {exc}")
                    continue
                primary_name = display_names[0] if display_names else os.path.basename(fallback_path)
                family_name = display_names[1] if len(display_names) > 1 else primary_name
                matched_font = {
                    'font_path': fallback_path,
                    'font_name': primary_name,
                    'font_family': family_name,
                }
                if task_logger:
                    task_logger.warning(
                        f"未在内置字体目录中找到匹配字体: {configured_font_name}，已改用内置字体: {primary_name}"
                    )
                break

        if not matched_font:
            if task_logger:
                task_logger.warning(
                    f"未在内置字体目录中找到匹配字体: {configured_font_name}，将继续使用系统字体解析"
                )
            return resolved_font

        resolved_font['font_path'] = matched_font['font_path']
        resolved_font['matched_font_name'] = matched_font['font_name']
        resolved_font['font_family'] = matched_font['font_family']
        try:
            temp_font_path = os.path.join(temp_fonts_dir, os.path.basename(matched_font['font_path']))
            shutil.copy2(matched_font['font_path'], temp_font_path)
            resolved_font['temp_font_path'] = temp_font_path
            if task_logger:
                task_logger.debug(
                    f"已将内置字幕字体复制到临时目录: {os.path.basename(matched_font['font_path'])}"
                )
        except Exception as exc:
            if task_logger:
                task_logger.warning(f"复制内置字幕字体失败: {exc}")

        return resolved_font

    def _resolve_subtitle_style_overrides(self, task_logger=None):
        """解析烧录字幕的外观配置。

        解析失败时返回 None，此时下游会退回历史默认观感，不会中断烧录。
        """
        try:
            overrides = parse_style_overrides(getattr(self, 'config', {}) or {})
        except Exception as exc:
            if task_logger:
                task_logger.warning(f"解析字幕外观配置失败，回退默认观感: {exc}")
            return None
        if task_logger:
            try:
                for issue in diagnose_style(overrides) or ():
                    task_logger.info(f"字幕外观提示: {issue}")
            except Exception:
                pass
        return overrides

    @staticmethod
    def _style_overrides_differ_from_defaults(style_overrides) -> bool:
        """判断用户是否**确实修改过**字幕外观配置（与出厂默认不同）。

        ``parse_style_overrides`` 总会返回一份完整字典（未配置的键填默认值），
        因此不能靠「字典是否为空」判断用户意图。ASS/SSA 输入分支需要这个判据：
        只有用户真的改过外观时才用 force_style 覆盖源样式，
        否则保留创作者样式，避免凭空破坏用户手作的 ASS。
        """
        if not isinstance(style_overrides, dict):
            return False
        try:
            defaults = parse_style_overrides({})
        except Exception:
            return True
        for key, value in style_overrides.items():
            if key not in defaults:
                return True
            if value != defaults[key]:
                return True
        return False

    @classmethod
    def _build_default_ass_document(
        cls,
        cues,
        font_family,
        video_width,
        video_height,
        *,
        prefer_single_line=True,
        single_line_min_font_scale=None,
        style_overrides=None,
    ):
        style, force_style = cls._build_subtitle_style_description(
            font_family,
            video_width,
            video_height,
            style_overrides,
        )
        font_name = force_style['FontName']
        ass_header = (
            "[Script Info]\n"
            "Title: Streaming Subtitle\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {style['PlayResX']}\n"
            f"PlayResY: {style['PlayResY']}\n"
            "WrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n"
            "Collisions: Normal\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,"
            f"{font_name},"
            f"{force_style['FontSize']},"
            f"{style['PrimaryColour']},"
            f"{style['SecondaryColour']},"
            f"{style['OutlineColour']},"
            f"{style['BackColour']},"
            f"{style['Bold']},0,0,0,100,100,0,0,"
            f"{style['BorderStyle']},"
            f"{force_style['Outline']},"
            f"{force_style['Shadow']},"
            f"{force_style['Alignment']},"
            f"{force_style['MarginL']},"
            f"{force_style['MarginR']},"
            f"{force_style['MarginV']},1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        ass_lines = []
        forced_wrap_count = 0
        font_override_count = 0
        overflow_warning_count = 0
        for cue in cues or []:
            cue_dict = cue if isinstance(cue, dict) else {}
            wrapped_result = cls._wrap_subtitle_text_for_ass(
                cue_dict.get('text', ''),
                video_width,
                video_height,
                return_meta=True,
                prefer_single_line=prefer_single_line,
                single_line_min_font_scale=single_line_min_font_scale,
                style_overrides=style_overrides,
            )
            # `return_meta=True` is expected to return a tuple, but keep a safe fallback
            # to satisfy static analysis and guard unexpected call-path changes.
            if isinstance(wrapped_result, tuple):
                text, wrap_meta = wrapped_result
            else:
                text = wrapped_result
                wrap_meta = {}
            if not text:
                continue
            if wrap_meta.get('forced_wrap'):
                forced_wrap_count += 1
            if wrap_meta.get('font_override'):
                font_override_count += 1
            if wrap_meta.get('overflow_warning'):
                overflow_warning_count += 1
            ass_lines.append(
                "Dialogue: 0,"
                f"{cls._seconds_to_ass_timestamp(cue_dict.get('start', 0.0))},"
                f"{cls._seconds_to_ass_timestamp(cue_dict.get('end', 0.0))},"
                "Default,,0,0,0,,"
                f"{text}"
            )

        body = '\n'.join(ass_lines)
        if body:
            body += '\n'
        if forced_wrap_count or font_override_count or overflow_warning_count:
            logger.debug(
                "ASS overflow guard summary: forced_wrap=%s, font_override=%s, overflow_warning=%s, cues=%s",
                forced_wrap_count,
                font_override_count,
                overflow_warning_count,
                len(ass_lines),
            )
        if overflow_warning_count:
            logger.warning(
                "ASS overflow guard hit minimum per-cue font size but still detected %s potentially risky cue(s)",
                overflow_warning_count,
            )
        return ass_header + body

    def _convert_srt_to_ass(
        self,
        subtitle_path,
        ass_path,
        task_logger,
        *,
        video_width=None,
        video_height=None,
        font_family=None,
        prefer_single_line=True,
        single_line_min_font_scale=None,
        style_overrides=None,
    ):
        """将SRT/VTT字幕转换为带默认流媒体样式的ASS格式。"""
        try:
            source_path = subtitle_path
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()

            if subtitle_ext == '.vtt':
                converted_srt = self._convert_vtt_to_srt(subtitle_path, task_logger)
                if not converted_srt or not os.path.exists(converted_srt):
                    task_logger.error("VTT转SRT失败，无法生成ASS字幕")
                    return False
                source_path = converted_srt

            with open(source_path, 'r', encoding='utf-8-sig', errors='replace') as subtitle_file:
                subtitle_content = subtitle_file.read()

            cues = self._parse_subtitle_text_to_cues(
                subtitle_content,
                video_width=video_width,
                video_height=video_height,
            )
            if not cues:
                task_logger.error(f"未解析出有效字幕条目，无法生成ASS: {os.path.basename(subtitle_path)}")
                return False

            config = getattr(self, 'config', {}) or {}
            if style_overrides is None:
                # 未显式指定时按当前配置解析，避免任何调用方漏传导致外观配置静默失效。
                style_overrides = self._resolve_subtitle_style_overrides(task_logger)
            ass_content = self._build_default_ass_document(
                cues,
                font_family=font_family,
                video_width=video_width,
                video_height=video_height,
                prefer_single_line=prefer_single_line,
                single_line_min_font_scale=(
                    single_line_min_font_scale
                    if single_line_min_font_scale is not None
                    else config.get('SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE', self._ASS_SINGLE_LINE_FONT_SCALE_MIN)
                ),
                style_overrides=style_overrides,
            )
            with open(ass_path, 'w', encoding='utf-8') as ass_file:
                ass_file.write(ass_content)

            task_logger.info(f"字幕转换为ASS成功: {ass_path}")
            return True

        except Exception as e:
            task_logger.error(f"SRT/VTT转ASS转换失败: {str(e)}")
            return False

    def _embed_subtitle_in_video(self, task_id, video_path, subtitle_path, task_logger):
        """使用FFmpeg将字幕嵌入视频（修复版本 - 添加超时机制）"""
        # 保存当前状态，稍后恢复
        task_before_encoding = get_task(task_id)
        previous_status = task_before_encoding['status'] if task_before_encoding else TASK_STATES['TRANSLATING_SUBTITLE']
        
        try:
            # subprocess imported at module level
            import os
            import tempfile
            import shutil
            import re
            import time
            import threading
            import queue

            # 仅使用项目内置 ffmpeg/ffprobe，如缺失仅在 Windows 上尝试下载作为兜底
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin or not os.path.exists(ffmpeg_bin):
                task_logger.error("未找到项目内置 FFmpeg，无法进行字幕嵌入。请确认仓库自带的 ffmpeg/ 目录完整（可重新解压或手动放置二进制）。")
                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                task_logger.error("未能定位 ffprobe，无法继续字幕嵌入")
                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            
            # 生成嵌入字幕后的视频文件路径
            video_dir = os.path.dirname(video_path)
            video_name = os.path.splitext(os.path.basename(video_path))[0]
            embedded_video_path = os.path.join(video_dir, f"{video_name}_with_subtitle.mp4")
            
            task_logger.info("开始将字幕嵌入视频...")
            # 仅在调试时输出详细路径信息
            task_logger.debug(f"视频路径: {video_path}")
            task_logger.debug(f"字幕路径: {subtitle_path}")
            
            # 设置任务状态为转码视频中
            update_task(task_id, status=TASK_STATES['ENCODING_VIDEO'])
            _raise_if_cancelled(task_id, task_logger)
            
            def _format_bitrate_for_log(bit_rate):
                bit_rate_int = self._coerce_int(bit_rate)
                if bit_rate_int is None or bit_rate_int <= 0:
                    return 'unknown'
                if bit_rate_int >= 1000000:
                    return f"{bit_rate_int / 1000000:.2f}Mbps"
                return f"{bit_rate_int / 1000:.0f}kbps"

            def _format_size_for_log(size_bytes):
                if not isinstance(size_bytes, int) or size_bytes < 0:
                    return 'unknown'
                if size_bytes >= 1024 ** 3:
                    return f"{size_bytes / (1024 ** 3):.2f}GB"
                return f"{size_bytes / (1024 ** 2):.2f}MB"

            def _log_output_media_summary(output_path):
                try:
                    output_video_info = self._get_video_stream_info(output_path, task_logger)
                    output_audio_info = self._get_audio_stream_info(output_path, task_logger)
                    output_size_bytes = os.path.getsize(output_path) if os.path.exists(output_path) else None
                    size_ratio_text = 'unknown'
                    if (
                        isinstance(output_size_bytes, int)
                        and isinstance(input_size_bytes, int)
                        and input_size_bytes > 0
                    ):
                        size_ratio_text = f"{output_size_bytes / input_size_bytes:.2f}x"
                    task_logger.info(
                        "输出媒体摘要: video_codec=%s, video_bitrate=%s, audio_codec=%s, audio_bitrate=%s, "
                        "file_size=%s, size_ratio=%s",
                        output_video_info.get('codec_name') or 'unknown',
                        _format_bitrate_for_log(output_video_info.get('bit_rate')),
                        output_audio_info.get('codec_name') or 'unknown',
                        _format_bitrate_for_log(output_audio_info.get('bit_rate')),
                        _format_size_for_log(output_size_bytes),
                        size_ratio_text,
                    )
                except Exception as e:
                    task_logger.warning(f"记录输出媒体摘要失败: {e}")

            # 获取视频时长和流信息用于计算进度与参数
            video_duration = self._get_video_duration(video_path, task_logger)
            stream_info = self._get_video_stream_info(video_path, task_logger)
            input_width = stream_info.get('width') or 0
            input_height = stream_info.get('height') or 0
            input_fps = stream_info.get('fps') or 30
            input_video_codec = str(stream_info.get('codec_name') or '').strip().lower()
            input_video_bit_rate = self._coerce_int(stream_info.get('bit_rate'))
            input_pix_fmt = stream_info.get('pix_fmt') if isinstance(stream_info, dict) else None
            input_size_bytes = os.path.getsize(video_path) if os.path.exists(video_path) else None
            # 探测音频信息（用于跟随原视频）
            audio_info = self._get_audio_stream_info(video_path, task_logger)
            input_audio_codec = str(audio_info.get('codec_name') or '').strip().lower()
            input_audio_bit_rate = self._coerce_int(audio_info.get('bit_rate'))
            # GOP 取 2 秒一关键帧
            gop = max(24, int(round(2 * input_fps)))
            # HEVC 适合更长 GOP，4 秒可提升压缩效率 5-10%
            gop_hevc = max(48, int(round(4 * input_fps)))
            task_logger.info(
                "输入媒体摘要: video_codec=%s, video_bitrate=%s, audio_codec=%s, audio_bitrate=%s, file_size=%s",
                input_video_codec or 'unknown',
                _format_bitrate_for_log(input_video_bit_rate),
                input_audio_codec or 'unknown',
                _format_bitrate_for_log(input_audio_bit_rate),
                _format_size_for_log(input_size_bytes),
            )
            task_logger.info(f"GOP 设置: H.264={gop} (2秒), HEVC={gop_hevc} (4秒)")

            def _ffmpeg_has_filter(filter_name: str) -> bool:
                try:
                    # subprocess imported at module level
                    result = subprocess.run(
                        [ffmpeg_bin, '-hide_banner', '-filters'],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20
                    )
                    if result.returncode == 0 and filter_name in result.stdout:
                        return True
                except Exception:
                    pass
                return False

            # 先规范字幕格式，再创建临时目录
            subtitle_ext = os.path.splitext(subtitle_path)[1].lower()
            if subtitle_ext not in ('.srt', '.ass', '.ssa', '.vtt'):
                task_logger.warning(f"未知字幕扩展名 {subtitle_ext}，默认按srt处理")
                subtitle_ext = '.srt'
            temp_dir = tempfile.mkdtemp(prefix='subtitle_embed_')
            simple_video = os.path.abspath(video_path)
            simple_output = os.path.join(video_dir, f".{video_name}_with_subtitle.tmp.mp4")
            # 字体目录（在临时目录内放置一份，避免Windows路径转义问题）
            temp_fonts_dir = os.path.join(temp_dir, "fonts")
            os.makedirs(temp_fonts_dir, exist_ok=True)
            
            try:
                # 准备临时字幕文件
                try:
                    if os.path.exists(simple_output):
                        os.remove(simple_output)
                except Exception as e:
                    task_logger.warning(f"清理旧临时输出文件失败: {e}")
                
                style_overrides = self._resolve_subtitle_style_overrides(task_logger)
                subtitle_font = self._resolve_subtitle_font(task_logger, temp_fonts_dir)
                font_family = subtitle_font.get('font_family') or subtitle_font.get('configured_font_name')
                if task_logger:
                    task_logger.debug(
                        f"当前字幕字体配置: {subtitle_font.get('configured_font_name')}"
                    )
                    if subtitle_font.get('matched_font_name'):
                        task_logger.debug(
                            f"匹配到内置字体: {subtitle_font.get('matched_font_name')}"
                        )

                if not font_family:
                    # 回退到常见的中文字体家族名称。优先使用 Noto Sans CJK SC
                    fallback_families = [
                        'Noto Sans CJK SC',
                        'Source Han Sans HW SC VF',
                        'Source Han Sans HW SC',
                        'Source Han Sans SC',
                        'Source Han Sans',
                        'Microsoft YaHei',
                        'SimHei'
                    ]
                    font_family = fallback_families[0]
                    task_logger.debug(f"使用回退字体家族名称: {font_family}")

                render_subtitle_ext = subtitle_ext
                render_subtitle_name = f"sub{render_subtitle_ext}"
                render_subtitle_path = os.path.join(temp_dir, render_subtitle_name)

                if subtitle_ext in ('.ass', '.ssa'):
                    shutil.copy2(subtitle_path, render_subtitle_path)
                    # ASS/SSA 输入此前一律「保留源样式」，用户设置的字号/颜色/
                    # 描边/背景全部静默失效，但日志照样打印「字幕外观提示」，
                    # 让人误以为配置已应用。
                    # 现在：只有用户**确实改过**外观配置时才用 force_style 覆盖
                    # 源样式（libass 的 force_style 优先于脚本内样式）；全默认
                    # 配置下仍保留创作者的源样式，避免凭空破坏用户手作的 ASS。
                    filter_segments = [
                        f"subtitles={render_subtitle_name}",
                        "fontsdir=fonts",
                        "charenc=UTF-8",
                    ]
                    if self._style_overrides_differ_from_defaults(style_overrides):
                        force_style = self._build_subtitle_force_style(
                            font_family, input_width, input_height, style_overrides
                        )
                        if force_style:
                            filter_segments.append(force_style)
                        task_logger.info(
                            "ASS/SSA 输入：检测到自定义字幕外观配置，"
                            "将以 force_style 覆盖源样式后烧录"
                        )
                    else:
                        task_logger.info(
                            "ASS/SSA 输入：未配置自定义字幕外观，保留源字幕样式进行烧录"
                        )
                else:
                    render_subtitle_ext = '.ass'
                    render_subtitle_name = "sub.ass"
                    render_subtitle_path = os.path.join(temp_dir, render_subtitle_name)
                    config = getattr(self, 'config', {}) or {}
                    if not self._convert_srt_to_ass(
                        subtitle_path,
                        render_subtitle_path,
                        task_logger,
                        video_width=input_width,
                        video_height=input_height,
                        font_family=font_family,
                        prefer_single_line=config.get('SUBTITLE_PREFER_SINGLE_LINE', True),
                        single_line_min_font_scale=config.get(
                            'SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE',
                            self._ASS_SINGLE_LINE_FONT_SCALE_MIN,
                        ),
                        style_overrides=style_overrides,
                    ):
                        task_logger.error("生成清晰ASS字幕失败，无法继续嵌入字幕流程")
                        update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                        return None
                    task_logger.info("已为非ASS字幕生成统一底部居中ASS临时文件")
                    filter_segments = [
                        f"subtitles={render_subtitle_name}",
                        "fontsdir=fonts",
                        "charenc=UTF-8",
                    ]
                vf_filter = ':'.join(filter_segments)

                # 若字幕滤镜不可用，提前报错并放弃嵌入
                if not _ffmpeg_has_filter('subtitles'):
                    task_logger.error("当前FFmpeg不包含 'subtitles' 滤镜（需要libass支持），无法嵌入字幕")
                    update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                    return None

                # --------------------------------------------------
                # 硬件编码检测与参数生成函数
                # --------------------------------------------------
                # 缓存硬件编码器检测结果，避免重复测试
                _hw_encoder_cache = {}
                _hw_encoder_error_cache = {}
                _hw_encoder_probe_meta_cache = {}
                amd_backend_cache = None

                def _is_encoder_listed(encoder_name: str) -> bool:
                    """仅检查编码器是否在 ffmpeg -encoders 中出现。"""
                    try:
                        list_result = subprocess.run(
                            [ffmpeg_bin, '-hide_banner', '-encoders'],
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            errors='replace',
                            timeout=10
                        )
                        encoder_text = f"{list_result.stdout or ''}\n{list_result.stderr or ''}"
                        return list_result.returncode == 0 and encoder_name in encoder_text
                    except Exception:
                        return False

                def _detect_hw_encoder(encoder_name: str) -> bool:
                    """通过实际测试编码来检测硬件编码器是否可用（而非仅检查编译列表）"""
                    if encoder_name in _hw_encoder_cache:
                        return _hw_encoder_cache[encoder_name]
                    try:
                        # 先做一次快速枚举（兼容不同 ffmpeg 版本将输出写到 stdout/stderr 的差异）
                        # 注意：枚举失败/未匹配不直接判定不可用，仍以“实际编码测试”结果为准，避免误判。
                        encoder_list_available = False
                        try:
                            list_result = subprocess.run(
                                [ffmpeg_bin, '-hide_banner', '-encoders'],
                                capture_output=True,
                                text=True,
                                encoding='utf-8',
                                errors='replace',
                                timeout=10
                            )
                            encoder_text = f"{list_result.stdout or ''}\n{list_result.stderr or ''}"
                            encoder_list_available = (
                                list_result.returncode == 0 and encoder_name in encoder_text
                            )
                        except Exception:
                            # 枚举失败不影响后续实际测试
                            pass

                        available, probe_err, probe_meta = self._probe_hw_encoder_availability(
                            ffmpeg_bin=ffmpeg_bin,
                            encoder_name=encoder_name,
                        )
                        _hw_encoder_probe_meta_cache[encoder_name] = dict(probe_meta or {})
                        _hw_encoder_cache[encoder_name] = available
                        if not available:
                            _hw_encoder_error_cache[encoder_name] = probe_err or '未知错误'
                            if not encoder_list_available:
                                task_logger.debug(
                                    f"编码器 {encoder_name} 未在 ffmpeg -encoders 列表中匹配到，且实测失败: "
                                    f"{self._short_error_text(probe_err)}"
                                )
                            else:
                                task_logger.debug(
                                    f"编码器 {encoder_name} 已编译但硬件不可用: {self._short_error_text(probe_err)}"
                                )
                            task_logger.warning(
                                f"编码器 {encoder_name} 探测失败: "
                                f"probe_size={probe_meta.get('probe_size', 'unknown')}, "
                                f"probe_retry={probe_meta.get('probe_retry', False)}, "
                                f"probe_returncode={probe_meta.get('probe_returncode', 'unknown')}, "
                                f"probe_error_short={probe_meta.get('probe_error_short', '')}"
                            )
                            task_logger.debug(
                                f"编码器 {encoder_name} 探测命令摘要: {probe_meta.get('probe_cmd_summary', '')}"
                            )
                        else:
                            _hw_encoder_error_cache[encoder_name] = ''
                            task_logger.debug(
                                f"编码器 {encoder_name} 测试成功，硬件可用: "
                                f"probe_size={probe_meta.get('probe_size', 'unknown')}, "
                                f"probe_retry={probe_meta.get('probe_retry', False)}, "
                                f"probe_returncode={probe_meta.get('probe_returncode', 0)}"
                            )
                        return available
                    except Exception as e:
                        task_logger.debug(f"检测编码器 {encoder_name} 时出错: {e}")
                        _hw_encoder_cache[encoder_name] = False
                        _hw_encoder_error_cache[encoder_name] = self._short_error_text(str(e))
                        _hw_encoder_probe_meta_cache[encoder_name] = {
                            'probe_size': 'unknown',
                            'probe_retry': False,
                            'probe_returncode': -1,
                            'probe_error_short': self._short_error_text(str(e)),
                            'probe_cmd_summary': '',
                        }
                    return False

                def _detect_amd_backend() -> str:
                    """检测 AMD 可用后端，返回 amf/vaapi/none。"""
                    nonlocal amd_backend_cache
                    if amd_backend_cache is not None:
                        return amd_backend_cache
                    if _detect_hw_encoder('hevc_amf'):
                        amd_backend_cache = 'amf'
                    elif _detect_hw_encoder('hevc_vaapi'):
                        amd_backend_cache = 'vaapi'
                    else:
                        amd_backend_cache = 'none'
                    return amd_backend_cache

                def _detect_nvidia() -> bool:
                    """检测 NVIDIA HEVC NVENC 是否可用"""
                    return _detect_hw_encoder('hevc_nvenc')

                def _detect_intel() -> bool:
                    """检测 Intel HEVC QSV 是否可用"""
                    return _detect_hw_encoder('hevc_qsv')

                def _detect_amd() -> bool:
                    """检测 AMD HEVC AMF/VAAPI 是否可用"""
                    return _detect_amd_backend() != 'none'

                def _get_best_encoder() -> str:
                    """自动检测最佳可用编码器，返回: nvidia/intel/amd/cpu"""
                    if _detect_nvidia():
                        task_logger.info("检测到 NVIDIA GPU，使用 NVENC HEVC 硬件编码")
                        return 'nvidia'
                    if _detect_intel():
                        task_logger.info("检测到 Intel GPU，使用 QSV HEVC 硬件编码")
                        return 'intel'
                    if _detect_amd():
                        task_logger.info("检测到 AMD GPU，使用 AMF/VAAPI HEVC 硬件编码")
                        return 'amd'
                    task_logger.info("未检测到可用的硬件编码器，使用 CPU 软编码")
                    return 'cpu'

                # 从配置中获取（可选）自定义视频参数
                custom_video_params = self._parse_custom_video_params(task_logger)
                # 编码器附加配置（质量模式/预设/增强开关/色彩元数据/CPU 编码器）
                encoder_settings = parse_encoder_config(getattr(self, 'config', {}) or {})

                # CPU 软编码器（x264 / x265）。与 VIDEO_ENCODER 正交：后者选硬件，
                # 这里选软编码实现。x265 输出 HEVC，实测编码耗时约为 x264 的 5 倍。
                cpu_codec = normalize_cpu_codec(encoder_settings.get('cpu_codec'))

                # tune 是按 CPU 编码器各自的白名单校验的：film / stillimage 在 x264
                # 下合法，在 x265 下会让编码直接失败。用户切到 x265 后这类取值会被
                # 丢弃，必须显式告知，否则「配置了却没生效」无从察觉。
                _configured_tune = str(
                    (getattr(self, 'config', {}) or {}).get('VIDEO_X264_TUNE', '') or ''
                ).strip()
                if _configured_tune and not encoder_settings.get('software_tune'):
                    task_logger.warning(
                        f"VIDEO_X264_TUNE='{_configured_tune}' 对 {cpu_codec} 不合法，"
                        f"已忽略该 tune（x265 不支持 film/stillimage）"
                    )

                # 固定质量值（CRF/CQ/QP，越小质量越高）。auto 模式按分辨率推荐，
                # manual 模式使用用户在设置页指定的值。
                target_quality = recommend_quality(input_height)
                effective_quality = target_quality
                if (
                    encoder_settings.get('quality_mode') == 'manual'
                    and encoder_settings.get('quality_value') is not None
                ):
                    effective_quality = float(encoder_settings['quality_value'])
                target_quality_str = format_quality_value(effective_quality)
                target_quality_int = max(0, min(51, int(round(effective_quality))))
                task_logger.info(
                    f"视频分辨率: {input_width}x{input_height}, 推荐质量={format_quality_value(target_quality)}, "
                    f"实际质量={target_quality_str} (int={target_quality_int}), "
                    f"质量模式={encoder_settings.get('quality_mode')}, "
                    f"硬件质量增强={'开' if encoder_settings.get('hw_quality_boost') else '关'}, "
                    f"硬件档位={encoder_settings.get('hw_quality_level')}"
                )

                # 色彩元数据：auto 模式透传源流信息，避免播放器按默认色域解释导致偏色
                color_params = []
                color_map = {}
                try:
                    color_map = normalize_color_metadata(
                        encoder_settings.get('color_metadata_mode'),
                        stream_info,
                    )
                    color_params = resolve_color_metadata(
                        encoder_settings.get('color_metadata_mode'),
                        stream_info,
                        logger=task_logger,
                    )
                except Exception as color_exc:
                    task_logger.debug(f"解析色彩元数据失败，跳过色彩参数: {color_exc}")
                if color_params:
                    task_logger.info(f"输出色彩元数据: {' '.join(color_params)}")

                def _video_param_ctx(amd_backend=None, cpu_codec_override=None, hw_quality_boost=None):
                    """汇总编码上下文，交由 video_encoder_params 统一构造参数。

                    cpu_codec_override 用于 x265 -> x264 降级阶段覆盖用户选择；
                    为 None 时使用配置里的 VIDEO_CPU_CODEC。这里刻意不与外层的
                    cpu_codec 同名，避免遮蔽导致「以为读的是配置、其实读的是形参」。
                    color_map 交给 encoder_params，由它把色彩 VUI 与（x265 的）
                    质量增强合并成同一条私有参数选项。

                    ``hw_quality_boost`` 显式传入时覆盖 ``encoder_settings`` 里的值。
                    降级重试必须走这条覆盖通道，**不能**就地改写 ``encoder_settings``：
                    那是闭包共享的 dict，改掉之后后面的 CPU 回退阶段读到的就是
                    「增强已关」，用户明明开着增强，回退后的成片却按关闭增强的
                    基础参数编码（0a98c26 引入的行为）。
                    """
                    ctx = {
                        'height': input_height,
                        'gop': gop,
                        'gop_hevc': gop_hevc,
                        'quality_mode': encoder_settings.get('quality_mode'),
                        'quality_value': encoder_settings.get('quality_value'),
                        'cpu_preset': encoder_settings.get('cpu_preset'),
                        'cpu_preset_hd': encoder_settings.get('cpu_preset_hd'),
                        'duration_s': video_duration,
                        'hw_quality_boost': (
                            encoder_settings.get('hw_quality_boost')
                            if hw_quality_boost is None
                            else hw_quality_boost
                        ),
                        'hw_quality_level': encoder_settings.get('hw_quality_level'),
                        'cpu_codec': cpu_codec_override or encoder_settings.get('cpu_codec'),
                        'software_tune': encoder_settings.get('software_tune'),
                        'color_map': color_map,
                        'custom_params': custom_video_params,
                    }
                    if amd_backend is not None:
                        ctx['amd_backend'] = amd_backend
                    return ctx

                # 针对软编码生成统一参数（libx264 或 libx265，由 VIDEO_CPU_CODEC 决定）。
                # 色彩 VUI 已由 build_encoder_params 合并在内，调用方不要再另行追加，
                # 否则 x265 会出现两条 -x265-params 而后者覆盖前者。
                def build_cpu_params(cpu_codec_override=None, hw_quality_boost=None):
                    return build_encoder_params(
                        'cpu',
                        _video_param_ctx(
                            cpu_codec_override=cpu_codec_override,
                            hw_quality_boost=hw_quality_boost,
                        ),
                    )

                def build_nvidia_params(hw_quality_boost=None):
                    """生成 NVIDIA NVENC HEVC 编码参数"""
                    return build_encoder_params(
                        'nvidia', _video_param_ctx(hw_quality_boost=hw_quality_boost)
                    )

                def build_intel_params(hw_quality_boost=None):
                    """生成 Intel QSV HEVC 编码参数"""
                    return build_encoder_params(
                        'intel', _video_param_ctx(hw_quality_boost=hw_quality_boost)
                    )

                def build_amd_params(hw_quality_boost=None):
                    """生成 AMD AMF/VAAPI HEVC 编码参数"""
                    return build_encoder_params(
                        'amd',
                        _video_param_ctx(_detect_amd_backend(), hw_quality_boost=hw_quality_boost),
                    )

                def is_vaapi_encoder() -> bool:
                    """检查当前是否使用 VAAPI 编码器（需要特殊的滤镜链处理）"""
                    return actual_encoder == 'amd' and _detect_amd_backend() == 'vaapi'

                # 确定使用的编码器
                encoder_pref = str(self.config.get('VIDEO_ENCODER', 'auto')).lower().strip()
                actual_encoder = encoder_pref
                
                if encoder_pref == 'auto':
                    actual_encoder = _get_best_encoder()
                elif encoder_pref == 'nvidia':
                    if not _detect_nvidia():
                        nvenc_listed = _is_encoder_listed('hevc_nvenc')
                        nvidia_device_visible = any(
                            os.path.exists(path)
                            for path in ('/dev/nvidia0', '/dev/nvidiactl', '/dev/nvidia-modeset')
                        )
                        nvenc_reason_raw = _hw_encoder_error_cache.get('hevc_nvenc') or '未知'
                        nvenc_reason = nvenc_reason_raw.replace('\n', ' ')
                        nvenc_probe_meta = _hw_encoder_probe_meta_cache.get('hevc_nvenc') or {}
                        nvenc_probe_size = nvenc_probe_meta.get('probe_size', 'unknown')
                        nvenc_probe_retry = nvenc_probe_meta.get('probe_retry', False)
                        nvenc_probe_error_short = nvenc_probe_meta.get('probe_error_short', '')
                        nvenc_probe_cmd_summary = nvenc_probe_meta.get('probe_cmd_summary', '')
                        task_logger.warning(
                            f"NVENC HEVC诊断信息: ffmpeg={ffmpeg_bin}, hevc_nvenc_listed={nvenc_listed}, "
                            f"nvidia_device_visible={nvidia_device_visible}, detect_error={nvenc_reason[:240]}, "
                            f"probe_size={nvenc_probe_size}, probe_retry={nvenc_probe_retry}, "
                            f"probe_error_short={nvenc_probe_error_short}"
                        )
                        if nvenc_probe_cmd_summary:
                            task_logger.debug(f"NVENC HEVC探测命令摘要: {nvenc_probe_cmd_summary}")

                        if self._should_keep_nvidia_preference_on_probe_failure(
                            nvenc_listed=nvenc_listed,
                            nvidia_device_visible=nvidia_device_visible,
                            detect_error=nvenc_reason_raw,
                        ):
                            task_logger.warning(
                                "NVENC HEVC 预检命中分辨率限制类错误，保留 NVIDIA 编码偏好并尝试真实转码"
                            )
                            actual_encoder = 'nvidia'
                        else:
                            task_logger.warning("配置使用 NVIDIA HEVC 编码但未检测到 NVENC，回退到自动检测")
                            task_logger.warning(
                                "NVENC 不可用常见原因：Docker 未启用 GPU 透传（gpus: all）、"
                                "主机未安装 nvidia-container-toolkit、"
                                "或 NVIDIA_DRIVER_CAPABILITIES 未包含 video,utility。"
                            )
                            actual_encoder = _get_best_encoder()
                elif encoder_pref == 'intel':
                    if not _detect_intel():
                        task_logger.warning("配置使用 Intel HEVC 编码但未检测到 QSV，回退到自动检测")
                        actual_encoder = _get_best_encoder()
                elif encoder_pref == 'amd':
                    if not _detect_amd():
                        task_logger.warning("配置使用 AMD HEVC 编码但未检测到 AMF/VAAPI，回退到自动检测")
                        actual_encoder = _get_best_encoder()

                # 根据编码器生成参数
                if actual_encoder == 'nvidia':
                    vparams = build_nvidia_params()
                    task_logger.info("使用 NVIDIA NVENC HEVC 硬件编码")
                elif actual_encoder == 'intel':
                    vparams = build_intel_params()
                    task_logger.info("使用 Intel QSV HEVC 硬件编码")
                elif actual_encoder == 'amd':
                    amd_backend = _detect_amd_backend()
                    if amd_backend == 'none':
                        task_logger.warning("AMD HEVC 编码已选中，但未检测到可用 AMF/VAAPI 后端，回退到 CPU 软编码")
                        actual_encoder = 'cpu'
                        vparams = build_cpu_params()
                    else:
                        vparams = build_amd_params()
                        encoder_name = 'AMF' if amd_backend == 'amf' else 'VAAPI'
                        task_logger.info(f"使用 AMD {encoder_name} HEVC 硬件编码（backend={amd_backend}）")
                else:
                    vparams = build_cpu_params()
                    task_logger.info(
                        f"使用 CPU 软编码 ({'libx265' if cpu_codec == 'x265' else 'libx264'})"
                    )

                # 音频优先直拷 AAC，非 AAC 再按源码率上限转 AAC
                aparams = self._build_audio_transcode_params(audio_info)

                # 软件编码的色彩 VUI 不再在这里追加：libx264/libx265 会静默忽略
                # -color_primaries/-color_trc，必须用编码器私有参数补写，而 x265 的
                # 质量增强与 VUI 必须写进**同一条** -x265-params（实测两条选项时后者
                # 完全覆盖前者）。因此合并动作放在 build_encoder_params 内部完成，
                # 这里只回显，避免两处各追加一条而互相覆盖。
                for _vui_index, _vui_token in enumerate(vparams):
                    if (
                        _vui_token in ('-x264-params', '-x265-params')
                        and _vui_index + 1 < len(vparams)
                    ):
                        task_logger.info(f"软件编码私有参数: {_vui_token} {vparams[_vui_index + 1]}")
                        break

                main_color_params = list(color_params)

                task_logger.info(f"视频编码参数: {' '.join(vparams)}")
                task_logger.info(f"音频编码参数: {' '.join(aparams)}")

                # 构建视频滤镜链
                # VAAPI 编码器需要特殊处理：在字幕滤镜后添加格式转换和硬件上传
                if is_vaapi_encoder():
                    # VAAPI: subtitles -> format=nv12 -> hwupload
                    final_vf = f"{vf_filter},format=nv12,hwupload"
                else:
                    final_vf = vf_filter

                cmd = self._build_embed_ffmpeg_cmd(
                    ffmpeg_bin=ffmpeg_bin,
                    input_video=simple_video,
                    vf_filter=final_vf,
                    vparams=vparams,
                    aparams=aparams,
                    output_video=simple_output,
                    color_params=main_color_params,
                )
                
                task_logger.debug(f"FFmpeg命令: {' '.join(cmd)}")
                task_logger.debug(f"临时目录: {temp_dir}")
                
                # 设置超时时间（根据视频时长估算）
                timeout = self._estimate_embed_timeout(
                    video_duration, cpu_codec if actual_encoder == 'cpu' else None
                )
                
                task_logger.debug(f"设置处理超时时间: {timeout//60} 分钟")
                
                # 执行 FFmpeg 命令并实时获取进度。
                # 抽成可复用函数：硬件编码失败时需要按「硬编关增强 → CPU」分级重试，
                # 每次都走同一套进度解析与超时/取消处理，避免逻辑分叉。
                def _execute_embed(embed_cmd, run_timeout, stage_label=''):
                    stage_suffix = f"[{stage_label}] " if stage_label else ''
                    proc = subprocess.Popen(
                        embed_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        cwd=temp_dir,  # 在临时目录执行
                        encoding='utf-8',
                        errors='replace'  # 遇到无法解码的字符时用?替换
                    )

                    # 创建线程来读取输出，避免管道阻塞
                    stage_output_queue = queue.Queue()
                    stage_error_queue = queue.Queue()

                    def read_stream(stream, target_queue, stream_name):
                        try:
                            if stream is not None:
                                for raw_line in stream:
                                    target_queue.put(raw_line.strip())
                            else:
                                task_logger.warning(f"{stage_suffix}process.{stream_name} 为 None，无法读取输出")
                        except Exception:
                            pass
                        finally:
                            target_queue.put(None)

                    # 启动读取线程
                    output_thread = threading.Thread(
                        target=read_stream,
                        args=(proc.stdout, stage_output_queue, 'stdout'),
                        daemon=True,
                    )
                    error_thread = threading.Thread(
                        target=read_stream,
                        args=(proc.stderr, stage_error_queue, 'stderr'),
                        daemon=True,
                    )
                    output_thread.start()
                    error_thread.start()

                    # 实时解析进度
                    stage_last_time = 0
                    stage_start_time = time.time()
                    stage_last_progress_time = stage_start_time
                    stage_error_messages = []
                    stage_timed_out = False

                    while True:
                        if is_task_cancelled(task_id):
                            task_logger.info(f"{stage_suffix}检测到任务取消请求，终止FFmpeg转码")
                            proc.terminate()
                            try:
                                proc.wait(timeout=PROCESS_TERMINATE_WAIT_SECONDS)
                            except subprocess.TimeoutExpired:
                                if proc.poll() is None:
                                    proc.kill()
                            raise TaskCancelledError("任务已取消")
                        # 检查超时
                        if time.time() - stage_start_time > run_timeout:
                            task_logger.error(f"{stage_suffix}FFmpeg处理超时（{run_timeout//60}分钟），强制终止")
                            # 标记「这是超时」而不是靠错误文本推断：超时被杀时
                            # stderr 里没有设备/参数类关键字，按「未知错误」处理会
                            # 让长视频先按同一硬编器把整片再跑一遍（最现实的失败
                            # 类型反而拿到最差的降级策略）。
                            stage_timed_out = True
                            proc.terminate()
                            try:
                                proc.wait(timeout=PROCESS_TERMINATE_WAIT_SECONDS)
                            except subprocess.TimeoutExpired:
                                if proc.poll() is None:
                                    proc.kill()
                            break

                        # 检查进程状态
                        if proc.poll() is not None:
                            break

                        # 读取输出
                        try:
                            line = stage_output_queue.get(timeout=1)
                            if line is None:
                                break

                            if line.startswith('out_time_us='):
                                try:
                                    # 解析当前处理时间（微秒）
                                    time_us = int(line.split('=')[1])
                                    current_time = time_us / 1000000.0  # 转换为秒

                                    if video_duration and current_time > stage_last_time:
                                        progress = min((current_time / video_duration) * 100, 100)
                                        # 更新任务进度显示
                                        update_task(task_id, upload_progress=f"{progress:.1f}%", silent=True)
                                        stage_last_time = current_time
                                        stage_last_progress_time = time.time()
                                except (ValueError, IndexError):
                                    continue
                        except queue.Empty:
                            # 检查是否长时间没有进度更新（可能卡死了）
                            if time.time() - stage_last_progress_time > 300:  # 5分钟没有进度更新
                                task_logger.debug(f"{stage_suffix}长时间没有进度更新，可能处理卡死")
                            continue

                        # 读取错误信息
                        try:
                            error_line = stage_error_queue.get_nowait()
                            if error_line:
                                stage_error_messages.append(error_line)
                                if len(stage_error_messages) > 50:  # 限制错误信息数量
                                    stage_error_messages.pop(0)
                        except queue.Empty:
                            pass

                    # 等待进程完成
                    try:
                        proc.wait(timeout=30)  # 最多等待30秒
                    except subprocess.TimeoutExpired:
                        task_logger.error(f"{stage_suffix}进程未能在30秒内正常结束，强制终止")
                        proc.kill()

                    # 进程退出后必须把管线里剩余的错误行排空：读取线程只 start 不
                    # join 时，`Unrecognized option` / `Error setting option` 这类
                    # 参数错误恰好落在 stderr 最后几行，会被竞态漏掉 —— 而它们正是
                    # 分级降级决策依赖的信号。先 join 让线程读到 EOF，再排空队列。
                    for stream_thread in (output_thread, error_thread):
                        try:
                            stream_thread.join(timeout=1.0)
                        except Exception:
                            pass
                    for _ in range(200):
                        try:
                            drained = stage_error_queue.get_nowait()
                        except queue.Empty:
                            break
                        if drained:
                            stage_error_messages.append(drained)
                            if len(stage_error_messages) > 50:  # 限制错误信息数量
                                stage_error_messages.pop(0)

                    error_text = '\n'.join(stage_error_messages)
                    return proc.returncode, error_text, os.path.exists(simple_output), stage_timed_out

                # 超时预算必须从**首次尝试之前**开始计：此前 deadline 在首轮返回
                # 之后才建立，首轮已消耗的时间没有从预算里扣掉。
                embed_started_at = time.monotonic()
                first_stage_budget = max(300.0, float(timeout))
                process_returncode, error_output_full, output_ready, first_timed_out = _execute_embed(
                    cmd, first_stage_budget
                )

                if process_returncode == 0 and output_ready:
                    # 成功：优先原子替换，避免重复复制大文件
                    self._finalize_embedded_video_output(simple_output, embedded_video_path)
                    _log_output_media_summary(embedded_video_path)
                    # 结束日志：成功
                    task_logger.info(f"字幕嵌入完成: {embedded_video_path}")

                    # 清除进度显示并恢复之前的状态
                    update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                    return embedded_video_path

                # 收集错误信息
                error_output_tail = error_output_full.strip() or "无详细错误信息"
                task_logger.error(f"字幕嵌入失败 (返回码: {process_returncode})")
                # 必须 join：直接把 splitlines() 的 list 插进 f-string 会打印
                # Python repr（`['...', '...']`），硬编失败时日志几乎不可读。
                task_logger.error(
                    "错误信息(尾部): %s", '\n'.join(error_output_tail.splitlines()[-50:])
                )

                def _rebuild_hw_params(boost_enabled):
                    """按同一硬件编码器重建参数（用于关闭质量增强后的降级重试）。

                    通过 ``hw_quality_boost`` 覆盖通道传值，不改共享 dict ——
                    就地改写会让后面的 CPU 回退阶段也读到「增强已关」。
                    """
                    if actual_encoder == 'nvidia':
                        return build_nvidia_params(hw_quality_boost=boost_enabled)
                    if actual_encoder == 'intel':
                        return build_intel_params(hw_quality_boost=boost_enabled)
                    if actual_encoder == 'amd':
                        return build_amd_params(hw_quality_boost=boost_enabled)
                    return build_cpu_params(hw_quality_boost=boost_enabled)

                # 分级降级：硬编+质量增强失败时，先尝试关闭增强保留硬件加速，
                # 仍失败才退回 CPU。直接跳 CPU 会让可用 GPU 白白闲置。
                # 但设备/驱动类错误关掉增强没有任何作用，直接走 CPU ——
                # 否则会按同一硬编器把整部视频白跑一遍。
                hw_error_detected = self._is_known_hw_encoder_error(error_output_full)
                hw_option_error = self._is_hw_option_error(error_output_full)
                boost_enabled = bool(encoder_settings.get('hw_quality_boost'))
                if actual_encoder in ('nvidia', 'intel', 'amd'):
                    failure_kind = self._describe_hw_failure(
                        actual_encoder,
                        timed_out=first_timed_out,
                        hw_option_error=hw_option_error,
                        hw_error_detected=hw_error_detected,
                        boost_enabled=boost_enabled,
                        returncode=process_returncode,
                    )
                    if failure_kind == 'timeout':
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 处理超时，跳过关增强重试直接回退 CPU 编码"
                        )
                    elif failure_kind == 'option_error':
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 不接受某个选项，"
                            f"先关闭质量增强后用同一编码器重试"
                        )
                    elif failure_kind == 'option_error_no_boost':
                        # 质量增强本来就没开，「先关增强重试」是空动作：此前的日志
                        # 照搬这句话，会把人往错的方向带（关闭后仍会失败）。
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 不接受某个选项，且质量增强未开启，"
                            f"直接回退 CPU 编码"
                        )
                    elif failure_kind == 'device_error':
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 报设备/驱动类错误，"
                            f"关闭质量增强无效，直接回退 CPU 编码"
                        )
                    else:
                        task_logger.warning(
                            f"硬件编码器 {actual_encoder} 失败（返回码: {process_returncode}），"
                            f"未检测到已知硬件错误，仍尝试降级重试"
                        )
                retry_stages = self._resolve_embed_retry_stages(
                    actual_encoder,
                    encoder_settings.get('hw_quality_boost'),
                    hw_error_detected,
                    hw_option_error,
                    cpu_codec,
                )
                if first_timed_out:
                    retry_stages = self._drop_same_encoder_stage_after_timeout(retry_stages)

                # 总预算 = 首次尝试 + 至多一轮完整降级，从首次尝试**之前**开始计。
                # 单阶段上限为 first_stage_budget，因此最坏总时长 ≈ 2 × 预估；
                # 此前 deadline 建在首轮返回之后（首轮不计入）且每阶段各自重置，
                # 最坏可到 3 倍。x265 降级到 x264 反而更快，沿用同一预算即可。
                overall_deadline = embed_started_at + 2 * first_stage_budget

                for stage in retry_stages:
                    if is_task_cancelled(task_id):
                        task_logger.info("检测到任务取消请求，跳过FFmpeg回退方案")
                        raise TaskCancelledError("任务已取消")

                    remaining_s = overall_deadline - time.monotonic()
                    if remaining_s <= 0:
                        task_logger.warning(
                            "回退总超时预算已耗尽，跳过剩余降级阶段: %s",
                            '/'.join(retry_stages[retry_stages.index(stage):]),
                        )
                        break

                    if stage == 'hw_no_boost':
                        stage_label = f"{actual_encoder} 关闭质量增强"
                        task_logger.warning("硬件编码或质量增强参数不被支持，尝试关闭质量增强后用同一编码器重试...")
                        stage_vparams = _rebuild_hw_params(False)
                        stage_filter = final_vf
                        stage_color_params = list(color_params)
                    elif stage == 'cpu_x264':
                        # x265 不可用（未编进 FFmpeg）或其参数不被接受：改用 libx264
                        # 重跑。滤镜链与通用色彩选项不变，只有软件编码实现换掉。
                        stage_label = 'CPU 软编码 (libx265 回退至 libx264)'
                        task_logger.warning(
                            "libx265 不可用或其参数不被接受，改用 libx264 软编码重试..."
                        )
                        stage_vparams = build_cpu_params(cpu_codec_override='x264')
                        stage_filter = vf_filter
                        stage_color_params = list(color_params)
                    else:
                        stage_label = 'CPU 软编码'
                        task_logger.warning("尝试使用CPU编码回退方案...")
                        stage_vparams = build_cpu_params()
                        stage_filter = vf_filter
                        stage_color_params = list(color_params)

                    # 每个阶段只拿「总预算减去已用掉的部分」，且单阶段不得超过一次
                    # 完整预算（否则前一级会把 CPU 回退的份额吃光）；用 60s 兜底
                    # 避免预算耗尽时把阶段压成 0。
                    stage_timeout = int(max(60.0, min(remaining_s, first_stage_budget)))

                    cmd_retry = self._build_embed_ffmpeg_cmd(
                        ffmpeg_bin=ffmpeg_bin,
                        input_video=simple_video,
                        vf_filter=stage_filter,
                        vparams=stage_vparams,
                        aparams=aparams,
                        output_video=simple_output,
                        color_params=stage_color_params,
                    )
                    task_logger.debug(f"回退FFmpeg命令({stage_label}): {' '.join(cmd_retry)}")

                    retry_returncode, retry_error, retry_ready, _stage_timed_out = _execute_embed(
                        cmd_retry, stage_timeout, stage_label
                    )
                    if retry_returncode == 0 and retry_ready:
                        self._finalize_embedded_video_output(simple_output, embedded_video_path)
                        _log_output_media_summary(embedded_video_path)
                        task_logger.info(f"字幕嵌入完成: {embedded_video_path}（{stage_label}回退）")
                        update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                        return embedded_video_path

                    task_logger.error(f"{stage_label}回退方案仍然失败 (返回码: {retry_returncode})")
                    if retry_error:
                        task_logger.error(f"FFmpeg错误({stage_label}回退): {retry_error.splitlines()[-50:]}")

                    # 清理上一阶段可能残留的半成品输出，避免下一次重试用旧文件误判成功
                    try:
                        if os.path.exists(simple_output):
                            os.remove(simple_output)
                    except Exception as cleanup_exc:
                        task_logger.warning(f"清理回退残留输出失败: {cleanup_exc}")

                update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                return None
            
            finally:
                # 清理残留临时输出
                try:
                    if simple_output and os.path.exists(simple_output):
                        os.remove(simple_output)
                except Exception as e:
                    task_logger.warning(f"清理临时输出文件失败: {e}")

                # 清理临时目录
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    task_logger.warning(f"清理临时目录失败: {e}")
                    
        except subprocess.TimeoutExpired:
            task_logger.error("FFmpeg执行超时")
            update_task(task_id, upload_progress=None, status=previous_status, silent=True)
            return None
        except FileNotFoundError:
            task_logger.error("FFmpeg未安装或不在PATH中")
            update_task(task_id, upload_progress=None, status=previous_status, silent=True)
            return None
        except Exception as e:
            task_logger.error(f"嵌入字幕时发生错误: {str(e)}")
            update_task(task_id, upload_progress=None, status=previous_status, silent=True)
            return None

    def _get_video_duration(self, video_path, task_logger):
        """获取视频时长（秒）"""
        try:
            # subprocess imported at module level
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin:
                raise FileNotFoundError('ffmpeg not found')
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                raise FileNotFoundError('ffprobe not found')
            cmd = [
                ffprobe_bin, '-v', 'quiet', '-print_format', 'json', 
                '-show_format', video_path
            ]
            
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                encoding='utf-8',
                errors='replace',
                timeout=60  # 添加60秒超时
            )
            
            if result.returncode == 0:
                import json
                data = json.loads(result.stdout)
                duration = float(data['format']['duration'])
                task_logger.info(f"视频时长: {duration:.2f} 秒")
                return duration
            else:
                task_logger.warning("无法获取视频时长，将无法显示转码进度")
                return None
        except subprocess.TimeoutExpired:
            task_logger.warning("获取视频时长超时")
            return None

    def _get_video_stream_info(self, video_path, task_logger):
        """获取视频流分辨率/帧率/像素格式/编码信息。"""
        # 使用类型注解明确字典值的类型
        info: dict[str, float | int | str | None] = {
            "width": None,
            "height": None,
            "fps": None,
            "pix_fmt": None,
            "codec_name": None,
            "bit_rate": None,
            # 色彩元数据：转码时透传，避免播放器按默认色域解释导致偏色
            "color_space": None,
            "color_primaries": None,
            "color_transfer": None,
            "color_range": None,
        }
        try:
            # subprocess/json handled at module level where needed
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin:
                raise FileNotFoundError('ffmpeg not found')
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                raise FileNotFoundError('ffprobe not found')
            cmd = [
                ffprobe_bin, '-v', 'quiet', '-print_format', 'json',
                '-select_streams', 'v:0', '-show_streams', video_path
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                streams = data.get('streams', [])
                if streams:
                    s = streams[0]
                    # 确保 info 字典不是 None
                    if info is not None:
                        info['width'] = s.get('width')
                        info['height'] = s.get('height')
                        info['pix_fmt'] = s.get('pix_fmt')
                        info['codec_name'] = s.get('codec_name')
                        info['color_space'] = s.get('color_space')
                        info['color_primaries'] = s.get('color_primaries')
                        info['color_transfer'] = s.get('color_transfer')
                        info['color_range'] = s.get('color_range')
                        bit_rate = s.get('bit_rate')
                        if bit_rate:
                            try:
                                info['bit_rate'] = int(bit_rate)
                            except Exception:
                                pass
                        r = s.get('avg_frame_rate') or s.get('r_frame_rate')
                        if r and r != '0/0':
                            try:
                                num, den = r.split('/')
                                fps = float(num) / float(den) if float(den) != 0 else 0
                                if fps > 1:
                                    # 使用类型断言解决类型问题
                                    info['fps'] = fps  # type: ignore
                            except Exception:
                                pass
            task_logger.info(f"视频流信息: {info}")
        except subprocess.TimeoutExpired:
            task_logger.warning("获取视频流信息超时")
        except Exception as e:
            task_logger.warning(f"获取视频流信息失败: {str(e)}")
        return info

    def _get_audio_stream_info(self, video_path, task_logger):
        """获取音频流信息（编码/码率/采样率等），用于音频参数设置。"""
        info: dict[str, int | str | None] = {
            "codec_name": None,
            "bit_rate": None,
            "sample_rate": None,
            "channels": None,
            "sample_fmt": None,
        }
        try:
            # subprocess/json handled at module level where needed
            ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
            if not ffmpeg_bin:
                raise FileNotFoundError('ffmpeg not found')
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=task_logger)
            if not ffprobe_bin:
                raise FileNotFoundError('ffprobe not found')
            cmd = [
                ffprobe_bin, '-v', 'quiet', '-print_format', 'json',
                '-select_streams', 'a:0', '-show_streams', video_path
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                streams = data.get('streams', [])
                if streams:
                    s = streams[0]
                    info['codec_name'] = s.get('codec_name')
                    bit_rate = s.get('bit_rate')
                    try:
                        if bit_rate:
                            info['bit_rate'] = int(bit_rate)
                    except Exception:
                        pass
                    sr = s.get('sample_rate')
                    try:
                        if sr:
                            info['sample_rate'] = int(sr)
                    except Exception:
                        pass
                    info['channels'] = s.get('channels')
                    info['sample_fmt'] = s.get('sample_fmt')
            task_logger.info(f"音频流信息: {info}")
        except subprocess.TimeoutExpired:
            task_logger.warning("获取音频流信息超时")
        except Exception as e:
            task_logger.warning(f"获取音频流信息失败: {str(e)}")
        return info

    def _generate_tags(self, task_id, task_logger):
        """生成视频标签"""
        from modules.ai_enhancer import generate_acfun_tags
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始生成视频标签")
        update_task(task_id, status=TASK_STATES['TAGGING'])
        
        # 优先使用翻译后的元数据，避免将原始导流文本继续送给标签模型
        from modules.utils import safe_str
        title = safe_str(task.get('video_title_translated') or task.get('video_title_original'))
        description = safe_str(task.get('description_translated') or task.get('description_original'))
        
        # 构建OpenAI配置
        openai_config = {
            'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''),
            'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''),
            'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
            'OPENAI_THINKING_ENABLED': self.config.get('OPENAI_THINKING_ENABLED', False),
            # 兜底端点：显式透传，确保 get_ai_client 能拿到（统一客户端也会从全局配置补齐）
            'FALLBACK_OPENAI_API_KEY': self.config.get('FALLBACK_OPENAI_API_KEY', ''),
            'FALLBACK_OPENAI_BASE_URL': self.config.get('FALLBACK_OPENAI_BASE_URL', ''),
            'FALLBACK_OPENAI_MODEL_NAME': self.config.get('FALLBACK_OPENAI_MODEL_NAME', ''),
            'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', ''),
        }
        
        if self.config.get('GENERATE_TAGS', True) and (title or description):
            tags = generate_acfun_tags(
                title, 
                description, 
                openai_config=openai_config,
                task_id=task_id
            )
            # 限制标签数量不超过6个
            if tags:
                tags = tags[:6]
                update_task(task_id, tags_generated=json.dumps(tags, ensure_ascii=False))
            else:
                task_logger.warning("标签生成失败")
                update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False))
        else:
            task_logger.warning("标签生成已禁用或缺少必要信息")
            update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False))
            
        task_logger.info(f"标签生成完成: {task.get('tags_generated', '[]')}")
        return True
    
    def _recommend_partition(self, task_id, task_logger):
        """推荐视频分区（平台感知：AcFun/Bilibili）"""
        from modules.ai_enhancer import (
            recommend_acfun_partition,
            recommend_bilibili_partition,
            recommend_partitions_aio,
        )

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return

        task_logger.info("开始推荐视频分区")
        update_task(task_id, status=TASK_STATES['PARTITIONING'])

        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        title_original = task.get('video_title_original', '') or ''
        description_original = task.get('description_original', '') or ''
        title_translated = task.get('video_title_translated', '') or ''
        description_translated = task.get('description_translated', '') or ''
        tags_generated = _normalize_tags_list(task.get('tags_generated'))
        if task.get('tags_generated') and not tags_generated:
            task_logger.warning("解析 AI 标签失败或标签为空，分区推荐将忽略标签上下文")
        upload_target = _get_task_upload_target(task)

        openai_config = {
            'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''),
            'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''),
            'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
            'OPENAI_THINKING_ENABLED': self.config.get('OPENAI_THINKING_ENABLED', False),
            'OPENAI_TIMEOUT_SECONDS': self.config.get('OPENAI_TIMEOUT_SECONDS', 600),
            # 兜底端点：显式透传，确保 get_ai_client 能拿到（统一客户端也会从全局配置补齐）
            'FALLBACK_OPENAI_API_KEY': self.config.get('FALLBACK_OPENAI_API_KEY', ''),
            'FALLBACK_OPENAI_BASE_URL': self.config.get('FALLBACK_OPENAI_BASE_URL', ''),
            'FALLBACK_OPENAI_MODEL_NAME': self.config.get('FALLBACK_OPENAI_MODEL_NAME', ''),
            'FIXED_PARTITION_ID': self.config.get('FIXED_PARTITION_ID', ''),
            'FIXED_PARTITION_ID_BILIBILI': self.config.get('FIXED_PARTITION_ID_BILIBILI', ''),
            'RECOMMEND_PARTITION_WITH_COVER': self.config.get('RECOMMEND_PARTITION_WITH_COVER', False),
        }
        cover_path = task.get('cover_path_local', '')

        task_logger.info(f"RECOMMEND_PARTITION设置: {self.config.get('RECOMMEND_PARTITION', False)}")
        from modules.utils import safe_str
        task_logger.info(f"标题长度: {len(safe_str(title))}, 描述长度: {len(safe_str(description))}")
        task_logger.info(f"任务目标平台: {upload_target}")

        if not self.config.get('RECOMMEND_PARTITION', False):
            task_logger.info("分区推荐功能已禁用，跳过推荐")
            return True

        if not (title or description):
            task_logger.warning("缺少标题和描述，无法进行分区推荐")
            return True

        targets_to_recommend = []
        if upload_target == UPLOAD_TARGET_BOTH:
            targets_to_recommend = [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]
        elif upload_target == UPLOAD_TARGET_BILIBILI:
            targets_to_recommend = [UPLOAD_TARGET_BILIBILI]
        else:
            targets_to_recommend = [UPLOAD_TARGET_ACFUN]

        platform_results = {}
        zone_data = []
        id_mapping_data = []

        if UPLOAD_TARGET_BILIBILI in targets_to_recommend:
            try:
                from .bilibili_zones import get_zone_list_sub
                zone_data = get_zone_list_sub()
                task_logger.info(f"成功读取bilibili分区数据，长度: {len(zone_data)}")
            except Exception as e:
                task_logger.error(f"读取bilibili分区数据失败: {e}")

        if UPLOAD_TARGET_ACFUN in targets_to_recommend:
            from .utils import get_app_subdir
            id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
            task_logger.info(f"尝试读取 AcFun 分区映射文件: {id_mapping_path}")
            try:
                if not os.path.exists(id_mapping_path):
                    task_logger.error(f"分区映射文件不存在: {id_mapping_path}")
                else:
                    with open(id_mapping_path, 'r', encoding='utf-8') as f:
                        id_mapping_data = json.load(f)
                    task_logger.info(f"成功读取 AcFun 分区映射文件，包含 {len(id_mapping_data)} 个分类")
            except Exception as e:
                task_logger.error(f"读取 AcFun 分区ID映射失败: {str(e)}")
                id_mapping_data = []

        if targets_to_recommend == [UPLOAD_TARGET_ACFUN, UPLOAD_TARGET_BILIBILI]:
            if not id_mapping_data:
                task_logger.warning("AcFun 分区映射数据为空，跳过AcFun推荐")
            if not zone_data:
                task_logger.warning("bilibili分区数据为空，跳过bilibili推荐")
            platform_results = recommend_partitions_aio(
                title,
                description,
                acfun_id_mapping_data=id_mapping_data,
                bilibili_zone_data=zone_data,
                title_original=title_original,
                description_original=description_original,
                title_translated=title_translated,
                description_translated=description_translated,
                tags=tags_generated,
                openai_config=openai_config,
                task_id=task_id,
                cover_path=cover_path,
                include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False),
            )
        else:
            for platform in targets_to_recommend:
                partition_selection = None
                if platform == UPLOAD_TARGET_BILIBILI:
                    if not zone_data:
                        task_logger.warning("bilibili分区数据为空，跳过bilibili推荐")
                        platform_results[platform] = {}
                        continue
                    partition_selection = recommend_bilibili_partition(
                        title,
                        description,
                        zone_data,
                        title_original=title_original,
                        description_original=description_original,
                        title_translated=title_translated,
                        description_translated=description_translated,
                        tags=tags_generated,
                        openai_config=openai_config,
                        task_id=task_id,
                        cover_path=cover_path,
                        include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False),
                    )
                else:
                    if not id_mapping_data:
                        task_logger.warning("AcFun 分区映射数据为空，跳过AcFun推荐")
                        platform_results[platform] = {}
                        continue
                    partition_selection = recommend_acfun_partition(
                        title,
                        description,
                        id_mapping_data,
                        title_original=title_original,
                        description_original=description_original,
                        title_translated=title_translated,
                        description_translated=description_translated,
                        tags=tags_generated,
                        openai_config=openai_config,
                        task_id=task_id,
                        cover_path=cover_path,
                        include_cover_for_ai=self.config.get('RECOMMEND_PARTITION_WITH_COVER', False),
                    )

                platform_results[platform] = partition_selection or {}

        for platform in targets_to_recommend:
            partition_selection = platform_results.get(platform) or {}
            recommended_partition_id = str(partition_selection.get('id') or '').strip()
            content_profile = partition_selection.get('content_profile') or {}
            if content_profile:
                task_logger.info(
                    "%s content_profile: domain=%s, subdomain=%s, format=%s, game_mode=%s, interview=%s, confidence=%s, reason=%s, entities=%s",
                    platform,
                    content_profile.get('domain'),
                    content_profile.get('subdomain'),
                    content_profile.get('content_format'),
                    content_profile.get('game_mode'),
                    content_profile.get('is_interview'),
                    content_profile.get('confidence'),
                    content_profile.get('reason_summary'),
                    content_profile.get('entities'),
                )
            if recommended_partition_id:
                selected_field = _get_partition_field_name(platform, 'selected')
                recommended_field = _get_partition_field_name(platform, 'recommended')

                latest_task = get_task(task_id) or {}
                updates: Dict[str, Any] = {recommended_field: recommended_partition_id}
                if not str(latest_task.get(selected_field) or '').strip():
                    updates[selected_field] = recommended_partition_id

                update_task(task_id, **updates)
                task_logger.info(
                    "%s 获取到推荐分区并已更新任务: source=%s, id=%s, confidence=%s, alternatives=%s, low_confidence=%s, reason=%s",
                    platform,
                    partition_selection.get('source'),
                    recommended_partition_id,
                    partition_selection.get('confidence'),
                    partition_selection.get('alternatives') or [],
                    partition_selection.get('low_confidence'),
                    partition_selection.get('reason_summary') or '',
                )
            else:
                if not openai_config.get('OPENAI_API_KEY'):
                    task_logger.warning(f"{platform} 分区推荐未命中：未配置OpenAI且规则匹配失败")
                else:
                    task_logger.warning(f"{platform} 分区推荐未命中：OpenAI与规则匹配均失败")

        task_logger.info(f"分区推荐流程完成，结果: {platform_results}")
        return True
    
    def _moderate_content(self, task_id, task_logger):
        """内容审核"""
        from modules.content_moderator import AlibabaCloudModerator
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始内容审核")
        update_task(task_id, status=TASK_STATES['MODERATING'])
        
        # 优先使用翻译后的标题和描述，如果没有则使用原始内容
        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        
        # 获取AI生成的标签
        tags_string = ""
        tags_list = [] # 初始化 tags_list
        if task.get('tags_generated'):
            try:
                tags_list = json.loads(task.get('tags_generated', '[]'))
                if tags_list:
                    # 用于附加到描述的字符串可以保持原样，或者根据需要调整
                    # tags_string = "，标签：" + "，".join(tags_list) 
                    pass # 暂时不修改附加到描述的逻辑，主要确保tags_list被正确赋值
            except json.JSONDecodeError:
                task_logger.warning("解析AI生成标签失败，内容审核时将不包含标签。")

        # 预处理内容，过滤掉URL等推广内容
        url_pattern = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+'
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        
        filtered_title = re.sub(url_pattern, '', title)
        filtered_title = re.sub(email_pattern, '', filtered_title)
        
        # 将标签附加到描述文本后进行审核 (这部分可以保留，也可以考虑是否还需要)
        # description_with_tags = description + tags_string 
        # 为了更清晰，我们先只审核原始描述，标签单独审核
        from modules.utils import safe_str
        filtered_description = re.sub(url_pattern, '', safe_str(description))
        filtered_description = re.sub(email_pattern, '', filtered_description)
        filtered_description = re.sub(r'\\n{3,}', '\\n\\n', filtered_description)
        
        task_logger.info("已过滤标题和描述中的URL和邮箱地址")
        task_logger.info(f"用于审核的描述文本: {filtered_description[:200]}...") # 日志记录部分内容

        # 阿里云配置
        aliyun_config = {
            'ALIYUN_ACCESS_KEY_ID': self.config.get('ALIYUN_ACCESS_KEY_ID', ''),
            'ALIYUN_ACCESS_KEY_SECRET': self.config.get('ALIYUN_ACCESS_KEY_SECRET', ''),
            'ALIYUN_CONTENT_MODERATION_REGION': self.config.get('ALIYUN_CONTENT_MODERATION_REGION', 'cn-shanghai')
        }
        
        text_moderation_service = self.config.get('ALIYUN_TEXT_MODERATION_SERVICE', 'comment_detection')
        task_logger.info(f"使用阿里云文本审核服务类型: {text_moderation_service}")

        moderator = AlibabaCloudModerator(aliyun_config, task_id)
        
        title_result = moderator.moderate_text(filtered_title, service_type=text_moderation_service)
        task_logger.info(f"标题审核结果: {title_result}")
        
        description_result = moderator.moderate_text(filtered_description, service_type=text_moderation_service)
        task_logger.info(f"描述审核结果: {description_result}")

        tags_for_moderation_string = ""
        if tags_list:
            tags_for_moderation_string = "，".join(tags_list) # 将标签列表转换为逗号分隔的字符串进行审核
            task_logger.info(f"用于审核的标签文本: {tags_for_moderation_string[:200]}...")
        
        tags_moderation_result = {"pass": True, "details": [{"label": "skipped", "suggestion": "pass", "reason": "没有生成标签或标签为空"}]}
        if tags_for_moderation_string:
            tags_moderation_result = moderator.moderate_text(tags_for_moderation_string, service_type=text_moderation_service)
            task_logger.info(f"标签审核结果: {tags_moderation_result}")
        else:
            task_logger.info("没有标签需要审核。")
        
        cover_result = {"pass": True, "details": [{"label": "skipped", "suggestion": "pass", "reason": "封面审核已禁用"}]}
        
        moderation_result = {
            "title": title_result,
            "description": description_result,
            "tags": tags_moderation_result, # 添加标签审核结果
            "cover": cover_result,
            "overall_pass": title_result.get("pass", True) and description_result.get("pass", True) and tags_moderation_result.get("pass", True) # 整体通过需要标签也通过
        }
        
        task_logger.info(f"综合审核结果: overall_pass={moderation_result['overall_pass']}")
        if not moderation_result["overall_pass"]:
            if not title_result.get("pass", True):
                task_logger.warning("标题未通过审核")
                for detail in title_result.get("details", []):
                    task_logger.warning(f"标题问题: {detail.get('label')} - {detail.get('reason')}")
                    
            if not description_result.get("pass", True):
                task_logger.warning("描述未通过审核")
                for detail in description_result.get("details", []):
                    task_logger.warning(f"描述问题: {detail.get('label')} - {detail.get('reason')}")
            
            if not tags_moderation_result.get("pass", True):
                task_logger.warning("标签未通过审核")
                for detail in tags_moderation_result.get("details", []):
                    task_logger.warning(f"标签问题: {detail.get('label')} - {detail.get('reason')}")
        
        update_task(
            task_id,
            moderation_result=json.dumps(moderation_result, ensure_ascii=False)
        )
        
        if moderation_result["overall_pass"]:
            task_logger.info("内容审核通过")
        else:
            task_logger.info("内容审核不通过，需要人工审核")
            update_task(
                task_id,
                status=TASK_STATES['AWAITING_REVIEW'],
                error_category=CONTENT_MODERATION_ERROR_CATEGORY,
            )

    def _get_embedded_video_candidate(self, video_path: str, subtitle_paths=None) -> str:
        """返回可复用的带字幕视频路径。

        复用的前提是产物不能比字幕文件更旧 —— 否则字幕已重新生成，
        旧的 ``_with_subtitle.mp4`` 会把错误字幕继续带到成片里。
        """
        def _is_fresh(candidate: str) -> bool:
            if not candidate or not os.path.exists(candidate):
                return False
            try:
                candidate_mtime = os.path.getmtime(candidate)
            except OSError:
                return False
            for subtitle_path in (subtitle_paths or []):
                if not subtitle_path or not os.path.exists(subtitle_path):
                    continue
                try:
                    if os.path.getmtime(subtitle_path) > candidate_mtime + 1e-6:
                        return False
                except OSError:
                    continue
            return True

        if not video_path:
            return ''
        if os.path.exists(video_path):
            base_name_no_ext = os.path.splitext(os.path.basename(video_path))[0]
            if base_name_no_ext.endswith('_with_subtitle') and _is_fresh(video_path):
                return video_path
        video_dir = os.path.dirname(video_path)
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        candidate = os.path.join(video_dir, f"{video_name}_with_subtitle.mp4")
        return candidate if _is_fresh(candidate) else ''

    def _resolve_existing_subtitle_assets(self, task_id: str, task: dict, task_dir: str):
        def _existing_path(value):
            path_value = str(value or '').strip()
            return path_value if path_value and os.path.exists(path_value) else ''

        subtitle_path_original = _existing_path(task.get('subtitle_path_original'))
        subtitle_path_translated = _existing_path(task.get('subtitle_path_translated'))
        subtitle_language = str(task.get('subtitle_language_detected') or '').strip().lower()

        if not subtitle_path_translated:
            translated_candidate = os.path.join(task_dir, f"translated_{task_id}.srt")
            if os.path.exists(translated_candidate):
                subtitle_path_translated = translated_candidate

        if not subtitle_path_original:
            for ext in ('.srt', '.vtt'):
                asr_candidate = os.path.join(task_dir, f"asr_{task_id}{ext}")
                if os.path.exists(asr_candidate):
                    subtitle_path_original = asr_candidate
                    break

        subtitle_candidates = []
        try:
            for name in os.listdir(task_dir):
                if not isinstance(name, str):
                    continue
                lower = name.lower()
                if lower.endswith('.srt') or lower.endswith('.vtt'):
                    subtitle_candidates.append(os.path.join(task_dir, name))
        except Exception:
            subtitle_candidates = []

        subtitle_candidates = sorted(set(subtitle_candidates))
        for candidate in subtitle_candidates:
            lower_name = os.path.basename(candidate).lower()
            if lower_name.startswith('translated_'):
                if not subtitle_path_translated:
                    subtitle_path_translated = candidate
                continue
            if not subtitle_path_original:
                subtitle_path_original = candidate

        return subtitle_path_original, subtitle_path_translated, subtitle_language

    def _prepare_subtitle_for_upload(self, task_id, task_logger):
        """上传前字幕处理（ASR/翻译/嵌入）。返回最新任务对象。"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在，无法执行上传前字幕处理")
            return None

        video_path = task.get('video_path_local', '')
        if not video_path or not os.path.exists(video_path):
            task_logger.warning("视频文件不存在，无法进行上传前的字幕处理")
            return get_task(task_id)

        try:
            _raise_if_cancelled(task_id, task_logger)
            task_dir = os.path.dirname(video_path)

            # 门控先于旧产物复用：否则质检失败的标记会被已存在的 _with_subtitle.mp4
            # 完全绕过，成片继续沿用旧的错误字幕。
            #
            # 注意这里是「延迟拦截」而不是直接 return：直接返回会连带阻断下面的
            # ASR 重新生成分支，导致用户修好 VAD 配置后重跑依然拿不到字幕。
            # 正确语义是——既有（可能已被判定不合格的）产物不得复用烧录，
            # 但新一轮 ASR 产物仍可在复位标记并重新质检后烧录。
            embed_quality_state = str(task.get('subtitle_quality_state') or '').strip().lower() or 'ok'
            qc_failed = task.get('subtitle_qc_failed') == 1
            embed_blocked = not _subtitle_embed_allowed(
                self.config,
                embed_quality_state,
                qc_failed,
                task_logger=task_logger,
            )
            if embed_blocked:
                task_logger.warning(
                    "上传前烧录被门控拦截（quality_state=%s, qc_failed=%s）：不会复用既有字幕产物",
                    embed_quality_state,
                    qc_failed,
                )

            config_embed_enabled = _as_bool(self.config.get('SUBTITLE_EMBED_IN_VIDEO', True))
            embed_allowed = config_embed_enabled and not embed_blocked

            reusable_embedded_video = self._get_embedded_video_candidate(
                video_path,
                subtitle_paths=_candidate_subtitle_paths(task, task_dir),
            )
            if reusable_embedded_video and not embed_blocked:
                if reusable_embedded_video != video_path:
                    update_task(task_id, video_path_local=reusable_embedded_video)
                task_logger.info("检测到已存在带字幕视频，复用现有转码产物")
                return get_task(task_id)
            if reusable_embedded_video and embed_blocked:
                task_logger.info("检测到既有带字幕视频，但本次被门控拦截，不复用该转码产物")

            should_embed_subtitle = embed_allowed
            translation_enabled = _as_bool(self.config.get('SUBTITLE_TRANSLATION_ENABLED', False))
            subtitle_path_original, subtitle_path_translated, subtitle_language = self._resolve_existing_subtitle_assets(
                task_id, task, task_dir
            )
            if subtitle_path_original and not subtitle_language:
                subtitle_language = self._detect_subtitle_language(subtitle_path_original)

            reusable_updates = {}
            if subtitle_path_original and task.get('subtitle_path_original') != subtitle_path_original:
                reusable_updates['subtitle_path_original'] = subtitle_path_original
            if subtitle_path_translated and task.get('subtitle_path_translated') != subtitle_path_translated:
                reusable_updates['subtitle_path_translated'] = subtitle_path_translated
            if subtitle_language and task.get('subtitle_language_detected') != subtitle_language:
                reusable_updates['subtitle_language_detected'] = subtitle_language
            if reusable_updates:
                update_task(task_id, **reusable_updates)
                task = get_task(task_id) or task

            if subtitle_path_translated and os.path.exists(subtitle_path_translated):
                if not should_embed_subtitle:
                    task_logger.info("检测到已存在翻译字幕且未开启烧录，复用现有字幕产物")
                    return get_task(task_id)
                # 质检对象必须是**源**字幕：``translated_*.srt`` 不匹配
                # ``asr_*`` 前缀，把它传进质检闸门会被判为「非 ASR 产物」而直接
                # 放行 —— 译文由 ASR 源字幕翻译而来，源头不合格就没有烧录价值。
                # 源字幕缺失时退回译文本身，但必须把来源（任务里记录的源字幕路径）
                # 显式继承过去，否则那条退回分支恒为放行（force_asr 的意义所在）。
                recorded_original = str(task.get('subtitle_path_original') or '')
                if subtitle_path_original and os.path.exists(subtitle_path_original):
                    qc_target = subtitle_path_original
                    qc_force_asr = None
                else:
                    qc_target = subtitle_path_translated
                    qc_force_asr = _is_asr_subtitle_artifact(task_id, recorded_original)
                if not self._ensure_asr_subtitle_qc(
                    task_id, qc_target, task_logger, embed_quality_state, force_asr=qc_force_asr
                )[0]:
                    return get_task(task_id)

                embedded_video_path = self._embed_subtitle_in_video(
                    task_id, video_path, subtitle_path_translated, task_logger
                )
                if embedded_video_path:
                    update_task(
                        task_id,
                        video_path_local=embedded_video_path,
                        subtitle_path_original=subtitle_path_original,
                        subtitle_path_translated=subtitle_path_translated,
                        subtitle_language_detected=subtitle_language,
                        subtitle_warning_message=None,
                    )
                    task_logger.info("检测到已存在翻译字幕，仅补做烧录")
                else:
                    task_logger.warning("复用已有翻译字幕烧录失败，保留原视频继续上传")
                    update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                return get_task(task_id)

            if subtitle_path_original and (str(subtitle_language or '').startswith('zh') or not translation_enabled):
                if not should_embed_subtitle:
                    task_logger.info("检测到已存在字幕且未开启烧录，复用现有字幕产物")
                    return get_task(task_id)
                if not self._ensure_asr_subtitle_qc(
                    task_id, subtitle_path_original, task_logger, embed_quality_state
                )[0]:
                    return get_task(task_id)

                embedded_video_path = self._embed_subtitle_in_video(
                    task_id, video_path, subtitle_path_original, task_logger
                )
                if embedded_video_path:
                    update_task(
                        task_id,
                        video_path_local=embedded_video_path,
                        subtitle_path_original=subtitle_path_original,
                        subtitle_path_translated=None,
                        subtitle_language_detected=subtitle_language or 'zh',
                        subtitle_warning_message=None,
                    )
                    task_logger.info("检测到已存在字幕，仅补做烧录")
                else:
                    task_logger.warning("复用已有字幕烧录失败，保留原视频继续上传")
                    update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                return get_task(task_id)

            if not translation_enabled and (subtitle_path_original or subtitle_path_translated):
                task_logger.info("检测到已有字幕产物但未开启烧录，跳过上传前重复字幕处理")
                return get_task(task_id)

            subtitle_files = []
            try:
                for name in os.listdir(task_dir):
                    if not isinstance(name, str):
                        continue
                    lower = name.lower()
                    if lower.endswith('.srt') or lower.endswith('.vtt'):
                        subtitle_files.append(os.path.join(task_dir, name))
            except Exception:
                pass

            if _is_asr_enabled(self.config):
                if translation_enabled:
                    task_logger.info("上传前执行字幕处理：启用字幕翻译，先尝试ASR/翻译/嵌入")
                    self._translate_subtitle(task_id, task_logger)
                else:
                    if not subtitle_files:
                        task_logger.info("上传前执行字幕处理：启用ASR但未启用字幕翻译，生成基础字幕文件")
                        try:
                            from modules.speech_recognition import create_speech_recognizer_from_config
                            recognizer = create_speech_recognizer_from_config(self.config, task_id)
                        except Exception as e:
                            recognizer = None
                            task_logger.error(f"创建语音识别器失败: {e}")
                        if recognizer:
                            _t2 = get_task(task_id)
                            prev_status2 = _t2['status'] if _t2 else TASK_STATES['UPLOADING']
                            update_task(task_id, status=TASK_STATES['ASR_TRANSCRIBING'])
                            asr_ext = '.srt' if str(self.config.get('SPEECH_RECOGNITION_OUTPUT_FORMAT', 'srt')).lower() == 'srt' else '.vtt'
                            asr_subtitle_path = os.path.join(task_dir, f"asr_{task_id}{asr_ext}")
                            _raise_if_cancelled(task_id, task_logger)
                            # 新一轮 ASR 即将复位全部旧标记，因此本轮产物可按配置重新
                            # 决定是否烧录（不再受入口时的 embed_blocked 约束）。
                            should_embed_subtitle = config_embed_enabled
                            update_task(
                                task_id,
                                asr_warning_message=None,
                                subtitle_qc_failed=0,
                                subtitle_qc_reason=None,
                                subtitle_qc_score=None,
                                subtitle_qc_checked_at=None,
                                subtitle_warning_message=None,
                                subtitle_quality_state=None,
                            )
                            out_path = recognizer.transcribe_video_to_subtitles(video_path, asr_subtitle_path)
                            update_task(task_id, status=prev_status2)
                            quality_state = (
                                str(getattr(recognizer, 'last_quality_state', '') or 'ok').strip().lower()
                                or 'ok'
                            )
                            if out_path and os.path.exists(out_path):
                                detected_lang = self._detect_subtitle_language(out_path)
                                update_task(
                                    task_id,
                                    asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None),
                                    subtitle_quality_state=quality_state,
                                    subtitle_path_original=out_path,
                                    subtitle_path_translated=None,
                                    subtitle_language_detected=detected_lang
                                )
                                task_logger.info(f"ASR 生成基础字幕成功: {os.path.basename(out_path)}")

                                if quality_state == 'failed' and _as_bool(
                                    self.config.get('ASR_FAILURE_BLOCKS_EMBED', True)
                                ):
                                    task_logger.error(
                                        "上传前 ASR 质量结局为 failed，拒绝烧录字幕并继续上传原视频"
                                    )
                                    self._quarantine_rejected_subtitle(out_path, task_logger)
                                    update_task(
                                        task_id,
                                        subtitle_warning_message='asr_failed_block_embed',
                                    )
                                elif should_embed_subtitle:
                                    qc_result = self._run_subtitle_qc(
                                        task_id,
                                        out_path,
                                        task_logger,
                                        strict=(quality_state == 'degraded'),
                                    )
                                    # 质检裁决统一走 ``_qc_result_allows_embed``：
                                    # 用户主动关闭质检（SUBTITLE_QC_DISABLED）按配置放行；
                                    # 「质检没跑成」（None）则由逃生口
                                    # ASR_FAILURE_BLOCKS_EMBED 决定（此前这条路径完全不
                                    # 读逃生口，与 README 的承诺相反）。
                                    if _qc_result_allows_embed(qc_result, self.config):
                                        embedded_video_path = self._embed_subtitle_in_video(
                                            task_id, video_path, out_path, task_logger
                                        )
                                        if embedded_video_path:
                                            update_task(
                                                task_id,
                                                video_path_local=embedded_video_path,
                                                subtitle_path_original=out_path,
                                                subtitle_path_translated=None,
                                                subtitle_language_detected=detected_lang,
                                                subtitle_warning_message=None,
                                            )
                                            task_logger.info("ASR 字幕烧录完成")
                                        else:
                                            task_logger.warning("ASR 字幕烧录失败，保留原视频继续上传")
                                            update_task(task_id, subtitle_warning_message='subtitle_embed_failed')
                                    else:
                                        reason_text = 'qc_unavailable' if qc_result is None else 'subtitle_qc_rejected'
                                        task_logger.warning(f"ASR 字幕质检未通过（{reason_text}），跳过上传前烧录")
                                        self._quarantine_rejected_subtitle(out_path, task_logger)
                                        update_task(task_id, subtitle_warning_message=reason_text)
                            else:
                                update_task(
                                    task_id,
                                    asr_warning_message=(getattr(recognizer, 'last_warning_message', '') or None),
                                    subtitle_quality_state='failed',
                                )
                                self._mark_subtitle_issue(task_id, 'asr_no_subtitle')
                                task_logger.warning("ASR 未能生成字幕，继续上传流程")
                    else:
                        task_logger.info("已存在字幕文件，跳过ASR 生成")
            else:
                task_logger.info("未启用语音识别，跳过上传前的字幕处理")
        except TaskCancelledError:
            task_logger.info("上传前字幕处理检测到任务取消请求")
            raise
        except Exception as e:
            task_logger.error(f"上传前字幕处理出现异常: {e}")

        return get_task(task_id)
    
    def _upload_to_target(self, task_id, task_logger, allow_missing_translations=False):
        """按任务平台分发上传实现。"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return

        if not self._ensure_required_translations_ready(
            task_id,
            task,
            task_logger,
            allow_missing_translations=allow_missing_translations,
        ):
            return

        if allow_missing_translations:
            task = self._ensure_force_upload_metadata_ready(task_id, task_logger)
            if not task:
                return

        upload_target = _get_task_upload_target(task)
        pending_platforms = _get_pending_upload_platforms(task, upload_target)
        task_logger.info(f"上传分发目标平台: {upload_target}")
        task_logger.info(f"待上传平台: {pending_platforms}")

        if not pending_platforms:
            task_logger.info("目标平台均已有上传结果，跳过重复上传")
            if task.get('status') != TASK_STATES['COMPLETED']:
                update_task(task_id, status=TASK_STATES['COMPLETED'], error_message=None, upload_progress=None)
            return

        if upload_target == UPLOAD_TARGET_BOTH:
            task_logger.info("双平台上传将字幕预处理延后到各平台上传阶段执行，确保视频已下载后再处理字幕")
            subtitle_prepared_in_this_round = False

            # 双平台投稿：按 AcFun -> bilibili 顺序执行，且对已成功的平台幂等跳过
            if UPLOAD_TARGET_ACFUN in pending_platforms:
                self._upload_to_acfun(task_id, task_logger, subtitle_prepared=False)
                subtitle_prepared_in_this_round = True
                task = get_task(task_id)
                if not task or task.get('status') == TASK_STATES['FAILED']:
                    return
            else:
                task_logger.info("检测到已有 AcFun 上传结果，跳过 AcFun 上传")

            if UPLOAD_TARGET_BILIBILI in pending_platforms:
                self._upload_to_bilibili(
                    task_id,
                    task_logger,
                    subtitle_prepared=subtitle_prepared_in_this_round
                )
            else:
                task_logger.info("检测到已有 bilibili 上传结果，跳过 bilibili 上传")
            return

        if upload_target == UPLOAD_TARGET_BILIBILI:
            if UPLOAD_TARGET_BILIBILI in pending_platforms:
                self._upload_to_bilibili(task_id, task_logger)
            else:
                task_logger.info("检测到已有 bilibili 上传结果，跳过 bilibili 上传")
            return

        if UPLOAD_TARGET_ACFUN in pending_platforms:
            self._upload_to_acfun(task_id, task_logger)
        else:
            task_logger.info("检测到已有 AcFun 上传结果，跳过 AcFun 上传")

    def _ensure_force_upload_metadata_ready(self, task_id, task_logger):
        """强制上传前继续未完成的 AI 处理阶段。"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在，无法继续上传前 AI 处理流程")
            return None

        from modules.utils import safe_str
        completed_stages = _get_completed_stages(task)

        def _has_usable_text(current_task):
            title_text = safe_str(current_task.get('video_title_translated') or current_task.get('video_title_original'))
            description_text = safe_str(current_task.get('description_translated') or current_task.get('description_original'))
            return bool(title_text or description_text)

        tags = _normalize_tags_list(task.get('tags_generated'))
        if self.config.get('GENERATE_TAGS', True) and not tags:
            if _has_usable_text(task):
                task_logger.info("强制上传前检测到标签为空，继续执行标签生成阶段")
                self._generate_tags(task_id, task_logger)
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)
            else:
                task_logger.warning("强制上传前标签缺失，但缺少标题和简介，无法补生成标签")
                update_task(task_id, tags_generated=json.dumps([], ensure_ascii=False), silent=True)
                task = get_task(task_id) or task
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_GENERATE_TAGS)

        if self.config.get('RECOMMEND_PARTITION', False):
            task = get_task(task_id) or task
            upload_target = _get_task_upload_target(task)
            missing_partition_platforms = [
                platform
                for platform in _get_upload_platforms_for_target(upload_target)
                if not _get_task_partition_id(task, platform, prefer_selected=True)
            ]
            if missing_partition_platforms:
                if _has_usable_text(task):
                    task_logger.info(
                        f"强制上传前检测到分区缺失 {missing_partition_platforms}，继续执行分区推荐阶段"
                    )
                    self._recommend_partition(task_id, task_logger)
                    task = get_task(task_id) or task
                    completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_RECOMMEND_PARTITION)
                else:
                    task_logger.warning("强制上传前分区缺失，但缺少标题和简介，无法补推荐分区")

        if self.config.get('CONTENT_MODERATION_ENABLED', False):
            task = get_task(task_id) or task
            if not task.get('moderation_result'):
                task_logger.info("强制上传前检测到内容审核未完成，继续执行内容审核阶段")
                self._moderate_content(task_id, task_logger)
                task = get_task(task_id) or task
                if task.get('status') == TASK_STATES['AWAITING_REVIEW']:
                    task_logger.info("内容审核需要人工处理，暂停强制上传")
                    return None
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_MODERATE_CONTENT)

        return get_task(task_id) or task

    def _upload_to_bilibili(self, task_id, task_logger, subtitle_prepared=False):
        """上传到 Bilibili - 带并发控制"""
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return

        global upload_semaphore
        if upload_semaphore is None:
            task_logger.warning("upload_semaphore 为 None，正在初始化...")
            init_upload_semaphore(1)
            task_logger.info(f"upload_semaphore 初始化完成，当前值: {upload_semaphore}")
            if upload_semaphore is None:
                task_logger.error("upload_semaphore 初始化失败，无法继续执行任务")
                return

        task_logger.info("等待获取上传锁...")
        try:
            assert upload_semaphore is not None, "upload_semaphore 应该已经初始化"
            with upload_semaphore:
                task_logger.info("获得上传锁，开始上传到 Bilibili")
                self._do_upload_to_bilibili(task_id, task_logger, subtitle_prepared=subtitle_prepared)
                task_logger.info("释放上传锁")
        except Exception as e:
            task_logger.error(f"获取或使用上传锁时出错: {e}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传锁异常: {str(e)}"
            )
            return

    def _upload_to_acfun(self, task_id, task_logger, subtitle_prepared=False):
        """上传到AcFun - 带并发控制"""
        from modules.acfun_uploader import AcfunUploader
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        # 使用信号量控制并发上传
        global upload_semaphore
        if upload_semaphore is None:
            task_logger.warning("upload_semaphore 为 None，正在初始化...")
            init_upload_semaphore(1)
            task_logger.info(f"upload_semaphore 初始化完成，当前值: {upload_semaphore}")
            # 确保初始化成功
            if upload_semaphore is None:
                task_logger.error("upload_semaphore 初始化失败，无法继续执行任务")
                return
        else:
            task_logger.info(f"upload_semaphore 已初始化，当前值: {upload_semaphore}")
        
        task_logger.info("等待获取上传锁...")
        try:
            # 类型断言，告诉 Pylance upload_semaphore 不是 None
            assert upload_semaphore is not None, "upload_semaphore 应该已经初始化"
            with upload_semaphore:
                task_logger.info("获得上传锁，开始上传到AcFun")
                self._do_upload_to_acfun(task_id, task_logger, subtitle_prepared=subtitle_prepared)
                task_logger.info("释放上传锁")
        except Exception as e:
            task_logger.error(f"获取或使用上传锁时出错: {e}")
            import traceback
            task_logger.error(traceback.format_exc())
            # 确保更新任务状态为失败
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传锁异常: {str(e)}"
            )
            return
    
    def _do_upload_to_acfun(self, task_id, task_logger, subtitle_prepared=False):
        """实际执行上传到AcFun的逻辑"""
        from modules.acfun_uploader import AcfunUploader
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        update_task(task_id, status=TASK_STATES['UPLOADING'])
        
        # 获取任务信息
        video_path = task.get('video_path_local', '') if task else ''
        cover_path = task.get('cover_path_local', '') if task else ''
        title = (task.get('video_title_translated', '') or task.get('video_title_original', '')) if task else ''
        description = (task.get('description_translated', '') or task.get('description_original', '')) if task else ''
        partition_id = _get_task_partition_id(task, UPLOAD_TARGET_ACFUN, prefer_selected=True) if task else ''
        missing_translation_fields = _get_missing_required_translation_fields(task, self.config)
        if missing_translation_fields:
            task_logger.warning(
                f"当前上传路径存在缺失译文字段 {missing_translation_fields}，将回退原文继续上传"
            )
        fixed_acfun_pid = str(self.config.get('FIXED_PARTITION_ID', '') or '').strip()
        if fixed_acfun_pid:
            partition_id = fixed_acfun_pid
        
        # 如果没有视频文件，先下载视频
        if not video_path or not os.path.exists(video_path):
            task_logger.info("检测到视频文件缺失，开始下载视频文件...")
            youtube_url = task.get('youtube_url', '') if task else ''
            if not youtube_url:
                task_logger.error("无法获取YouTube URL，无法下载视频")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message="无法获取YouTube URL，无法下载视频"
                )
                return
            
            # 下载视频文件
            self._download_video_file(task_id, youtube_url, task_logger)
            
            # 重新获取任务信息
            task = get_task(task_id)
            video_path = task.get('video_path_local', '') if task else ''
            cover_path = task.get('cover_path_local', '') if task else ''
        
        # 无论封面文件当前是否存在，都先统一做一次恢复/校验：
        # _recover_cover_path() 内部已包含 realpath + commonpath 的 downloads 目录边界检查，
        # 可阻止路径遍历或 symlink 指向 downloads 外部的情况；对于合法且已存在的路径，
        # 其 fast-return 会直接返回已校验过的路径，不改变原有功能。
        cover_path = self._recover_cover_path(task_id, cover_path, task_logger)
        
        if not subtitle_prepared:
            task = self._prepare_subtitle_for_upload(task_id, task_logger) or task
            video_path = task.get('video_path_local', '') if task else video_path

        # 重新设置状态为上传中（字幕翻译可能已在上述步骤执行）
        update_task(task_id, status=TASK_STATES['UPLOADING'])

        # 解析标签
        tags = _normalize_tags_list(task.get('tags_generated') if task else None)
        
        # 获取元数据
        metadata_path = task.get('metadata_json_path_local', '') if task else ''
        original_url = ''
        original_uploader = ''
        original_upload_date = ''
        
        if metadata_path and os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                    original_url = metadata.get('webpage_url', '')
                    original_uploader = metadata.get('uploader', '')
                    original_upload_date = metadata.get('upload_date', '')
            except Exception as e:
                task_logger.error(f"读取视频元数据失败: {str(e)}")

        # 可选：将YouTube上传者名字放到标签第一位（注意标签数量限制）
        if self.config.get('YOUTUBE_UPLOADER_AS_FIRST_TAG', False):
            from modules.utils import safe_str
            uploader_tag = safe_str(original_uploader).strip()
            if uploader_tag:
                normalized_uploader = uploader_tag.lower()
                cleaned_tags = []
                for tag in tags:
                    tag_value = safe_str(tag).strip()
                    if not tag_value:
                        continue
                    if tag_value.lower() == normalized_uploader:
                        continue
                    cleaned_tags.append(tag_value)
                tags = [uploader_tag] + cleaned_tags
                if len(tags) > 6:
                    tags = tags[:6]
                try:
                    update_task(task_id, tags_generated=json.dumps(tags, ensure_ascii=False))
                except Exception:
                    pass
        
        # AcFun配置（仅支持Cookies，推荐使用设置页二维码登录自动生成）
        acfun_cookies_path = resolve_cookie_file_path(
            path_value=self.config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'),
            default_relative_path='cookies/ac_cookies.json',
            service_name='AcFun',
            logger_obj=task_logger,
            allow_json_txt_fallback=True
        )
        
        # 获取封面处理模式
        cover_mode = self.config.get('COVER_PROCESSING_MODE', 'crop')
        
        # 检查必要参数
        missing_params = []
        if not video_path or not os.path.exists(video_path):
            missing_params.append("video_path (视频文件)")
        if not cover_path or not os.path.isfile(cover_path):
            missing_params.append("cover_path (封面文件)")
        if not title:
            missing_params.append("title (视频标题)")
        if not partition_id:
            missing_params.append("partition_id (分区ID)")
        
        if missing_params:
            error_msg = f"上传参数不完整，缺少: {', '.join(missing_params)}"
            task_logger.error(error_msg)
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=error_msg
            )
            return
        
        # 检查登录凭据 - 必须提供有效的Cookie文件
        cookie_file_exists = os.path.exists(acfun_cookies_path)

        # 验证cookies文件（如果存在）
        cookies_valid = False
        if cookie_file_exists:
            is_valid, error_msg = validate_cookies(acfun_cookies_path, "AcFun")
            if is_valid:
                cookies_valid = True
                task_logger.info("AcFun Cookies文件验证通过")
            else:
                task_logger.warning(f"AcFun Cookies文件验证失败: {error_msg}")
                task_logger.error("AcFun Cookies无效，请在设置页重新扫码登录或上传可用Cookies")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message=f"AcFun Cookies无效，请重新登录: {error_msg}"
                )
                return

        if not cookies_valid:
            task_logger.error("AcFun登录信息不完整，需要有效的Cookie文件")
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message="AcFun登录信息不完整，需要有效的Cookie文件（可在设置页扫码登录）"
            )
            return
        
        # 创建上传器并执行上传
        try:
            uploader = AcfunUploader(cookie_file=acfun_cookies_path)
            
            cancel_event = get_task_cancel_event(task_id)
            # 上传视频
            success, result = uploader.upload_video(
                video_file_path=video_path,
                cover_file_path=cover_path,
                title=title,
                description=description,
                tags=tags,
                partition_id=partition_id,
                original_url=original_url,
                original_uploader=original_uploader,
                original_upload_date=original_upload_date,
                upload_append_repost_notice=bool(self.config.get('UPLOAD_APPEND_REPOST_NOTICE', True)),
                task_id=task_id,
                cover_mode=cover_mode,
                cancel_event=cancel_event
            )
            
            if success:
                task_logger.info(f"视频上传成功: {result}")
                update_task(
                    task_id,
                    status=TASK_STATES['COMPLETED'],
                    acfun_upload_response=json.dumps(result, ensure_ascii=False)
                )
                # 单平台(AcFun)上传成功后立即删除本地文件，腾出磁盘空间，
                # 避免大视频长期占用磁盘导致后续任务因 No space left on device 失败。
                # 双平台(both)场景由 B 站上传分支（最后平台）统一删除，这里不重复删。
                try:
                    if (
                        self.config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False)
                        and _get_task_upload_target(task) == UPLOAD_TARGET_ACFUN
                    ):
                        if delete_task_files(task_id):
                            task_logger.info(f"AcFun 上传完成，已删除本地文件释放空间: {task_id}")
                        else:
                            task_logger.warning(f"AcFun 上传完成，但本地文件删除失败，请手动清理: {task_id}")
                except Exception as _e:
                    task_logger.warning(f"上传后删除本地文件失败（不影响已完成的 AcFun 上传）: {_e}")
            else:
                task_logger.error(f"视频上传失败: {result}")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message=f"上传失败: {result}"
                )
        except Exception as e:
            task_logger.error(f"上传过程中发生异常: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传异常: {str(e)}"
            )

    def _do_upload_to_bilibili(self, task_id, task_logger, subtitle_prepared=False):
        """实际执行上传到 Bilibili 的逻辑"""
        from modules.bilibili_uploader import BilibiliUploader

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return

        update_task(task_id, status=TASK_STATES['UPLOADING'], upload_progress='0.0%')

        video_path = task.get('video_path_local', '') if task else ''
        cover_path = task.get('cover_path_local', '') if task else ''
        title = (task.get('video_title_translated', '') or task.get('video_title_original', '')) if task else ''
        description = (task.get('description_translated', '') or task.get('description_original', '')) if task else ''
        upload_target = _get_task_upload_target(task)
        effective_limits = _get_effective_metadata_limits(upload_target)
        partition_id = ''
        missing_translation_fields = _get_missing_required_translation_fields(task, self.config)
        if missing_translation_fields:
            task_logger.warning(
                f"当前上传路径存在缺失译文字段 {missing_translation_fields}，将回退原文继续上传"
            )

        if not video_path or not os.path.exists(video_path):
            task_logger.info("检测到视频文件缺失，开始下载视频文件...")
            youtube_url = task.get('youtube_url', '') if task else ''
            if not youtube_url:
                task_logger.error("无法获取YouTube URL，无法下载视频")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message="无法获取YouTube URL，无法下载视频",
                    upload_progress=None
                )
                return

            self._download_video_file(task_id, youtube_url, task_logger)
            task = get_task(task_id)
            video_path = task.get('video_path_local', '') if task else ''
            cover_path = task.get('cover_path_local', '') if task else ''

        # 无论封面文件当前是否存在，都先统一做一次恢复/校验：
        # _recover_cover_path() 内部已包含 realpath + commonpath 的 downloads 目录边界检查，
        # 可阻止路径遍历或 symlink 指向 downloads 外部的情况；对于合法且已存在的路径，
        # 其 fast-return 会直接返回已校验过的路径，不改变原有功能。
        cover_path = self._recover_cover_path(task_id, cover_path, task_logger)

        if not subtitle_prepared:
            task = self._prepare_subtitle_for_upload(task_id, task_logger) or task
            video_path = task.get('video_path_local', '') if task else video_path

        update_task(task_id, status=TASK_STATES['UPLOADING'], upload_progress='0.0%')

        tags = []
        tags = _normalize_tags_list(task.get('tags_generated') if task else None)

        metadata_path = task.get('metadata_json_path_local', '') if task else ''
        original_url = task.get('youtube_url', '') if task else ''
        original_uploader = ''
        original_upload_date = ''

        if metadata_path and os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                    original_url = metadata.get('webpage_url', original_url)
                    original_uploader = metadata.get('uploader', '')
                    original_upload_date = metadata.get('upload_date', '')
            except Exception as e:
                task_logger.error(f"读取视频元数据失败: {str(e)}")

        # bilibili 转载页会单独展示 source，这里只保留说明文案和正文，避免 URL 重复出现。
        try:
            from modules.bilibili_uploader import format_bilibili_description

            description = format_bilibili_description(
                base_desc=description,
                original_url=original_url,
                original_uploader=original_uploader,
                original_upload_date=original_upload_date,
                append_repost_notice=bool(self.config.get('UPLOAD_APPEND_REPOST_NOTICE', True)),
                max_len=effective_limits['description_limit'],
            )
        except Exception as e:
            task_logger.warning(f"构建bilibili投稿简介失败，回退原简介: {e}")

        if self.config.get('YOUTUBE_UPLOADER_AS_FIRST_TAG', False):
            from modules.utils import safe_str
            uploader_tag = safe_str(original_uploader).strip()
            if uploader_tag:
                normalized_uploader = uploader_tag.lower()
                cleaned_tags = []
                for tag in tags:
                    tag_value = safe_str(tag).strip()
                    if not tag_value:
                        continue
                    if tag_value.lower() == normalized_uploader:
                        continue
                    cleaned_tags.append(tag_value)
                tags = [uploader_tag] + cleaned_tags
                if len(tags) > 12:
                    tags = tags[:12]
                try:
                    update_task(task_id, tags_generated=json.dumps(tags, ensure_ascii=False))
                except Exception:
                    pass

        bilibili_cookies_path = resolve_cookie_file_path(
            path_value=self.config.get('BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json'),
            default_relative_path='cookies/bili_cookies.json',
            service_name='Bilibili',
            logger_obj=task_logger,
            allow_json_txt_fallback=False,
        )

        # 固定 bilibili 分区优先；否则使用任务分区。并校验分区合法性
        task_partition_id = _get_task_partition_id(
            task,
            UPLOAD_TARGET_BILIBILI,
            prefer_selected=True,
        ) if task else ''
        fixed_bili_pid = str(self.config.get('FIXED_PARTITION_ID_BILIBILI', '') or '').strip()
        partition_id = fixed_bili_pid or task_partition_id
        try:
            from .bilibili_zones import collect_valid_tids
            valid_tids = collect_valid_tids()
            if partition_id and str(partition_id) not in valid_tids:
                task_logger.warning(f"bilibili分区ID无效: {partition_id}")
                partition_id = ''
            if not partition_id and task_partition_id and str(task_partition_id) in valid_tids:
                partition_id = str(task_partition_id)
        except Exception as e:
            task_logger.warning(f"校验 bilibili 分区列表失败，继续使用当前分区ID: {e}")

        missing_params = []
        if not video_path or not os.path.exists(video_path):
            missing_params.append("video_path (视频文件)")
        if not cover_path or not os.path.isfile(cover_path):
            missing_params.append("cover_path (封面文件)")
        if not title:
            missing_params.append("title (视频标题)")
        if not partition_id:
            missing_params.append("partition_id (分区ID)")
        if not bilibili_cookies_path:
            missing_params.append("BILIBILI_COOKIES_PATH")

        if missing_params:
            error_msg = f"上传参数不完整，缺少: {', '.join(missing_params)}"
            task_logger.error(error_msg)
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=error_msg,
                upload_progress=None
            )
            return

        cookie_file_exists = os.path.exists(bilibili_cookies_path)
        if not cookie_file_exists:
            task_logger.error(f"Bilibili Cookies文件不存在: {bilibili_cookies_path}")
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"Bilibili Cookies文件不存在: {bilibili_cookies_path}",
                upload_progress=None
            )
            return

        is_valid, error_msg = validate_cookies(bilibili_cookies_path, "Bilibili")
        if not is_valid:
            task_logger.error(f"Bilibili Cookies文件验证失败: {error_msg}")
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"Bilibili Cookies无效: {error_msg}",
                upload_progress=None
            )
            return

        try:
            uploader = BilibiliUploader(cookie_file=bilibili_cookies_path)
            last_progress_text = '0.0%'

            def _normalize_progress_text(progress_text):
                text = str(progress_text or '').strip()
                if not text:
                    return '上传中...'
                match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
                if match:
                    try:
                        percent = float(match.group(1))
                        percent = max(0.0, min(100.0, percent))
                        return f"{percent:.1f}%"
                    except Exception:
                        return '上传中...'
                try:
                    numeric = float(text)
                    if 0.0 <= numeric <= 1.0:
                        numeric *= 100.0
                    numeric = max(0.0, min(100.0, numeric))
                    return f"{numeric:.1f}%"
                except Exception:
                    return '上传中...'

            def _on_progress(progress_text):
                nonlocal last_progress_text
                normalized_text = _normalize_progress_text(progress_text)
                if normalized_text == last_progress_text:
                    return
                last_progress_text = normalized_text
                update_task(task_id, upload_progress=normalized_text, silent=True)

            success, result = uploader.upload_video(
                video_file_path=video_path,
                cover_file_path=cover_path,
                title=title,
                description=description,
                tags=tags,
                partition_id=partition_id,
                youtube_url=original_url,
                task_id=task_id,
                progress_callback=_on_progress,
                title_limit=effective_limits['title_limit'],
                description_limit=effective_limits['description_limit'],
            )

            if success:
                task_logger.info(f"bilibili上传成功: {result}")
                update_task(
                    task_id,
                    status=TASK_STATES['COMPLETED'],
                    bilibili_upload_response=json.dumps(result, ensure_ascii=False),
                    upload_progress=None
                )
                # 上传到 B 站成功后立即删除本地文件，腾出磁盘空间，
                # 避免大视频（如 CS2 比赛直播流，单条可达十余 GB）长期占用磁盘，
                # 导致后续任务因「No space left on device」而失败。
                # - bilibili 单平台：此处即为最后平台，直接删除；
                # - both 双平台：AcFun 已先上传完成，B 站为最后平台，同样删除；
                # - acfun 单平台：不会进入本分支，由 AcFun 分支处理。
                try:
                    if (
                        self.config.get('DELETE_DOWNLOAD_FILES_AFTER_UPLOAD', False)
                        and _get_task_upload_target(task) != UPLOAD_TARGET_ACFUN
                    ):
                        if delete_task_files(task_id):
                            task_logger.info(f"bilibili 上传完成，已删除本地文件释放空间: {task_id}")
                        else:
                            task_logger.warning(f"bilibili 上传完成，但本地文件删除失败，请手动清理: {task_id}")
                except Exception as _e:
                    task_logger.warning(f"上传后删除本地文件失败（不影响已完成的 B 站上传）: {_e}")
            else:
                task_logger.error(f"bilibili上传失败: {result}")
                update_task(
                    task_id,
                    status=TASK_STATES['FAILED'],
                    error_message=f"上传失败: {result}",
                    upload_progress=None
                )
        except Exception as e:
            task_logger.error(f"bilibili上传过程中发生异常: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传异常: {str(e)}",
                upload_progress=None
            )

# 任务控制函数
def start_task(task_id, config=None):
    """
    启动任务处理
    
    Args:
        task_id: 任务ID
        config: 配置信息
    
    Returns:
        success: 启动是否成功
    """
    # 获取任务信息
    task = get_task(task_id)
    if not task:
        logger.error(f"任务 {task_id} 不存在")
        return False
    
    # 任务已经在处理中或已完成
    if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['AWAITING_REVIEW'], TASK_STATES['FAILED']]:
        logger.warning(f"任务 {task_id} 状态为 {task['status']}，不能启动")
        return False
    
    # 如果没有提供配置，尝试从Flask app获取
    if not config:
        try:
            from flask import current_app
            if 'Y2A_SETTINGS' in current_app.config:
                config = current_app.config['Y2A_SETTINGS']
                logger.info("从Flask应用获取配置")
        except (ImportError, RuntimeError):
            logger.warning("无法从Flask应用获取配置，使用空配置")
            config = {}
    
    # 使用全局任务处理器，确保并发控制生效
    processor = get_global_task_processor(config)
    
    # 调度任务
    job_id = processor.schedule_task(task_id)
    
    return job_id is not None

def force_upload_task(task_id, config=None):
    """
    强制上传任务
    
    Args:
        task_id: 任务ID
        config: 配置信息
    
    Returns:
        success: 操作是否成功
    """
    # 获取任务信息
    task = get_task(task_id)
    if not task:
        logger.error(f"任务 {task_id} 不存在")
        return False
    
    # 允许状态为"等待人工审核"、"已完成"、"等待处理"或"准备上传"的任务进行上传
    allowed_states = [TASK_STATES['AWAITING_REVIEW'], TASK_STATES['COMPLETED'], TASK_STATES['PENDING'], TASK_STATES['READY_FOR_UPLOAD']]
    if task['status'] not in allowed_states:
        logger.warning(f"任务 {task_id} 状态为 {task['status']}，只有以下状态的任务可以上传: {', '.join(allowed_states)}")
        return False
    
    # 如果没有提供配置，尝试从Flask app获取
    if not config:
        try:
            from flask import current_app
            if 'Y2A_SETTINGS' in current_app.config:
                config = current_app.config['Y2A_SETTINGS']
                logger.info("从Flask应用获取配置")
        except (ImportError, RuntimeError):
            logger.warning("无法从Flask应用获取配置，使用空配置")
            config = {}
    
    # 使用全局任务处理器，确保并发控制生效
    processor = get_global_task_processor(config)
    
    # 创建任务日志记录器
    task_logger = setup_task_logger(task_id)
    
    try:
        # 直接执行上传步骤
        processor._upload_to_target(task_id, task_logger, allow_missing_translations=True)

        # 上传流程（强制路径）不会进入 process_task 的 finally，需手动唤醒队列
        try:
            import threading, time
            def delayed_check():
                # 等待一下，确保任务状态已写回数据库
                time.sleep(1)
                processor._check_and_start_next_pending_task()
            threading.Thread(target=delayed_check, daemon=True).start()
            logger.info("强制上传结束，已触发检查下一条pending任务")
        except Exception as _e:
            logger.warning(f"强制上传后触发队列检查失败（忽略）：{_e}")

        return True
    except Exception as e:
        task_logger.error(f"强制上传任务 {task_id} 失败: {str(e)}")
        import traceback
        task_logger.error(traceback.format_exc())
        update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"强制上传失败: {str(e)}")
        return False

# 全局任务处理器实例
_global_task_processor = None

def get_global_task_processor(config=None):
    """
    获取全局任务处理器实例，确保并发控制生效
    
    Args:
        config: 配置信息
        
    Returns:
        TaskProcessor: 全局任务处理器实例
    """
    global _global_task_processor
    
    if _global_task_processor is None:
        logger.info("创建全局任务处理器实例")
        _global_task_processor = TaskProcessor(config)
    elif config:
        logger.info("配置已更新，刷新全局任务处理器运行时配置")
        _global_task_processor.refresh_config(config)
    
    return _global_task_processor

def shutdown_global_task_processor():
    """关闭全局任务处理器"""
    global _global_task_processor
    if _global_task_processor:
        _global_task_processor.shutdown()
        _global_task_processor = None
        logger.info("全局任务处理器已关闭")

# 初始化数据库
init_db() 
