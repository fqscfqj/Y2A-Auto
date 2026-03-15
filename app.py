#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import mimetypes
import shutil
import time
import datetime
import uuid
import threading
from urllib.parse import urlparse

from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file, session, Response, stream_with_context
from functools import wraps
from flask_cors import CORS
from PIL import Image, UnidentifiedImageError
from modules.youtube_handler import extract_video_urls_from_playlist
from modules.utils import get_app_subdir
from modules.config_manager import load_config, update_config, reset_specific_config
from modules.whisper_languages import WHISPER_LANGUAGE_LIST
from modules.task_manager import add_task, start_task, get_task, get_tasks_paginated, get_tasks_by_status, update_task, delete_task, force_upload_task, TASK_STATES, clear_all_tasks, retry_failed_tasks, register_task_updates_listener, unregister_task_updates_listener, resolve_cookie_file_path
from modules.acfun_auth import AcfunQrLoginSession
from modules.bilibili_auth import BilibiliQrLoginSession
from queue import Empty
from modules.youtube_monitor import youtube_monitor
from modules.speech_pipeline_settings import (
    SPEECH_PIPELINE_CHECKBOXES,
    SPEECH_PIPELINE_FLOAT_FIELDS,
    SPEECH_PIPELINE_INT_FIELDS,
)
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.urandom(24)  # 用于flash消息
app.jinja_env.globals.update(now=datetime.now())  # 添加当前时间到模板全局变量

ALLOWED_COVER_EXTENSIONS = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.webp': 'image/webp',
}

# bilibili二维码登录会话（内存）
_BILIBILI_QR_SESSIONS = {}
_BILIBILI_QR_SESSION_LOCK = threading.Lock()
_BILIBILI_QR_SESSION_TTL_SECONDS = 300
# AcFun二维码登录会话（内存）
_ACFUN_QR_SESSIONS = {}
_ACFUN_QR_SESSION_LOCK = threading.Lock()
_ACFUN_QR_SESSION_TTL_SECONDS = 420
# 登录安全状态存储
def _get_security_state_path():
    try:
        db_dir = get_app_subdir('db')
    except Exception:
        # 回退到当前目录下的db
        db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db')
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, 'security_state.json')

def _load_security_state():
    path = _get_security_state_path()
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 兼容缺失字段
                if not isinstance(data, dict):
                    data = {}
        else:
            data = {}
    except Exception:
        data = {}
    # 默认值
    return {
        'failed_attempts': int(data.get('failed_attempts', 0) or 0),
        'locked_until': float(data.get('locked_until', 0) or 0.0),
        'last_attempt': float(data.get('last_attempt', 0) or 0.0),
    }

def _save_security_state(state):
    try:
        path = _get_security_state_path()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _cleanup_bilibili_qr_sessions():
    now_ts = time.time()
    with _BILIBILI_QR_SESSION_LOCK:
        stale_ids = []
        for sid, item in _BILIBILI_QR_SESSIONS.items():
            created_at = float(item.get('created_at', 0) or 0)
            if now_ts - created_at > _BILIBILI_QR_SESSION_TTL_SECONDS:
                stale_ids.append(sid)
        for sid in stale_ids:
            _BILIBILI_QR_SESSIONS.pop(sid, None)


def _create_bilibili_qr_session():
    _cleanup_bilibili_qr_sessions()
    session_id = str(uuid.uuid4())
    session_obj = BilibiliQrLoginSession()
    with _BILIBILI_QR_SESSION_LOCK:
        _BILIBILI_QR_SESSIONS[session_id] = {
            'created_at': time.time(),
            'session': session_obj,
        }
    return session_id, session_obj


def _get_bilibili_qr_session(session_id: str):
    if not session_id:
        return None
    _cleanup_bilibili_qr_sessions()
    with _BILIBILI_QR_SESSION_LOCK:
        item = _BILIBILI_QR_SESSIONS.get(session_id)
    if not item:
        return None
    return item.get('session')


def _cleanup_acfun_qr_sessions():
    now_ts = time.time()
    with _ACFUN_QR_SESSION_LOCK:
        stale_ids = []
        for sid, item in _ACFUN_QR_SESSIONS.items():
            created_at = float(item.get('created_at', 0) or 0)
            if now_ts - created_at > _ACFUN_QR_SESSION_TTL_SECONDS:
                stale_ids.append(sid)
        for sid in stale_ids:
            _ACFUN_QR_SESSIONS.pop(sid, None)


def _create_acfun_qr_session():
    _cleanup_acfun_qr_sessions()
    session_id = str(uuid.uuid4())
    session_obj = AcfunQrLoginSession()
    with _ACFUN_QR_SESSION_LOCK:
        _ACFUN_QR_SESSIONS[session_id] = {
            'created_at': time.time(),
            'session': session_obj,
        }
    return session_id, session_obj


def _get_acfun_qr_session(session_id: str):
    if not session_id:
        return None
    _cleanup_acfun_qr_sessions()
    with _ACFUN_QR_SESSION_LOCK:
        item = _ACFUN_QR_SESSIONS.get(session_id)
    if not item:
        return None
    return item.get('session')


_SETTINGS_SAVE_OPERATIONS = {}
_SETTINGS_SAVE_LOCK = threading.Lock()
_SETTINGS_SAVE_TTL_SECONDS = 600


def _new_settings_save_state(operation_id: str) -> dict:
    now_ts = time.time()
    return {
        'operation_id': operation_id,
        'stage': 'saving_config',
        'message': '正在准备保存设置',
        'detail': '正在提交保存任务，请稍候。',
        'percent': None,
        'downloaded_bytes': None,
        'total_bytes': None,
        'done': False,
        'level': 'info',
        'success': None,
        'messages': [],
        'created_at': now_ts,
        'updated_at': now_ts,
        'expires_at': None,
    }


def _cleanup_settings_save_operations():
    now_ts = time.time()
    with _SETTINGS_SAVE_LOCK:
        stale_ids = []
        for operation_id, state in _SETTINGS_SAVE_OPERATIONS.items():
            expires_at = state.get('expires_at')
            if expires_at and now_ts >= float(expires_at):
                stale_ids.append(operation_id)
        for operation_id in stale_ids:
            _SETTINGS_SAVE_OPERATIONS.pop(operation_id, None)


def _update_settings_save_progress(operation_id: str, **fields) -> dict:
    _cleanup_settings_save_operations()
    with _SETTINGS_SAVE_LOCK:
        state = dict(_SETTINGS_SAVE_OPERATIONS.get(operation_id) or _new_settings_save_state(operation_id))
        state.update(fields)
        state['updated_at'] = time.time()
        if state.get('done'):
            state['expires_at'] = state['updated_at'] + _SETTINGS_SAVE_TTL_SECONDS
        _SETTINGS_SAVE_OPERATIONS[operation_id] = state
        return dict(state)


def _get_settings_save_progress(operation_id: str):
    _cleanup_settings_save_operations()
    with _SETTINGS_SAVE_LOCK:
        state = _SETTINGS_SAVE_OPERATIONS.get(operation_id)
        return dict(state) if state else None


def _append_settings_message(messages: list, category: str, text: str):
    clean_text = str(text or '').strip()
    if not clean_text:
        return
    messages.append({'category': category, 'text': clean_text})


def _get_task_dir_real(task_id: str) -> str:
    downloads_dir_real = os.path.realpath(get_app_subdir('downloads'))
    task_dir_real = os.path.realpath(os.path.join(downloads_dir_real, str(task_id)))
    if task_dir_real != downloads_dir_real and not task_dir_real.startswith(downloads_dir_real + os.sep):
        raise ValueError('非法任务目录')
    return task_dir_real


def _is_safe_task_file(path: str, task_dir_real: str) -> bool:
    try:
        file_real = os.path.realpath(path)
    except (ValueError, OSError):
        return False
    return file_real == task_dir_real or file_real.startswith(task_dir_real + os.sep)


def _get_cover_file_info(path: str):
    ext = os.path.splitext(str(path or ''))[1].lower()
    return ext, ALLOWED_COVER_EXTENSIONS.get(ext)


def _validate_cover_upload(file_storage):
    if not file_storage or not getattr(file_storage, 'filename', ''):
        raise ValueError('请选择要上传的封面图片')

    ext, _ = _get_cover_file_info(file_storage.filename)
    if ext not in ALLOWED_COVER_EXTENSIONS:
        raise ValueError('仅支持 JPG、JPEG、PNG、WEBP 格式的封面图片')

    current_pos = file_storage.stream.tell()
    try:
        with Image.open(file_storage.stream) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f'上传文件不是有效图片: {exc}') from exc
    finally:
        file_storage.stream.seek(current_pos)

    return ext


def _find_original_cover_backup(task_dir_real: str):
    for ext in ALLOWED_COVER_EXTENSIONS:
        candidate = os.path.join(task_dir_real, f'original_cover{ext}')
        if os.path.exists(candidate) and _is_safe_task_file(candidate, task_dir_real):
            return candidate
    return None


def _get_current_cover_path(task: dict, task_dir_real: str):
    cover_path = str(task.get('cover_path_local') or '').strip()
    if cover_path and os.path.exists(cover_path) and _is_safe_task_file(cover_path, task_dir_real):
        return os.path.realpath(cover_path)

    for name in ('cover.jpg', 'cover.png', 'cover.webp', 'thumbnail.jpg', 'thumbnail.png', 'thumbnail.webp'):
        candidate = os.path.join(task_dir_real, name)
        if os.path.exists(candidate) and _is_safe_task_file(candidate, task_dir_real):
            return os.path.realpath(candidate)

    for filename in os.listdir(task_dir_real):
        if filename.lower().endswith(tuple(ALLOWED_COVER_EXTENSIONS.keys())):
            candidate = os.path.join(task_dir_real, filename)
            if os.path.exists(candidate) and _is_safe_task_file(candidate, task_dir_real):
                return os.path.realpath(candidate)

    return ''


def _replace_task_cover(task: dict, uploaded_file):
    task_id = str(task.get('id') or '').strip()
    if not task_id:
        raise ValueError('任务不存在')

    task_dir_real = _get_task_dir_real(task_id)
    os.makedirs(task_dir_real, exist_ok=True)

    current_cover_path = _get_current_cover_path(task, task_dir_real)
    if not current_cover_path:
        raise ValueError('当前任务没有可替换的原始封面')

    ext = _validate_cover_upload(uploaded_file)
    original_backup = _find_original_cover_backup(task_dir_real)

    if not original_backup:
        current_ext, _ = _get_cover_file_info(current_cover_path)
        if current_ext not in ALLOWED_COVER_EXTENSIONS:
            raise ValueError('当前原始封面格式不受支持，无法创建恢复备份')
        original_backup = os.path.join(task_dir_real, f'original_cover{current_ext}')
        shutil.copy2(current_cover_path, original_backup)

    for existing_ext in ALLOWED_COVER_EXTENSIONS:
        custom_candidate = os.path.join(task_dir_real, f'custom_cover{existing_ext}')
        if os.path.exists(custom_candidate):
            os.remove(custom_candidate)

    new_cover_path = os.path.join(task_dir_real, f'custom_cover{ext}')
    uploaded_file.save(new_cover_path)
    update_task(task_id, cover_path_local=new_cover_path, silent=True)
    return new_cover_path


def _restore_task_cover(task: dict):
    task_id = str(task.get('id') or '').strip()
    if not task_id:
        raise ValueError('任务不存在')

    task_dir_real = _get_task_dir_real(task_id)
    if not os.path.isdir(task_dir_real):
        raise ValueError('任务目录不存在，无法恢复原封面')

    original_backup = _find_original_cover_backup(task_dir_real)
    if not original_backup:
        raise ValueError('未找到原始封面备份，无法恢复')

    update_task(task_id, cover_path_local=original_backup, silent=True)
    return original_backup


def _is_ajax_request() -> bool:
    requested_with = request.headers.get('X-Requested-With', '')
    accept_header = request.headers.get('Accept', '')
    return requested_with == 'XMLHttpRequest' or 'application/json' in accept_header


def _extract_settings_uploads(files_storage) -> dict:
    uploads = {}
    for field_name in ('youtube_cookies_file', 'acfun_cookies_file', 'bilibili_cookies_file'):
        file_storage = files_storage.get(field_name)
        if not file_storage or not getattr(file_storage, 'filename', ''):
            continue
        uploads[field_name] = {
            'filename': file_storage.filename,
            'content': file_storage.read()
        }
    return uploads


def _persist_settings_uploads(form_data: dict, uploads: dict):
    cookies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')
    os.makedirs(cookies_dir, exist_ok=True)

    file_specs = {
        'youtube_cookies_file': ('yt_cookies.txt', 'YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt', 'YouTube'),
        'acfun_cookies_file': ('ac_cookies.json', 'ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json', 'AcFun'),
        'bilibili_cookies_file': ('bili_cookies.json', 'BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json', 'Bilibili'),
    }

    for field_name, payload in uploads.items():
        spec = file_specs.get(field_name)
        if not spec or not payload.get('filename'):
            continue
        save_name, config_key, relative_path, service_name = spec
        target_path = os.path.join(cookies_dir, save_name)
        with open(target_path, 'wb') as target_file:
            target_file.write(payload.get('content') or b'')
        form_data[config_key] = relative_path
        logger.info(f"{service_name} cookies文件已上传并保存到: {target_path}")


def _build_settings_progress_reporter(operation_id: str | None):
    if not operation_id:
        return None

    def _report(payload: dict):
        _update_settings_save_progress(
            operation_id,
            stage=payload.get('stage', 'saving_config'),
            message=payload.get('message', ''),
            detail=payload.get('detail', ''),
            percent=payload.get('percent'),
            downloaded_bytes=payload.get('downloaded_bytes'),
            total_bytes=payload.get('total_bytes'),
            level=payload.get('level', 'info')
        )

    return _report


