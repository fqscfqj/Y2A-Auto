#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import sqlite3
import datetime
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import threading
import time
from apscheduler.schedulers.background import BackgroundScheduler
from logging.handlers import RotatingFileHandler
from modules.task_manager import add_task
from .utils import get_app_subdir

def setup_youtube_monitor_logger():
    """设置YouTube监控专用日志"""
    logger = logging.getLogger('Y2A-Auto.YouTube-Monitor')
    
    # 如果已经设置过处理器，直接返回
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.INFO)
    
    # 创建logs目录
    logs_dir = get_app_subdir('logs')
    os.makedirs(logs_dir, exist_ok=True)
    
    # 文件处理器 - 使用轮转日志
    log_file = os.path.join(logs_dir, 'youtube_monitor.log')
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.INFO)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter('%(asctime)s - YouTube监控 - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.INFO)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_youtube_monitor_logger()

class YouTubeMonitor:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.youtube = None
        self.scheduler = BackgroundScheduler()
        self.db_path = os.path.join(get_app_subdir('db'), 'youtube_monitor.db')
        self._init_database()
        self._init_youtube_api()
        
    def _init_database(self):
        """初始化数据库"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        # 检查数据库是否为新建
        is_new_database = not os.path.exists(self.db_path)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 监控配置表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitor_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT 1,
                    region_code TEXT DEFAULT 'US',
                    category_id TEXT DEFAULT '0',
                    time_period INTEGER DEFAULT 7,
                    max_results INTEGER DEFAULT 10,
                    min_view_count INTEGER DEFAULT 0,
                    min_like_count INTEGER DEFAULT 0,
                    min_comment_count INTEGER DEFAULT 0,
                    keywords TEXT DEFAULT '',
                    exclude_keywords TEXT DEFAULT '',
                    channel_ids TEXT DEFAULT '',
                    exclude_channel_ids TEXT DEFAULT '',
                    min_duration INTEGER DEFAULT 0,
                    max_duration INTEGER DEFAULT 0,
                    schedule_type TEXT DEFAULT 'manual',
                    schedule_interval INTEGER DEFAULT 120,
                    order_by TEXT DEFAULT 'viewCount',
                    start_date TEXT DEFAULT '',
                    rate_limit_requests INTEGER DEFAULT 20,
                    rate_limit_window INTEGER DEFAULT 60,
                    last_run_time TEXT,
                    created_time TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_time TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 为现有表添加新字段（如果不存在）
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN order_by TEXT DEFAULT 'viewCount'")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN start_date TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN rate_limit_requests INTEGER DEFAULT 100")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN rate_limit_window INTEGER DEFAULT 60")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN auto_add_to_tasks BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            
            # 添加新的监控类型字段
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN monitor_type TEXT DEFAULT 'youtube_search'")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN channel_mode TEXT DEFAULT 'latest'")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN channel_keywords TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN end_date TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN latest_days INTEGER DEFAULT 7")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN latest_max_results INTEGER DEFAULT 20")
            except sqlite3.OperationalError:
                pass
            
            # 添加历史搬运进度记录字段
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN historical_progress_date TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            # 添加当前时间段处理偏移量字段
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN historical_offset INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            
            # 监控历史表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitor_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id INTEGER,
                    video_id TEXT NOT NULL,
                    video_title TEXT,
                    channel_title TEXT,
                    view_count INTEGER,
                    like_count INTEGER,
                    comment_count INTEGER,
                    duration TEXT,
                    published_at TEXT,
                    added_to_tasks BOOLEAN DEFAULT 0,
                    run_time TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (config_id) REFERENCES monitor_configs (id)
                )
            ''')
            
            conn.commit()
        
        # 如果是新数据库或表为空，尝试从配置文件恢复
        self._restore_configs_from_files()
    
    def _restore_configs_from_files(self):
        """从配置文件恢复监控配置到数据库"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            
            if not os.path.exists(config_dir):
                logger.info("配置文件目录不存在，跳过恢复")
                return
            
            # 检查数据库中是否已有配置
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM monitor_configs')
                existing_count = cursor.fetchone()[0]
                
                if existing_count > 0:
                    logger.info(f"数据库中已有 {existing_count} 个配置，跳过恢复")
                    return
            
            # 扫描配置文件
            config_files = []
            for filename in os.listdir(config_dir):
                if filename.startswith('monitor_config_') and filename.endswith('.json'):
                    config_files.append(filename)
            
            if not config_files:
                logger.info("未找到配置文件，跳过恢复")
                return
            
            logger.info(f"发现 {len(config_files)} 个配置文件，开始恢复到数据库")
            
            restored_count = 0
            for filename in sorted(config_files):
                try:
                    config_file_path = os.path.join(config_dir, filename)
                    with open(config_file_path, 'r', encoding='utf-8') as f:
                        config_data = json.load(f)
                    
                    # 提取配置ID
                    original_config_id = config_data.get('config_id')
                    if not original_config_id:
                        logger.warning(f"配置文件 {filename} 缺少config_id，跳过")
                        continue
                    
                    # 移除不需要插入数据库的字段
                    config_data.pop('config_id', None)
                    config_data.pop('created_time', None)
                    
                    # 恢复到数据库，保持原有ID
                    restored_id = self._restore_single_config(config_data, original_config_id)
                    if restored_id:
                        restored_count += 1
                        logger.info(f"恢复配置: {config_data.get('name', '未命名')} (ID: {original_config_id} -> {restored_id})")
                    
                except Exception as e:
                    logger.error(f"恢复配置文件 {filename} 失败: {str(e)}")
                    continue
            
            if restored_count > 0:
                logger.info(f"成功恢复 {restored_count} 个监控配置")
                
                # 启动已启用的自动调度配置
                self._restart_restored_schedules()
            else:
                logger.warning("没有成功恢复任何配置")
                
        except Exception as e:
            logger.error(f"从配置文件恢复失败: {str(e)}")
    
    def _restore_single_config(self, config_data, target_id):
        """恢复单个配置到数据库，尝试保持原有ID"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 检查目标ID是否可用
                cursor.execute('SELECT id FROM monitor_configs WHERE id = ?', (target_id,))
                if cursor.fetchone():
                    logger.warning(f"ID {target_id} 已存在，使用自动分配的ID")
                    target_id = None
                
                if target_id:
                    # 尝试使用指定的ID
                    cursor.execute('''
                        INSERT INTO monitor_configs (
                            id, name, enabled, monitor_type, channel_mode, region_code, category_id, time_period, max_results,
                            min_view_count, min_like_count, min_comment_count, keywords,
                            exclude_keywords, channel_ids, channel_keywords, exclude_channel_ids,
                            min_duration, max_duration, schedule_type, schedule_interval,
                            order_by, start_date, end_date, latest_days, latest_max_results,
                            rate_limit_requests, rate_limit_window, auto_add_to_tasks, historical_progress_date, historical_offset
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        target_id,
                        config_data.get('name'),
                        config_data.get('enabled', True),
                        config_data.get('monitor_type', 'youtube_search'),
                        config_data.get('channel_mode', 'latest'),
                        config_data.get('region_code', 'US'),
                        config_data.get('category_id', '0'),
                        config_data.get('time_period', 7),
                        config_data.get('max_results', 10),
                        config_data.get('min_view_count', 0),
                        config_data.get('min_like_count', 0),
                        config_data.get('min_comment_count', 0),
                        config_data.get('keywords', ''),
                        config_data.get('exclude_keywords', ''),
                        config_data.get('channel_ids', ''),
                        config_data.get('channel_keywords', ''),
                        config_data.get('exclude_channel_ids', ''),
                        config_data.get('min_duration', 0),
                        config_data.get('max_duration', 0),
                        config_data.get('schedule_type', 'manual'),
                        config_data.get('schedule_interval', 120),
                        config_data.get('order_by', 'viewCount'),
                        config_data.get('start_date', ''),
                        config_data.get('end_date', ''),
                        config_data.get('latest_days', 7),
                        config_data.get('latest_max_results', 20),
                        config_data.get('rate_limit_requests', 100),
                        config_data.get('rate_limit_window', 60),
                        config_data.get('auto_add_to_tasks', False),
                        config_data.get('historical_progress_date', ''),
                        config_data.get('historical_offset', 0)
                    ))
                    
                    # 使用指定的ID
                    config_id = target_id
                else:
                    # 使用自动分配的ID
                    cursor.execute('''
                        INSERT INTO monitor_configs (
                            name, enabled, monitor_type, channel_mode, region_code, category_id, time_period, max_results,
                            min_view_count, min_like_count, min_comment_count, keywords,
                            exclude_keywords, channel_ids, channel_keywords, exclude_channel_ids,
                            min_duration, max_duration, schedule_type, schedule_interval,
                            order_by, start_date, end_date, latest_days, latest_max_results,
                            rate_limit_requests, rate_limit_window, auto_add_to_tasks, historical_progress_date, historical_offset
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        config_data.get('name'),
                        config_data.get('enabled', True),
                        config_data.get('monitor_type', 'youtube_search'),
                        config_data.get('channel_mode', 'latest'),
                        config_data.get('region_code', 'US'),
                        config_data.get('category_id', '0'),
                        config_data.get('time_period', 7),
                        config_data.get('max_results', 10),
                        config_data.get('min_view_count', 0),
                        config_data.get('min_like_count', 0),
                        config_data.get('min_comment_count', 0),
                        config_data.get('keywords', ''),
                        config_data.get('exclude_keywords', ''),
                        config_data.get('channel_ids', ''),
                        config_data.get('channel_keywords', ''),
                        config_data.get('exclude_channel_ids', ''),
                        config_data.get('min_duration', 0),
                        config_data.get('max_duration', 0),
                        config_data.get('schedule_type', 'manual'),
                        config_data.get('schedule_interval', 60),
                        config_data.get('order_by', 'viewCount'),
                        config_data.get('start_date', ''),
                        config_data.get('end_date', ''),
                        config_data.get('latest_days', 7),
                        config_data.get('latest_max_results', 20),
                        config_data.get('rate_limit_requests', 100),
                        config_data.get('rate_limit_window', 60),
                        config_data.get('auto_add_to_tasks', False),
                        config_data.get('historical_progress_date', ''),
                        config_data.get('historical_offset', 0)
                    ))
                    
                    config_id = cursor.lastrowid
                
                conn.commit()
                return config_id
                
        except Exception as e:
            logger.error(f"恢复单个配置失败: {str(e)}")
            return None
    
    def _restart_restored_schedules(self):
        """重新启动已恢复配置的自动调度"""
        try:
            configs = self.get_monitor_configs()
            restored_schedules = 0
            
            for config in configs:
                if config['enabled'] and config['schedule_type'] == 'auto':
                    self._schedule_monitor(config['id'], config['schedule_interval'])
                    restored_schedules += 1
            
            if restored_schedules > 0:
                logger.info(f"重新启动了 {restored_schedules} 个自动调度任务")
                
                # 启动调度器
                if not self.scheduler.running:
                    self.scheduler.start()
                    
        except Exception as e:
            logger.error(f"重新启动调度任务失败: {str(e)}")
    
    def _init_youtube_api(self):
        """初始化YouTube API"""
        if self.api_key:
            try:
                self.youtube = build('youtube', 'v3', developerKey=self.api_key)
                logger.info("YouTube API初始化成功")
            except Exception as e:
                logger.error(f"YouTube API初始化失败: {str(e)}")
                self.youtube = None
    
    def set_api_key(self, api_key):
        """设置API密钥"""
        self.api_key = api_key
        self._init_youtube_api()
    
    def create_monitor_config(self, config_data):
        """创建监控配置"""
        logger.info(f"开始创建监控配置: {config_data.get('name', '未命名')}")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    INSERT INTO monitor_configs (
                        name, enabled, monitor_type, channel_mode, region_code, category_id, time_period, max_results,
                        min_view_count, min_like_count, min_comment_count, keywords,
                        exclude_keywords, channel_ids, channel_keywords, exclude_channel_ids,
                        min_duration, max_duration, schedule_type, schedule_interval,
                        order_by, start_date, end_date, latest_days, latest_max_results,
                        rate_limit_requests, rate_limit_window, auto_add_to_tasks, historical_progress_date, historical_offset
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    config_data.get('name'),
                    config_data.get('enabled', True),
                    config_data.get('monitor_type', 'youtube_search'),
                    config_data.get('channel_mode', 'latest'),
                    config_data.get('region_code', 'US'),
                    config_data.get('category_id', '0'),
                    config_data.get('time_period', 7),
                    config_data.get('max_results', 10),
                    config_data.get('min_view_count', 0),
                    config_data.get('min_like_count', 0),
                    config_data.get('min_comment_count', 0),
                    config_data.get('keywords', ''),
                    config_data.get('exclude_keywords', ''),
                    config_data.get('channel_ids', ''),
                    config_data.get('channel_keywords', ''),
                    config_data.get('exclude_channel_ids', ''),
                    config_data.get('min_duration', 0),
                    config_data.get('max_duration', 0),
                    config_data.get('schedule_type', 'manual'),
                    config_data.get('schedule_interval', 120),
                    config_data.get('order_by', 'viewCount'),
                    config_data.get('start_date', ''),
                    config_data.get('end_date', ''),
                    config_data.get('latest_days', 7),
                    config_data.get('latest_max_results', 20),
                    config_data.get('rate_limit_requests', 100),
                    config_data.get('rate_limit_window', 60),
                    config_data.get('auto_add_to_tasks', False),
                    config_data.get('historical_progress_date', ''),
                    config_data.get('historical_offset', 0)
                ))
                
                config_id = cursor.lastrowid
                conn.commit()
                
                # 如果为频道监控的持续跟进最新模式，则在创建时将基准时间设为当前时间
                try:
                    is_channel_monitor = bool(config_data.get('channel_ids') and str(config_data.get('channel_ids')).strip())
                    if config_data.get('channel_mode') == 'latest' and is_channel_monitor:
                        cursor.execute(
                            'UPDATE monitor_configs SET last_run_time = CURRENT_TIMESTAMP WHERE id = ?',
                            (config_id,)
                        )
                        conn.commit()
                        logger.info("创建配置：已为持续跟进最新模式设置基准时间为当前时间")
                except Exception as e:
                    logger.warning(f"设置最新跟进基准时间失败: {str(e)}")
                
                logger.info(f"监控配置已创建，ID: {config_id}, 名称: {config_data.get('name')}")
            
            # 保存配置到文件
            self._save_config_to_file(config_id, config_data)
            
            # 如果是自动调度，添加到调度器
            if config_data.get('schedule_type') == 'auto':
                logger.info(f"配置 {config_id} 启用自动调度，间隔: {config_data.get('schedule_interval', 60)}分钟")
                self._schedule_monitor(config_id, config_data.get('schedule_interval', 60))
            
            return config_id
                
        except Exception as e:
            logger.error(f"创建监控配置失败: {str(e)}")
            raise
    
    def _save_config_to_file(self, config_id, config_data):
        """保存配置到文件"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            os.makedirs(config_dir, exist_ok=True)
            
            config_file = os.path.join(config_dir, f"monitor_config_{config_id}.json")
            
            # 添加配置ID到数据中
            config_data_with_id = config_data.copy()
            config_data_with_id['config_id'] = config_id
            config_data_with_id['created_time'] = datetime.now().isoformat()
            
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config_data_with_id, f, ensure_ascii=False, indent=2)
            
            logger.info(f"监控配置已保存到文件: {config_file}")
        except Exception as e:
            logger.error(f"保存配置文件失败: {str(e)}")
    
    def _load_config_from_file(self, config_id):
        """从文件加载配置"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            config_file = os.path.join(config_dir, f"monitor_config_{config_id}.json")
            
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载配置文件失败: {str(e)}")
        return None
    
    def _delete_config_file(self, config_id):
        """删除配置文件"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            config_file = os.path.join(config_dir, f"monitor_config_{config_id}.json")
            
            if os.path.exists(config_file):
                os.remove(config_file)
                logger.info(f"配置文件已删除: {config_file}")
        except Exception as e:
            logger.error(f"删除配置文件失败: {str(e)}")
    
    def get_monitor_configs(self):
        """获取所有监控配置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM monitor_configs ORDER BY created_time DESC')
            
            columns = [description[0] for description in cursor.description]
            configs = []
            for row in cursor.fetchall():
                config = dict(zip(columns, row))
                configs.append(config)
            
            return configs
    
    def get_monitor_config(self, config_id):
        """获取指定监控配置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM monitor_configs WHERE id = ?', (config_id,))
            
            row = cursor.fetchone()
            if row:
                columns = [description[0] for description in cursor.description]
                return dict(zip(columns, row))
            return None
    
    def update_monitor_config(self, config_id, config_data):
        """更新监控配置"""
        logger.info(f"更新监控配置，ID: {config_id}")
        
        try:
            # 获取原有配置
            old_config = self.get_monitor_config(config_id)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 检查历史搬运模式下时间范围是否发生变化
                should_reset_offset = False
                if (config_data.get('channel_mode') == 'historical' and old_config and 
                    old_config.get('channel_mode') == 'historical'):
                    # 检查开始日期或结束日期是否变化
                    old_start = old_config.get('start_date', '')
                    old_end = old_config.get('end_date', '')
                    new_start = config_data.get('start_date', '')
                    new_end = config_data.get('end_date', '')
                    
                    if old_start != new_start or old_end != new_end:
                        should_reset_offset = True
                        logger.info(f"检测到历史搬运模式时间范围变化，将重置偏移量：{old_start}-{old_end} → {new_start}-{new_end}")
                
                # 如果需要重置偏移量，将其设为0
                if should_reset_offset:
                    config_data['historical_offset'] = 0
                
                cursor.execute('''
                    UPDATE monitor_configs SET
                        name = ?, enabled = ?, monitor_type = ?, channel_mode = ?, region_code = ?, category_id = ?,
                        time_period = ?, max_results = ?, min_view_count = ?,
                        min_like_count = ?, min_comment_count = ?, keywords = ?,
                        exclude_keywords = ?, channel_ids = ?, channel_keywords = ?, exclude_channel_ids = ?,
                        min_duration = ?, max_duration = ?, schedule_type = ?,
                        schedule_interval = ?, order_by = ?, start_date = ?, end_date = ?,
                        latest_days = ?, latest_max_results = ?,
                        rate_limit_requests = ?, rate_limit_window = ?, auto_add_to_tasks = ?,
                        historical_progress_date = ?, historical_offset = ?, updated_time = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (
                    config_data.get('name'),
                    config_data.get('enabled', True),
                    config_data.get('monitor_type', 'youtube_search'),
                    config_data.get('channel_mode', 'latest'),
                    config_data.get('region_code', 'US'),
                    config_data.get('category_id', '0'),
                    config_data.get('time_period', 7),
                    config_data.get('max_results', 10),
                    config_data.get('min_view_count', 0),
                    config_data.get('min_like_count', 0),
                    config_data.get('min_comment_count', 0),
                    config_data.get('keywords', ''),
                    config_data.get('exclude_keywords', ''),
                    config_data.get('channel_ids', ''),
                    config_data.get('channel_keywords', ''),
                    config_data.get('exclude_channel_ids', ''),
                    config_data.get('min_duration', 0),
                    config_data.get('max_duration', 0),
                    config_data.get('schedule_type', 'manual'),
                    config_data.get('schedule_interval', 120),
                    config_data.get('order_by', 'viewCount'),
                    config_data.get('start_date', ''),
                    config_data.get('end_date', ''),
                    config_data.get('latest_days', 7),
                    config_data.get('latest_max_results', 20),
                    config_data.get('rate_limit_requests', 100),
                    config_data.get('rate_limit_window', 60),
                    config_data.get('auto_add_to_tasks', False),
                    config_data.get('historical_progress_date', ''),
                    config_data.get('historical_offset', old_config.get('historical_offset', 0) if old_config and not should_reset_offset else 0),
                    config_id
                ))
                
                conn.commit()
                
                # 如果从其他模式切换为持续跟进最新模式，则将基准时间设为当前时间
                try:
                    became_latest = (
                        config_data.get('channel_mode') == 'latest' and 
                        (not old_config or old_config.get('channel_mode') != 'latest')
                    )
                    is_channel_monitor = bool(config_data.get('channel_ids') and str(config_data.get('channel_ids')).strip())
                    if became_latest and is_channel_monitor:
                        cursor.execute(
                            'UPDATE monitor_configs SET last_run_time = CURRENT_TIMESTAMP WHERE id = ?',
                            (config_id,)
                        )
                        conn.commit()
                        logger.info("切换为持续跟进最新模式：已设置基准时间为当前时间")
                except Exception as e:
                    logger.warning(f"更新最新跟进基准时间失败: {str(e)}")
                
                if should_reset_offset:
                    logger.info(f"历史搬运偏移量已重置为0")
                
                logger.info(f"监控配置已更新: {config_data.get('name')} (ID: {config_id})")
            
            # 保存配置到文件
            self._save_config_to_file(config_id, config_data)
            
            # 更新调度
            logger.info(f"更新调度设置: {config_data.get('schedule_type', 'manual')}")
            self._update_schedule(config_id, config_data)
                
        except Exception as e:
            logger.error(f"更新监控配置失败，ID: {config_id}, 错误: {str(e)}")
            raise
    
    def delete_monitor_config(self, config_id):
        """删除监控配置"""
        logger.info(f"开始删除监控配置，ID: {config_id}")
        
        try:
            # 获取配置信息用于日志
            config = self.get_monitor_config(config_id)
            config_name = config['name'] if config else f"ID-{config_id}"
            
            # 移除调度
            self._remove_schedule(config_id)
            
            # 删除配置文件
            self._delete_config_file(config_id)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 获取历史记录数量
                cursor.execute('SELECT COUNT(*) FROM monitor_history WHERE config_id = ?', (config_id,))
                history_count = cursor.fetchone()[0]
                
                cursor.execute('DELETE FROM monitor_configs WHERE id = ?', (config_id,))
                cursor.execute('DELETE FROM monitor_history WHERE config_id = ?', (config_id,))
                conn.commit()
                
                logger.info(f"监控配置已删除: {config_name} (ID: {config_id}), 同时删除了 {history_count} 条历史记录")
                
        except Exception as e:
            logger.error(f"删除监控配置失败，ID: {config_id}, 错误: {str(e)}")
            raise
    
    def run_monitor(self, config_id):
        """执行监控任务"""
        logger.info(f"开始执行监控任务，配置ID: {config_id}")
        
        if not self.youtube:
            logger.error("YouTube API未初始化")
            return False, "YouTube API未初始化"
        
        config = self.get_monitor_config(config_id)
        if not config:
            logger.error(f"监控配置不存在: {config_id}")
            return False, "监控配置不存在"
        
        logger.info(f"执行监控配置: {config['name']} (ID: {config_id})")
        logger.info(f"监控类型: {config.get('monitor_type', 'youtube_search')}, "
                   f"频道模式: {config.get('channel_mode', 'latest')}")
        
        try:
            # 获取视频
            logger.info("开始获取视频数据...")
            videos = self._fetch_trending_videos(config)
            logger.info(f"获取到 {len(videos)} 个视频")
            
            # 筛选视频
            logger.info("开始筛选视频...")
            filtered_videos = self._filter_videos(videos, config)
            logger.info(f"筛选后剩余 {len(filtered_videos)} 个视频")
            
            # 历史搬运模式需要考虑偏移量
            if config.get('channel_mode') == 'historical':
                current_offset = config.get('historical_offset', 0)
                if current_offset > 0:
                    logger.info(f"历史搬运模式，跳过前 {current_offset} 个视频")
                    filtered_videos = filtered_videos[current_offset:]
                    logger.info(f"应用偏移量后剩余 {len(filtered_videos)} 个视频")
            
            # 保存到历史记录
            added_count = 0
            processed_count = 0
            auto_add_enabled = config.get('auto_add_to_tasks', False)
            
            # 获取添加到任务队列的数量限制
            # 所有模式都使用rate_limit_requests来控制每次添加的视频数量
            max_add_to_tasks = config.get('rate_limit_requests', 20) if auto_add_enabled else 0
            
            logger.info(f"开始处理视频，自动添加到任务队列: {'是' if auto_add_enabled else '否'}")
            if auto_add_enabled:
                logger.info(f"本次最大添加到任务队列数量: {max_add_to_tasks}")
            
            for video in filtered_videos:
                # 检查是否已经处理过
                if not self._is_video_processed(video['id'], config_id):
                    # 检查是否还能添加到任务队列
                    should_add_to_tasks = auto_add_enabled and added_count < max_add_to_tasks
                    
                    # 始终保存到历史记录，但是否添加到任务队列由auto_add_enabled控制
                    self._save_video_history(video, config_id, auto_add_to_tasks=should_add_to_tasks)
                    processed_count += 1
                    
                    if should_add_to_tasks:
                        added_count += 1
                        logger.info(f"视频已添加到任务队列 ({added_count}/{max_add_to_tasks}): {video['title']}")
                    else:
                        if auto_add_enabled:
                            logger.info(f"视频已保存到历史记录: {video['title']}")
                        else:
                            logger.info(f"视频已保存到历史记录（未添加到任务队列）: {video['title']}")
                    
                    # 如果启用了自动添加且达到上限，跳出循环
                    if auto_add_enabled and added_count >= max_add_to_tasks:
                        logger.info(f"已达到本次添加上限 {max_add_to_tasks}，剩余视频将在下次运行时处理")
                        break
                else:
                    logger.debug(f"视频已处理过，跳过: {video['title']}")
            
            # 更新最后运行时间
            self._update_last_run_time(config_id)
            
            logger.info(f"监控任务完成 - 配置: {config['name']}, "
                       f"处理新视频: {processed_count}, 添加到任务队列: {added_count}")
            
            # 更新历史搬运进度（如果是历史模式）
            if config.get('channel_mode') == 'historical':
                # 获取应用偏移量前的完整筛选结果
                original_filtered = self._filter_videos(videos, config)
                self._update_historical_progress(config_id, original_filtered, added_count)
            
            return True, f"监控完成，处理了 {processed_count} 个新视频，添加了 {added_count} 个到任务队列"
            
        except Exception as e:
            logger.error(f"监控任务执行失败 - 配置: {config['name']} (ID: {config_id}), 错误: {str(e)}")
            return False, f"监控失败: {str(e)}"
    
    def _fetch_trending_videos(self, config):
        """获取视频"""
        try:
            # 设置时间范围
            published_after = None
            published_before = None
            
            # 历史搬运模式的智能时间推进
            if config.get('channel_mode') == 'historical' and config.get('start_date'):
                published_after, published_before, current_offset = self._get_historical_time_range(config)
            elif config.get('start_date'):
                # 如果设置了开始日期，使用开始日期
                start_date = datetime.strptime(config['start_date'], '%Y-%m-%d')
                published_after = start_date.isoformat() + 'Z'
                logger.info(f"使用开始日期: {config['start_date']}")
                
                # 检查是否设置了结束日期
                if config.get('end_date'):
                    end_date = datetime.strptime(config['end_date'], '%Y-%m-%d')
                    # 结束日期加一天，确保包含当天的视频
                    end_date = end_date + timedelta(days=1)
                    published_before = end_date.isoformat() + 'Z'
                    logger.info(f"使用结束日期: {config['end_date']}")
            else:
                # 否则根据模式计算
                is_channel_monitor = bool(config.get('channel_ids') and str(config.get('channel_ids')).strip())
                if config.get('channel_mode') == 'latest' and is_channel_monitor:
                    # 持续跟进最新（频道监控）：从开启/上次运行时间开始算
                    last_run_str = config.get('last_run_time')
                    if last_run_str:
                        # SQLite CURRENT_TIMESTAMP 格式为 'YYYY-MM-DD HH:MM:SS'
                        try:
                            last_run_dt = datetime.strptime(last_run_str, '%Y-%m-%d %H:%M:%S')
                            published_after = last_run_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
                            logger.info(f"最新跟进模式：使用上次运行时间为基准: {published_after}")
                        except Exception:
                            # 解析失败则退回到当前时间
                            published_after = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
                            logger.info("最新跟进模式：无法解析上次运行时间，使用当前时间为基准")
                    else:
                        # 首次运行：从当前时间开始，不处理历史视频
                        published_after = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
                        logger.info("最新跟进模式：首次运行，仅从当前时间开始跟进新发布的视频")
                else:
                    # 其他模式或全局检索：使用配置的时间段
                    if config.get('channel_mode') == 'latest':
                        # 保持原有行为：对于非频道监控的'latest'，按调度间隔*2估算窗口
                        interval_hours = config.get('schedule_interval', 120) / 60 * 2
                        days = max(1, interval_hours / 24)  # 至少1天
                        logger.info(f"使用动态时间段: 最近 {days:.1f} 天（基于调度间隔 {config.get('schedule_interval', 120)} 分钟）")
                    else:
                        days = config.get('time_period', 7)
                        logger.info(f"使用时间段: 最近 {days} 天")
                    published_after = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
            
            # 如果指定了频道，优先使用频道搜索
            if config.get('channel_ids') and config['channel_ids'].strip():
                logger.info(f"使用频道监控模式，频道数量: {len([ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()])}")
                return self._fetch_channel_videos(config, published_after, published_before)
            else:
                logger.info(f"使用YouTube搜索模式，关键词: {config.get('keywords', '无')}")
                return self._fetch_search_videos(config, published_after, published_before)
                
        except HttpError as e:
            logger.error(f"YouTube API错误: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"获取视频数据失败: {str(e)}")
            raise
    
    def _fetch_search_videos(self, config, published_after, published_before=None):
        """通过搜索获取视频"""
        # 构建搜索参数
        search_params = {
            'part': 'id,snippet',
            'type': 'video',
            'order': config.get('order_by', 'viewCount'),
            'publishedAfter': published_after,
            'maxResults': min(config['max_results'] * 2, 50),  # 获取更多结果用于筛选
            'regionCode': config['region_code']
        }
        
        # 添加结束日期限制
        if published_before:
            search_params['publishedBefore'] = published_before
        
        # 添加关键词搜索
        if config['keywords']:
            search_params['q'] = config['keywords']
        
        # 添加分类过滤
        if config['category_id'] and config['category_id'] != '0':
            search_params['videoCategoryId'] = config['category_id']
        
        # 执行搜索
        search_response = self.youtube.search().list(**search_params).execute()
        
        video_ids = [item['id']['videoId'] for item in search_response['items']]
        
        if not video_ids:
            return []
        
        # 获取视频详细信息
        videos_response = self.youtube.videos().list(
            part='id,snippet,statistics,contentDetails',
            id=','.join(video_ids)
        ).execute()
        
        return videos_response['items']
    
    def _fetch_channel_videos(self, config, published_after, published_before=None):
        """从指定频道获取视频"""
        all_videos = []
        channel_ids = [ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()]
        
        # 实现请求速率限制
        request_count = 0
        max_requests = config.get('rate_limit_requests', 4)
        # 使用调度间隔作为时间窗口（转换为秒）
        request_window = config.get('schedule_interval', 60) * 60
        
        # 根据频道模式调整获取策略
        channel_mode = config.get('channel_mode', 'latest')
        logger.info(f"开始处理 {len(channel_ids)} 个频道，模式: {channel_mode}，请求限制: {max_requests}/{config.get('schedule_interval', 60)}分钟")
        
        for i, channel_id in enumerate(channel_ids, 1):
            if request_count >= max_requests:
                logger.warning(f"达到请求限制 {max_requests}/{config.get('schedule_interval', 60)}分钟，跳过剩余 {len(channel_ids) - i + 1} 个频道")
                break
                
            try:
                logger.info(f"处理频道 {i}/{len(channel_ids)}: {channel_id}")
                
                if channel_mode == 'search':
                    # 频道内搜索模式
                    videos = self._fetch_channel_search_videos(channel_id, config, published_after, published_before)
                    request_count += 2  # 估算搜索请求数
                else:
                    # 历史搬运和最新跟进模式都使用播放列表方式
                    videos = self._fetch_channel_playlist_videos(channel_id, config, published_after, published_before)
                    request_count += 3  # 频道信息 + 播放列表 + 视频详情
                
                all_videos.extend(videos)
                logger.info(f"频道 {channel_id} 获取到 {len(videos)} 个视频")
                
                # 简单的速率限制
                if request_count >= max_requests:
                    break
                    
            except Exception as e:
                logger.error(f"获取频道 {channel_id} 视频失败: {str(e)}")
                continue
        
        logger.info(f"频道视频获取完成，总计 {len(all_videos)} 个视频，使用了 {request_count} 个API请求")
        return all_videos
    
    def _fetch_channel_search_videos(self, channel_id, config, published_after, published_before=None):
        """在指定频道内搜索视频"""
        try:
            keywords = config.get('channel_keywords', '')
            if not keywords:
                logger.warning(f"频道搜索模式但未设置关键词，跳过频道: {channel_id}")
                return []
            
            # 构建搜索参数
            search_params = {
                'part': 'id,snippet',
                'type': 'video',
                'channelId': channel_id,
                'q': keywords,
                'publishedAfter': published_after,
                'maxResults': config.get('max_results', 10),
                'order': config.get('order_by', 'relevance')
            }
            
            # 添加结束日期限制
            if published_before:
                search_params['publishedBefore'] = published_before
            
            logger.info(f"在频道 {channel_id} 内搜索关键词: {keywords}")
            
            # 执行搜索
            search_response = self.youtube.search().list(**search_params).execute()
            
            video_ids = [item['id']['videoId'] for item in search_response['items']]
            
            if not video_ids:
                logger.info(f"频道 {channel_id} 搜索无结果")
                return []
            
            # 获取视频详细信息
            videos_response = self.youtube.videos().list(
                part='id,snippet,statistics,contentDetails',
                id=','.join(video_ids)
            ).execute()
            
            return videos_response['items']
            
        except Exception as e:
            logger.error(f"频道搜索失败 {channel_id}: {str(e)}")
            return []
    
    def _fetch_channel_playlist_videos(self, channel_id, config, published_after, published_before=None):
        """从频道播放列表获取视频"""
        try:
            # 获取频道的上传播放列表ID
            channel_response = self.youtube.channels().list(
                part='contentDetails',
                id=channel_id
            ).execute()
            
            if not channel_response['items']:
                logger.warning(f"找不到频道: {channel_id}")
                return []
            
            upload_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
            logger.debug(f"频道 {channel_id} 上传播放列表ID: {upload_playlist_id}")
            
            # 根据频道模式调整获取数量
            channel_mode = config.get('channel_mode', 'latest')
            if channel_mode == 'historical':
                # 历史搬运模式，获取更多视频，支持分页
                max_results_per_page = 50  # YouTube API单次最大50
                max_total_videos = 500  # 最多检查500个视频
            else:
                # 最新跟进模式
                max_results_per_page = config.get('latest_max_results', 20)
                max_total_videos = max_results_per_page
            
            # 分页获取播放列表中的视频
            all_playlist_items = []
            next_page_token = None
            videos_fetched = 0
            
            while videos_fetched < max_total_videos:
                # 计算本次请求的数量
                current_page_size = min(max_results_per_page, max_total_videos - videos_fetched)
                
                playlist_params = {
                    'part': 'snippet',
                    'playlistId': upload_playlist_id,
                    'maxResults': current_page_size
                }
                
                if next_page_token:
                    playlist_params['pageToken'] = next_page_token
                
                playlist_response = self.youtube.playlistItems().list(**playlist_params).execute()
                current_items = playlist_response['items']
                all_playlist_items.extend(current_items)
                videos_fetched += len(current_items)
                
                # 检查是否还有更多页面
                next_page_token = playlist_response.get('nextPageToken')
                if not next_page_token or len(current_items) == 0:
                    break
                
                # 在历史搬运模式下，如果我们已经找到足够的时间范围内的视频，可以提前停止
                if channel_mode == 'historical':
                    # 快速检查当前这批视频中最早的时间
                    if current_items:
                        earliest_video_time = min(item['snippet']['publishedAt'] for item in current_items)
                        # 如果最早的视频都比我们的开始时间还早，说明后面的视频都不会在范围内了
                        if earliest_video_time < published_after:
                            logger.info(f"找到了早于开始时间的视频，停止获取更多视频")
                            break
            
            logger.info(f"频道 {channel_id} 总共获取了 {len(all_playlist_items)} 个播放列表项目")
            
            # 筛选时间范围内的视频
            video_ids = []
            for item in all_playlist_items:
                video_published = item['snippet']['publishedAt']
                
                # 检查开始时间
                if video_published < published_after:
                    continue
                
                # 检查结束时间
                if published_before and video_published >= published_before:
                    continue
                
                video_ids.append(item['snippet']['resourceId']['videoId'])
            
            logger.info(f"频道 {channel_id} 在时间范围内找到 {len(video_ids)} 个视频")
            
            if not video_ids:
                return []
            
            # 获取视频详细信息
            videos_response = self.youtube.videos().list(
                part='id,snippet,statistics,contentDetails',
                id=','.join(video_ids)
            ).execute()
            
            videos = videos_response['items']
            
            # 历史搬运模式需要按时间正序排列（从最老到最新）
            channel_mode = config.get('channel_mode', 'latest')
            if channel_mode == 'historical':
                # 按发布时间正序排列（最老的在前）
                videos.sort(key=lambda x: x['snippet']['publishedAt'])
                logger.info(f"历史搬运模式：已按时间正序排列视频（从最老到最新）")
            
            return videos
                    
        except Exception as e:
            logger.error(f"频道播放列表获取失败 {channel_id}: {str(e)}")
            return []
    
    def _filter_videos(self, videos, config):
        """根据配置筛选视频"""
        filtered = []
        
        for video in videos:
            # 基本信息
            video_info = {
                'id': video['id'],
                'title': video['snippet']['title'],
                'channel_title': video['snippet']['channelTitle'],
                'channel_id': video['snippet']['channelId'],
                'published_at': video['snippet']['publishedAt'],
                'duration': video['contentDetails']['duration'],
                'view_count': int(video['statistics'].get('viewCount', 0)),
                'like_count': int(video['statistics'].get('likeCount', 0)),
                'comment_count': int(video['statistics'].get('commentCount', 0))
            }
            
            # 应用筛选条件
            if not self._meets_criteria(video_info, config):
                continue
                
            filtered.append(video_info)
            
            # 限制结果数量
            if len(filtered) >= config['max_results']:
                break
        
        return filtered
    
    def _meets_criteria(self, video_info, config):
        """检查视频是否符合筛选条件"""
        # 检查开始日期
        if config.get('start_date'):
            try:
                start_date = datetime.strptime(config['start_date'], '%Y-%m-%d')
                video_date = datetime.fromisoformat(video_info['published_at'].replace('Z', '+00:00'))
                if video_date < start_date.replace(tzinfo=video_date.tzinfo):
                    return False
            except Exception as e:
                logger.warning(f"日期比较失败: {str(e)}")
        
        # 检查观看数
        if video_info['view_count'] < config['min_view_count']:
            return False
        
        # 检查点赞数
        if video_info['like_count'] < config['min_like_count']:
            return False
        
        # 检查评论数
        if video_info['comment_count'] < config['min_comment_count']:
            return False
        
        # 检查排除关键词
        if config['exclude_keywords']:
            exclude_words = [word.strip().lower() for word in config['exclude_keywords'].split(',')]
            title_lower = video_info['title'].lower()
            for word in exclude_words:
                if word and word in title_lower:
                    return False
        
        # 检查频道ID过滤
        if config['exclude_channel_ids']:
            exclude_channels = [ch.strip() for ch in config['exclude_channel_ids'].split(',')]
            if video_info['channel_id'] in exclude_channels:
                return False
        
        # 检查指定频道（如果没有指定频道，则不限制）
        if config['channel_ids'] and config['channel_ids'].strip():
            include_channels = [ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()]
            if include_channels and video_info['channel_id'] not in include_channels:
                return False
        
        # 检查视频时长
        duration_seconds = self._parse_duration(video_info['duration'])
        if config['min_duration'] > 0 and duration_seconds < config['min_duration']:
            return False
        if config['max_duration'] > 0 and duration_seconds > config['max_duration']:
            return False
        
        return True
    
    def _parse_duration(self, duration_str):
        """解析ISO 8601时长格式为秒数"""
        import re
        
        # PT1H30M45S -> 1小时30分45秒
        pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
        match = re.match(pattern, duration_str)
        
        if not match:
            return 0
        
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        
        return hours * 3600 + minutes * 60 + seconds
    
    def _is_video_processed(self, video_id, config_id):
        """检查视频是否已经处理过"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id FROM monitor_history WHERE video_id = ? AND config_id = ?',
                (video_id, config_id)
            )
            return cursor.fetchone() is not None
    
    def _save_video_history(self, video_info, config_id, auto_add_to_tasks=False):
        """保存视频到历史记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO monitor_history (
                    config_id, video_id, video_title, channel_title,
                    view_count, like_count, comment_count, duration,
                    published_at, added_to_tasks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                config_id,
                video_info['id'],
                video_info['title'],
                video_info['channel_title'],
                video_info['view_count'],
                video_info['like_count'],
                video_info['comment_count'],
                video_info['duration'],
                video_info['published_at'],
                1 if auto_add_to_tasks else 0
            ))
            
            conn.commit()
            logger.info(f"视频已保存到历史记录: {video_info['title']}")
            
            # 如果启用自动添加到任务队列，直接添加
            if auto_add_to_tasks:
                task_id = self._add_video_to_tasks(video_info, auto_start=True)
                if task_id:
                    # 更新数据库标记为已添加
                    cursor.execute(
                        'UPDATE monitor_history SET added_to_tasks = 1 WHERE video_id = ? AND config_id = ?',
                        (video_info['id'], config_id)
                    )
    
    def _add_video_to_tasks(self, video_info, auto_start=True):
        """将视频添加到任务队列"""
        try:
            video_url = f"https://www.youtube.com/watch?v={video_info['id']}"
            task_id = add_task(video_url)
            
            if task_id:
                logger.info(f"视频已添加到任务队列: {video_info['title']}, 任务ID: {task_id}")
                
                # 移除自动启动逻辑，让全局任务处理器的队列管理机制来处理
                # 这样避免重复调度和冲突
                logger.info(f"任务已添加，将由队列管理器自动处理: {task_id}")
                
                return task_id  # 返回任务ID而不是布尔值
            else:
                logger.error("添加任务失败，未返回任务ID")
                return None
                
        except Exception as e:
            logger.error(f"添加视频到任务队列失败: {str(e)}")
            return None
    
    def add_video_to_tasks_manually(self, video_id, config_id):
        """手动将视频添加到任务队列"""
        logger.info(f"手动添加视频到任务队列: {video_id}, 配置ID: {config_id}")
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT video_id, video_title, channel_title, view_count, like_count, 
                       comment_count, duration, published_at, added_to_tasks
                FROM monitor_history 
                WHERE video_id = ? AND config_id = ?
            ''', (video_id, config_id))
            
            row = cursor.fetchone()
            if not row:
                logger.warning(f"视频不存在: {video_id}, 配置ID: {config_id}")
                return False, "视频不存在"
            
            if row[8]:  # added_to_tasks
                logger.warning(f"视频已经添加到任务队列: {row[1]}")
                return False, "视频已经添加到任务队列"
            
            # 构建视频信息
            video_info = {
                'id': row[0],
                'title': row[1],
                'channel_title': row[2],
                'view_count': row[3],
                'like_count': row[4],
                'comment_count': row[5],
                'duration': row[6],
                'published_at': row[7]
            }
            
            logger.info(f"准备添加视频: {video_info['title']} (频道: {video_info['channel_title']})")
            
            # 添加到任务队列，是否自动启动由系统配置决定
            # 尝试获取系统配置
            auto_start = False
            try:
                from flask import current_app
                if hasattr(current_app, 'config') and 'Y2A_SETTINGS' in current_app.config:
                    auto_start = current_app.config['Y2A_SETTINGS'].get('AUTO_MODE_ENABLED', False)
            except (ImportError, RuntimeError):
                # 如果无法从Flask获取，尝试从文件加载
                import os
                import json
                config_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.json')
                if os.path.exists(config_file):
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                            auto_start = config.get('AUTO_MODE_ENABLED', False)
                    except Exception as e:
                        logger.warning(f"读取配置文件失败: {str(e)}")
            
            logger.info(f"手动添加视频到任务队列")
            task_id = self._add_video_to_tasks(video_info, auto_start=False)
            if task_id:
                self._mark_video_added_to_tasks(video_id, config_id)
                logger.info(f"视频成功添加到任务队列: {video_info['title']}, 任务ID: {task_id}")
                return True, f"视频已成功添加到任务队列，任务ID: {task_id}"
            else:
                logger.error(f"添加视频到任务队列失败: {video_info['title']}")
                return False, "添加到任务队列失败"
    
    def _mark_video_added_to_tasks(self, video_id, config_id):
        """标记视频已添加到任务队列"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE monitor_history SET added_to_tasks = 1 WHERE video_id = ? AND config_id = ?',
                (video_id, config_id)
            )
            conn.commit()
    
    def _get_historical_time_range(self, config):
        """获取历史搬运模式的智能时间范围"""
        config_id = config.get('config_id') or config.get('id')
        
        # 获取当前进度
        current_offset = config.get('historical_offset', 0)
        start_date_str = config.get('start_date', '')
        end_date_str = config.get('end_date', '')
        
        if not start_date_str:
            logger.error("历史搬运模式需要设置开始日期")
            return None, None, 0
        
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            published_after = start_date.isoformat() + 'Z'
            
            # 设置结束日期
            published_before = None
            if end_date_str:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
                # 结束日期加一天，确保包含当天的视频
                end_date = end_date + timedelta(days=1)
                published_before = end_date.isoformat() + 'Z'
                logger.info(f"历史搬运范围: {start_date_str} 到 {end_date_str}，当前偏移量: {current_offset}")
            else:
                # 如果没有结束日期，搬运到当前时间
                published_before = datetime.now().isoformat() + 'Z'
                logger.info(f"历史搬运范围: {start_date_str} 到现在，当前偏移量: {current_offset}")
            
            return published_after, published_before, current_offset
            
        except Exception as e:
            logger.error(f"计算历史搬运时间范围失败: {str(e)}")
            return None, None, 0
    
    def _update_historical_progress(self, config_id, all_filtered_videos, added_count):
        """更新历史搬运进度"""
        try:
            # 获取当前配置
            config = self.get_monitor_config(config_id)
            if not config:
                return
            
            current_offset = config.get('historical_offset', 0)
            
            # 更新偏移量
            new_offset = current_offset + added_count
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE monitor_configs SET historical_offset = ? WHERE id = ?',
                    (new_offset, config_id)
                )
                conn.commit()
            
            logger.info(f"历史搬运偏移量更新为: {new_offset} (本次添加 {added_count} 个视频)")
            
            # 检查是否已经处理完所有视频
            if new_offset >= len(all_filtered_videos):
                logger.info(f"历史搬运已完成！总共处理了 {new_offset} 个视频")
                
        except Exception as e:
            logger.error(f"更新历史搬运进度失败: {str(e)}")
    
    def _update_last_run_time(self, config_id):
        """更新最后运行时间"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE monitor_configs SET last_run_time = CURRENT_TIMESTAMP WHERE id = ?',
                (config_id,)
            )
            conn.commit()
    
    def _schedule_monitor(self, config_id, interval_minutes):
        """添加监控任务到调度器"""
        job_id = f"monitor_{config_id}"
        
        try:
            config = self.get_monitor_config(config_id)
            config_name = config['name'] if config else f"ID-{config_id}"
            
            self.scheduler.add_job(
                func=self.run_monitor,
                trigger='interval',
                minutes=interval_minutes,
                id=job_id,
                args=[config_id],
                replace_existing=True
            )
            
            if not self.scheduler.running:
                self.scheduler.start()
                logger.info("调度器已启动")
                
            logger.info(f"添加监控调度任务: {config_name} ({job_id}), 间隔: {interval_minutes}分钟")
        except Exception as e:
            logger.error(f"添加调度任务失败: {str(e)}")
    
    def _update_schedule(self, config_id, config_data):
        """更新调度任务"""
        job_id = f"monitor_{config_id}"
        
        # 移除现有任务
        self._remove_schedule(config_id)
        
        # 如果是自动调度，重新添加
        if config_data.get('schedule_type') == 'auto' and config_data.get('enabled'):
            self._schedule_monitor(config_id, config_data.get('schedule_interval', 60))
    
    def _remove_schedule(self, config_id):
        """移除调度任务"""
        job_id = f"monitor_{config_id}"
        
        try:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info(f"移除监控调度任务: {job_id}")
        except Exception as e:
            logger.error(f"移除调度任务失败: {str(e)}")
    
    def get_monitor_history(self, config_id=None, limit=100):
        """获取监控历史记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if config_id:
                cursor.execute('''
                    SELECT h.*, c.name as config_name 
                    FROM monitor_history h
                    JOIN monitor_configs c ON h.config_id = c.id
                    WHERE h.config_id = ?
                    ORDER BY h.run_time DESC
                    LIMIT ?
                ''', (config_id, limit))
            else:
                cursor.execute('''
                    SELECT h.*, c.name as config_name 
                    FROM monitor_history h
                    JOIN monitor_configs c ON h.config_id = c.id
                    ORDER BY h.run_time DESC
                    LIMIT ?
                ''', (limit,))
            
            columns = [description[0] for description in cursor.description]
            history = []
            for row in cursor.fetchall():
                record = dict(zip(columns, row))
                history.append(record)
            
            return history
    
    def clear_monitor_history(self, config_id):
        """清除指定配置的监控历史记录"""
        logger.info(f"开始清除监控历史记录，配置ID: {config_id}")
        
        try:
            # 获取配置信息用于日志
            config = self.get_monitor_config(config_id)
            config_name = config['name'] if config else f"ID-{config_id}"
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 获取要删除的记录数
                cursor.execute('SELECT COUNT(*) FROM monitor_history WHERE config_id = ?', (config_id,))
                count = cursor.fetchone()[0]
                
                if count > 0:
                    # 删除历史记录
                    cursor.execute('DELETE FROM monitor_history WHERE config_id = ?', (config_id,))
                    conn.commit()
                    logger.info(f"已清除配置 {config_name} (ID: {config_id}) 的 {count} 条监控历史记录")
                    return True, f"成功清除 {count} 条历史记录"
                else:
                    logger.info(f"配置 {config_name} (ID: {config_id}) 没有历史记录需要清除")
                    return True, "没有历史记录需要清除"
                    
        except Exception as e:
            logger.error(f"清除监控历史记录失败，配置ID: {config_id}, 错误: {str(e)}")
            return False, f"清除历史记录失败: {str(e)}"
    
    def clear_all_monitor_history(self):
        """清除所有监控历史记录"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 获取要删除的记录数
                cursor.execute('SELECT COUNT(*) FROM monitor_history')
                count = cursor.fetchone()[0]
                
                if count > 0:
                    # 删除所有历史记录
                    cursor.execute('DELETE FROM monitor_history')
                    conn.commit()
                    logger.info(f"已清除所有 {count} 条监控历史记录")
                    return True, f"成功清除所有 {count} 条历史记录"
                else:
                    return True, "没有历史记录需要清除"
                    
        except Exception as e:
            logger.error(f"清除所有监控历史记录失败: {str(e)}")
            return False, f"清除历史记录失败: {str(e)}"
    
    def start_all_schedules(self):
        """启动所有自动调度的监控任务"""
        logger.info("开始启动所有自动调度的监控任务")
        
        configs = self.get_monitor_configs()
        auto_configs = [config for config in configs if config['enabled'] and config['schedule_type'] == 'auto']
        
        logger.info(f"找到 {len(auto_configs)} 个启用的自动调度配置")
        
        for config in auto_configs:
            logger.info(f"启动调度: {config['name']} (ID: {config['id']}), 间隔: {config['schedule_interval']}分钟")
            self._schedule_monitor(config['id'], config['schedule_interval'])
        
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("调度器已启动")
        
        logger.info(f"所有自动调度任务启动完成，共 {len(auto_configs)} 个任务")
    
    def stop_all_schedules(self):
        """停止所有调度任务"""
        if self.scheduler.running:
            self.scheduler.shutdown()
    
    def restore_configs_from_files_manually(self):
        """手动从配置文件恢复监控配置"""
        logger.info("手动触发配置恢复...")
        
        try:
            # 停止所有现有调度
            self.stop_all_schedules()
            
            # 重新恢复配置
            self._restore_configs_from_files()
            
            # 重新启动调度
            self._restart_restored_schedules()
            
            logger.info("配置恢复完成")
            return True, "配置恢复完成"
            
        except Exception as e:
            logger.error(f"配置恢复失败: {str(e)}")
            return False, f"配置恢复失败: {str(e)}"
    
    def reset_historical_offset(self, config_id):
        """重置历史搬运偏移量"""
        logger.info(f"重置历史搬运偏移量，配置ID: {config_id}")
        
        try:
            # 获取配置信息用于日志
            config = self.get_monitor_config(config_id)
            if not config:
                return False, "配置不存在"
            
            config_name = config['name']
            current_offset = config.get('historical_offset', 0)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE monitor_configs SET historical_offset = 0 WHERE id = ?',
                    (config_id,)
                )
                conn.commit()
            
            logger.info(f"配置 {config_name} (ID: {config_id}) 的历史搬运偏移量已从 {current_offset} 重置为 0")
            return True, f"偏移量已从 {current_offset} 重置为 0，将重新开始历史搬运"
            
        except Exception as e:
            logger.error(f"重置历史搬运偏移量失败，配置ID: {config_id}, 错误: {str(e)}")
            return False, f"重置偏移量失败: {str(e)}"

# 全局监控实例
youtube_monitor = YouTubeMonitor()
