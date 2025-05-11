#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging

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
    "LOG_CLEANUP_DAYS": 7, # 保留最近多少天的日志
    "LOG_CLEANUP_INTERVAL": 12, # 日志清理间隔（小时）
    "YOUTUBE_COOKIES_PATH": "cookies.txt", # 相对于项目根目录
    "ACFUN_USERNAME": "",
    "ACFUN_PASSWORD": "",
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "OPENAI_MODEL_NAME": "gpt-3.5-turbo",
    "ALIYUN_ACCESS_KEY_ID": "",
    "ALIYUN_ACCESS_KEY_SECRET": "",
    "ALIYUN_CONTENT_MODERATION_REGION": "cn-shanghai",
    "ALIYUN_TEXT_MODERATION_SERVICE": "comment_detection_pro",
    "COVER_PROCESSING_MODE": "crop"
}

def load_config():
    """
    加载配置文件，如果不存在则创建默认配置
    
    Returns:
        dict: 配置字典
    """
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.json')
    
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
                    save_config(config, config_path) # <--- 这一行需要缩进
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
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.json')
    
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        logger.info("配置已保存到文件")
        return True
    except Exception as e:
        logger.error(f"保存配置文件时出错: {str(e)}")
        return False

def update_config(new_settings):
    """
    更新配置
    
    Args:
        new_settings (dict): 新的配置项
        
    Returns:
        dict: 更新后的完整配置
    """
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.json')
    
    # 加载当前配置
    current_config = load_config()
    
    # 更新配置
    for key, value in new_settings.items():
        if key in DEFAULT_CONFIG: # 只更新DEFAULT_CONFIG中存在的键
            # 处理布尔值（表单提交的可能是字符串）
            if isinstance(DEFAULT_CONFIG[key], bool) and isinstance(value, str):
                current_config[key] = value.lower() in ('true', 'yes', 'y', '1', 'on')
            else:
                current_config[key] = value
        elif key in current_config: # 如果键不在DEFAULT_CONFIG但在current_config中，也更新（可能是一些动态添加的或旧的配置）
             current_config[key] = value

    
    # 保存更新后的配置
    save_config(current_config, config_path)
    
    return current_config 