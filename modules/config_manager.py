#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
from .utils import get_app_subdir
from .speech_pipeline_settings import (
    inject_speech_pipeline_defaults,
    migrate_legacy_speech_pipeline_config,
)

# 获取日志记录器
logger = logging.getLogger('config_manager')

_YOUTUBE_DOWNLOAD_QUALITY_MODES = ('highest', 'manual')
_YOUTUBE_DOWNLOAD_MAX_HEIGHT_VALUES = ('2160', '1440', '1080', '720', '480', '360')
_YOUTUBE_DOWNLOAD_QUALITY_MODE_DEFAULT = 'highest'
_YOUTUBE_DOWNLOAD_MAX_HEIGHT_DEFAULT = '1080'

# 默认配置
DEFAULT_CONFIG = {
    "AUTO_MODE_ENABLED": False, # 无人值守自动投稿总开关
    "TRANSLATE_TITLE": False,
    "TRANSLATE_DESCRIPTION": False,
    "UPLOAD_APPEND_REPOST_NOTICE": True,
    "GENERATE_TAGS": False,
    "YOUTUBE_UPLOADER_AS_FIRST_TAG": False,
    "RECOMMEND_PARTITION": False,
    "RECOMMEND_PARTITION_WITH_COVER": False,
    "CONTENT_MODERATION_ENABLED": False,
    "LOG_CLEANUP_ENABLED": True, # 是否启用日志自动清理
    "LOG_CLEANUP_HOURS": 72, # 保留最近多少小时的日志
    "LOG_CLEANUP_INTERVAL": 12, # 日志清理间隔（小时）
    "DOWNLOAD_CLEANUP_ENABLED": False, # 是否启用下载内容自动清理
    "DOWNLOAD_CLEANUP_HOURS": 72, # 保留最近多少小时的下载内容
    "DOWNLOAD_CLEANUP_INTERVAL": 24, # 下载内容清理间隔（小时）
    # 主动消息推送
    "NOTIFY_ENABLED": False,
    "NOTIFY_EVENT_TASK_ADDED": True,
    "NOTIFY_EVENT_TASK_COMPLETED": True,
    "NOTIFY_EVENT_TASK_FAILED": True,
    "NOTIFY_EVENT_LOGIN_SUCCESS": True,
    "NOTIFY_EVENT_LOGIN_LOCKED": True,
    "NOTIFY_EVENT_QR_LOGIN_SUCCESS": True,
    "NOTIFY_EVENT_QR_LOGIN_FAILED": True,
    "NOTIFY_WECOM_ENABLED": False,
    "NOTIFY_WECOM_WEBHOOK_URL": "",
    "NOTIFY_SERVERCHAN_ENABLED": False,
    "NOTIFY_SERVERCHAN_SENDKEY": "",
    "NOTIFY_MESSAGE_PUSHER_ENABLED": False,
    "NOTIFY_MESSAGE_PUSHER_SERVER": "",
    "NOTIFY_MESSAGE_PUSHER_USERNAME": "",
    "NOTIFY_MESSAGE_PUSHER_TOKEN": "",
    "NOTIFY_MESSAGE_PUSHER_CHANNEL": "",
    "password_protection_enabled": False,
    "password": "",
    # 登录安全控制
    "LOGIN_MAX_FAILED_ATTEMPTS": 5,  # 达到该失败次数后触发锁定
    "LOGIN_LOCKOUT_MINUTES": 15,     # 被锁定后持续的分钟数
    "LOGIN_SESSION_TIMEOUT_MINUTES": 30,  # 登录空闲超时时长（分钟）
    "YOUTUBE_COOKIES_PATH": "cookies/yt_cookies.txt", # 相对于项目根目录
    "ACFUN_COOKIES_PATH": "cookies/ac_cookies.json", # AcFun Cookie文件路径
    "BILIBILI_COOKIES_PATH": "cookies/bili_cookies.json", # bilibili Cookie 文件路径
    "ACFUN_USERNAME": "",
    "ACFUN_PASSWORD": "",
    "UPLOAD_TARGET_DEFAULT": "acfun",  # 任务默认投稿平台：acfun|bilibili|both
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "OPENAI_MODEL_NAME": "gpt-3.5-turbo",
    "OPENAI_THINKING_ENABLED": False,
    "OPENAI_TIMEOUT_SECONDS": 600,  # OpenAI API 请求超时秒数；思考模型输出可达64k token，建议不低于300
    # 固定分区ID（可选）：如设置则推荐分区将直接使用该ID
    "FIXED_PARTITION_ID": "",
    # bilibili固定分区ID（可选）：如设置则bilibili推荐分区将直接使用该ID
    "FIXED_PARTITION_ID_BILIBILI": "",
    # 字幕翻译可单独指定OpenAI Base URL；为空则回退到 OPENAI_BASE_URL
    "SUBTITLE_OPENAI_BASE_URL": "",
    # 字幕翻译可单独指定 API Key 与 模型名；为空则分别回退到 OPENAI_API_KEY 与 OPENAI_MODEL_NAME
    "SUBTITLE_OPENAI_API_KEY": "",
    "SUBTITLE_OPENAI_MODEL_NAME": "",
    "SUBTITLE_OPENAI_THINKING_ENABLED": False,
    "YOUTUBE_API_KEY": "",
    "YOUTUBE_API_PROXY_ENABLED": False,  # 是否为 YouTube Data API 监控启用独立代理
    "YOUTUBE_API_PROXY_URL": "",  # 监控 API 代理地址，格式：http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
    "YOUTUBE_API_PROXY_USERNAME": "",  # 监控 API 代理用户名（可选）
    "YOUTUBE_API_PROXY_PASSWORD": "",  # 监控 API 代理密码（可选）
    "ALIYUN_ACCESS_KEY_ID": "",
    "ALIYUN_ACCESS_KEY_SECRET": "",
    "ALIYUN_CONTENT_MODERATION_REGION": "cn-shanghai",
    "ALIYUN_TEXT_MODERATION_SERVICE": "comment_detection_pro",
    "COVER_PROCESSING_MODE": "crop",
    # YouTube下载相关配置
    "YOUTUBE_PROXY_ENABLED": False,  # 是否启用代理
    "YOUTUBE_PROXY_URL": "",  # 代理地址，格式：http://proxy.example.com:8080 或 socks5://127.0.0.1:1080
    "YOUTUBE_PROXY_USERNAME": "",  # 代理用户名（可选）
    "YOUTUBE_PROXY_PASSWORD": "",  # 代理密码（可选）
    "YOUTUBE_DOWNLOAD_THREADS": 4,  # yt-dlp下载线程数（并发片段数）
    "YOUTUBE_DOWNLOAD_QUALITY_MODE": _YOUTUBE_DOWNLOAD_QUALITY_MODE_DEFAULT,  # highest|manual
    "YOUTUBE_DOWNLOAD_MAX_HEIGHT": _YOUTUBE_DOWNLOAD_MAX_HEIGHT_DEFAULT,  # 手动画质上限
    "YOUTUBE_THROTTLED_RATE": "",  # 限制下载速度，格式如：1M、500K等，留空不限制
    # 外部工具路径
    "FFMPEG_LOCATION": "",  # 可选：覆盖 ffmpeg 可执行文件路径；留空则使用项目内置版本
    "FFMPEG_AUTO_DOWNLOAD": True,  # 仅在 Windows 且缺失时尝试联网补齐 ffmpeg/ 目录
    # 字幕翻译相关配置
    "SUBTITLE_TRANSLATION_ENABLED": False,  # 是否启用字幕翻译
    "YOUTUBE_AUTO_GENERATED_SUBTITLES_ENABLED": False,  # 下载 YouTube 自动生成字幕（可选）
    "SUBTITLE_SOURCE_LANGUAGE": "auto",  # 源语言 (auto, en, ja, ko等)
    "SUBTITLE_TARGET_LANGUAGE": "zh",  # 目标语言 (zh, en, ja, ko等)
    "SUBTITLE_FONT_NAME": "SourceHanSansHWSC-VF.otf",  # 烧录字幕使用的内置字体文件名
    "SUBTITLE_API_PROVIDER": "openai",  # API提供商 (仅支持openai)
    "SUBTITLE_BATCH_SIZE": 3,  # 批次大小
    "SUBTITLE_MAX_RETRIES": 3,  # 最大重试次数
    "SUBTITLE_RETRY_DELAY": 2,  # 重试延迟(秒)
    "SUBTITLE_EMBED_IN_VIDEO": True,  # 是否将字幕嵌入视频
    "SUBTITLE_KEEP_ORIGINAL": True,  # 是否保留原始字幕文件
    "SUBTITLE_MAX_WORKERS": 2,  # 字幕翻译最大并发线程数

    # ASR 源字幕预检（可选）
    "SUBTITLE_QC_ENABLED": True,  # 质量优先：失败则不烧录字幕，但保留字幕文件并继续上传原视频（任务最终仍为 completed）
    "SUBTITLE_QC_PROVIDER": "openai",  # openai / none
    "SUBTITLE_QC_BASE_URL": "",  # 留空则回退到 SUBTITLE_OPENAI_BASE_URL / OPENAI_BASE_URL
    "SUBTITLE_QC_API_KEY": "",  # 留空则回退到 SUBTITLE_OPENAI_API_KEY / OPENAI_API_KEY
    "SUBTITLE_QC_MODEL_NAME": "",  # 留空则回退到 SUBTITLE_OPENAI_MODEL_NAME / OPENAI_MODEL_NAME
    "SUBTITLE_QC_THINKING_ENABLED": False,  # 字幕质检独立思考开关
    "SUBTITLE_QC_THRESHOLD": 0.60,  # 通过阈值（0-1），仅作为 AI 复核分数下限（质量优先）
    "SUBTITLE_QC_SAMPLE_MAX_ITEMS": 80,  # AI 抽样条目上限（实际会按边界程度自适应收缩）
    "SUBTITLE_QC_MAX_CHARS": 9000,  # AI 送检最大字符数上限（实际会按边界程度自适应收缩）
    # 并发控制配置
    "MAX_CONCURRENT_TASKS": 2,  # 最大并发任务数
    "MAX_CONCURRENT_UPLOADS": 1,  # 最大并发上传数
    "STUCK_TASK_CHECK_INTERVAL_SECONDS": 300,  # 自动扫描并恢复卡住任务的时间间隔（秒）
    # 视频转码相关（硬编默认输出 HEVC/H.265，CPU 保持 H.264）
    "VIDEO_ENCODER": "auto",  # auto/cpu/nvidia/intel/amd - 自动检测或指定编码器
    "VIDEO_CUSTOM_PARAMS_ENABLED": False,  # 是否启用自定义转码参数
    "VIDEO_CUSTOM_PARAMS": "",  # 自定义 FFmpeg 视频编码参数（启用自定义参数时使用）
    # 语音识别（无字幕转写）
    "SPEECH_RECOGNITION_ENABLED": False,  # 启用语音识别生成字幕
    "SPEECH_RECOGNITION_PROVIDER": "whisper",  # whisper（OpenAI兼容）
    # Whisper/OpenAI 兼容配置（可单独配置，未设置则回退到 OPENAI_*）
    "WHISPER_API_KEY": "",
    "WHISPER_BASE_URL": "",
    "WHISPER_MODEL_NAME": "whisper-1",
    "WHISPER_TIMESTAMP_GRANULARITIES": "segment,word",
    # Voxtral（Mistral /v1/audio/transcriptions）配置
    "VOXTRAL_API_KEY": "",
    "VOXTRAL_BASE_URL": "https://api.mistral.ai/v1",
    "VOXTRAL_MODEL_NAME": "voxtral-mini-latest",
    "VOXTRAL_TIMESTAMP_GRANULARITIES": "segment,word",
    "VOXTRAL_DIARIZE": False,
    "VOXTRAL_CONTEXT_BIAS": "",
    "VOXTRAL_LANGUAGE": "",
    "VOXTRAL_MAX_AUDIO_DURATION_S": 10800,
    "VOXTRAL_LONG_AUDIO_MARGIN_S": 5,
    "VOXTRAL_ENFORCE_MAX_DURATION": True,
    # 语音活动检测（VAD）
    "VAD_ENABLED": True,
    "VAD_PROVIDER": "silero-vad",
    "VAD_SILERO_THRESHOLD": 0.55,
    "VAD_SILERO_MIN_SPEECH_MS": 300,   # 过滤过短噪声脉冲，更贴近正常口语起句
    "VAD_SILERO_MIN_SILENCE_MS": 320,   # 收紧切分，降低单个搜索窗跨句概率
    "VAD_SILERO_MAX_SPEECH_S": 120,
    "VAD_SILERO_SPEECH_PAD_MS": 120,    # 降低边界填充，避免窗口过宽
    "VAD_MAX_SEGMENT_S": 15.0,          # 质量优先：限制搜索窗，减少跨句/跨轮次漂移
    # 音频分片策略（针对长音频）
    "AUDIO_CHUNK_WINDOW_S": 15.0,  # canonical 默认值：更利于 segment 时间戳稳定对齐
    "AUDIO_CHUNK_OVERLAP_S": 0.4,  # 略增重叠避免句首句尾丢失
    # VAD后处理约束（宽松策略 - 搜索窗口，非字幕边界）
    "VAD_MERGE_GAP_S": 0.35,  # 缩小自动合并窗口，减少跨句吞并
    "VAD_MIN_SEGMENT_S": 0.8,  # 允许略短片段保留独立句边界
    "VAD_MAX_SEGMENT_S_FOR_SPLIT": 15.0,  # 与搜索窗硬上限对齐
    "VAD_REFINEMENT_ENABLED": True,  # 对粗检出的语音窗执行二次边界收敛
    "VAD_MIN_SPEECH_COVERAGE_RATIO": 0.015,  # 低于该占比时触发宽松VAD重试
    # 转写参数
    "WHISPER_LANGUAGE": "",  # 强制语言（如 en, zh, ja），空=自动检测
    "WHISPER_PROMPT": "",  # 转写提示（引导生成，减少幻觉）
    "WHISPER_TRANSLATE": False,  # 是否翻译为英文
    "WHISPER_MAX_WORKERS": 3,  # 预留（当前顺序处理）
    # 文本后处理
    "SUBTITLE_MAX_LINE_LENGTH": 42,  # 每行最大字符数
    "SUBTITLE_MAX_LINES": 2,  # 每个字幕最多行数
    "SUBTITLE_NORMALIZE_PUNCTUATION": True,  # 标准化标点
    "SUBTITLE_FILTER_FILLER_WORDS": False,  # 过滤填充词（um, uh等）
    # 最终字幕后处理（时序与极短片段处理）
    "SUBTITLE_TIME_OFFSET_S": 0.0,  # 全局时间偏移（秒，可为负）
    "SUBTITLE_MIN_CUE_DURATION_S": 0.6,  # 每条字幕最短时长（秒）
    "SUBTITLE_MERGE_GAP_S": 0.3,  # 若相邻间隙不超过该值则合并
    "SUBTITLE_MIN_TEXT_LENGTH": 2,  # 文本长度不足时进行合并/丢弃
    # 字幕后处理启用开关（仅对Whisper生效）
    "SUBTITLE_TIME_OFFSET_ENABLED": False,
    "SUBTITLE_MIN_CUE_DURATION_ENABLED": False,
    "SUBTITLE_MERGE_GAP_ENABLED": False,
    "SUBTITLE_MIN_TEXT_LENGTH_ENABLED": False,
    "SUBTITLE_MAX_LINE_LENGTH_ENABLED": False,
    "SUBTITLE_MAX_LINES_ENABLED": False,
    # 重试与回退策略
    "WHISPER_MAX_RETRIES": 3,  # API调用最大重试次数
    "WHISPER_RETRY_DELAY_S": 2.0,  # 重试延迟（秒，指数退避）
    # 任务调度
    "PENDING_SCAN_INTERVAL_SECONDS": 30,  # 待处理任务扫描间隔（秒）
}