def _perform_settings_save(form_data: dict, uploads: dict, operation_id: str | None = None) -> dict:
    form_data = dict(form_data or {})
    uploads = uploads or {}
    messages = []
    progress_reporter = _build_settings_progress_reporter(operation_id)

    def report(stage: str, message: str, detail: str = '', percent=None, level: str = 'info', downloaded_bytes=None, total_bytes=None):
        if not progress_reporter:
            return
        progress_reporter({
            'stage': stage,
            'message': message,
            'detail': detail,
            'percent': percent,
            'downloaded_bytes': downloaded_bytes,
            'total_bytes': total_bytes,
            'level': level,
        })

    try:
        report('saving_config', '正在保存配置', '正在校验并写入设置。')
        form_data.pop('save_operation_id', None)

        new_password = form_data.get('new_password')
        confirm_password = form_data.get('confirm_password')
        if new_password:
            if new_password == confirm_password:
                form_data['password'] = new_password
            else:
                _append_settings_message(messages, 'danger', '新密码两次输入不一致，密码未更新。')

        form_data.pop('new_password', None)
        form_data.pop('confirm_password', None)

        checkboxes = [
            'AUTO_MODE_ENABLED', 'TRANSLATE_TITLE', 'TRANSLATE_DESCRIPTION',
            'UPLOAD_APPEND_REPOST_NOTICE',
            'GENERATE_TAGS', 'YOUTUBE_UPLOADER_AS_FIRST_TAG', 'RECOMMEND_PARTITION',
            'RECOMMEND_PARTITION_WITH_COVER', 'CONTENT_MODERATION_ENABLED',
            'OPENAI_THINKING_ENABLED', 'SUBTITLE_OPENAI_THINKING_ENABLED',
            'LOG_CLEANUP_ENABLED', 'SUBTITLE_TRANSLATION_ENABLED', 'SUBTITLE_EMBED_IN_VIDEO',
            'SUBTITLE_KEEP_ORIGINAL', 'YOUTUBE_PROXY_ENABLED', 'password_protection_enabled',
            'SPEECH_RECOGNITION_ENABLED',
            'VAD_ENABLED',
            'SUBTITLE_NORMALIZE_PUNCTUATION', 'SUBTITLE_FILTER_FILLER_WORDS',
            'SUBTITLE_TIME_OFFSET_ENABLED', 'SUBTITLE_MIN_CUE_DURATION_ENABLED',
            'SUBTITLE_MERGE_GAP_ENABLED', 'SUBTITLE_MIN_TEXT_LENGTH_ENABLED',
            'SUBTITLE_MAX_LINE_LENGTH_ENABLED', 'SUBTITLE_MAX_LINES_ENABLED',
            'SUBTITLE_QC_ENABLED',
            'FFMPEG_AUTO_DOWNLOAD', 'WHISPER_TRANSLATE', 'WHISPER_FALLBACK_TO_FIXED_CHUNKS',
            'VIDEO_CUSTOM_PARAMS_ENABLED',
            'FIREREDASR_ENABLED',
            'VOXTRAL_DIARIZE'
        ]
        for checkbox in SPEECH_PIPELINE_CHECKBOXES:
            if checkbox not in checkboxes:
                checkboxes.append(checkbox)
        for checkbox in checkboxes:
            if checkbox not in form_data:
                form_data[checkbox] = 'off'

        numeric_fields = [
            'MAX_CONCURRENT_TASKS', 'MAX_CONCURRENT_UPLOADS', 'LOG_CLEANUP_HOURS',
            'LOG_CLEANUP_INTERVAL', 'SUBTITLE_BATCH_SIZE', 'SUBTITLE_MAX_RETRIES',
            'SUBTITLE_RETRY_DELAY', 'SUBTITLE_MAX_WORKERS', 'YOUTUBE_DOWNLOAD_THREADS',
            'LOGIN_MAX_FAILED_ATTEMPTS', 'LOGIN_LOCKOUT_MINUTES',
            'VAD_SILERO_MIN_SPEECH_MS',
            'VAD_SILERO_MIN_SILENCE_MS', 'VAD_SILERO_MAX_SPEECH_S',
            'VAD_SILERO_SPEECH_PAD_MS', 'VAD_MAX_SEGMENT_S',
            'SUBTITLE_QC_SAMPLE_MAX_ITEMS', 'SUBTITLE_QC_MAX_CHARS',
            'SUBTITLE_MIN_TEXT_LENGTH',
            'WHISPER_MAX_WORKERS', 'WHISPER_MAX_RETRIES',
            'FIREREDASR_TIMEOUT', 'FIREREDASR_MAX_RETRIES'
        ]
        for field in SPEECH_PIPELINE_INT_FIELDS:
            if field not in numeric_fields:
                numeric_fields.append(field)
        for field in numeric_fields:
            if field in form_data:
                try:
                    print(f"DEBUG: 转换前 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")
                    original_value = form_data[field]
                    form_data[field] = str(int(form_data[field]))
                    print(f"DEBUG: 转换后 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")
                except (ValueError, TypeError) as e:
                    print(f"DEBUG: 转换失败 - field: {field}, value: {form_data[field]}, error: {e}")
                    defaults = {
                        'MAX_CONCURRENT_TASKS': 3,
                        'MAX_CONCURRENT_UPLOADS': 1,
                        'LOG_CLEANUP_HOURS': 168,
                        'LOG_CLEANUP_INTERVAL': 24,
                        'SUBTITLE_BATCH_SIZE': 5,
                        'SUBTITLE_MAX_RETRIES': 3,
                        'SUBTITLE_RETRY_DELAY': 5,
                        'SUBTITLE_MAX_WORKERS': 0,
                        'YOUTUBE_DOWNLOAD_THREADS': 4,
                        'LOGIN_MAX_FAILED_ATTEMPTS': 5,
                        'LOGIN_LOCKOUT_MINUTES': 15,
                        'VAD_SILERO_MIN_SPEECH_MS': 300,
                        'VAD_SILERO_MIN_SILENCE_MS': 320,
                        'VAD_SILERO_MAX_SPEECH_S': 120,
                        'VAD_SILERO_SPEECH_PAD_MS': 120,
                        'VAD_MAX_SEGMENT_S': 30,
                        'SUBTITLE_QC_SAMPLE_MAX_ITEMS': 80,
                        'SUBTITLE_QC_MAX_CHARS': 9000,
                        'FIREREDASR_TIMEOUT': 300,
                        'FIREREDASR_MAX_RETRIES': 3
                    }
                    defaults.update(SPEECH_PIPELINE_INT_FIELDS)
                    form_data[field] = str(defaults.get(field, 1))
                    print(f"DEBUG: 使用默认值 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")

        float_fields = [
            'VAD_SILERO_THRESHOLD',
            'SUBTITLE_TIME_OFFSET_S', 'SUBTITLE_MIN_CUE_DURATION_S', 'SUBTITLE_MERGE_GAP_S',
            'SUBTITLE_QC_THRESHOLD',
            'WHISPER_RETRY_DELAY_S', 'AUDIO_CHUNK_WINDOW_S', 'AUDIO_CHUNK_OVERLAP_S',
            'VAD_MERGE_GAP_S', 'VAD_MIN_SEGMENT_S', 'VAD_MAX_SEGMENT_S_FOR_SPLIT'
        ]
        for field in SPEECH_PIPELINE_FLOAT_FIELDS:
            if field not in float_fields:
                float_fields.append(field)
        for field in float_fields:
            if field in form_data:
                try:
                    print(f"DEBUG: 转换前 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")
                    original_value = form_data[field]
                    if str(original_value).strip() == '':
                        raise ValueError('empty string')
                    form_data[field] = str(float(original_value))
                    print(f"DEBUG: 转换后 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")
                except (ValueError, TypeError) as e:
                    print(f"DEBUG: 转换失败 - field: {field}, value: {form_data[field]}, error: {e}")
                    float_defaults = {
                        'VAD_SILERO_THRESHOLD': 0.55,
                        'SUBTITLE_TIME_OFFSET_S': 0.0,
                        'SUBTITLE_MIN_CUE_DURATION_S': 0.6,
                        'SUBTITLE_MERGE_GAP_S': 0.3,
                        'SUBTITLE_QC_THRESHOLD': 0.35,
                        'WHISPER_RETRY_DELAY_S': 2.0,
                        'AUDIO_CHUNK_WINDOW_S': 30.0,
                        'AUDIO_CHUNK_OVERLAP_S': 0.4,
                        'VAD_MERGE_GAP_S': 0.35,
                        'VAD_MIN_SEGMENT_S': 0.8,
                        'VAD_MAX_SEGMENT_S_FOR_SPLIT': 30.0,
                    }
                    float_defaults.update(SPEECH_PIPELINE_FLOAT_FIELDS)
                    form_data[field] = str(float_defaults.get(field, 0.0))
                    print(f"DEBUG: 使用默认值 - field: {field}, value: {form_data[field]}, type: {type(form_data[field])}")

        if 'SUBTITLE_FONT_NAME' in form_data:
            form_data['SUBTITLE_FONT_NAME'] = str(form_data['SUBTITLE_FONT_NAME']).strip()

        _persist_settings_uploads(form_data, uploads)
        updated_config = update_config(form_data)

        try:
            from modules.task_manager import get_global_task_processor
            app.config['Y2A_SETTINGS'] = updated_config
            get_global_task_processor(updated_config)
            logger.info("配置已更新并同步到任务处理器")
        except Exception as e:
            logger.warning(f"同步任务处理器配置失败: {e}")

        try:
            need_ffmpeg = False
            if str(updated_config.get('SPEECH_RECOGNITION_ENABLED', False)).lower() in ['true', '1', 'on']:
                need_ffmpeg = True
            if str(updated_config.get('FIREREDASR_ENABLED', False)).lower() in ['true', '1', 'on']:
                need_ffmpeg = True
            if str(updated_config.get('SUBTITLE_EMBED_IN_VIDEO', False)).lower() in ['true', '1', 'on']:
                need_ffmpeg = True

            if need_ffmpeg:
                from modules.ffmpeg_manager import get_windows_ffmpeg_manual_setup_message
                from modules.youtube_handler import get_ffmpeg_path
                report('checking_ffmpeg', '正在检查 FFmpeg', '已启用依赖 FFmpeg 的功能，正在检查本地环境。')
                ff_path = get_ffmpeg_path(
                    logger=logger,
                    force_refresh=True,
                    progress_callback=progress_reporter
                )
                if ff_path and os.path.exists(ff_path):
                    logger.info(f"FFmpeg 已就绪: {ff_path}")
                    report('completed', 'FFmpeg 已就绪', ff_path, percent=100.0, level='success')
                else:
                    warning_msg = get_windows_ffmpeg_manual_setup_message()
                    logger.warning(warning_msg)
                    _append_settings_message(messages, 'warning', warning_msg)
                    report('warning', 'FFmpeg 未就绪', warning_msg, level='warning')
            else:
                report('completed', '配置已保存', '当前设置不需要额外下载 FFmpeg。', percent=100.0, level='success')
        except Exception as e:
            from modules.ffmpeg_manager import get_windows_ffmpeg_manual_setup_message
            warning_msg = f'检查内置 FFmpeg 状态失败: {e}。{get_windows_ffmpeg_manual_setup_message()}'
            logger.warning(warning_msg)
            _append_settings_message(messages, 'warning', warning_msg)
            report('warning', 'FFmpeg 检查失败', warning_msg, level='warning')

        if 'YOUTUBE_API_KEY' in form_data:
            api_key = form_data['YOUTUBE_API_KEY']
            if api_key:
                youtube_monitor.set_api_key(api_key)
                logger.info("YouTube API密钥已更新并同步到监控系统")

        _append_settings_message(messages, 'success', '配置已成功保存')
        final_level = 'warning' if any(msg['category'] in ('warning', 'danger') for msg in messages) else 'success'
        final_stage = 'warning' if final_level == 'warning' else 'completed'
        final_message = '配置已保存，但有提醒需要处理。' if final_level == 'warning' else '配置已成功保存'
        final_detail = next((msg['text'] for msg in messages if msg['category'] in ('warning', 'danger')), '设置已生效。')
        return {
            'success': True,
            'messages': messages,
            'updated_config': updated_config,
            'final_stage': final_stage,
            'final_message': final_message,
            'final_detail': final_detail,
            'final_level': final_level,
        }
    except Exception as e:
        logger.exception("保存设置失败: %s", e)
        _append_settings_message(messages, 'danger', f'保存设置失败: {e}')
        return {
            'success': False,
            'messages': messages,
            'updated_config': None,
            'final_stage': 'failed',
            'final_message': '保存设置失败',
            'final_detail': str(e),
            'final_level': 'error',
        }


def _finalize_settings_save_operation(operation_id: str, result: dict):
    current_state = _get_settings_save_progress(operation_id) or _new_settings_save_state(operation_id)
    percent = current_state.get('percent')
    if result.get('success') and percent is None:
        percent = 100.0

    _update_settings_save_progress(
        operation_id,
        stage=result.get('final_stage', 'completed'),
        message=result.get('final_message', ''),
        detail=result.get('final_detail', ''),
        percent=percent,
        done=True,
        level=result.get('final_level', 'success'),
        success=result.get('success'),
        messages=result.get('messages', []),
    )


def _run_settings_save_operation(operation_id: str, form_data: dict, uploads: dict):
    result = _perform_settings_save(form_data, uploads, operation_id=operation_id)
    _finalize_settings_save_operation(operation_id, result)


