#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
from .utils import get_app_subdir

# 获取日志记录器
logger = logging.getLogger('config_manager')

# 默认配置
DEFAULT_CONFIG = {
    "AUTO_MODE_ENABLED": False, # 无人值守自动投稿总开关
    "TRANSLATE_TITLE": False,
    "TRANSLATE_DESCRIPTION": False,
    "GENERATE_TAGS": False,
    "RECOMMEND_PARTITION": False,
    "CONTENT_MODERATION_ENABLED": False,
    "LOG_CLEANUP_ENABLED": True, # 是否启用日志自动清理
    "LOG_CLEANUP_HOURS": 72, # 保留最近多少小时的日志
    "LOG_CLEANUP_INTERVAL": 12, # 日志清理间隔（小时）
    "DOWNLOAD_CLEANUP_ENABLED": False, # 是否启用下载内容自动清理
    "DOWNLOAD_CLEANUP_HOURS": 72, # 保留最近多少小时的下载内容
    "DOWNLOAD_CLEANUP_INTERVAL": 24, # 下载内容清理间隔（小时）
    "password_protection_enabled": False,
    "password": "",
    # 登录安全控制
    "LOGIN_MAX_FAILED_ATTEMPTS": 5,  # 达到该失败次数后触发锁定
    "LOGIN_LOCKOUT_MINUTES": 15,     # 被锁定后持续的分钟数
    "YOUTUBE_COOKIES_PATH": "cookies/yt_cookies.txt", # 相对于项目根目录
    "ACFUN_COOKIES_PATH": "cookies/ac_cookies.txt", # AcFun Cookie文件路径
    "ACFUN_USERNAME": "",
    "ACFUN_PASSWORD": "",
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "OPENAI_MODEL_NAME": "gpt-3.5-turbo",
    # 固定分区ID（可选）：如设置则推荐分区将直接使用该ID
    "FIXED_PARTITION_ID": "",
    # 字幕翻译可单独指定OpenAI Base URL；为空则回退到 OPENAI_BASE_URL
    "SUBTITLE_OPENAI_BASE_URL": "",
    # 字幕翻译可单独指定 API Key 与 模型名；为空则分别回退到 OPENAI_API_KEY 与 OPENAI_MODEL_NAME
    "SUBTITLE_OPENAI_API_KEY": "",
    "SUBTITLE_OPENAI_MODEL_NAME": "",
    "YOUTUBE_API_KEY": "",
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
    "YOUTUBE_THROTTLED_RATE": "",  # 限制下载速度，格式如：1M、500K等，留空不限制
    # 外部工具路径
    "FFMPEG_LOCATION": "",  # 可选：覆盖 ffmpeg 可执行文件路径；留空则使用项目内置版本
    "FFMPEG_AUTO_DOWNLOAD": True,  # 仅在 Windows 且缺失时尝试联网补齐 ffmpeg/ 目录
    # 字幕翻译相关配置
    "SUBTITLE_TRANSLATION_ENABLED": False,  # 是否启用字幕翻译
    "SUBTITLE_SOURCE_LANGUAGE": "auto",  # 源语言 (auto, en, ja, ko等)
    "SUBTITLE_TARGET_LANGUAGE": "zh",  # 目标语言 (zh, en, ja, ko等)
    "SUBTITLE_API_PROVIDER": "openai",  # API提供商 (仅支持openai)
    "SUBTITLE_BATCH_SIZE": 3,  # 批次大小
    "SUBTITLE_MAX_RETRIES": 3,  # 最大重试次数
    "SUBTITLE_RETRY_DELAY": 2,  # 重试延迟(秒)
    "SUBTITLE_EMBED_IN_VIDEO": True,  # 是否将字幕嵌入视频
    "SUBTITLE_KEEP_ORIGINAL": True,  # 是否保留原始字幕文件
    "SUBTITLE_MAX_WORKERS": 2,  # 字幕翻译最大并发线程数
    # 并发控制配置
    "MAX_CONCURRENT_TASKS": 2,  # 最大并发任务数
    "MAX_CONCURRENT_UPLOADS": 1,  # 最大并发上传数
    # 视频转码相关
    "VIDEO_ENCODER": "cpu"  # 选择视频编码器：cpu / nvenc / qsv / amf
    ,
    # 语音识别（无字幕转写）
    "SPEECH_RECOGNITION_ENABLED": False,  # 启用语音识别生成字幕
    "SPEECH_RECOGNITION_PROVIDER": "whisper",  # whisper（OpenAI兼容）
    "SPEECH_RECOGNITION_OUTPUT_FORMAT": "srt",  # srt 或 vtt
    # 语音识别结果质量门槛（开关+阈值）：少于最小条目数则视为无字幕
    "SPEECH_RECOGNITION_MIN_SUBTITLE_LINES_ENABLED": True,
    "SPEECH_RECOGNITION_MIN_SUBTITLE_LINES": 5,
    # Whisper/OpenAI 兼容配置（可单独配置，未设置则回退到 OPENAI_*）
    "WHISPER_API_KEY": "",
    "WHISPER_BASE_URL": "",
    "WHISPER_MODEL_NAME": "whisper-1",
    # 语音活动检测（VAD）
    "VAD_ENABLED": False,
    "VAD_PROVIDER": "silero-vad",
    "VAD_API_URL": "http://localhost:8080/vad",
    "VAD_API_TOKEN": "",
    "VAD_SILERO_THRESHOLD": 0.5,
    "VAD_SILERO_MIN_SPEECH_MS": 250,
    "VAD_SILERO_MIN_SILENCE_MS": 100,
    "VAD_SILERO_MAX_SPEECH_S": 120,
    "VAD_SILERO_SPEECH_PAD_MS": 30,
    "VAD_MAX_SEGMENT_S": 90,
    # 音频分片策略（针对长音频）
    "AUDIO_CHUNK_WINDOW_S": 25.0,  # 固定窗口大小（20-30s推荐）
    "AUDIO_CHUNK_OVERLAP_S": 0.2,  # 重叠时间确保连续性
    # VAD后处理约束（控制字幕粒度）
    "VAD_MERGE_GAP_S": 0.25,  # 合并间隙小于此值的片段（0.20-0.25s）
    "VAD_MIN_SEGMENT_S": 1.0,  # 最短片段时长（0.8-1.2s）
    "VAD_MAX_SEGMENT_S_FOR_SPLIT": 8.0,  # 超过此时长需二次切分（8-10s）
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
    # 重试与回退策略
    "WHISPER_MAX_RETRIES": 3,  # API调用最大重试次数
    "WHISPER_RETRY_DELAY_S": 2.0,  # 重试延迟（秒，指数退避）
    "WHISPER_FALLBACK_TO_FIXED_CHUNKS": True,  # VAD失败时回退到固定切分
}

CONFIG_FILE = "config.json"
config = {}

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
                
                # 确保所有默认配置项都存在
                missing_keys = False
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                        missing_keys = True
                
                # 如果有新添加的默认键，则保存更新后的配置
                if missing_keys:
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
            else:
                current_config[key] = new_config[key]

    # 保存更新后的配置
    save_config(current_config, config_path)
    
    return current_config

# 初始化时加载配置
load_config() 