DEFAULT_CONFIG = inject_speech_pipeline_defaults(DEFAULT_CONFIG)


def normalize_youtube_download_quality_mode(value):
    normalized = str(value or _YOUTUBE_DOWNLOAD_QUALITY_MODE_DEFAULT).strip().lower()
    if normalized not in _YOUTUBE_DOWNLOAD_QUALITY_MODES:
        return _YOUTUBE_DOWNLOAD_QUALITY_MODE_DEFAULT
    return normalized


def normalize_youtube_download_max_height(value):
    normalized = str(value or _YOUTUBE_DOWNLOAD_MAX_HEIGHT_DEFAULT).strip()
    if normalized not in _YOUTUBE_DOWNLOAD_MAX_HEIGHT_VALUES:
        return _YOUTUBE_DOWNLOAD_MAX_HEIGHT_DEFAULT
    return normalized


def normalize_login_session_timeout_minutes(value):
    try:
        normalized = int(str(value).strip())
    except (AttributeError, TypeError, ValueError):
        return 30
    return max(1, normalized)


def _prune_unknown_config_keys(config_data):
    clean_config = {}
    removed_keys = []
    for key, value in (config_data or {}).items():
        if key in DEFAULT_CONFIG:
            clean_config[key] = value
        else:
            removed_keys.append(key)
    return clean_config, removed_keys