def _is_safe_redirect_url(target):
    """Validate that a redirect target is safe (same origin, not external)."""
    if not target:
        return False
    # Normalize whitespace to prevent bypass via leading/trailing spaces or
    # control characters (e.g. " http://evil.com" or "\nhttp://evil.com")
    target = target.strip()
    if not target:
        return False
    # Reject backslashes — some browsers treat \\ as // (e.g. \\evil.com)
    if '\\' in target:
        return False
    # Reject any URL with a scheme (e.g. http://, https://) or a netloc
    # (e.g. //evil.com protocol-relative URLs). Only purely relative URLs
    # (path, query, fragment) are allowed.
    parsed_target = urlparse(target)
    if parsed_target.scheme or parsed_target.netloc:
        return False
    # At this point, target is a relative URL without scheme or netloc,
    # which is safe from open redirect to an external host.
    return True


# 登录验证装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        config = load_config()
        if config.get('password_protection_enabled'):
            if 'logged_in' not in session:
                flash('请先登录以访问此页面。', 'info')
                return redirect(url_for('login', next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function

# 配置CORS，允许来自YouTube的跨域请求
CORS(app, resources={
    r"/tasks/add_via_extension": {
        "origins": ["*://www.youtube.com", "*://youtube.com", "https://www.youtube.com", "https://youtube.com"],
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# 确保日志目录存在
log_dir = get_app_subdir('logs')
os.makedirs(log_dir, exist_ok=True)

# 配置日志
log_file = os.path.join(log_dir, 'app.log')
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 文件处理器
file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=10, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.WARNING)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.WARNING)
# 确保Windows控制台编码正确
if os.name == 'nt':
    import sys
    import codecs
    # 检查Python版本和reconfigure方法可用性
    python_version = sys.version_info
    print(f"DEBUG: 当前Python版本: {python_version.major}.{python_version.minor}.{python_version.micro}")
    print(f"DEBUG: 操作系统: {os.name}, sys.stdout类型: {type(sys.stdout)}")
    
    # 强制设置stdout和stderr为UTF-8编码
    if hasattr(sys.stdout, 'reconfigure'):
        print("DEBUG: sys.stdout.reconfigure方法可用，正在设置UTF-8编码")
        try:
            sys.stdout.reconfigure(encoding='utf-8')  # type: ignore
            print("DEBUG: sys.stdout.reconfigure执行成功")
        except Exception as e:
            print(f"DEBUG: sys.stdout.reconfigure执行失败: {e}")
    else:
        print("DEBUG: sys.stdout.reconfigure方法不可用，跳过stdout编码设置")
        
    if hasattr(sys.stderr, 'reconfigure'):
        print("DEBUG: sys.stderr.reconfigure方法可用，正在设置UTF-8编码")
        try:
            sys.stderr.reconfigure(encoding='utf-8')  # type: ignore
            print("DEBUG: sys.stderr.reconfigure执行成功")
        except Exception as e:
            print(f"DEBUG: sys.stderr.reconfigure执行失败: {e}")
    else:
        print("DEBUG: sys.stderr.reconfigure方法不可用，跳过stderr编码设置")
    
    # 设置环境变量
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 为控制台处理器设置编码
    try:
        console_handler.setStream(codecs.getwriter('utf-8')(sys.stdout.buffer))  # type: ignore
        print("DEBUG: 控制台处理器编码设置成功")
    except Exception as e:
        print(f"DEBUG: 控制台处理器编码设置失败: {e}")

# 配置根日志记录器
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# 强制设置所有日志记录器的默认编码为UTF-8
try:
    logging.getLogger().handlers[0].encoding = 'utf-8'  # type: ignore
    print("DEBUG: 第一个日志处理器编码设置成功")
except Exception as e:
    print(f"DEBUG: 第一个日志处理器编码设置失败: {e}")

try:
    if len(logging.getLogger().handlers) > 1:
        logging.getLogger().handlers[1].encoding = 'utf-8'  # type: ignore
        print("DEBUG: 第二个日志处理器编码设置成功")
except Exception as e:
    print(f"DEBUG: 第二个日志处理器编码设置失败: {e}")

# 配置应用日志记录器
logger = logging.getLogger('Y2A-Auto')
logger.setLevel(logging.WARNING)

def init_id_mapping():
    """
    初始化AcFun分区ID映射.
    id_mapping.json 文件现在应该由 acfunid/ 目录直接提供，并包含在Docker镜像中。
    此函数仅记录一条信息，不再执行文件生成或检查逻辑。
    """
    logger.info("AcFun分区ID映射 (id_mapping.json) 应由 'acfunid/' 目录提供。")

# 模板辅助函数
def task_status_display(status):
    """将任务状态代码转换为显示文本"""
    status_map = {
        TASK_STATES['PENDING']: '等待处理',
        TASK_STATES['DOWNLOADING']: '下载中',
        TASK_STATES['DOWNLOADED']: '下载完成',
        TASK_STATES['TRANSLATING_SUBTITLE']: '翻译字幕中',
    TASK_STATES['ASR_TRANSCRIBING']: '语音转写中',
        TASK_STATES['ENCODING_VIDEO']: '转码视频中',
        TASK_STATES['TRANSLATING']: '翻译中',
        TASK_STATES['TAGGING']: '生成标签中',
        TASK_STATES['PARTITIONING']: '推荐分区中',
        TASK_STATES['MODERATING']: '内容审核中',
        TASK_STATES['AWAITING_REVIEW']: '等待人工审核',
        TASK_STATES['READY_FOR_UPLOAD']: '准备上传',
        TASK_STATES['UPLOADING']: '上传中',
        TASK_STATES['COMPLETED']: '已完成',
        TASK_STATES['FAILED']: '失败',
        'fetching_info': '采集信息中',
        'info_fetched': '信息已采集',
    }
    return status_map.get(status, status)

def task_status_color(status):
    """将任务状态代码转换为显示颜色"""
    color_map = {
        TASK_STATES['PENDING']: 'secondary',
        TASK_STATES['DOWNLOADING']: 'info',
        TASK_STATES['DOWNLOADED']: 'info',
        TASK_STATES['TRANSLATING_SUBTITLE']: 'info',
    TASK_STATES['ASR_TRANSCRIBING']: 'info',
        TASK_STATES['ENCODING_VIDEO']: 'info',
        TASK_STATES['TRANSLATING']: 'info',
        TASK_STATES['TAGGING']: 'info',
        TASK_STATES['PARTITIONING']: 'info',
        TASK_STATES['MODERATING']: 'info',
        TASK_STATES['AWAITING_REVIEW']: 'warning',
        TASK_STATES['READY_FOR_UPLOAD']: 'primary',
        TASK_STATES['UPLOADING']: 'primary',
        TASK_STATES['COMPLETED']: 'success',
        TASK_STATES['FAILED']: 'danger'
    }
    return color_map.get(status, 'secondary')

def _get_bilibili_zone_data():
    try:
        from bilibili_api import video_zone
        return video_zone.get_zone_list_sub() or []
    except Exception as e:
        logger.warning(f"读取bilibili分区数据失败: {e}")
        return []


def _load_acfun_partition_mapping():
    id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
    try:
        with open(id_mapping_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读取AcFun分区映射失败: {e}")
        return []


def _build_bilibili_partition_mapping():
    id_mapping = []
    zone_data = _get_bilibili_zone_data()
    for parent in zone_data:
        if not isinstance(parent, dict):
            continue
        parent_tid = parent.get('tid')
        parent_name = parent.get('name')
        if parent_tid in (None, 0, '0') or not parent_name:
            continue
        id_mapping.append({
            'category': parent_name,
            'partitions': [{
                'id': str(parent_tid),
                'name': parent_name,
                'sub_partitions': [
                    {
                        'id': str(sub.get('tid')),
                        'name': sub.get('name'),
                    }
                    for sub in (parent.get('sub') or [])
                    if isinstance(sub, dict) and sub.get('tid') not in (None, 0, '0') and sub.get('name')
                ]
            }]
        })
    return id_mapping


def get_partition_name(partition_id, upload_target='acfun'):
    """根据分区ID和平台获取分区名称"""
    if not partition_id:
        return None

    target = str(upload_target or 'acfun').strip().lower()
    pid = str(partition_id)

    if target == 'bilibili':
        try:
            zone_data = _get_bilibili_zone_data()
            for parent in zone_data:
                if str(parent.get('tid')) == pid:
                    return parent.get('name')
                for sub in parent.get('sub', []) or []:
                    if str(sub.get('tid')) == pid:
                        return sub.get('name')
        except Exception as e:
            logger.error(f"获取bilibili分区名称时出错: {str(e)}")
        return None

    # 默认 AcFun
    id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
    try:
        with open(id_mapping_path, 'r', encoding='utf-8') as f:
            id_mapping = json.load(f)

        for category in id_mapping:
            for partition in category.get('partitions', []):
                if str(partition.get('id')) == pid:
                    return partition.get('name')

                for sub_partition in partition.get('sub_partitions', []):
                    if str(sub_partition.get('id')) == pid:
                        return sub_partition.get('name')
    except Exception as e:
        logger.error(f"获取AcFun分区名称时出错: {str(e)}")

    return None

def parse_json(json_str):
    """将JSON字符串解析为Python对象"""
    if not json_str:
        return {}  # 返回空字典
    
    try:
        return json.loads(json_str)
    except Exception as e:
        logger.error(f"解析JSON时出错: {str(e)}")
        return {} # 返回空字典

def parse_youtube_duration(duration_str):
    """解析YouTube ISO 8601时长格式为秒数"""
    import re
    
    if not duration_str:
        return 0
    
    # PT1H30M45S -> 1小时30分45秒
    pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
    match = re.match(pattern, duration_str)
    
    if not match:
        return 0
    
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    
    return hours * 3600 + minutes * 60 + seconds

# 注册模板过滤器
app.jinja_env.filters['parse_youtube_duration'] = parse_youtube_duration

ALIYUN_LABEL_MAP = {
    "pornographic_adult": "疑似色情内容",
    "sexual_terms": "疑似性健康内容",
    "sexual_suggestive": "疑似低俗内容",
    "political_figure": "疑似政治人物",
    "political_entity": "疑似政治实体",
    "political_n": "疑似敏感政治内容",
    "political_p": "疑似涉政禁宣人物",
    "political_a": "涉政专项升级保障",
    "violent_extremist": "疑似极端组织",
    "violent_incidents": "疑似极端主义内容",
    "violent_weapons": "疑似武器弹药",
    "contraband_drug": "疑似毒品相关",
    "contraband_gambling": "疑似赌博相关",
    "contraband_act": "疑似违禁行为",
    "contraband_entity": "疑似违禁工具",
    "inappropriate_discrimination": "疑似偏见歧视内容",
    "inappropriate_ethics": "疑似不良价值观内容",
    "inappropriate_profanity": "疑似攻击辱骂内容",
    "inappropriate_oral": "疑似低俗口头语内容",
    "inappropriate_superstition": "疑似封建迷信内容",
    "inappropriate_nonsense": "疑似无意义灌水内容",
    "pt_to_sites": "疑似站外引流",
    "pt_by_recruitment": "疑似网赚兼职广告",
    "pt_to_contact": "疑似引流广告号",
    "religion_b": "疑似涉及佛教",
    "religion_t": "疑似涉及道教",
    "religion_c": "疑似涉及基督教",
    "religion_i": "疑似涉及伊斯兰教",
    "religion_h": "疑似涉及印度教",
    "customized": "命中自定义词库",
    "nonLabel": "内容正常", # 通常表示无风险
    "normal": "内容正常" # 另一种表示无风险的标签
    # 可以根据需要添加更多映射
}

def get_aliyun_label_chinese(label_value):
    """获取阿里云审核标签的中文含义"""
    return ALIYUN_LABEL_MAP.get(label_value, label_value) # 如果找不到映射，返回原始标签

# 注册模板辅助函数
app.jinja_env.globals.update(
    task_status_display=task_status_display,
    task_status_color=task_status_color,
    get_partition_name=get_partition_name,
    parse_json=parse_json,
    get_aliyun_label_chinese=get_aliyun_label_chinese # 添加新的辅助函数
)

@app.route('/login', methods=['GET', 'POST'])
def login():
    config = load_config()
    # 如果密码保护未启用，或已登录，则重定向到首页
    if not config.get('password_protection_enabled'):
        return redirect(url_for('index'))
    if 'logged_in' in session:
        return redirect(url_for('index'))

    # 读取登录安全状态
    sec = _load_security_state()
    now_ts = time.time()
    # 检查是否处于锁定期
    if sec.get('locked_until', 0) and now_ts < sec['locked_until']:
        remaining = int(sec['locked_until'] - now_ts)
        minutes = remaining // 60
        seconds = remaining % 60
        flash(f'登录已被临时锁定，请 {minutes} 分 {seconds} 秒后重试。', 'danger')
        return render_template('login.html')

    if request.method == 'POST':
        password = request.form.get('password')
        stored_password = config.get('password')

        # 检查是否有设置密码
        if not stored_password:
            flash('系统尚未设置密码，无法登录。请在禁用密码保护的情况下，进入设置页面设置密码。', 'danger')
            return render_template('login.html')

        if password and password == stored_password:
            session['logged_in'] = True
            session.permanent = True  # session持久化
            # 登录成功，重置失败计数与锁定
            sec.update({'failed_attempts': 0, 'locked_until': 0, 'last_attempt': now_ts})
            _save_security_state(sec)
            flash('登录成功', 'success')
            next_url = request.args.get('next')
            safe_next_url = next_url if _is_safe_redirect_url(next_url) else None
            return redirect(safe_next_url or url_for('index'))
        else:
            # 密码错误，更新失败计数
            max_attempts = int(config.get('LOGIN_MAX_FAILED_ATTEMPTS', 5) or 5)
            lock_minutes = int(config.get('LOGIN_LOCKOUT_MINUTES', 15) or 15)
            failed = int(sec.get('failed_attempts', 0) or 0) + 1
            sec['failed_attempts'] = failed
            sec['last_attempt'] = now_ts
            # 达到阈值则锁定
            if failed >= max_attempts:
                sec['locked_until'] = now_ts + lock_minutes * 60
                _save_security_state(sec)
                flash(f'密码错误次数过多（{failed}/{max_attempts}），已锁定 {lock_minutes} 分钟。', 'danger')
            else:
                _save_security_state(sec)
                remain = max_attempts - failed
                flash(f'密码错误。还可尝试 {remain} 次后将被锁定。', 'danger')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('您已成功退出。', 'info')
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    """首页"""
    logger.info("访问首页")
    # 统计信息用于仪表盘
    try:
        from modules.task_manager import get_db_connection
        conn = get_db_connection()
        cur = conn.cursor()

        # 本地时间的今日起止
        now_local = datetime.now()
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        fmt = "%Y-%m-%d %H:%M:%S"
        start_str = today_start.strftime(fmt)
        end_str = tomorrow_start.strftime(fmt)

        # 各类计数
        cur.execute("SELECT COUNT(*) FROM tasks")
        total_tasks = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['AWAITING_REVIEW'],))
        awaiting_review = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['FAILED'],))
        failed_total = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['PENDING'],))
        pending_total = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TASK_STATES['READY_FOR_UPLOAD'],))
        ready_total = cur.fetchone()[0] or 0

        # 进行中的状态集合
        processing_states = (
            'fetching_info', 'info_fetched',
            TASK_STATES['TRANSLATING'], TASK_STATES['TAGGING'], TASK_STATES['PARTITIONING'],
            TASK_STATES['MODERATING'], TASK_STATES['DOWNLOADING'], TASK_STATES['DOWNLOADED'],
            TASK_STATES['ASR_TRANSCRIBING'], TASK_STATES['TRANSLATING_SUBTITLE'],
            TASK_STATES['ENCODING_VIDEO'], TASK_STATES['UPLOADING']
        )
        placeholders = ",".join(["?"] * len(processing_states))
        cur.execute(f"SELECT COUNT(*) FROM tasks WHERE status IN ({placeholders})", processing_states)
        in_progress = cur.fetchone()[0] or 0

        # 今日完成/失败/新增任务
        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ? AND updated_at < ?",
            (TASK_STATES['COMPLETED'], start_str, end_str)
        )
        completed_today = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ? AND updated_at >= ? AND updated_at < ?",
            (TASK_STATES['FAILED'], start_str, end_str)
        )
        failed_today = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND created_at < ?",
            (start_str, end_str)
        )
        created_today = cur.fetchone()[0] or 0

        # 最近任务（按更新时间倒序）
        cur.execute(
            "SELECT id, video_title_translated, video_title_original, status, updated_at, upload_target, acfun_upload_response, bilibili_upload_response FROM tasks ORDER BY updated_at DESC LIMIT 10"
        )
        rows = cur.fetchall()
        recent_tasks = []
        for r in rows:
            upload_id = None
            upload_target = (r[5] or 'acfun').lower()
            try:
                if upload_target == 'both':
                    resp_b = json.loads(r[7]) if r[7] else None
                    resp_a = json.loads(r[6]) if r[6] else None
                    bv = resp_b.get('bvid') if isinstance(resp_b, dict) else None
                    ac = resp_a.get('ac_number') if isinstance(resp_a, dict) else None
                    if bv and ac:
                        upload_id = f"{bv} / AC{ac}"
                    elif bv:
                        upload_id = bv
                    elif ac:
                        upload_id = f"AC{ac}"
                elif upload_target == 'bilibili':
                    resp = json.loads(r[7]) if r[7] else None
                    if isinstance(resp, dict):
                        upload_id = resp.get('bvid') or resp.get('aid')
                else:
                    resp = json.loads(r[6]) if r[6] else None
                    if isinstance(resp, dict):
                        upload_id = resp.get('ac_number')
            except Exception:
                upload_id = None
            recent_tasks.append({
                'id': r[0],
                'title': r[1] or r[2] or '未获取标题',
                'status': r[3],
                'updated_at': r[4],
                'upload_target': upload_target,
                'upload_id': upload_id
            })

        conn.close()

        stats = {
            'total_tasks': total_tasks,
            'awaiting_review': awaiting_review,
            'failed_total': failed_total,
            'pending_total': pending_total,
            'ready_total': ready_total,
            'in_progress': in_progress,
            'completed_today': completed_today,
            'failed_today': failed_today,
            'created_today': created_today
        }
    except Exception as e:
        logger.warning(f"首页统计失败: {e}")
        stats = {
            'total_tasks': 0,
            'awaiting_review': 0,
            'failed_total': 0,
            'pending_total': 0,
            'ready_total': 0,
            'in_progress': 0,
            'completed_today': 0,
            'failed_today': 0,
            'created_today': 0
        }
        recent_tasks = []

    return render_template('index.html', stats=stats, recent_tasks=recent_tasks)