def load_config():
    """
    加载配置文件，如果不存在则创建默认配置
    
    Returns:
        dict: 配置字典
    """
    config_path = os.path.join(get_app_subdir('config'), 'config.json')
    
    # 确保config目录存在
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    
    try:
        # 尝试读取配置文件
        if os.path.exists(config_path) and os.path.getsize(config_path) > 2:  # 文件存在且不为空
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.info("成功加载配置文件")

                config, migrated_legacy_speech = migrate_legacy_speech_pipeline_config(config)
                config, removed_keys = _prune_unknown_config_keys(config)
                
                # 确保所有默认配置项都存在
                missing_keys = False
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                        missing_keys = True

                # 验证视频编码器配置是否合法
                encoder_value = str(config.get('VIDEO_ENCODER', 'auto')).lower().strip()
                valid_encoders = ('auto', 'cpu', 'nvidia', 'intel', 'amd')
                encoder_changed = False
                if encoder_value not in valid_encoders:
                    logger.warning(f"检测到无效的视频编码器配置 {encoder_value}，已自动回退为 auto")
                    config['VIDEO_ENCODER'] = 'auto'
                    encoder_changed = True

                upload_target_before = config.get('UPLOAD_TARGET_DEFAULT')
                upload_target_normalized = str(upload_target_before or 'acfun').strip().lower()
                if upload_target_normalized not in ('acfun', 'bilibili', 'both'):
                    upload_target_normalized = 'acfun'
                config['UPLOAD_TARGET_DEFAULT'] = upload_target_normalized
                upload_target_changed = config['UPLOAD_TARGET_DEFAULT'] != upload_target_before

                quality_mode_before = config.get('YOUTUBE_DOWNLOAD_QUALITY_MODE')
                config['YOUTUBE_DOWNLOAD_QUALITY_MODE'] = normalize_youtube_download_quality_mode(
                    quality_mode_before
                )
                quality_mode_changed = config['YOUTUBE_DOWNLOAD_QUALITY_MODE'] != quality_mode_before

                quality_height_before = config.get('YOUTUBE_DOWNLOAD_MAX_HEIGHT')
                config['YOUTUBE_DOWNLOAD_MAX_HEIGHT'] = normalize_youtube_download_max_height(
                    quality_height_before
                )
                quality_height_changed = config['YOUTUBE_DOWNLOAD_MAX_HEIGHT'] != quality_height_before

                session_timeout_before = config.get('LOGIN_SESSION_TIMEOUT_MINUTES')
                config['LOGIN_SESSION_TIMEOUT_MINUTES'] = normalize_login_session_timeout_minutes(
                    session_timeout_before
                )
                session_timeout_changed = (
                    config['LOGIN_SESSION_TIMEOUT_MINUTES'] != session_timeout_before
                )
                removed_unknown_keys = bool(removed_keys)

                # 如果有新添加的默认键或需要纠正的项，则保存更新后的配置
                if (
                    missing_keys
                    or encoder_changed
                    or upload_target_changed
                    or quality_mode_changed
                    or quality_height_changed
                    or session_timeout_changed
                    or migrated_legacy_speech
                    or removed_unknown_keys
                ):
                    if migrated_legacy_speech:
                        logger.info("检测到旧版 ASR/VAD 默认值，已自动迁移到质量优先默认配置")
                    if removed_keys:
                        logger.info("已清理过期配置项: %s", ', '.join(sorted(removed_keys)))
                    save_config(config, config_path)
                return config
    except (json.JSONDecodeError, FileNotFoundError, PermissionError) as e:
        logger.warning(f"读取配置文件时出错: {str(e)}")
    
    # 如果配置文件不存在或读取失败，创建默认配置
    logger.info("使用默认配置并创建配置文件")
    save_config(DEFAULT_CONFIG, config_path)
    return DEFAULT_CONFIG

def save_config(config, config_path=None):
    """
    保存配置到文件
    
    Args:
        config (dict): 配置字典
        config_path (str, optional): 配置文件路径，如果不提供则使用默认路径
    
    Returns:
        bool: 保存是否成功
    """
    if not config_path:
        config_path = os.path.join(get_app_subdir('config'), 'config.json')
    
    # 确保config目录存在
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        logger.info("配置已保存到文件")
        return True
    except Exception as e:
        logger.error(f"保存配置文件时出错: {str(e)}")
        return False

def update_config(new_config):
    """
    更新配置
    
    Args:
        new_config (dict): 新的配置项
        
    Returns:
        dict: 更新后的完整配置
    """
    config_path = os.path.join(get_app_subdir('config'), 'config.json')
    
    # 加载当前配置
    current_config = load_config()
    
    # 更新配置
    for key in DEFAULT_CONFIG:
        if key in new_config:
            # 特殊处理布尔值
            if isinstance(DEFAULT_CONFIG[key], bool):
                current_config[key] = str(new_config[key]).lower() in ['true', '1', 'on']
            elif key == 'password':
                if new_config[key]: # Only update password if a new one is provided
                    current_config[key] = new_config[key]
            elif key == 'VIDEO_ENCODER':
                # 支持硬件编码：auto/cpu/nvidia/intel/amd
                encoder_value = str(new_config[key]).lower().strip()
                valid_encoders = ('auto', 'cpu', 'nvidia', 'intel', 'amd')
                if encoder_value in valid_encoders:
                    current_config[key] = encoder_value
                else:
                    logger.warning("无效的视频编码器配置值，已回退为 auto")
                    current_config[key] = 'auto'
            elif key == 'UPLOAD_TARGET_DEFAULT':
                target = str(new_config[key]).strip().lower()
                current_config[key] = target if target in ('acfun', 'bilibili', 'both') else 'acfun'
            elif key == 'YOUTUBE_DOWNLOAD_QUALITY_MODE':
                current_config[key] = normalize_youtube_download_quality_mode(new_config[key])
            elif key == 'YOUTUBE_DOWNLOAD_MAX_HEIGHT':
                current_config[key] = normalize_youtube_download_max_height(new_config[key])
            elif key == 'LOGIN_SESSION_TIMEOUT_MINUTES':
                current_config[key] = normalize_login_session_timeout_minutes(new_config[key])
            else:
                current_config[key] = new_config[key]

    current_config, _ = _prune_unknown_config_keys(current_config)

    # 保存更新后的配置
    save_config(current_config, config_path)
    
    return current_config

def reset_specific_config(keys):
    """
    重置指定的配置项为默认值
    
    Args:
        keys (list): 要重置的配置键列表
        
    Returns:
        dict: 更新后的配置
    """
    config_path = os.path.join(get_app_subdir('config'), 'config.json')
    current_config = load_config()
    updated = False
    
    for key in keys:
        if key in DEFAULT_CONFIG:
            current_config[key] = DEFAULT_CONFIG[key]
            updated = True
            
    if updated:
        save_config(current_config, config_path)
        
    return current_config

# 初始化时加载配置
load_config()