@app.route('/tasks')
@login_required
def tasks():
    """任务列表页面"""
    logger.info("访问任务列表页面")
    
    # 获取分页参数
    page = request.args.get('page', 1, type=int)
    per_page = 20  # 每页显示20条记录
    
    # 获取分页数据
    pagination_data = get_tasks_paginated(page=page, per_page=per_page)
    config = load_config()
    
    return render_template('tasks.html', 
                         tasks=pagination_data['tasks'],
                         pagination=pagination_data,
                         config=config)
    
@app.route('/tasks/stream')
@login_required
def tasks_event_stream():
    """Server-Sent Events stream for realtime task updates."""

    def generate():
        listener = register_task_updates_listener()
        try:
            yield 'data: {"type":"welcome"}\n\n'
            while True:
                try:
                    event = listener.get(timeout=10)  # 减少心跳间隔到 10 秒
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except Empty:
                    yield 'data: {"type":"heartbeat"}\n\n'
        except GeneratorExit:
            pass
        finally:
            unregister_task_updates_listener(listener)

    response = Response(stream_with_context(generate()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    response.headers['Transfer-Encoding'] = 'chunked'
    return response

@app.route('/manual_review')
@login_required
def manual_review():
    """人工审核列表页面"""
    logger.info("访问人工审核列表页面")
    review_tasks = get_tasks_by_status(TASK_STATES['AWAITING_REVIEW'])
    
    # 封面图片现在直接从downloads目录提供
    
    return render_template('manual_review.html', tasks=review_tasks)

@app.route('/tasks/<task_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_task(task_id):
    """任务编辑页面"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if request.method == 'POST':
        action = request.form.get('action', 'save_metadata').strip().lower()
        redirect_target = url_for('edit_task', task_id=task_id)

        if action == 'replace_cover':
            try:
                cover_file = request.files.get('cover_file')
                _replace_task_cover(task, cover_file)
                flash('任务封面已更新。', 'success')
            except Exception as e:
                logger.warning(f"替换任务 {task_id} 封面失败: {e}")
                flash(f'更换封面失败: {e}', 'danger')
            return redirect(redirect_target)

        if action == 'restore_cover':
            try:
                _restore_task_cover(task)
                flash('已恢复原始封面。', 'success')
            except Exception as e:
                logger.warning(f"恢复任务 {task_id} 原始封面失败: {e}")
                flash(f'恢复原封面失败: {e}', 'danger')
            return redirect(redirect_target)

        upload_target = str(task.get('upload_target') or 'acfun').lower()
        # 处理表单提交
        video_title = request.form.get('video_title_translated', '')
        description = request.form.get('description_translated', '')
        legacy_partition_id = request.form.get('selected_partition_id', '')
        partition_id_acfun = request.form.get('selected_partition_id_acfun', '')
        partition_id_bilibili = request.form.get('selected_partition_id_bilibili', '')
        tags_json = request.form.get('tags_json', '[]')

        if upload_target == 'both':
            partition_id_acfun = partition_id_acfun or legacy_partition_id
            partition_id_bilibili = partition_id_bilibili or legacy_partition_id
        elif upload_target == 'bilibili':
            partition_id_bilibili = partition_id_bilibili or legacy_partition_id
        else:
            partition_id_acfun = partition_id_acfun or legacy_partition_id
        # 更新任务信息
        update_data = {
            'video_title_translated': video_title,
            'description_translated': description,
            'selected_partition_id_acfun': partition_id_acfun,
            'selected_partition_id_bilibili': partition_id_bilibili,
            'tags_generated': tags_json
        }

        # 只有在安全状态下才允许设置为可上传状态，避免与正在处理的任务产生竞态条件
        safe_states_to_make_uploadable = [
            TASK_STATES['DOWNLOADED'],        # 已下载，可以上传
            TASK_STATES['MODERATING'],        # 审核中，可以手动干预
            TASK_STATES['AWAITING_REVIEW'],   # 等待人工审核
            TASK_STATES['FAILED'],            # 失败状态，可以重试
            TASK_STATES['UPLOADING']          # 允许重置卡住的上传状态
        ]
        
        if task['status'] in safe_states_to_make_uploadable:
            update_data['status'] = TASK_STATES['READY_FOR_UPLOAD']
        
        # 调试：检查update_task函数参数类型
        print(f"DEBUG: update_task参数 - task_id: {type(task_id)}, update_data: {type(update_data)}")
        print(f"DEBUG: update_data内容: {update_data}")
        # 检查是否有silent参数类型问题
        if 'silent' in update_data:
            print(f"DEBUG: silent参数值: {update_data['silent']}, 类型: {type(update_data['silent'])}")
        try:
            # 确保silent参数是布尔类型
            final_update_data = update_data.copy()
            silent_param = False  # 默认值
            
            if 'silent' in final_update_data:
                if isinstance(final_update_data['silent'], str):
                    silent_param = final_update_data['silent'].lower() in ('true', 'yes', '1', 'on')
                elif isinstance(final_update_data['silent'], bool):
                    silent_param = final_update_data['silent']
                # 从final_update_data中移除silent，避免重复传递
                final_update_data.pop('silent')
            
            update_task(task_id, silent=silent_param, **final_update_data)
            print("DEBUG: update_task调用成功")
        except Exception as e:
            print(f"DEBUG: update_task调用失败: {e}")
        logger.info(f"任务 {task_id} 信息已更新")
        updated_task = get_task(task_id)
        if updated_task and updated_task['status'] == TASK_STATES['READY_FOR_UPLOAD']:
            flash('任务已保存，当前可单独执行上传。', 'success')
        else:
            flash('任务已保存。', 'success')

        return redirect(redirect_target)
    
    # GET请求，显示编辑页面
    # 封面图片现在直接从downloads目录提供
    upload_target = str(task.get('upload_target') or 'acfun').lower()
    acfun_id_mapping = _load_acfun_partition_mapping()
    bilibili_id_mapping = _build_bilibili_partition_mapping()
    id_mapping = bilibili_id_mapping if upload_target == 'bilibili' else acfun_id_mapping
    
    # 准备标签字符串
    tags_string = ""
    if task.get('tags_generated'):
        try:
            tags = json.loads(task['tags_generated'])
            tags_string = ", ".join(tags)
        except Exception as e:
            logger.error(f"解析标签JSON失败: {str(e)}")
    
    # 获取当前配置
    config = load_config()
    can_upload = task['status'] in [
        TASK_STATES['COMPLETED'],
        TASK_STATES['PENDING'],
        TASK_STATES['READY_FOR_UPLOAD'],
        TASK_STATES['AWAITING_REVIEW']
    ]
    has_original_cover_backup = False
    has_cover_preview = False
    is_custom_cover_active = False
    current_cover_filename = ''
    try:
        task_dir_real = _get_task_dir_real(task_id)
        has_original_cover_backup = bool(os.path.isdir(task_dir_real) and _find_original_cover_backup(task_dir_real))
        active_cover_path = _get_current_cover_path(task, task_dir_real) if os.path.isdir(task_dir_real) else ''
        has_cover_preview = bool(active_cover_path)
        current_cover_filename = os.path.basename(active_cover_path) if active_cover_path else ''
        is_custom_cover_active = current_cover_filename.startswith('custom_cover.')
    except Exception:
        has_original_cover_backup = False
        has_cover_preview = bool(task.get('cover_path_local'))
        is_custom_cover_active = False
        current_cover_filename = os.path.basename(str(task.get('cover_path_local') or ''))
    
    return render_template(
        'edit_task.html', 
        task=task, 
        id_mapping=id_mapping, 
        acfun_id_mapping=acfun_id_mapping,
        bilibili_id_mapping=bilibili_id_mapping,
        tags_string=tags_string,
        config=config,
        upload_target=upload_target,
        can_upload=can_upload,
        has_cover_preview=has_cover_preview,
        has_original_cover_backup=has_original_cover_backup,
        is_custom_cover_active=is_custom_cover_active,
        current_cover_filename=current_cover_filename
    )

@app.route('/tasks/<task_id>/cover')
@login_required
def get_task_cover(task_id):
    """获取任务封面图片"""
    task = get_task(task_id)
    
    if not task:
        # 返回默认图片或404
        return '', 404
    
    cover_path = task.get('cover_path_local')
    
    if cover_path and os.path.exists(cover_path):
        mime_type, _ = mimetypes.guess_type(cover_path)
        return send_file(cover_path, mimetype=mime_type)
    
    # 如果没有封面，尝试在任务目录中查找
    downloads_dir = get_app_subdir('downloads')
    task_dir = os.path.join(downloads_dir, task_id)

    # 防止路径遍历攻击：验证路径在downloads目录内
    try:
        task_dir_real = os.path.realpath(task_dir)
        downloads_dir_real = os.path.realpath(downloads_dir)
        if not task_dir_real.startswith(downloads_dir_real + os.sep):
            return '', 404
    except (ValueError, OSError):
        return '', 404

    if os.path.exists(task_dir_real):
        # 查找常见的封面文件名
        cover_names = ['cover.jpg', 'cover.png', 'cover.webp', 'thumbnail.jpg', 'thumbnail.png', 'thumbnail.webp']
        for name in cover_names:
            potential_cover = os.path.join(task_dir_real, name)
            potential_cover_real = os.path.realpath(potential_cover)
            if potential_cover_real.startswith(downloads_dir_real + os.sep) and os.path.exists(potential_cover_real):
                return send_file(potential_cover_real)

        # 查找任何图片文件
        for filename in os.listdir(task_dir_real):
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                file_path = os.path.join(task_dir_real, filename)
                file_path_real = os.path.realpath(file_path)
                if file_path_real.startswith(downloads_dir_real + os.sep):
                    return send_file(file_path_real)
    
    # 没有找到封面
    return '', 404

@app.route('/tasks/<task_id>/review')
@login_required
def review_task(task_id):
    """重定向到任务编辑页面"""
    return redirect(url_for('edit_task', task_id=task_id))

@app.route('/tasks/add_via_extension', methods=['POST', 'OPTIONS'])
@login_required
def add_task_via_extension():
    """
    通过浏览器扩展或API添加任务 (JSON格式)
    支持Telegram Bot、浏览器扩展等外部服务调用
    """
    # 处理CORS预检请求
    if request.method == 'OPTIONS':
        return '', 200
    
    try:
        # 优先从JSON获取，兼容form表单
        if request.is_json:
            data = request.get_json()
            youtube_url = data.get('youtube_url') if data else None
            upload_target = data.get('upload_target') if data else None
        else:
            youtube_url = request.form.get('youtube_url')
            upload_target = request.form.get('upload_target')

        if not youtube_url:
            return jsonify({'success': False, 'message': 'YouTube URL不能为空'}), 400

        config = load_config()
        if not upload_target:
            upload_target = config.get('UPLOAD_TARGET_DEFAULT', 'acfun')
        
        # 判断是否为播放列表URL
        if 'youtube.com/playlist' in youtube_url or 'youtu.be/playlist' in youtube_url:
            # 提取所有视频URL
            cookies_path = config.get('YOUTUBE_COOKIES_PATH')
            video_urls = extract_video_urls_from_playlist(youtube_url, cookies_path)
            if not video_urls:
                return jsonify({'success': False, 'message': '未能提取到播放列表中的视频'}), 400
            
            added_count = 0
            task_ids = []
            for url in video_urls:
                task_id = add_task(url, upload_target=upload_target)
                if task_id:
                    added_count += 1
                    task_ids.append(task_id)
                    # 自动模式下启动任务
                    if config.get('AUTO_MODE_ENABLED', False):
                        start_task(task_id, config)
            
            return jsonify({
                'success': True,
                'message': f'已批量添加 {added_count} 个视频任务（来自播放列表）',
                'task_ids': task_ids,
                'count': added_count
            }), 200
        else:
            # 单个视频
            task_id = add_task(youtube_url, upload_target=upload_target)
            if task_id:
                if config.get('AUTO_MODE_ENABLED', False):
                    logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
                    start_task(task_id, config)
                    return jsonify({
                        'success': True,
                        'message': f'任务已添加并开始处理',
                        'task_id': task_id
                    }), 200
                else:
                    return jsonify({
                        'success': True,
                        'message': '任务已添加',
                        'task_id': task_id
                    }), 200
            else:
                return jsonify({'success': False, 'message': '添加任务失败'}), 500
                
    except Exception as e:
        logger.error(f"通过扩展添加任务失败: {str(e)}")
        return jsonify({'success': False, 'message': '服务器内部错误，请稍后重试'}), 500

@app.route('/tasks/add', methods=['POST'])
@login_required
def add_task_route():
    """添加新任务，支持播放列表批量添加"""
    youtube_url = request.form.get('youtube_url')
    upload_target = request.form.get('upload_target')
    
    if not youtube_url:
        flash('YouTube URL不能为空', 'danger')
        return redirect(url_for('tasks'))

    config = load_config()
    if not upload_target:
        upload_target = config.get('UPLOAD_TARGET_DEFAULT', 'acfun')

    # 判断是否为播放列表URL
    if 'youtube.com/playlist' in youtube_url or 'youtu.be/playlist' in youtube_url:
        # 提取所有视频URL
        cookies_path = config.get('YOUTUBE_COOKIES_PATH')
        video_urls = extract_video_urls_from_playlist(youtube_url, cookies_path)
        if not video_urls:
            flash('未能提取到播放列表中的视频', 'danger')
            return redirect(url_for('tasks'))
        added_count = 0
        for url in video_urls:
            task_id = add_task(url, upload_target=upload_target)
            if task_id:
                added_count += 1
        flash(f'已批量添加 {added_count} 个视频任务（来自播放列表）', 'success')
        return redirect(url_for('tasks'))
    else:
        task_id = add_task(youtube_url, upload_target=upload_target)
        if task_id:
            if config.get('AUTO_MODE_ENABLED', False):
                logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
                start_task(task_id, config)
                flash(f'任务已添加并开始处理: {youtube_url}', 'success')
            else:
                flash(f'任务已添加: {youtube_url}', 'success')
        else:
            flash(f'添加任务失败: {youtube_url}', 'danger')
        return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/start', methods=['POST'])
@login_required
def start_task_route(task_id):
    """开始处理任务"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['FAILED']]:
        flash(f'当前任务状态为 {task_status_display(task["status"])}，不能启动', 'warning')
        return redirect(url_for('tasks'))
    
    # 获取当前配置
    config = load_config()
    
    # 启动任务处理
    success = start_task(task_id, config)
    
    if success:
        # 检查是否是自动模式
        if config.get('AUTO_MODE_ENABLED', False):
            flash('任务已启动，自动模式将会自动完成下载、处理和上传', 'info')
            
            # 等待一段时间后检查任务状态
            import threading
            
            # 使用传统页面刷新方式
        else:
            flash('任务处理已启动', 'success')
    else:
        flash('启动任务处理失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/delete', methods=['POST'])
@login_required
def delete_task_route(task_id):
    """删除任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    success = delete_task(task_id, delete_files)
    
    if success:
        flash('任务已删除', 'success')
    else:
        flash('删除任务失败', 'danger')
    
    return redirect(url_for('tasks'))


@app.route('/tasks/clear_all', methods=['POST'])
@login_required
def clear_all_tasks_route():
    """清空所有任务（可选择同时删除任务文件）"""
    try:
        delete_files = request.form.get('delete_files', 'true').lower() in ['true', '1', 'on']
        success = clear_all_tasks(delete_files=delete_files)
        if success:
            flash('所有任务已清空', 'success')
        else:
            flash('清空任务失败，请查看日志', 'danger')
    except Exception as e:
        logger.error(f"清空所有任务失败: {e}")
        flash(f'清空任务失败: {e}', 'danger')
    return redirect(url_for('tasks'))


@app.route('/tasks/retry_failed', methods=['POST'])
@login_required
def retry_failed_tasks_route():
    """重新调度所有失败的任务（从任务管理器调用）"""
    try:
        # 加载最新配置
        cfg = load_config()
        result = retry_failed_tasks(cfg)
        if isinstance(result, dict):
            scheduled = result.get('scheduled', 0)
            total = result.get('total', 0)
            flash(f'已重新调度 {scheduled}/{total} 个失败任务', 'success')
        else:
            flash('重新调度失败，请查看日志', 'danger')
    except Exception as e:
        logger.error(f"重试失败任务失败: {e}")
        flash(f'重试失败任务失败: {e}', 'danger')
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/force_upload', methods=['POST'])
@login_required
def force_upload_task_route(task_id):
    """强制上传任务"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('manual_review'))
    
    # 获取当前配置
    config = load_config()
    upload_target = str(task.get('upload_target') or 'acfun').lower()
    platform_name = '双平台' if upload_target == 'both' else ('bilibili' if upload_target == 'bilibili' else 'AcFun')
    
    # 启动后台强制上传
    logger.info(f"开始后台强制上传任务 {task_id} 到{platform_name}")
    flash(f'已启动强制上传到{platform_name}，正在后台处理...', 'info')
    
    import threading
    
    def background_force_upload():
        """后台强制上传函数"""
        try:
            # 调用上传函数
            success = force_upload_task(task_id, config)
                
            if success:
                logger.info(f"任务 {task_id} 后台强制上传成功")
            else:
                logger.error(f"任务 {task_id} 后台强制上传失败")
        except Exception as e:
            logger.error(f"任务 {task_id} 后台强制上传出错: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
    
    # 启动后台线程
    upload_thread = threading.Thread(target=background_force_upload, daemon=True)
    upload_thread.start()

    next_url = request.form.get('next') or request.args.get('next')
    safe_next_url = next_url if _is_safe_redirect_url(next_url) else None
    if safe_next_url:
        return redirect(safe_next_url)
    return redirect(url_for('manual_review'))

@app.route('/tasks/reset_stuck', methods=['POST'])
@login_required
def reset_stuck_tasks_route():
    """重置卡住的任务"""
    from modules.task_manager import reset_stuck_tasks
    
    try:
        reset_count = reset_stuck_tasks()
        if reset_count > 0:
            flash(f'已重置 {reset_count} 个卡住的任务', 'success')
        else:
            flash('没有发现卡住的任务', 'info')
    except Exception as e:
        logger.error(f"重置卡住任务失败: {str(e)}")
        flash('重置卡住任务失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/abandon', methods=['POST'])
@login_required
def abandon_task_route(task_id):
    """放弃任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    # 更新任务状态为失败
    update_task(task_id, status=TASK_STATES['FAILED'], error_message="用户主动放弃任务")
    
    if delete_files:
        # 删除任务文件
        from modules.task_manager import delete_task_files
        delete_task_files(task_id)
    
    flash('任务已废弃', 'success')
    return redirect(url_for('tasks'))

# 系统健康检查辅助函数

def check_docker_volumes():
    """检查Docker挂载卷状态"""
    volumes = {}
    app_root = os.path.dirname(os.path.abspath(__file__))
    
    volume_paths = [
        ('config', 'config'),
        ('db', 'db'),
        ('downloads', 'downloads'),
        ('logs', 'logs'),
        ('cookies', 'cookies'),
        ('temp', 'temp')
    ]
    
    for name, path in volume_paths:
        full_path = os.path.join(app_root, path)
        volumes[name] = {
            'path': full_path,
            'exists': os.path.exists(full_path),
            'is_mount': os.path.ismount(full_path),
            'writable': os.access(full_path, os.W_OK) if os.path.exists(full_path) else False,
            'size_mb': get_directory_size(full_path) if os.path.exists(full_path) else 0
        }
    
    return volumes

def get_directory_size(path):
    """获取目录大小(MB)"""
    try:
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    total_size += os.path.getsize(fp)
        return round(total_size / 1024 / 1024, 2)
    except:
        return 0

def get_database_info():
    """获取数据库文件信息"""
    try:
        from modules.task_manager import get_db_path
        db_path = get_db_path()
        
        if os.path.exists(db_path):
            stat_info = os.stat(db_path)
            return {
                'path': db_path,
                'size': stat_info.st_size,
                'writable': os.access(db_path, os.W_OK),
                'last_modified': stat_info.st_mtime
            }
        else:
            return {
                'path': db_path,
                'size': 0,
                'writable': False,
                'last_modified': None
            }
    except Exception as e:
        return {
            'path': 'unknown',
            'size': 0,
            'writable': False,
            'error': str(e)
        }

def get_database_debug_info():
    """获取数据库调试信息"""
    try:
        from modules.task_manager import get_db_path
        db_path = get_db_path()
        
        debug_info = {
            'db_path': db_path,
            'db_exists': os.path.exists(db_path),
            'db_dir': os.path.dirname(db_path),
            'db_dir_exists': os.path.exists(os.path.dirname(db_path)),
            'db_dir_writable': os.access(os.path.dirname(db_path), os.W_OK) if os.path.exists(os.path.dirname(db_path)) else False,
            'current_user': os.environ.get('USER', 'unknown'),
            'current_uid': os.getuid() if hasattr(os, 'getuid') else 'unknown',  # type: ignore
            'current_gid': os.getgid() if hasattr(os, 'getgid') else 'unknown'   # type: ignore
        }
        
        if os.path.exists(db_path):
            stat_info = os.stat(db_path)
            debug_info.update({
                'db_size': stat_info.st_size,
                'db_mode': oct(stat_info.st_mode)[-3:],
                'db_uid': stat_info.st_uid,
                'db_gid': stat_info.st_gid
            })
        
        return debug_info
    except Exception as e:
        return {'error': str(e)}

def get_file_info(file_path):
    """获取文件详细信息"""
    try:
        info = {
            'exists': os.path.exists(file_path),
            'size': 0,
            'readable': False,
            'last_modified': None
        }
        
        if info['exists']:
            stat_info = os.stat(file_path)
            info.update({
                'size': stat_info.st_size,
                'readable': os.access(file_path, os.R_OK),
                'last_modified': stat_info.st_mtime
            })
        
        return info
    except Exception as e:
        return {
            'exists': False,
            'size': 0,
            'readable': False,
            'last_modified': None,
            'error': str(e)
        }

def get_path_debug_info(file_path):
    """获取路径调试信息"""
    try:
        debug_info = {
            'path': file_path,
            'dirname': os.path.dirname(file_path),
            'basename': os.path.basename(file_path),
            'dirname_exists': os.path.exists(os.path.dirname(file_path)),
            'dirname_readable': os.access(os.path.dirname(file_path), os.R_OK) if os.path.exists(os.path.dirname(file_path)) else False,
            'dirname_writable': os.access(os.path.dirname(file_path), os.W_OK) if os.path.exists(os.path.dirname(file_path)) else False
        }
        
        # 列出目录内容
        if debug_info['dirname_exists'] and debug_info['dirname_readable']:
            try:
                debug_info['directory_contents'] = os.listdir(os.path.dirname(file_path))
            except:
                debug_info['directory_contents'] = 'permission_denied'
        
        return debug_info
    except Exception as e:
        return {'error': str(e)}

@app.route('/system_health')
def system_health():
    """系统健康检查 - 增强Docker环境兼容性"""
    from modules.task_manager import get_db_connection, validate_cookies, resolve_cookie_file_path
    import sqlite3
    import os
    import platform
    import sys
    
    # 检测运行环境
    is_docker = os.path.exists('/.dockerenv') or os.environ.get('CONTAINER') == 'docker'
    
    health_status = {
        'environment': {
            'platform': platform.system(),
            'python_version': sys.version.split()[0],
            'is_docker': is_docker,
            'user': os.environ.get('USER', 'unknown'),
            'working_directory': os.getcwd()
        },
        'database': {'status': 'unknown', 'message': ''},
        'youtube_cookies': {'status': 'unknown', 'message': ''},
        'acfun_cookies': {'status': 'unknown', 'message': ''},
        'bilibili_cookies': {'status': 'unknown', 'message': ''},
        'stuck_tasks': {'count': 0, 'tasks': []},
        'recent_errors': [],
        'docker_volumes': {}
    }
    
    # Docker环境特殊检查
    if is_docker:
        health_status['docker_volumes'] = check_docker_volumes()
    
    # 检查数据库
    try:
        logger.info("开始数据库健康检查...")
        conn = get_db_connection()
        
        # 测试基本连接
        cursor = conn.execute('SELECT COUNT(*) FROM tasks')
        task_count = cursor.fetchone()[0]
        
        # 检查数据库文件权限和位置
        db_info = get_database_info()
        
        health_status['database'] = {
            'status': 'ok',
            'message': f'数据库正常，共有 {task_count} 个任务',
            'location': db_info['path'],
            'size_mb': round(db_info['size'] / 1024 / 1024, 2),
            'writable': db_info['writable']
        }
        
        # 检查卡住的任务
        stuck_cursor = conn.execute('''
            SELECT id, status, created_at, updated_at, error_message
            FROM tasks 
            WHERE status IN ('processing', 'downloading', 'uploading', 'fetching_info', 'translating')
            AND datetime(updated_at) < datetime('now', '-30 minutes')
        ''')
        stuck_tasks = stuck_cursor.fetchall()
        health_status['stuck_tasks'] = {
            'count': len(stuck_tasks),
            'tasks': [{'id': t[0][:8] + '...', 'status': t[1], 'updated': t[3]} for t in stuck_tasks]
        }
        
        # 检查最近的错误
        error_cursor = conn.execute('''
            SELECT id, error_message, updated_at
            FROM tasks 
            WHERE status = 'failed' AND error_message IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 5
        ''')
        error_tasks = error_cursor.fetchall()
        health_status['recent_errors'] = [
            {'id': t[0][:8] + '...', 'error': t[1][:100] + '...' if len(t[1]) > 100 else t[1], 'time': t[2]}
            for t in error_tasks
        ]
        
        conn.close()
        logger.info("数据库健康检查完成")
    except Exception as e:
        logger.error(f"数据库健康检查失败: {str(e)}")
        health_status['database'] = {
            'status': 'error',
            'message': f'数据库错误: {str(e)}',
            'details': get_database_debug_info()
        }
    

    # 检查cookies - 使用更健壮的路径处理
    try:
        logger.info("开始cookies健康检查...")
        config = load_config()
        
        # YouTube cookies
        yt_cookies_path = config.get('YOUTUBE_COOKIES_PATH', 'cookies/yt_cookies.txt')
        if yt_cookies_path:
            # 如果是相对路径，转换为绝对路径
            yt_cookies_path = resolve_cookie_file_path(
                path_value=yt_cookies_path,
                default_relative_path='cookies/yt_cookies.txt',
                service_name='YouTube',
                logger_obj=logger,
                allow_json_txt_fallback=False
            )
            
            try:
                logger.debug(f"检查YouTube cookies文件: {yt_cookies_path}")
                is_valid, message = validate_cookies(yt_cookies_path, "YouTube")
                
                # 获取文件详细信息
                file_info = get_file_info(yt_cookies_path)
                
                health_status['youtube_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': yt_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception as e:
                logger.error(f"YouTube cookies检查异常: {str(e)}")
                health_status['youtube_cookies'] = {
                    'status': 'error',
                    'message': f'检查失败: {str(e)}',
                    'path': yt_cookies_path,
                    'debug_info': get_path_debug_info(yt_cookies_path)
                }
        else:
            health_status['youtube_cookies'] = {
                'status': 'warning',
                'message': '未配置YouTube cookies路径'
            }
        
        # AcFun cookies
        ac_cookies_path = resolve_cookie_file_path(
            path_value=config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'),
            default_relative_path='cookies/ac_cookies.json',
            service_name='AcFun',
            logger_obj=logger,
            allow_json_txt_fallback=True
        )
        if ac_cookies_path:
            try:
                logger.debug(f"检查AcFun cookies文件: {ac_cookies_path}")
                is_valid, message = validate_cookies(ac_cookies_path, "AcFun")
                
                # 获取文件详细信息
                file_info = get_file_info(ac_cookies_path)
                
                health_status['acfun_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': ac_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception as e:
                logger.error(f"AcFun cookies检查异常: {str(e)}")
                health_status['acfun_cookies'] = {
                    'status': 'error',
                    'message': f'检查失败: {str(e)}',
                    'path': ac_cookies_path,
                    'debug_info': get_path_debug_info(ac_cookies_path)
                }
        else:
            health_status['acfun_cookies'] = {
                'status': 'warning',
                'message': '未配置AcFun cookies路径'
            }

        # Bilibili cookies
        bili_cookies_path = config.get('BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json')
        if bili_cookies_path:
            bili_cookies_path = resolve_cookie_file_path(
                path_value=bili_cookies_path,
                default_relative_path='cookies/bili_cookies.json',
                service_name='Bilibili',
                logger_obj=logger,
                allow_json_txt_fallback=False
            )

            try:
                logger.debug(f"检查Bilibili cookies文件: {bili_cookies_path}")
                is_valid, message = validate_cookies(bili_cookies_path, "Bilibili")
                file_info = get_file_info(bili_cookies_path)
                health_status['bilibili_cookies'] = {
                    'status': 'ok' if is_valid else 'error',
                    'message': message,
                    'path': bili_cookies_path,
                    'exists': file_info['exists'],
                    'size': file_info['size'],
                    'readable': file_info['readable'],
                    'last_modified': file_info['last_modified']
                }
            except Exception as e:
                logger.error(f"Bilibili cookies检查异常: {str(e)}")
                health_status['bilibili_cookies'] = {
                    'status': 'error',
                    'message': f'检查失败: {str(e)}',
                    'path': bili_cookies_path,
                    'debug_info': get_path_debug_info(bili_cookies_path)
                }
        else:
            health_status['bilibili_cookies'] = {
                'status': 'warning',
                'message': '未配置Bilibili cookies路径'
            }
        
        logger.info("cookies健康检查完成")
            
    except Exception as e:
        logger.error(f"检查cookies时发生错误: {str(e)}")
        health_status['youtube_cookies'] = {
            'status': 'error',
            'message': f'检查失败: {str(e)}',
            'debug_info': str(e)
        }
        health_status['acfun_cookies'] = {
            'status': 'error',
            'message': f'检查失败: {str(e)}',
            'debug_info': str(e)
        }
        health_status['bilibili_cookies'] = {
            'status': 'error',
            'message': f'检查失败: {str(e)}',
            'debug_info': str(e)
        }
    
    return jsonify(health_status)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """设置页面，用于管理配置"""
    if request.method == 'POST':
        form_data = request.form.to_dict()
        uploads = _extract_settings_uploads(request.files)
        operation_id = str(form_data.get('save_operation_id') or uuid.uuid4())

        if _is_ajax_request():
            _update_settings_save_progress(
                operation_id,
                stage='saving_config',
                message='正在准备保存设置',
                detail='保存任务已创建，正在后台执行。',
                percent=None,
                done=False,
                level='info',
                success=None,
                messages=[]
            )
            save_thread = threading.Thread(
                target=_run_settings_save_operation,
                args=(operation_id, form_data, uploads),
                daemon=True,
                name=f'settings-save-{operation_id[:8]}'
            )
            save_thread.start()
            return jsonify({
                'success': True,
                'messages': [],
                'operation_id': operation_id
            })

        result = _perform_settings_save(form_data, uploads)
        for item in result.get('messages', []):
            flash(item.get('text', ''), item.get('category', 'info'))
        return redirect(url_for('settings'))
    
    # GET请求，显示设置页面
    config = load_config()
    acfun_partition_mapping = _load_acfun_partition_mapping()
    bilibili_partition_mapping = _build_bilibili_partition_mapping()
    return render_template(
        'settings.html',
        config=config,
        whisper_languages=WHISPER_LANGUAGE_LIST,
        acfun_partition_mapping=acfun_partition_mapping,
        bilibili_partition_mapping=bilibili_partition_mapping
    )


@app.route('/settings/save-progress/<operation_id>', methods=['GET'])
@login_required
def settings_save_progress(operation_id):
    progress = _get_settings_save_progress(operation_id)
    if not progress:
        return jsonify({
            'found': False,
            'stage': None,
            'message': '',
            'detail': '',
            'percent': None,
            'downloaded_bytes': None,
            'total_bytes': None,
            'done': True,
            'level': 'error',
            'success': False,
            'messages': []
        })

    return jsonify({
        'found': True,
        'stage': progress.get('stage'),
        'message': progress.get('message'),
        'detail': progress.get('detail'),
        'percent': progress.get('percent'),
        'downloaded_bytes': progress.get('downloaded_bytes'),
        'total_bytes': progress.get('total_bytes'),
        'done': progress.get('done', False),
        'level': progress.get('level', 'info'),
        'success': progress.get('success'),
        'messages': progress.get('messages', [])
    })

@app.route('/settings/acfun/qrcode/start', methods=['POST'])
@login_required
def acfun_qrcode_start():
    """发起 AcFun 二维码登录并返回二维码图片。"""
    config = load_config()
    cookie_path = resolve_cookie_file_path(
        path_value=config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'),
        default_relative_path='cookies/ac_cookies.json',
        service_name='AcFun',
        logger_obj=logger,
        allow_json_txt_fallback=True
    )

    try:
        session_id, qr_session = _create_acfun_qr_session()
        qr_data = qr_session.generate()
        return jsonify({
            'success': True,
            'session_id': session_id,
            'image_base64': qr_data.get('image_base64', ''),
            'mime_type': qr_data.get('mime_type', 'image/png'),
            'expires_in': _ACFUN_QR_SESSION_TTL_SECONDS,
            'qr_expires_in_ms': qr_data.get('expires_in_ms', 120000),
            'cookie_path': cookie_path,
        })
    except Exception as e:
        logger.error(f"发起 AcFun 二维码登录失败: {e}")
        return jsonify({'success': False, 'message': '二维码登录失败，请稍后重试'}), 500

@app.route('/settings/acfun/qrcode/status/<session_id>', methods=['GET'])
@login_required
def acfun_qrcode_status(session_id):
    """轮询 AcFun 二维码登录状态。"""
    qr_session = _get_acfun_qr_session(session_id)
    if not qr_session:
        return jsonify({'success': False, 'message': '二维码会话不存在或已过期'}), 404

    config = load_config()
    cookie_path = resolve_cookie_file_path(
        path_value=config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.json'),
        default_relative_path='cookies/ac_cookies.json',
        service_name='AcFun',
        logger_obj=logger,
        allow_json_txt_fallback=True
    )

    try:
        status_data = qr_session.check_status(cookie_file=cookie_path)
        status = status_data.get('status')
        # done/failed 状态保留到 TTL 自动清理，避免前端再次检查时立刻报“会话过期”
        # 仅 timeout（QR码确实过期）时立即移除
        if status == 'timeout':
            with _ACFUN_QR_SESSION_LOCK:
                _ACFUN_QR_SESSIONS.pop(session_id, None)
        return jsonify({'success': True, **status_data})
    except Exception as e:
        logger.error(f"查询 AcFun 二维码登录状态失败: {e}")
        return jsonify({'success': False, 'message': '查询登录状态失败，请稍后重试'}), 500

@app.route('/settings/bilibili/qrcode/start', methods=['POST'])
@login_required
def bilibili_qrcode_start():
    """发起 bilibili 二维码登录并返回二维码图片。"""
    config = load_config()
    cookie_path = config.get('BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json')
    if not os.path.isabs(cookie_path):
        cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), cookie_path)

    try:
        session_id, qr_session = _create_bilibili_qr_session()
        qr_data = qr_session.generate()
        return jsonify({
            'success': True,
            'session_id': session_id,
            'image_base64': qr_data.get('image_base64', ''),
            'mime_type': qr_data.get('mime_type', 'image/png'),
            'expires_in': _BILIBILI_QR_SESSION_TTL_SECONDS,
            'cookie_path': cookie_path,
        })
    except Exception as e:
        logger.error(f"发起 bilibili 二维码登录失败: {e}")
        return jsonify({'success': False, 'message': '二维码登录失败，请稍后重试'}), 500

@app.route('/settings/bilibili/qrcode/status/<session_id>', methods=['GET'])
@login_required
def bilibili_qrcode_status(session_id):
    """轮询 bilibili 二维码登录状态。"""
    qr_session = _get_bilibili_qr_session(session_id)
    if not qr_session:
        return jsonify({'success': False, 'message': '二维码会话不存在或已过期'}), 404

    config = load_config()
    cookie_path = config.get('BILIBILI_COOKIES_PATH', 'cookies/bili_cookies.json')
    if not os.path.isabs(cookie_path):
        cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), cookie_path)

    try:
        status_data = qr_session.check_status(cookie_file=cookie_path)
        status = status_data.get('status')
        if status in ('done', 'timeout', 'failed'):
            with _BILIBILI_QR_SESSION_LOCK:
                _BILIBILI_QR_SESSIONS.pop(session_id, None)
        return jsonify({'success': True, **status_data})
    except Exception as e:
        logger.error(f"查询 bilibili 二维码登录状态失败: {e}")
        return jsonify({'success': False, 'message': '查询登录状态失败，请稍后重试'}), 500

@app.route('/settings/reset', methods=['POST'])
@login_required
def reset_settings():
    """重置设置"""
    try:
        data = request.get_json() or {}
        keys = data.get('keys', [])
        
        if keys:
            # 重置指定项
            reset_specific_config(keys)
            flash('当前页面的设置已重置为默认值。', 'success')
        else:
            # 如果未指定keys，则不执行任何操作或返回错误
            # 为了防止误操作全重置，这里要求必须指定keys
            return jsonify({'status': 'error', 'message': '未指定要重置的配置项'}), 400
            
        return jsonify({'status': 'success', 'message': '设置已重置'})
    except Exception as e:
        logger.error(f"重置设置失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '重置设置失败，请稍后重试'}), 500

@app.route('/logs/cleanup', methods=['POST'])
@login_required
def cleanup_logs_route():
    """手动触发日志清理"""
    config = load_config()
    hours = int(request.form.get('hours', config.get('LOG_CLEANUP_HOURS', 168)))
    
    result = cleanup_logs(hours)
    
    if result.get('success'):
        flash(f"日志清理成功，删除了{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"日志清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

@app.route('/maintenance/clear_logs', methods=['POST'])
@login_required
def clear_logs_route():
    """立即清空特定日志文件"""
    result = clear_specific_logs()
    
    if result.get('success'):
        processed_files_str = "、".join(result['processed_files'])
        flash(f"日志清理成功，已处理{result['files_processed']}个文件（{processed_files_str}），释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"日志清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

@app.route('/maintenance/cleanup_downloads', methods=['POST'])
@login_required
def cleanup_downloads_route():
    """手动触发下载内容清理"""
    config = load_config()
    hours = int(request.form.get('hours', config.get('DOWNLOAD_CLEANUP_HOURS', 72)))
    
    result = cleanup_downloads(hours)
    
    if result.get('success'):
        flash(f"下载内容清理成功，删除了{result['dirs_removed']}个目录、{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"下载内容清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))


def _human_readable_size(num_bytes: float) -> str:
    # Simple helper for human readable file sizes
    if num_bytes is None:
        return '0B'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f}PB"


def cleanup_logs(hours: int):
    """删除logs目录下指定小时之前的日志文件（不包括当前运行日志）"""
    try:
        logs_dir = get_app_subdir('logs')
        if not os.path.exists(logs_dir):
            return {'success': True, 'files_removed': 0, 'bytes_freed': 0, 'bytes_freed_readable': '0B'}

        cutoff = time.time() - float(hours) * 3600
        files_removed = 0
        bytes_freed = 0

        for filename in os.listdir(logs_dir):
            path = os.path.join(logs_dir, filename)
            # skip current top-level app and manager logs when present
            if filename in ('app.log', 'task_manager.log'):
                continue
            try:
                stat = os.stat(path)
                if stat.st_mtime < cutoff:
                    bytes_freed += stat.st_size if stat.st_size else 0
                    if os.path.isfile(path):
                        os.remove(path)
                        files_removed += 1
                    elif os.path.isdir(path):
                        shutil.rmtree(path)
                        files_removed += 1
            except Exception:
                continue

        return {'success': True, 'files_removed': files_removed, 'bytes_freed': bytes_freed, 'bytes_freed_readable': _human_readable_size(bytes_freed)}
    except Exception as e:
        logger.warning(f"日志清理失败: {e}")
        return {'success': False, 'error': str(e)}


def clear_specific_logs():
    """清空特定日志文件并删除 task_xxx.log 文件"""
    try:
        logs_dir = get_app_subdir('logs')
        processed_files = []
        bytes_freed = 0

        # 清空 app.log 和 task_manager.log
        for fname in ('app.log', 'task_manager.log'):
            fpath = os.path.join(logs_dir, fname)
            if os.path.exists(fpath):
                try:
                    bytes_freed += os.path.getsize(fpath)
                    open(fpath, 'w', encoding='utf-8').close()
                    processed_files.append(fname)
                except Exception:
                    pass

        # 删除所有task_xxx.log文件
        for filename in os.listdir(logs_dir):
            if filename.startswith('task_') and filename.endswith('.log'):
                path = os.path.join(logs_dir, filename)
                try:
                    bytes_freed += os.path.getsize(path) if os.path.exists(path) else 0
                    os.remove(path)
                    processed_files.append(filename)
                except Exception:
                    pass

        return {'success': True, 'files_processed': len(processed_files), 'processed_files': processed_files, 'bytes_freed': bytes_freed, 'bytes_freed_readable': _human_readable_size(bytes_freed)}
    except Exception as e:
        logger.warning(f"清空日志失败: {e}")
        return {'success': False, 'error': str(e)}


def cleanup_downloads(hours: int):
    """清理下载目录中指定hours之前的任务目录"""
    try:
        downloads_dir = get_app_subdir('downloads')
        if not os.path.exists(downloads_dir):
            return {'success': True, 'dirs_removed': 0, 'files_removed': 0, 'bytes_freed': 0, 'bytes_freed_readable': '0B'}

        cutoff = time.time() - float(hours) * 3600
        dirs_removed = 0
        files_removed = 0
        bytes_freed = 0

        for entry in os.listdir(downloads_dir):
            path = os.path.join(downloads_dir, entry)
            try:
                if os.path.isdir(path):
                    # check last modification
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        # accumulate size
                        for root, dirs, files in os.walk(path):
                            for f in files:
                                fp = os.path.join(root, f)
                                if os.path.exists(fp):
                                    bytes_freed += os.path.getsize(fp)
                                    files_removed += 1
                        shutil.rmtree(path)
                        dirs_removed += 1
            except Exception:
                continue

        return {'success': True, 'dirs_removed': dirs_removed, 'files_removed': files_removed, 'bytes_freed': bytes_freed, 'bytes_freed_readable': _human_readable_size(bytes_freed)}
    except Exception as e:
        logger.warning(f"下载内容清理失败: {e}")
        return {'success': False, 'error': str(e)}


def configure_app(app, config):
    """为Flask app应用一些基础配置值（如 secret_key、上传限制等）"""
    try:
        # 使用配置中的SECRET_KEY提高会话安全
        secret = config.get('SECRET_KEY') if isinstance(config, dict) else None
        if secret:
            app.secret_key = secret

        max_content = config.get('MAX_CONTENT_LENGTH_MB', None) if isinstance(config, dict) else None
        if max_content:
            try:
                app.config['MAX_CONTENT_LENGTH'] = int(max_content) * 1024 * 1024
            except Exception:
                pass

        # 允许覆盖的内容
        app.config['Y2A_SETTINGS'] = config
    except Exception as e:
        logger.warning(f"应用配置失败: {e}")


def auto_start_pending_tasks(config):
    """在启动时尝试自动启动pending状态的任务"""
    try:
        from modules.task_manager import get_global_task_processor, get_tasks_by_status, TASK_STATES
        processor = get_global_task_processor(config)
        if not processor:
            return

        # 循环尝试启动下一个pending任务，直到并发数或没有更多pending
        # 我们设置一个上限避免无限循环
        attempts = 0
        while attempts < 200:
            attempts += 1
            try:
                processor._check_and_start_next_pending_task()
            except Exception:
                break
            # 如果没有pending则退出
            pending = get_tasks_by_status(TASK_STATES['PENDING'])
            if not pending:
                break
            time.sleep(0.05)
    except Exception as e:
        logger.warning(f"自动启动pending任务失败: {e}")


def schedule_log_cleanup():
    """为日志清理创建并启动一个BackgroundScheduler, 返回调度器对象"""
    try:
        config = load_config()
        interval_hours = int(config.get('LOG_CLEANUP_INTERVAL', 24))
        if not config.get('LOG_CLEANUP_ENABLED', False):
            return None

        scheduler = BackgroundScheduler()
        def _job():
            cleanup_logs(int(config.get('LOG_CLEANUP_HOURS', 168)))
        scheduler.add_job(_job, 'interval', hours=interval_hours, id='log_cleanup', replace_existing=True)
        scheduler.start()
        return scheduler
    except Exception as e:
        logger.warning(f"启动日志清理定时任务失败: {e}")
        return None


def schedule_download_cleanup():
    try:
        config = load_config()
        interval_hours = int(config.get('DOWNLOAD_CLEANUP_INTERVAL', 24))
        if not config.get('DOWNLOAD_CLEANUP_ENABLED', False):
            return None

        scheduler = BackgroundScheduler()
        def _job():
            cleanup_downloads(int(config.get('DOWNLOAD_CLEANUP_HOURS', 72)))
        scheduler.add_job(_job, 'interval', hours=interval_hours, id='download_cleanup', replace_existing=True)
        scheduler.start()
        return scheduler
    except Exception as e:
        logger.warning(f"启动下载内容清理定时任务失败: {e}")
        return None


# YouTube监控系统路由
@app.route('/youtube_monitor')
@login_required
def youtube_monitor_index():
    """YouTube监控主页"""
    configs = youtube_monitor.get_monitor_configs()
    history = youtube_monitor.get_monitor_history(limit=50)
    return render_template('youtube_monitor.html', configs=configs, history=history)

@app.route('/youtube_monitor/config', methods=['GET', 'POST'])
@login_required
def youtube_monitor_config():
    """监控配置页面"""
    if request.method == 'POST':
        try:
            # 安全的整数转换函数
            def safe_int(value, default=0):
                if not value or value.strip() == '':
                    return default
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            # 获取监控类型和模式
            monitor_type = request.form.get('monitor_type', 'youtube_search')
            channel_mode = request.form.get('channel_mode', 'latest')
            
            config_data = {
                'name': request.form.get('name', '').strip(),
                'enabled': 'enabled' in request.form,
                'monitor_type': monitor_type,
                'channel_mode': channel_mode,
                'region_code': request.form.get('region_code', 'US'),
                'category_id': request.form.get('category_id', '0'),
                'time_period': safe_int(request.form.get('time_period'), 7),
                'max_results': safe_int(request.form.get('max_results'), 10),
                'min_view_count': safe_int(request.form.get('min_view_count'), 0),
                'min_like_count': safe_int(request.form.get('min_like_count'), 0),
                'min_comment_count': safe_int(request.form.get('min_comment_count'), 0),
                'keywords': request.form.get('keywords', ''),
                'exclude_keywords': request.form.get('exclude_keywords', ''),
                'channel_ids': request.form.get('channel_ids', ''),
                'channel_keywords': request.form.get('channel_keywords', ''),
                'exclude_channel_ids': request.form.get('exclude_channel_ids', ''),
                'min_duration': safe_int(request.form.get('min_duration'), 0),
                'max_duration': safe_int(request.form.get('max_duration'), 0),
                'schedule_type': request.form.get('schedule_type', 'manual'),
                'schedule_interval': safe_int(request.form.get('schedule_interval'), 120),
                'order_by': request.form.get('order_by', 'viewCount'),
                'start_date': request.form.get('start_date', ''),
                'end_date': request.form.get('end_date', ''),
                'latest_days': safe_int(request.form.get('latest_days'), 7),
                'latest_max_results': safe_int(request.form.get('latest_max_results'), 20),
                'rate_limit_requests': safe_int(request.form.get('rate_limit_requests'), 20),
                'rate_limit_window': safe_int(request.form.get('rate_limit_window'), 60),
                'auto_add_to_tasks': 'auto_add_to_tasks' in request.form,
                'video_types': ','.join(request.form.getlist('video_types') or ['video','short','live'])
            }
            
            # 验证必填项
            if not config_data['name']:
                flash('配置名称不能为空', 'danger')
                return render_template('youtube_monitor_config.html')
            
            config_id = youtube_monitor.create_monitor_config(config_data)
            flash(f'监控配置 "{config_data["name"]}" 创建成功！', 'success')
            return redirect(url_for('youtube_monitor_index'))
            
        except Exception as e:
            flash(f'创建监控配置失败: {str(e)}', 'danger')
    
    return render_template('youtube_monitor_config.html')

@app.route('/youtube_monitor/config/<int:config_id>/edit', methods=['GET', 'POST'])
@login_required
def youtube_monitor_config_edit(config_id):
    """编辑监控配置"""
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        flash('监控配置不存在', 'danger')
        return redirect(url_for('youtube_monitor_index'))
    
    if request.method == 'POST':
        try:
            # 安全的整数转换函数
            def safe_int(value, default=0):
                if not value or value.strip() == '':
                    return default
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            # 获取监控类型和模式
            monitor_type = request.form.get('monitor_type', 'youtube_search')
            channel_mode = request.form.get('channel_mode', 'latest')
            
            config_data = {
                'name': request.form.get('name', '').strip(),
                'enabled': 'enabled' in request.form,
                'monitor_type': monitor_type,
                'channel_mode': channel_mode,
                'region_code': request.form.get('region_code', 'US'),
                'category_id': request.form.get('category_id', '0'),
                'time_period': safe_int(request.form.get('time_period'), 7),
                'max_results': safe_int(request.form.get('max_results'), 10),
                'min_view_count': safe_int(request.form.get('min_view_count'), 0),
                'min_like_count': safe_int(request.form.get('min_like_count'), 0),
                'min_comment_count': safe_int(request.form.get('min_comment_count'), 0),
                'keywords': request.form.get('keywords', ''),
                'exclude_keywords': request.form.get('exclude_keywords', ''),
                'channel_ids': request.form.get('channel_ids', ''),
                'channel_keywords': request.form.get('channel_keywords', ''),
                'exclude_channel_ids': request.form.get('exclude_channel_ids', ''),
                'min_duration': safe_int(request.form.get('min_duration'), 0),
                'max_duration': safe_int(request.form.get('max_duration'), 0),
                'schedule_type': request.form.get('schedule_type', 'manual'),
                'schedule_interval': safe_int(request.form.get('schedule_interval'), 120),
                'order_by': request.form.get('order_by', 'viewCount'),
                'start_date': request.form.get('start_date', ''),
                'end_date': request.form.get('end_date', ''),
                'latest_days': safe_int(request.form.get('latest_days'), 7),
                'latest_max_results': safe_int(request.form.get('latest_max_results'), 20),
                'rate_limit_requests': safe_int(request.form.get('rate_limit_requests'), 20),
                'rate_limit_window': safe_int(request.form.get('rate_limit_window'), 60),
                'auto_add_to_tasks': 'auto_add_to_tasks' in request.form,
                'video_types': ','.join(request.form.getlist('video_types') or ['video','short','live'])
            }
            
            # 验证必填项
            if not config_data['name']:
                flash('配置名称不能为空', 'danger')
                return render_template('youtube_monitor_config.html', config=config, is_edit=True)
            
            youtube_monitor.update_monitor_config(config_id, config_data)
            flash(f'监控配置更新成功！', 'success')
            return redirect(url_for('youtube_monitor_index'))
            
        except Exception as e:
            flash(f'更新监控配置失败: {str(e)}', 'danger')
    
    return render_template('youtube_monitor_config.html', config=config, is_edit=True)

@app.route('/youtube_monitor/config/<int:config_id>/delete', methods=['POST'])
@login_required
def youtube_monitor_config_delete(config_id):
    """删除监控配置"""
    try:
        config = youtube_monitor.get_monitor_config(config_id)
        if config:
            youtube_monitor.delete_monitor_config(config_id)
            flash(f'监控配置 "{config["name"]}" 删除成功！', 'success')
        else:
            flash('监控配置不存在', 'danger')
    except Exception as e:
        flash(f'删除监控配置失败: {str(e)}', 'danger')
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/config/<int:config_id>/run', methods=['POST'])
@login_required
def youtube_monitor_run(config_id):
    """立即执行一次监控任务"""
    success, message = youtube_monitor.run_monitor(config_id)
    if success:
        flash(message or '监控已执行', 'success')
    else:
        flash(message or '监控执行失败', 'danger')
    return redirect(url_for('youtube_monitor_history', config_id=config_id))

@app.route('/youtube_monitor/history/<int:config_id>')
@login_required
def youtube_monitor_history(config_id):
    """查看指定监控配置的发现历史"""
    config = youtube_monitor.get_monitor_config(config_id)
    if not config:
        flash('监控配置不存在', 'danger')
        return redirect(url_for('youtube_monitor_index'))
    
    history = youtube_monitor.get_monitor_history(config_id, limit=200)
    
    # 计算统计数据
    stats = {
        'total_records': len(history),
        'added_to_tasks': 0,
        'avg_views': 0,
        'avg_likes': 0
    }
    
    if history:
        total_views = 0
        total_likes = 0
        
        for record in history:
            if record.get('added_to_tasks'):
                stats['added_to_tasks'] += 1
            total_views += record.get('view_count', 0)
            total_likes += record.get('like_count', 0)
        
        stats['avg_views'] = int(total_views / len(history))
        stats['avg_likes'] = int(total_likes / len(history))
    
    return render_template('youtube_monitor_history.html', history=history, config=config, stats=stats)

@app.route('/youtube_monitor/add_to_tasks', methods=['POST'])
@login_required
def youtube_monitor_add_to_tasks():
    """从监控历史中添加视频到任务列表"""
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    config_id = data.get('config_id')
    if not video_id or not config_id:
        return jsonify({'success': False, 'message': '参数不完整'}), 400
    try:
        config_id_int = int(config_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'config_id 无效'}), 400

    success, message = youtube_monitor.add_video_to_tasks_manually(video_id, config_id_int)

    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'message': message}), 400

@app.route('/youtube_monitor/history/<int:config_id>/clear', methods=['POST'])
@login_required
def youtube_monitor_clear_history(config_id):
    """清空指定监控任务的历史记录"""
    youtube_monitor.clear_monitor_history(config_id)
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/history/clear_all', methods=['POST'])
@login_required
def youtube_monitor_clear_all_history():
    """清空所有历史记录"""
    youtube_monitor.clear_all_monitor_history()
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/restore_configs', methods=['POST'])
@login_required
def youtube_monitor_restore_configs():
    """恢复默认监控配置"""
    youtube_monitor.restore_configs_from_files_manually()
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/youtube_monitor/config/<int:config_id>/reset_offset', methods=['POST'])
@login_required
def youtube_monitor_reset_offset(config_id):
    """重置频道监控的视频偏移量"""
    youtube_monitor.reset_historical_offset(config_id)
    
    return redirect(url_for('youtube_monitor_index'))

@app.route('/api/cookies/sync', methods=['POST'])
@login_required
def sync_cookies():
    """
    接收从浏览器扩展同步过来的Cookie
    """
    try:
        if not request.is_json:
            return jsonify({'error': '请求必须是JSON格式'}), 400
        
        data = request.get_json()
        
        # 验证必要字段
        required_fields = ['source', 'timestamp', 'cookies', 'cookieCount']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'缺少必要字段: {field}'}), 400
        
        # 验证来源
        if data['source'] not in ['userscript', 'extension']:
            return jsonify({'error': '不支持的cookie来源'}), 400
        
        # 验证cookie数据
        cookies_content = data['cookies']
        if not cookies_content or not isinstance(cookies_content, str):
            return jsonify({'error': 'cookie数据无效'}), 400
        
        # 保存cookie到文件
        cookies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')
        os.makedirs(cookies_dir, exist_ok=True)
        
        youtube_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
        
        # 不再创建备份文件（用户要求禁用备份功能）
        # if data['source'] == 'userscript' and os.path.exists(youtube_cookies_path):
        #     backup_path = youtube_cookies_path + f'.backup.{int(time.time())}'
        #     try:
        #         shutil.copy2(youtube_cookies_path, backup_path)
        #         logger.info(f"已备份原有cookie文件到: {backup_path}")
        #     except Exception as e:
        #         logger.warning(f"备份cookie文件失败: {str(e)}")
        
        # 写入新的cookie文件
        try:
            with open(youtube_cookies_path, 'w', encoding='utf-8') as f:
                f.write(cookies_content)
            
            # 记录同步信息
            sync_info = {
                'timestamp': data['timestamp'],
                'sync_time': time.time(),
                'cookie_count': data['cookieCount'],
                'user_agent': data.get('userAgent', ''),
                'source_url': data.get('url', ''),
                'file_size': len(cookies_content)
            }
            
            source_name = '浏览器扩展' if data['source'] == 'extension' else '油猴脚本'
            logger.info(f"Cookie同步成功 - 来源: {source_name}, 数量: {data['cookieCount']}, 大小: {len(cookies_content)} bytes")
            
            # 备份文件功能已禁用，不再需要清理逻辑
            # try:
            #     backup_files = []
            #     for file in os.listdir(cookies_dir):
            #         if file.startswith('yt_cookies.txt.backup.'):
            #             backup_path = os.path.join(cookies_dir, file)
            #             backup_files.append((os.path.getmtime(backup_path), backup_path))
            #     
            #     # 按时间排序，删除多余的备份
            #     if len(backup_files) > 5:
            #         backup_files.sort()
            #         for _, old_backup in backup_files[:-5]:
            #             try:
            #                 os.remove(old_backup)
            #                 logger.debug(f"已删除旧备份文件: {old_backup}")
            #             except Exception as e:
            #                 logger.warning(f"删除旧备份文件失败: {str(e)}")
            # except Exception as e:
            #     logger.warning(f"清理备份文件失败: {str(e)}")
            
            return jsonify({
                'success': True,
                'message': 'Cookie同步成功',
                'sync_info': sync_info
            }), 200
            
        except Exception as e:
            logger.error(f"写入cookie文件失败: {str(e)}")
            return jsonify({'error': '保存cookie失败，请稍后重试'}), 500

    except Exception as e:
        logger.error(f"Cookie同步API异常: {str(e)}")
        return jsonify({'error': '服务器内部错误，请稍后重试'}), 500

@app.route('/api/cookies/status', methods=['GET'])
@login_required
def get_cookie_status():
    """
    提供Cookie状态给浏览器扩展
    """
    try:
        cookies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')
        youtube_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
        
        status = {
            'youtube_cookies_exists': os.path.exists(youtube_cookies_path),
            'last_modified': None,
            'file_size': 0,
            'line_count': 0
        }
        
        if status['youtube_cookies_exists']:
            stat_info = os.stat(youtube_cookies_path)
            status['last_modified'] = stat_info.st_mtime
            status['file_size'] = stat_info.st_size
            
            # 统计行数
            try:
                with open(youtube_cookies_path, 'r', encoding='utf-8') as f:
                    status['line_count'] = sum(1 for line in f if line.strip() and not line.startswith('#'))
            except Exception as e:
                logger.warning(f"读取cookie文件失败: {str(e)}")
                status['line_count'] = -1
        
        return jsonify(status), 200
        
    except Exception as e:
        logger.error(f"获取cookie状态失败: {str(e)}")
        return jsonify({'error': '获取状态失败，请稍后重试'}), 500

@app.route('/api/cookies/refresh-needed', methods=['POST'])
@login_required
def cookie_refresh_needed():
    """
    接收浏览器扩展的通知，标记某个网站的Cookie需要刷新
    """
    try:
        data = request.get_json()
        reason = data.get('reason', 'unknown')
        video_url = data.get('video_url', '')
        
        logger.warning(f"收到Cookie刷新需求 - 原因: {reason}, 视频: {video_url}")
        
        # 这里可以实现通知机制，比如：
        # 1. 发送到浏览器扩展
        # 2. 在Web界面显示提示
        # 3. 发送邮件通知等
        
        return jsonify({
            'success': True,
            'message': 'Cookie刷新需求已记录',
            'suggestion': '请使用浏览器扩展重新同步Cookie'
        }), 200
        
    except Exception as e:
        logger.error(f"处理Cookie刷新需求失败: {str(e)}")
        return jsonify({'error': '处理失败，请稍后重试'}), 500

if __name__ == '__main__':
    logger.info("Y2A-Auto 启动中...")

    # 初始化AcFun分区ID映射
    init_id_mapping()

    # 加载配置
    config = load_config()
    app.config['Y2A_SETTINGS'] = config
    logger.info(f"配置已加载: {json.dumps(config, ensure_ascii=False, indent=2)}")

    # 初始化全局任务处理器，确保并发控制生效
    from modules.task_manager import get_global_task_processor, shutdown_global_task_processor
    get_global_task_processor(config)
    logger.info("全局任务处理器已初始化")

    # 自动启动所有pending任务（如果启用了自动模式）
    if config.get('AUTO_MODE_ENABLED', False):
        logger.info("自动模式已启用，正在启动所有pending任务...")
        auto_start_pending_tasks(config)

    # 初始化YouTube监控API
    if config.get('YOUTUBE_API_KEY'):
        youtube_monitor.set_api_key(config['YOUTUBE_API_KEY'])
        youtube_monitor.start_all_schedules()
        logger.info("YouTube监控系统已初始化")

    # 配置应用
    configure_app(app, config)

    # 设置日志清理定时任务
    log_cleanup_scheduler = schedule_log_cleanup()

    # 设置下载内容清理定时任务
    download_cleanup_scheduler = schedule_download_cleanup()

    try:
        logger.info(f"服务启动，监听地址: http://127.0.0.1:{5000}")
        # 使用标准Flask运行
        app.run(host='0.0.0.0', port=5000, debug=False)
    except KeyboardInterrupt:
        logger.info("接收到退出信号，服务正在关闭...")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
    finally:
        # 关闭全局任务处理器
        shutdown_global_task_processor()

        if log_cleanup_scheduler:
            log_cleanup_scheduler.shutdown()
        if download_cleanup_scheduler:
            download_cleanup_scheduler.shutdown()
        logger.info("服务已关闭")
