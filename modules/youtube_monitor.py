#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
import socket
import ssl
from typing import Optional, Dict, List, Any, Union, Tuple
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC
import httplib2
import threading
import time
import re
from urllib.parse import quote, urlsplit
from apscheduler.schedulers.background import BackgroundScheduler
from logging.handlers import RotatingFileHandler
from modules.task_manager import add_task
from .config_manager import load_config
from .utils import get_app_subdir

class QuotaBudgetExhausted(Exception):
    """当日跨配置 search 配额预算（或该配置公平份额）已耗尽，本轮搜索跳过。"""


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

API_INIT_STATUS_DIRECT_READY = 'direct_ready'
API_INIT_STATUS_PROXY_READY = 'proxy_ready'
API_INIT_STATUS_MISSING_API_KEY = 'missing_api_key'
API_INIT_STATUS_INIT_FAILED = 'init_failed'


def get_api_init_status_message(status_code: Optional[str]) -> str:
    messages = {
        API_INIT_STATUS_DIRECT_READY: 'YouTube API 初始化成功，当前为直连模式',
        API_INIT_STATUS_PROXY_READY: 'YouTube API 初始化成功，独立代理已启用',
        API_INIT_STATUS_MISSING_API_KEY: 'YouTube API 密钥未配置，请先在设置页完成接入。',
        API_INIT_STATUS_INIT_FAILED: 'YouTube API 初始化失败，请检查 API 密钥、代理配置与网络连通性。',
    }
    if status_code is None:
        return 'YouTube API 未初始化，请检查设置。'
    return messages.get(status_code, 'YouTube API 未初始化，请检查设置。')


MONITOR_CONFIG_FIELD_DEFAULTS: Dict[str, Any] = {
    'name': None,
    'enabled': True,
    'monitor_type': 'youtube_search',
    'channel_mode': 'latest',
    'region_code': 'US',
    'category_id': '0',
    'time_period': 7,
    'max_results': 10,
    'min_view_count': 0,
    'min_like_count': 0,
    'min_comment_count': 0,
    'keywords': '',
    'exclude_keywords': '',
    'channel_ids': '',
    'channel_keywords': '',
    'exclude_channel_ids': '',
    'min_duration': 0,
    'max_duration': 0,
    'schedule_type': 'manual',
    'schedule_interval': 120,
    'order_by': 'viewCount',
    'start_date': '',
    'end_date': '',
    'latest_days': 7,
    'latest_max_results': 20,
    'rate_limit_requests': 100,
    'rate_limit_window': 60,
    'auto_add_to_tasks': False,
    'historical_progress_date': '',
    'historical_offset': 0,
    'video_types': 'video,short,live',
}

MONITOR_CONFIG_DB_FIELDS: Tuple[str, ...] = tuple(MONITOR_CONFIG_FIELD_DEFAULTS.keys())


class YouTubeMonitor:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.youtube: Optional[Any] = None
        self.youtube_http: Optional[httplib2.Http] = None
        self.scheduler = BackgroundScheduler()
        self.db_path = os.path.join(get_app_subdir('db'), 'youtube_monitor.db')
        self._last_fetch_had_errors = False
        self._api_proxy_enabled = False
        self._last_api_init_error: Optional[str] = None
        # 配额相关运行态（惰性创建，这里显式初始化避免访问时 AttributeError）
        self._last_fetch_quota_skipped = False
        self._quota_depleted_day = None
        self._quota_tables_ready = False
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
            
            # 添加可监控内容类型字段（逗号分隔: video,short,live）
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN video_types TEXT DEFAULT 'video,short,live'")
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
                    video_type TEXT,
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
            
            # 为历史表新增 video_type 字段（向后兼容）
            try:
                cursor.execute("ALTER TABLE monitor_history ADD COLUMN video_type TEXT")
            except sqlite3.OperationalError:
                pass
            
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
                    config_id = self._insert_monitor_config_record(cursor, config_data, target_id=target_id)
                else:
                    config_id = self._insert_monitor_config_record(cursor, config_data)
                
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
                    self._schedule_monitor(config['id'], self._resolve_schedule_interval_minutes(config))
                    restored_schedules += 1
            
            if restored_schedules > 0:
                logger.info(f"重新启动了 {restored_schedules} 个自动调度任务")
                
                # 启动调度器
                if not self.scheduler.running:
                    self.scheduler.start()
                    
        except Exception as e:
            logger.error(f"重新启动调度任务失败: {str(e)}")
    
    def _normalize_proxy_url(self, proxy_url: str) -> str:
        """标准化代理地址，缺失协议时默认使用 HTTP。"""
        normalized = str(proxy_url or '').strip()
        if not normalized:
            return ''
        if '://' not in normalized:
            normalized = f'http://{normalized}'
        return normalized

    def _build_proxy_url_with_auth(self, proxy_url: str, username: str, password: str) -> str:
        """根据用户名密码构造带认证信息的代理 URL。"""
        normalized = self._normalize_proxy_url(proxy_url)
        if not normalized:
            return ''

        if username and password:
            protocol, rest = normalized.split('://', 1)
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}"
            return f"{protocol}://{auth}@{rest}"
        return normalized

    def _resolve_api_proxy_url(self, runtime_config: Dict[str, Any]) -> Optional[str]:
        """从配置中解析监控 API 独立代理。"""
        if not runtime_config.get('YOUTUBE_API_PROXY_ENABLED', False):
            return None

        proxy_url = self._build_proxy_url_with_auth(
            str(runtime_config.get('YOUTUBE_API_PROXY_URL', '') or ''),
            str(runtime_config.get('YOUTUBE_API_PROXY_USERNAME', '') or '').strip(),
            str(runtime_config.get('YOUTUBE_API_PROXY_PASSWORD', '') or '').strip(),
        )
        if not proxy_url:
            return None

        parsed = urlsplit(proxy_url)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("YouTube 监控 API 代理地址无效，请填写包含主机名的 http:// 或 socks5:// 地址")

        return proxy_url

    def _get_api_init_success_status(self) -> str:
        return API_INIT_STATUS_PROXY_READY if self._api_proxy_enabled else API_INIT_STATUS_DIRECT_READY

    def _build_youtube_http(self, runtime_config: Dict[str, Any]) -> httplib2.Http:
        """构造用于 YouTube Data API 的 HTTP transport。"""
        http_timeout = socket.getdefaulttimeout()
        if http_timeout is None:
            http_timeout = DEFAULT_HTTP_TIMEOUT_SEC

        proxy_url = self._resolve_api_proxy_url(runtime_config)
        self._api_proxy_enabled = bool(proxy_url)

        if proxy_url:
            return httplib2.Http(
                timeout=http_timeout,
                proxy_info=lambda method: httplib2.proxy_info_from_url(proxy_url, method=method),
            )

        return httplib2.Http(timeout=http_timeout, proxy_info=None)

    def _init_youtube_api(self, runtime_config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """初始化 YouTube API，并显式控制是否走独立代理。"""
        if runtime_config is None:
            config = dict(load_config() or {})
        else:
            config = dict(runtime_config)
        self.api_key = str(config.get('YOUTUBE_API_KEY') or self.api_key or '').strip()
        self.youtube = None
        self.youtube_http = None
        self._api_proxy_enabled = False
        self._last_api_init_error = None

        if not self.api_key:
            logger.info("YouTube API密钥未配置，跳过监控 API 初始化")
            self._last_api_init_error = API_INIT_STATUS_MISSING_API_KEY
            return False, API_INIT_STATUS_MISSING_API_KEY

        try:
            self.youtube_http = self._build_youtube_http(config)
            self.youtube = build(
                'youtube',
                'v3',
                developerKey=self.api_key,
                http=self.youtube_http,
            )
            status_code = self._get_api_init_success_status()
            self._last_api_init_error = None
            if status_code == API_INIT_STATUS_PROXY_READY:
                logger.info('YouTube API 初始化成功，独立代理已启用')
                return True, API_INIT_STATUS_PROXY_READY
            logger.info('YouTube API 初始化成功，当前为直连模式')
            return True, API_INIT_STATUS_DIRECT_READY
        except Exception:
            self.youtube = None
            self.youtube_http = None
            self._last_api_init_error = API_INIT_STATUS_INIT_FAILED
            logger.error('YouTube API 初始化失败，请检查 API 密钥、代理配置与网络连通性。')
            return False, API_INIT_STATUS_INIT_FAILED

    def reload_api_client(self, runtime_config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """根据当前配置重建监控 API 客户端。"""
        return self._init_youtube_api(runtime_config)

    def set_api_key(self, api_key: str) -> Tuple[bool, str]:
        """兼容旧调用：仅设置 API 密钥并按当前配置重建客户端。"""
        current_config = dict(load_config() or {})
        current_config['YOUTUBE_API_KEY'] = api_key
        return self._init_youtube_api(current_config)

    def _format_run_error_message(self, error: Exception) -> str:
        """把网络层错误转换为更可操作的监控提示。"""
        if isinstance(error, HttpError):
            return f"监控失败: {str(error)}"

        error_text = str(error).lower()
        network_markers = (
            'timed out',
            'timeout',
            'connection refused',
            'network is unreachable',
            'temporary failure',
            'name or service not known',
            'nodename nor servname',
            'proxy',
            'getaddrinfo',
            'unable to find the server',
            '11001',
            '10060',
        )
        is_network_error = isinstance(error, ssl.SSLError) or (
            isinstance(error, (TimeoutError, socket.timeout, httplib2.HttpLib2Error, OSError))
            and any(marker in error_text for marker in network_markers)
        )

        if is_network_error:
            if self._api_proxy_enabled:
                return (
                    "监控失败：YouTube Data API 网络不可达或请求超时。"
                    "请检查“YouTube 监控 API”代理配置、代理容器状态与目标地址连通性。"
                )
            return (
                "监控失败：YouTube Data API 网络不可达或请求超时。"
                "当前未启用“YouTube 监控 API”代理，请在设置中单独配置该代理，"
                "确认服务器可直连 YouTube Data API，或暂时关闭监控功能。"
            )

        return f"监控失败: {str(error)}"

    def _collect_monitor_config_values(
        self,
        config_data: Dict[str, Any],
        field_default_overrides: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """按统一字段顺序生成监控配置 SQL 参数，避免多处手写字段列表。"""
        defaults = dict(MONITOR_CONFIG_FIELD_DEFAULTS)
        if field_default_overrides:
            defaults.update(field_default_overrides)
        return [
            config_data.get(field, defaults[field])
            for field in MONITOR_CONFIG_DB_FIELDS
        ]

    def _insert_monitor_config_record(
        self,
        cursor: sqlite3.Cursor,
        config_data: Dict[str, Any],
        target_id: Optional[int] = None,
        field_default_overrides: Optional[Dict[str, Any]] = None,
    ) -> int:
        """插入监控配置记录，可选保留指定 ID 用于恢复旧配置。"""
        fields = list(MONITOR_CONFIG_DB_FIELDS)
        values = self._collect_monitor_config_values(config_data, field_default_overrides)

        if target_id is not None:
            fields.insert(0, 'id')
            values.insert(0, target_id)

        columns_sql = ', '.join(fields)
        placeholders_sql = ', '.join(['?'] * len(fields))
        cursor.execute(
            f'INSERT INTO monitor_configs ({columns_sql}) VALUES ({placeholders_sql})',
            tuple(values)
        )
        if target_id is not None:
            return target_id

        lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise RuntimeError('插入监控配置记录失败：未获取到新记录 ID')
        return lastrowid

    def _update_monitor_config_record(
        self,
        cursor: sqlite3.Cursor,
        config_id: int,
        config_data: Dict[str, Any],
        field_default_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        """按统一字段顺序更新监控配置记录。"""
        assignments_sql = ', '.join(f'{field} = ?' for field in MONITOR_CONFIG_DB_FIELDS)
        values = self._collect_monitor_config_values(config_data, field_default_overrides)
        cursor.execute(
            f'UPDATE monitor_configs SET {assignments_sql}, updated_time = CURRENT_TIMESTAMP WHERE id = ?',
            tuple(values + [config_id])
        )
    
    def create_monitor_config(self, config_data):
        """创建监控配置"""
        logger.info(f"开始创建监控配置: {config_data.get('name', '未命名')}")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                config_id = self._insert_monitor_config_record(cursor, config_data)
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
            
            # 如果是自动调度，添加到调度器（尊重用户配置的间隔，配额由共享每日预算兜底）
            if config_data.get('schedule_type') == 'auto':
                interval = self._resolve_schedule_interval_minutes(config_data)
                logger.info(f"配置 {config_id} 启用自动调度，间隔: {interval}分钟")
                self._schedule_monitor(config_id, interval)
            
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

            # 防止路径遍历攻击：验证路径在config目录内
            config_file_real = os.path.realpath(config_file)
            config_dir_real = os.path.realpath(config_dir)
            if not config_file_real.startswith(config_dir_real + os.sep):
                logger.error(f"配置文件路径不在config目录内，拒绝保存: {config_id}")
                return

            # 添加配置ID到数据中
            config_data_with_id = config_data.copy()
            config_data_with_id['config_id'] = config_id
            config_data_with_id['created_time'] = datetime.now().isoformat()

            with open(config_file_real, 'w', encoding='utf-8') as f:
                json.dump(config_data_with_id, f, ensure_ascii=False, indent=2)

            logger.info(f"监控配置已保存到文件: {config_file_real}")
        except Exception as e:
            logger.error(f"保存配置文件失败: {str(e)}")

    def _load_config_from_file(self, config_id):
        """从文件加载配置"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            config_file = os.path.join(config_dir, f"monitor_config_{config_id}.json")

            # 防止路径遍历攻击：验证路径在config目录内
            config_file_real = os.path.realpath(config_file)
            config_dir_real = os.path.realpath(config_dir)
            if not config_file_real.startswith(config_dir_real + os.sep):
                logger.error(f"配置文件路径不在config目录内，拒绝加载: {config_id}")
                return None

            if os.path.exists(config_file_real):
                with open(config_file_real, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载配置文件失败: {str(e)}")
        return None

    def _delete_config_file(self, config_id):
        """删除配置文件"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            config_file = os.path.join(config_dir, f"monitor_config_{config_id}.json")

            # 防止路径遍历攻击：验证路径在config目录内
            config_file_real = os.path.realpath(config_file)
            config_dir_real = os.path.realpath(config_dir)
            if not config_file_real.startswith(config_dir_real + os.sep):
                logger.error(f"配置文件路径不在config目录内，拒绝删除: {config_id}")
                return

            if os.path.exists(config_file_real):
                os.remove(config_file_real)
                logger.info(f"配置文件已删除: {config_file_real}")
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

                update_field_defaults = {
                    'historical_offset': old_config.get('historical_offset', 0) if old_config and not should_reset_offset else 0,
                    'video_types': old_config.get('video_types', 'video,short,live') if old_config else 'video,short,live',
                }
                self._update_monitor_config_record(cursor, config_id, config_data, update_field_defaults)
                
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
    
    def run_monitor(self, config_id: int) -> Tuple[bool, str]:
        """执行监控任务"""
        logger.info(f"开始执行监控任务，配置ID: {config_id}")
        
        # 添加调试日志 - 检查 YouTube API 对象状态
        logger.debug(f"YouTube API 对象状态: {type(self.youtube)}, 值: {self.youtube}")
        if not self.youtube:
            init_status = self._last_api_init_error or API_INIT_STATUS_INIT_FAILED
            if init_status == API_INIT_STATUS_MISSING_API_KEY:
                init_message = 'YouTube API 密钥未配置，请先在设置页完成接入。'
            elif init_status == API_INIT_STATUS_INIT_FAILED:
                init_message = 'YouTube API 初始化失败，请检查 API 密钥、代理配置与网络连通性。'
            else:
                init_message = 'YouTube API 未初始化，请检查设置。'
            logger.error("YouTube API未初始化: %s", init_message)
            return False, f"监控失败：{init_message}"
        
        config = self.get_monitor_config(config_id)
        if not config:
            logger.error(f"监控配置不存在: {config_id}")
            return False, "监控配置不存在"

        # 执行入口复核配置状态：配置在 15s 立即任务触发前被禁用/删除时，
        # 不得再发 API 请求或写历史（配合 _remove_schedule 取消立即任务双保险）。
        if not config.get('enabled', True):
            logger.info(f"监控配置已禁用，跳过执行: {config.get('name', config_id)} (ID: {config_id})")
            return False, "监控配置已禁用，跳过执行"

        logger.info(f"执行监控配置: {config['name']} (ID: {config_id})")
        logger.info(f"监控类型: {config.get('monitor_type', 'youtube_search')}, "
                   f"频道模式: {config.get('channel_mode', 'latest')}")
        
        try:
            # 获取视频
            logger.info("开始获取视频数据...")
            # 每次运行前重置错误标记与预算跳过标记
            self._last_fetch_had_errors = False
            self._last_fetch_quota_skipped = False
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
                # 先去重：已处理过的直接跳过，不参与本轮统计
                if self._is_video_processed(video['id'], config_id):
                    logger.debug(f"视频已处理过，跳过: {video['title']}")
                    continue

                # 未处理（新视频）：记录到历史（去重），并按配置决定是否加入任务队列。
                should_add_to_tasks = auto_add_enabled and added_count < max_add_to_tasks

                # 始终保存到历史记录，但是否添加到任务队列由 auto_add_enabled 控制
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

                # max_results 的截断必须在「去重之后」：每轮最多处理 max_results 条
                # 新视频，剩余候选留到后续轮次继续去重（避免 max_results 过小导致长期 0 新视频）。
                if processed_count >= config['max_results']:
                    logger.info(f"已达到本轮 max_results={config['max_results']} 新视频上限，剩余候选将在后续轮次处理")
                    break
                # 若启用了自动添加且达到任务队列上限，也跳出循环
                if auto_add_enabled and added_count >= max_add_to_tasks:
                    logger.info(f"已达到本次添加上限 {max_add_to_tasks}，剩余视频将在下次运行时处理")
                    break
            
            # 更新最后运行时间。抓取错误或配额跳过都必须保留原时间游标，
            # 否则配额恢复后会永久漏掉跳过窗口内发布的视频。
            if not self._last_fetch_had_errors and not self._last_fetch_quota_skipped:
                self._update_last_run_time(config_id)
            else:
                logger.warning("本次抓取不完整（错误或配额跳过），不更新last_run_time以避免漏掉新视频")
            
            # 预算跳过是明确的独立状态（区别于正常“0 结果”），并入完成消息
            quota_note = (
                "（部分搜索因配额预算耗尽被跳过，配额将在次日太平洋时间重置）"
                if getattr(self, '_last_fetch_quota_skipped', False) else ""
            )
            logger.info(f"监控任务完成 - 配置: {config['name']}, "
                       f"处理新视频: {processed_count}, 添加到任务队列: {added_count}{quota_note}")
            
            # 更新历史搬运进度（如果是历史模式）
            if config.get('channel_mode') == 'historical':
                # 获取应用偏移量前的完整筛选结果
                original_filtered = self._filter_videos(videos, config)
                self._update_historical_progress(config_id, original_filtered, added_count)
            
            return True, f"监控完成，处理了 {processed_count} 个新视频，添加了 {added_count} 个到任务队列{quota_note}"
            
        except Exception as e:
            logger.error(f"监控任务执行失败 - 配置: {config['name']} (ID: {config_id}), 错误: {str(e)}")
            return False, self._format_run_error_message(e)
    
    def _fetch_trending_videos(self, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """获取视频"""
        try:
            # 设置时间范围
            published_after: Optional[str] = None
            published_before: Optional[str] = None
            
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
                    # 统一使用配置的时间段（time_period，默认7天）作为搜索窗口，
                    # 避免动态1天窗口漏掉稍早发布的视频；配合任务去重不会重复建任务。
                    days = max(config.get('time_period', 7), 1)
                    logger.info(f"使用时间段: 最近 {days} 天（time_period 配置）")
                    published_after = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
            
            # 如果指定了频道，优先使用频道搜索
            if config.get('channel_ids') and config['channel_ids'].strip():
                logger.info(f"使用频道监控模式，频道数量: {len([ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()])}")
                if published_after is not None:
                    return self._fetch_channel_videos(config, published_after, published_before)
                else:
                    logger.error("published_after 为 None，无法获取频道视频")
                    return []
            else:
                logger.info(f"使用YouTube搜索模式，关键词: {config.get('keywords', '无')}")
                if published_after is not None:
                    return self._fetch_search_videos(config, published_after, published_before)
                else:
                    logger.error("published_after 为 None，无法搜索视频")
                    return []
                
        except HttpError as e:
            logger.error(f"YouTube API错误: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"获取视频数据失败: {str(e)}")
            raise
    
    def _fetch_search_videos(self, config: Dict[str, Any], published_after: str, published_before: Optional[str] = None) -> List[Dict[str, Any]]:
        """通过搜索获取视频"""
        # 确保 YouTube API 对象可用
        if not self.youtube:
            logger.error("YouTube API 未初始化")
            return []
            
        # 构建搜索参数
        search_params = {
            'part': 'id,snippet',
            'type': 'video',
            'order': config.get('order_by', 'viewCount'),
            'publishedAfter': published_after,
            # 候选数量下限保护：max_results 过小时（如 1）会导致每轮候选极少，
            # 在 monitor_history 已饱和时去重后恒为 0 新视频（即使 YouTube 上有新视频）。
            # 这里对「每轮获取量」设下限 10，确保总能采样到足够候选以命中真正的新视频。
            'maxResults': max(min(config['max_results'] * 2, 50), 10),  # 获取更多结果用于筛选（含下限保护）
            'regionCode': config['region_code']
        }

        # 根据所选类型决定是否在搜索阶段过滤直播或非直播
        selected_types = str(config.get('video_types', 'video,short,live')).split(',')
        selected_types = [t.strip() for t in selected_types if t.strip()]
        only_live = set(selected_types) == {'live'}
        include_live = 'live' in set(selected_types)
        only_non_live = not include_live
        # live 精准过滤在搜索阶段；Shorts 不使用 videoDuration=short（过宽），改为详情阶段判断
        if only_live:
            search_params['eventType'] = 'live'
        elif only_non_live:
            search_params['eventType'] = 'completed'
        
        # 添加结束日期限制
        if published_before:
            search_params['publishedBefore'] = published_before
        
        # 添加分类过滤
        if config['category_id'] and config['category_id'] != '0':
            search_params['videoCategoryId'] = config['category_id']

        # 添加关键词搜索：YouTube q 参数原生支持使用“|”进行 OR 搜索。
        # 把多个关键词合并到一次 search.list，避免关键词数量线性消耗搜索配额。
        raw_kw = (config.get('keywords') or '').strip()
        keywords_list = [k for k in re.split(r'[\s,;，；\n]+', raw_kw) if k] if raw_kw else []
        if keywords_list:
            search_params['q'] = '|'.join(keywords_list)
            logger.info(f"多关键词 OR 搜索，共 {len(keywords_list)} 个: {keywords_list}")

        # 集中式当日熔断（单一控制点）：今日配额耗尽则直接跳过整轮搜索，
        # 不再发出任何 search.list（普通搜索与频道搜索共用同一持久化状态）。
        if self._quota_budget_remaining() <= 0:
            self._last_fetch_quota_skipped = True
            logger.warning("今日 search 配额已耗尽（当日熔断），跳过本轮搜索（将于次日太平洋时间重置）")
            return []

        try:
            logger.debug(f"准备执行搜索请求，参数: {search_params}")
            search_request = self.youtube.search().list(**search_params)
            search_response = self._execute_with_retry(
                search_request,
                'search.list',
                quota_consumer=lambda: self._try_consume_quota(1, config.get('id')),
                operation_type='search',
            )
        except QuotaBudgetExhausted:
            self._last_fetch_quota_skipped = True
            logger.warning(
                f"配置 {config.get('name', config.get('id', 'unknown'))} 当日 search 配额预算已耗尽，"
                "跳过本轮（将于次日太平洋时间重置）"
            )
            return []
        except Exception as e:
            logger.error(f"关键词搜索失败: q={search_params.get('q') or '无'}, 错误: {e}")
            self._last_fetch_had_errors = True
            return []

        if not search_response or 'items' not in search_response:
            logger.error(f"搜索响应异常: {search_response}")
            self._last_fetch_had_errors = True
            return []

        # 候选数量下限保护：max_results 过小时（如 1）会让候选过少，
        # 在 monitor_history 已饱和时去重后恒为 0 新视频。
        total_budget = max(min(config['max_results'] * 2, 50), 10)
        video_ids = list(dict.fromkeys(
            item['id']['videoId']
            for item in search_response['items']
            if 'id' in item and 'videoId' in item['id']
        ))[:total_budget]
        logger.info(f"搜索得到候选 {len(video_ids)} 个（预算 {total_budget}）")
        
        if not video_ids:
            return []
        
        # 获取视频详细信息（带重试）
        logger.debug(f"准备执行视频详情请求，YouTube API 对象: {type(self.youtube)}")
        videos_request = self.youtube.videos().list(
            part='id,snippet,statistics,contentDetails,liveStreamingDetails',
            id=','.join(video_ids)
        )
        logger.debug(f"视频详情请求已创建: {type(videos_request)}")
        videos_response = self._execute_with_retry(videos_request, 'videos.list')
        
        # 添加调试日志 - 检查视频响应
        logger.debug(f"视频响应类型: {type(videos_response)}, 值: {videos_response}")
        if videos_response is None:
            logger.error("视频响应为 None")
            self._last_fetch_had_errors = True
            return []
        
        if 'items' not in videos_response:
            logger.error(f"视频响应中缺少 'items' 字段，响应内容: {videos_response}")
            self._last_fetch_had_errors = True
            return []
        
        return videos_response['items']
    
    def _fetch_channel_videos(self, config: Dict[str, Any], published_after: str, published_before: Optional[str] = None) -> List[Dict[str, Any]]:
        """从指定频道获取视频"""
        all_videos = []
        channel_ids = [ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()]
        
        # 实现请求速率限制
        request_count = 0
        max_requests = config.get('rate_limit_requests', 4)
        # 使用调度间隔作为时间窗口（转换为秒）
        request_window = config.get('schedule_interval', 120) * 60
        
        # 根据频道模式调整获取策略
        channel_mode = config.get('channel_mode', 'latest')
        logger.info(f"开始处理 {len(channel_ids)} 个频道，模式: {channel_mode}，请求限制: {max_requests}/{config.get('schedule_interval', 120)}分钟")
        
        had_error = False
        for i, channel_id in enumerate(channel_ids, 1):
            if request_count >= max_requests:
                logger.warning(f"达到请求限制 {max_requests}/{config.get('schedule_interval', 120)}分钟，跳过剩余 {len(channel_ids) - i + 1} 个频道")
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
                had_error = True
                continue
        
        logger.info(f"频道视频获取完成，总计 {len(all_videos)} 个视频，使用了 {request_count} 个API请求")
        # 记录本次获取是否出现错误，供上层决定是否更新last_run_time
        self._last_fetch_had_errors = self._last_fetch_had_errors or had_error
        return all_videos
    
    def _fetch_channel_search_videos(self, channel_id: str, config: Dict[str, Any], published_after: str, published_before: Optional[str] = None) -> List[Dict[str, Any]]:
        """在指定频道内搜索视频"""
        # 确保 YouTube API 对象可用
        if not self.youtube:
            logger.error("YouTube API 未初始化")
            return []
            
        try:
            keywords = config.get('channel_keywords', '')
            
            # 构建搜索参数
            search_params = {
                'part': 'id,snippet',
                'type': 'video',
                'channelId': channel_id,
                'q': keywords or None,
                'publishedAfter': published_after,
                'maxResults': max(config.get('max_results', 10), 10),  # 候选下限保护，避免 max_results 过小导致恒为 0 新视频
                'order': config.get('order_by', 'relevance')
            }
            # 根据选择增加 eventType/videoDuration
            selected_types = str(config.get('video_types', 'video,short,live')).split(',')
            selected_types = [t.strip() for t in selected_types if t.strip()]
            only_live = set(selected_types) == {'live'}
            include_live = 'live' in set(selected_types)
            only_non_live = not include_live
            if only_live:
                search_params['eventType'] = 'live'
            elif only_non_live:
                search_params['eventType'] = 'completed'
            
            # 添加结束日期限制
            if published_before:
                search_params['publishedBefore'] = published_before
            
            logger.info(f"在频道 {channel_id} 内搜索关键词: {keywords}")

            # 集中式当日熔断：今日配额耗尽则直接跳过该频道搜索，不再发请求。
            if self._quota_budget_remaining() <= 0:
                self._last_fetch_quota_skipped = True
                logger.warning(
                    f"今日 search 配额已耗尽（当日熔断），跳过频道 {channel_id} 的搜索"
                    "（将于次日太平洋时间重置）"
                )
                return []
            # 计入跨配置共享的每日 search 预算（单一全局令牌桶）：每个实际 attempt 原子扣减，
            # 配额耗尽时由 _execute_with_retry 触发当日熔断并抛出 QuotaBudgetExhausted。
            logger.debug(f"准备执行频道搜索请求，YouTube API 对象: {type(self.youtube)}")
            # 清理 None 值，避免 API 报错
            search_params = {k: v for k, v in search_params.items() if v is not None}
            search_request = self.youtube.search().list(**search_params)
            try:
                search_response = self._execute_with_retry(
                    search_request, f'search.list (channel {channel_id})',
                    quota_consumer=lambda: self._try_consume_quota(1, config.get('id')),
                    operation_type='search',
                )
            except QuotaBudgetExhausted:
                # 今日全局配额已耗尽：停止该频道搜索（当日熔断，后续频道/轮次一并停）。
                self._last_fetch_quota_skipped = True
                logger.warning(
                    f"今日 search 配额已耗尽，跳过频道 {channel_id} 的搜索"
                    "（配额将在次日太平洋时间重置）"
                )
                return []
            
            # 添加调试日志 - 检查频道搜索响应
            logger.debug(f"频道搜索响应类型: {type(search_response)}, 值: {search_response}")
            if search_response is None:
                logger.error(f"频道 {channel_id} 搜索响应为 None")
                self._last_fetch_had_errors = True
                return []
            
            if 'items' not in search_response:
                logger.error(f"频道 {channel_id} 搜索响应中缺少 'items' 字段，响应内容: {search_response}")
                self._last_fetch_had_errors = True
                return []
            
            video_ids = [item['id']['videoId'] for item in search_response['items']]
            
            if not video_ids:
                logger.info(f"频道 {channel_id} 搜索无结果")
                return []
            
            # 获取视频详细信息（带重试）
            logger.debug(f"准备执行频道视频详情请求，YouTube API 对象: {type(self.youtube)}")
            videos_request = self.youtube.videos().list(
                part='id,snippet,statistics,contentDetails,liveStreamingDetails',
                id=','.join(video_ids)
            )
            logger.debug(f"频道视频详情请求已创建: {type(videos_request)}")
            videos_response = self._execute_with_retry(videos_request, f'videos.list (channel {channel_id})')
            
            # 添加调试日志 - 检查频道视频响应
            logger.debug(f"频道视频响应类型: {type(videos_response)}, 值: {videos_response}")
            if videos_response is None:
                logger.error(f"频道 {channel_id} 视频响应为 None")
                self._last_fetch_had_errors = True
                return []
            
            if 'items' not in videos_response:
                logger.error(f"频道 {channel_id} 视频响应中缺少 'items' 字段，响应内容: {videos_response}")
                self._last_fetch_had_errors = True
                return []
            
            return videos_response['items']
            
        except Exception as e:
            logger.error(f"频道搜索失败 {channel_id}: {str(e)}")
            raise
    
    def _fetch_channel_playlist_videos(self, channel_id, config, published_after, published_before=None):
        """从频道播放列表获取视频"""
        try:
            # 获取频道的上传播放列表ID
            if self.youtube is None:
                logger.error(f"频道 {channel_id} YouTube API 对象为 None")
                return []
            channel_request = self.youtube.channels().list(
                part='contentDetails',
                id=channel_id
            )
            channel_response = self._execute_with_retry(channel_request, f'channels.list (channel {channel_id})')
            
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
                
                if self.youtube is None:
                    logger.error(f"频道 {channel_id} YouTube API 对象为 None")
                    break
                playlist_request = self.youtube.playlistItems().list(**playlist_params)
                playlist_response = self._execute_with_retry(playlist_request, f'playlistItems.list (channel {channel_id})')
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
            if self.youtube is None:
                logger.error(f"频道 {channel_id} YouTube API 对象为 None")
                return []
            videos_request = self.youtube.videos().list(
                part='id,snippet,statistics,contentDetails,liveStreamingDetails',
                id=','.join(video_ids)
            )
            videos_response = self._execute_with_retry(videos_request, f'videos.list (channel {channel_id})')
            
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
            # 向上抛出让上层决定是否继续以及是否更新last_run_time
            raise

    def _http_error_quota_reason(self, e: HttpError) -> Optional[str]:
        """从 Google API HttpError 中提取结构化错误 reason（类型安全）。

        429 既可能是「当日配额耗尽」（quotaExceeded / dailyLimitExceeded），也可能是
        「短时窗口速率限制」（rateLimitExceeded）。二者处理完全不同：前者要触发当日
        熔断、后者只需重试。只有结构化 reason 能可靠区分，故优先解析错误中的 reason。

        googleapiclient 在不同响应形态下，`error_details` 可能是：
          - list[dict]（含 error.errors[]，最常见，字段缺 reason 时退文本）；
          - str（仅 error.message 时，googleapiclient 把 error_details 设为字符串，
            此时 `for d in details` 会遍历字符、用 `.get` 会 AttributeError —— 必须类型判定）；
          - dict（整段 error 对象）。
        为避免对字符串形态崩溃，先归一化成一个「错误对象列表」再逐个取 reason；
        仍取不到时退化到 `e.content` 原始 JSON 与错误文本匹配。
        """
        raw = getattr(e, 'error_details', None)
        err_objs: list = []
        if isinstance(raw, list):
            err_objs = raw
        elif isinstance(raw, dict):
            err_objs = raw.get('errors') or [raw]
        elif isinstance(raw, str):
            # 某些版本/场景会把 error_details 反序列化为字符串形式的 JSON
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    err_objs = parsed
                elif isinstance(parsed, dict):
                    err_objs = parsed.get('errors') or [parsed]
            except (ValueError, TypeError):
                err_objs = []
        for d in err_objs:
            if isinstance(d, dict):
                r = d.get('reason')
                if r:
                    return r
        # error_details 无 reason 或形态异常时，尝试从原始响应体 e.content 解析
        content = getattr(e, 'content', None)
        if isinstance(content, (bytes, bytearray)):
            try:
                parsed = json.loads(content.decode('utf-8', 'replace'))
                if isinstance(parsed, dict):
                    error_obj = parsed.get('error')
                    errors = error_obj.get('errors') if isinstance(error_obj, dict) else []
                    for d in (errors or []):
                        if isinstance(d, dict) and d.get('reason'):
                            return d['reason']
            except (ValueError, TypeError):
                pass
        # 最后退化到错误文本抓 reason（容忍 JSON 引号包裹与非引号纯文本）
        m = re.search(r'\breason["\']?\s*:\s*["\']?([A-Za-z_]+)', str(e))
        if m:
            return m.group(1)
        return None

    def _execute_with_retry(self, request: Any, description: str, max_attempts: int = 3,
                            backoff_seconds: float = 1.0, quota_consumer: Optional[Any] = None,
                            operation_type: str = 'other') -> Any:
        """对YouTube API请求执行带重试的调用，用于处理临时性网络/SSL问题。

        quota_consumer: 可选可调用对象，在**每次实际 attempt 前**调用一次；
            返回 False 表示当日配额预算 / 该配置公平份额已耗尽，立即以
            QuotaBudgetExhausted 中止（不再发起请求、不再重试）。
            重试的每次 attempt 都是一次真实 API 请求，因此每次 attempt 都扣减配额。

        operation_type: 'search' 表示 search.list 类操作（消耗每日 search 配额，且
            需要在「当日配额耗尽」reason 时触发集中式熔断）；'other' 表示 videos.list /
            channels.list / playlistItems.list 等（使用不同配额维度，即便触发配额错误
            也**不**熔断 search 预算，仅向上抛出交由调用方处理）。

        配额熔断的精确触发条件（仅对 search 操作）：
            - HTTP 429 且 reason ∈ {quotaExceeded, dailyLimitExceeded} → 当日配额真的耗尽
              → 持久化熔断 + 抛 QuotaBudgetExhausted（停止当前及后续所有 search）。
            - reason == rateLimitExceeded（短时速率限制）→ 可重试，**不**熔断
              （否则一次短时限流就会误杀当天所有 search）。
            - 其它 429 / 5xx → 按原逻辑退避重试。
        """
        attempt = 0
        last_exception: Optional[Exception] = None
        while attempt < max_attempts:
            if quota_consumer is not None and not quota_consumer():
                # 本地预算预测不足：当日不应再发请求
                raise QuotaBudgetExhausted(description)
            try:
                return request.execute()
            except HttpError as e:
                resp = getattr(e, 'resp', None)
                status = getattr(resp, 'status', None) if resp is not None else None
                reason = self._http_error_quota_reason(e)
                daily_quota_error = reason in ('quotaExceeded', 'dailyLimitExceeded')
                if daily_quota_error:
                    if operation_type == 'search':
                        # 明确的当日配额耗尽：触发集中式当日熔断（持久化 + 内存标志），
                        # 上层据此停止当前及后续所有 search 请求，不再重试、不再烧配额。
                        logger.warning(
                            f"调用 {description} 触发当日配额耗尽({reason})，"
                            f"标记今日 search 配额耗尽并当日熔断: {e}"
                        )
                        self._mark_quota_depleted_today()
                        raise QuotaBudgetExhausted(description)
                    # 非 search 操作（videos.list 等）的配额错误：不熔断 search 预算，
                    # 直接抛出交由上层处理（其配额维度与 search 无关）。
                    logger.warning(
                        f"调用 {description} 触发配额错误({reason})，非 search 操作不熔断 search 预算: {e}"
                    )
                    raise
                elif status == 429 or reason == 'rateLimitExceeded':
                    # 短时速率限制：可重试，不触发当日熔断（避免误杀全天 search）
                    last_exception = e
                elif status and 500 <= status < 600:
                    last_exception = e
                else:
                    raise
            except ssl.SSLError as e:
                # 记录并重试，同时尝试重建客户端
                last_exception = e
                logger.warning(f"SSL错误，准备重试并重建API客户端: {str(e)}")
                self._init_youtube_api()
            except OSError as e:
                # 网络层错误，重试
                last_exception = e
            attempt += 1
            sleep_time = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(f"调用 {description} 失败（第 {attempt} 次），{type(last_exception).__name__}: {last_exception}，{sleep_time:.1f}s 后重试...")
            time.sleep(sleep_time)
        # 达到最大重试次数仍失败
        if last_exception is not None:
            raise last_exception
        else:
            raise Exception("未知错误：重试次数耗尽但未捕获到具体异常")
    
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
            # 基于 API 字段的内容类型判定：live 优先，其次 shorts，再否则 video
            try:
                video_info['video_type'] = self._detect_video_type(video)
            except Exception:
                video_info['video_type'] = 'video'
            
            # 应用筛选条件
            if not self._meets_criteria(video_info, config):
                continue
                
            filtered.append(video_info)
        
        # 注意：此处**不再**按 max_results 截断。截断必须在「历史去重之后」进行
        # （见 run_monitor）：否则 max_results 过小时（如 1）会直接把候选截断在
        # 去重之前，仅保留第一条候选，而该候选若已在历史中则整轮 0 新视频，
        # 且其余候选永远没有机会参与去重（见 Issue #125）。
        return filtered

    def _detect_video_type(self, video: Dict[str, Any]) -> str:
        """根据 API 字段判定视频类型: live / short / video
        - live: 有 liveStreamingDetails 或 snippet.liveBroadcastContent in {live, upcoming}
        - short: 结合 API 信号判定 Shorts（不单纯依赖时长）：
            1) 标题/描述/标签包含 #shorts 或 shorts（大小写不敏感）
            2) 竖屏缩略图比例（通过 snippet.thumbnails 宽高比判断）且时长 <= 61 秒（作为辅证）
        备注：YouTube Data API 无官方 Shorts 标记，只能多信号近似。
        """
        # 直播判定
        live_flag = str(video.get('snippet', {}).get('liveBroadcastContent', '')).lower()
        has_live_details = bool(video.get('liveStreamingDetails'))
        if has_live_details or live_flag in ('live', 'upcoming'):
            return 'live'

        # Shorts 判定
        if self._is_shorts(video):
            return 'short'

        return 'video'

    def _is_shorts(self, video: Dict[str, Any]) -> bool:
        """综合 API 线索判断是否为 Shorts。
        优先依据 #shorts 标签/文本；其次以竖屏+<=61s 作为辅证，避免仅用时长误判。
        """
        snippet = video.get('snippet', {})
        title = str(snippet.get('title', '')).lower()
        description = str(snippet.get('description', '')).lower()
        tags = [str(t).lower() for t in snippet.get('tags', [])] if isinstance(snippet.get('tags'), list) else []

        # 1) 文本或标签中包含 shorts 相关标识
        shorts_keywords = ['#shorts', 'shorts']
        if any(kw in title or kw in description for kw in shorts_keywords):
            return True
        if any('short' == t or 'shorts' == t or '#shorts' == t for t in tags):
            return True

        # 2) 竖屏比例 + 短时长（<= 61s）作为辅证
        duration_seconds = self._parse_duration(video.get('contentDetails', {}).get('duration', '') or 'PT0S')
        # 放宽判定：若时长 <= 61s 则视为 Shorts（兼容缩略图缺少宽高信息的情况）
        if duration_seconds <= 61:
            return True

        return False

    def _is_vertical_from_thumbnails(self, snippet: Dict[str, Any]) -> bool:
        """根据缩略图宽高判断是否竖屏。取可用缩略图中最接近原比例的一个进行判断。"""
        thumbs = snippet.get('thumbnails', {}) or {}
        # 选取一个具有明确宽高的缩略图条目
        order = ['maxres', 'standard', 'high', 'medium', 'default']
        for key in order:
            t = thumbs.get(key)
            if not t:
                continue
            w = t.get('width')
            h = t.get('height')
            if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                # 竖屏：高度/宽度比 >= 1.2 视为竖屏
                return (h / w) >= 1.2
        return False
    
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
        
        # 类型过滤
        allowed_types = str(config.get('video_types', 'video,short,live')).split(',') if config.get('video_types') is not None else ['video','short','live']
        allowed_types = [t.strip() for t in allowed_types if t.strip()]
        # 兼容旧记录：若未检测出类型，按普通视频处理
        vtype = video_info.get('video_type', 'video')
        if allowed_types and vtype not in allowed_types:
            return False

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
                    config_id, video_id, video_type, video_title, channel_title,
                    view_count, like_count, comment_count, duration,
                    published_at, added_to_tasks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                config_id,
                video_info['id'],
                video_info.get('video_type', 'video'),
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
    
    # YouTube Data API 自 2026-06 起为 search.list 使用独立配额桶：默认 100 次/天，
    # 每次调用在该桶中计 1。多个监控配置共享同一项目的配额，因此用一个跨配置
    # 「每日 search 次数」令牌桶统一兜底，避免高频调度 + 多配置触发 quotaExceeded。
    # 官方配额按太平洋时间（America/Los_Angeles）0 点重置，见
    # https://developers.google.com/youtube/v3/determine_quota_cost
    _MONITOR_DAILY_SEARCH_LIMIT = 100
    # 保留 5% 余量，兼容同一项目中不经过本进程的少量搜索调用。
    _MONITOR_DAILY_SEARCH_BUDGET = int(_MONITOR_DAILY_SEARCH_LIMIT * 0.95)  # 95 次/天
    # 所有实际运行的手动配置共用一个配额槽。SQLite 自增配置 ID 为正数，
    # 因此使用 -1 不会与真实配置冲突。这既限制手动运行抢占自动任务额度，
    # 又避免从未运行的手动配置永久占用份额。
    _MANUAL_QUOTA_POOL_ID = -1
    _QUOTA_TZ_NAME = 'America/Los_Angeles'

    def _resolve_schedule_interval_minutes(self, config):
        """返回该配置的调度间隔（分钟），尊重用户在设置页填写的 schedule_interval。

        之前无条件强制 1440（每日一次）属于行为回归：UI 仍允许 1–43200 并显示旧间隔，
        但系统忽略了它，导致用户设的间隔不生效。这里恢复「尊重用户配置」的语义。

        配额安全不再靠强制每日调度来保证，而是统一交由跨配置共享的每日 search 令牌桶
        （见 _try_consume_quota）兜底：无论调度多频繁，每天实际发出的 search 请求
        总量被限制在 _MONITOR_DAILY_SEARCH_BUDGET 以内，且立即检查（start_all_schedules
        的 15s 触发）也计入同一预算，不会绕过。

        为安全与调度资源考虑，间隔下限 1 分钟、上限 43200 分钟（30 天），与设置页
        声明（1–43200，最长 30 天）一致：用户配置每周/每月等长周期调度被真正尊重，
        不再被静默截断成每日一次。
        """
        raw = config.get('schedule_interval')
        try:
            iv = int(raw)
        except (TypeError, ValueError):
            iv = 120
        iv = max(1, min(iv, 43200))
        return iv

    def _quota_day_str(self):
        """返回 YouTube 配额日（America/Los_Angeles 本地日期，与官方重置对齐）。

        UTC 0 点比太平洋时间早约 7/8 小时，若按 UTC 日重置会提前放出第二天的额度，
        导致边界多发一轮搜索；改用洛杉矶本地日期后与官方配额日一致。
        """
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(self._QUOTA_TZ_NAME)).strftime('%Y-%m-%d')
        except Exception:
            # 无 IANA tzdata 的环境：保守回退到固定 UTC-8（太平洋标准时间，不含 DST）。
            # PT 为 UTC-7（夏令时）或 UTC-8（冬令时）。固定 UTC-8 的边界与冬令时一致、
            # 比夏令时晚 1 小时——因此配额日边界**绝不会早于**真实 PT 日，最坏只是比
            # 真实 PT 日最多晚 1 小时重置（延迟而非提前，安全侧，不会提前放出第二批额度）。
            # 生产环境仍应在 requirements 中安装 tzdata 使 ZoneInfo 始终可用。
            return (datetime.utcnow() - timedelta(hours=8)).strftime('%Y-%m-%d')

    def _quota_reset_tables(self):
        """惰性创建配额相关表（幂等）。"""
        if getattr(self, '_quota_tables_ready', False):
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'CREATE TABLE IF NOT EXISTS monitor_quota_budget ('
                '  quota_date TEXT PRIMARY KEY,'
                '  used INTEGER NOT NULL'
                ')'
            )
            conn.execute(
                'CREATE TABLE IF NOT EXISTS monitor_quota_usage ('
                '  config_id INTEGER NOT NULL,'
                '  quota_date TEXT NOT NULL,'
                '  used INTEGER NOT NULL,'
                '  PRIMARY KEY (config_id, quota_date)'
                ')'
            )
            conn.commit()
        self._quota_tables_ready = True

    def _quota_budget_remaining(self):
        """返回今日跨配置共享的 search 预算剩余次数（SQLite 持久化，按 PT 配额日重置）。

        若当日已触发熔断（持久化或内存 fail-closed 标志），直接返回 0，确保即便
        持久化写入失败，本进程也不会继续发请求（绝不提前释放额度）。
        """
        # 内存熔断标志（持久化失败时的 fail-closed 兜底）：与当日配额日绑定，
        # 跨日自动失效（新一天 _quota_day_str 变化后标志不匹配，重新读 DB）。
        if getattr(self, '_quota_depleted_day', None) == self._quota_day_str():
            return 0
        self._quota_reset_tables()
        day = self._quota_day_str()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                'SELECT used FROM monitor_quota_budget WHERE quota_date = ?', (day,)
            ).fetchone()
        used = row[0] if row else 0
        return max(0, self._MONITOR_DAILY_SEARCH_BUDGET - used)

    def _mark_quota_depleted_today(self):
        """标记今日配额已耗尽（持久化 + 内存 fail-closed 标志）。

        一旦 YouTube 返回当日配额耗尽 reason（quotaExceeded / dailyLimitExceeded），
        当天不应再发任何 search.list——把全局预算置满，使后续一切 _try_consume_quota
        直接失败，实现「当日熔断」。

        持久化失败时**绝不静默吞掉**：先设置内存标志（本进程立即 fail-closed 停发），
        再对 DB 写入重试 3 次；全部失败则记 CRITICAL 日志。这样即使进程/磁盘异常，
        当前实例也不会在熔断状态下继续烧配额，下一轮/另一进程读到持久化状态后也同样停。
        """
        self._quota_reset_tables()
        day = self._quota_day_str()
        # 先置内存标志：即便随后 DB 写入失败，本进程也停止发请求（fail-closed）
        self._quota_depleted_day = day
        self._last_fetch_quota_skipped = True
        last_err = None
        for attempt_i in range(3):
            try:
                with sqlite3.connect(self.db_path, timeout=30) as conn:
                    conn.execute(
                        'INSERT INTO monitor_quota_budget (quota_date, used) '
                        'VALUES (?, ?) ON CONFLICT(quota_date) DO UPDATE SET used = ?',
                        (day, self._MONITOR_DAILY_SEARCH_BUDGET, self._MONITOR_DAILY_SEARCH_BUDGET),
                    )
                return
            except Exception as ex:
                last_err = ex
                time.sleep(0.05 * (attempt_i + 1))
        logger.critical(
            f"当日 search 配额熔断持久化失败（本进程已 fail-closed 停止发请求）：{last_err}"
        )

    def _is_search_consumer(self, c: Dict[str, Any]) -> bool:
        """判断一个监控配置是否「实际消费 search.list」配额。

        与 `_fetch_trending_videos` 的分流逻辑保持一致（权威谓词，单一来源）：
        - 未启用 → 不消费；
        - 有 `channel_ids` 且 `channel_mode == 'search'` → 走 `_fetch_channel_search_videos`（search.list）；
        - 有 `channel_ids` 但 `channel_mode` 非 search（latest/historical）→ 走 playlist 模式，不消费 search；
        - 无 `channel_ids`（普通关键词/全局检索）→ 走 `_fetch_search_videos`（search.list）。

        因此「真正消费 search」的配置 = 启用 且 （无 channel_ids 或 channel_mode=='search'）。
        注意：manual 配置同样会经由 `_fetch_trending_videos` 消费 search，故**不排除 manual**——
        否则手动运行可先吃满全局桶、饿死自动配置（reviewer 指出的真实抢占场景）。
        """
        if not c.get('enabled'):
            return False
        channel_ids = (c.get('channel_ids') or '')
        if isinstance(channel_ids, str) and channel_ids.strip():
            return c.get('channel_mode') == 'search'
        return True

    def _manual_quota_pool_active(self) -> bool:
        """返回今日是否已有手动 search 运行实际消费配额。"""
        self._quota_reset_tables()
        day = self._quota_day_str()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                'SELECT used FROM monitor_quota_usage WHERE config_id = ? AND quota_date = ?',
                (self._MANUAL_QUOTA_POOL_ID, day),
            ).fetchone()
        return bool(row and row[0] > 0)

    def _quota_usage_identity(self, config_id):
        """返回份额记账 ID，以及该调用是否属于手动配置。"""
        try:
            config = self.get_monitor_config(config_id)
        except Exception:
            config = None
        is_manual = bool(config and config.get('schedule_type') == 'manual')
        return (self._MANUAL_QUOTA_POOL_ID if is_manual else config_id), is_manual

    def _per_config_share(self, include_manual: bool = False) -> int:
        """返回单个自动消费者（或手动共享槽）当天的公平份额。"""
        count = max(1, self._monitor_search_config_count(include_manual=include_manual) or 1)
        return max(1, self._MONITOR_DAILY_SEARCH_BUDGET // count)

    def _monitor_search_config_count(self, include_manual: bool = False):
        """返回今日需分配 search.list 份额的有效消费槽数。

        这是公平份额分母。谓词来自 `_is_search_consumer`（与分流逻辑同源），
        因此只计入真正会调用 search.list 的自动配置；playlist/latest/
        historical 频道配置不计入。手动配置不逐个预留：今日首次实际运行后，
        所有手动搜索共用一个消费槽，从而不会让大量休眠的手动配置占用额度。

        `include_manual=True` 用于手动请求的首次预扣：它在用量行尚未写入时就把
        手动共享槽纳入分母，防止首次调用按全局 95 次的份额计算。
        """
        try:
            configs = self.get_monitor_configs()
        except Exception:
            return 0
        automatic_count = sum(
            1 for c in configs
            if c.get('schedule_type') == 'auto' and self._is_search_consumer(c)
        )
        manual_active = include_manual or self._manual_quota_pool_active()
        return automatic_count + int(manual_active)

    def _try_consume_quota(self, n=1, config_id=None):
        """原子扣减 n 次 search 配额。

        双层限流，既防超额又防单配置饿死其它配置（根因修复）：
        1. **全局硬上限**：所有 search 消费者（auto / manual）共享一天 95 次，
           单语句条件 UPDATE（used + n <= budget）跨线程 / 跨进程原子，绝不超过日配额。
        2. **公平份额**：在全局桶之上，每个自动 search 配置拥有一个消费槽，
           当天实际运行的手动 search 共用一个槽。份额为 `budget / 消费槽数`
           （最少 1 次），高频或手动运行无法先吃光全局桶；从未运行的手动
           配置也不会空占份额。
           - 配置份额耗尽时回滚本次全局扣减，把额度让给其它配置（公平，不浪费）。
           - 全局桶耗尽时直接返回 False（不回滚——额度已被其它配置合法占用）。
           - 未传 config_id（历史兼容调用）仅受全局桶约束。

        返回 True 表示扣减成功；False 表示今日配额（全局或本配置份额）已耗尽。
        当日熔断（_mark_quota_depleted_today 把 used 置满）后此处自然返回 False。
        """
        self._quota_reset_tables()
        day = self._quota_day_str()
        # 纵深防御：若当日已 fail-closed 熔断（持久化失败时的内存兜底），直接拒绝，
        # 不依赖 DB 读取（DB 写入失败时读到的仍是旧余额，会误放行）。
        if getattr(self, '_quota_depleted_day', None) == day:
            return False
        share = None
        usage_id = config_id
        if config_id is not None:
            usage_id, is_manual = self._quota_usage_identity(config_id)
            share = self._per_config_share(include_manual=is_manual)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                'INSERT INTO monitor_quota_budget (quota_date, used) VALUES (?, 0) '
                'ON CONFLICT(quota_date) DO NOTHING', (day,)
            )
            # 全局硬上限
            cur = conn.execute(
                'UPDATE monitor_quota_budget SET used = used + ? '
                'WHERE quota_date = ? AND used + ? <= ?',
                (n, day, n, self._MONITOR_DAILY_SEARCH_BUDGET),
            )
            if cur.rowcount != 1:
                return False  # 今日全局预算已耗尽
            if share is not None:
                conn.execute(
                    'INSERT INTO monitor_quota_usage (config_id, quota_date, used) '
                    'VALUES (?, ?, 0) ON CONFLICT(config_id, quota_date) DO NOTHING',
                    (usage_id, day),
                )
                cur2 = conn.execute(
                    'UPDATE monitor_quota_usage SET used = used + ? '
                    'WHERE config_id = ? AND quota_date = ? AND used + ? <= ?',
                    (n, usage_id, day, n, share),
                )
                if cur2.rowcount != 1:
                    # 本配置当日公平份额耗尽：回滚本次全局扣减，让出额度给其它配置
                    conn.execute(
                        'UPDATE monitor_quota_budget SET used = MAX(0, used - ?) '
                        'WHERE quota_date = ?', (n, day)
                    )
                    conn.commit()
                    return False
            conn.commit()
        return True

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
        
        # 如果是自动调度，重新添加（尊重用户配置的间隔，配额由共享每日预算兜底）
        if config_data.get('schedule_type') == 'auto' and config_data.get('enabled'):
            self._schedule_monitor(config_id, self._resolve_schedule_interval_minutes(config_data))
    
    def _remove_schedule(self, config_id):
        """移除调度任务（含启动后立即检查任务）"""
        job_id = f"monitor_{config_id}"
        immediate_id = f"monitor_immediate_{config_id}"

        for jid in (job_id, immediate_id):
            try:
                if self.scheduler.get_job(jid):
                    self.scheduler.remove_job(jid)
                    logger.info(f"移除监控调度任务: {jid}")
            except Exception as e:
                logger.error(f"移除调度任务失败: {jid}, 错误: {str(e)}")
    
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
    
    def start_all_schedules(self, immediate=True):
        """启动所有自动调度的监控任务。

        immediate=True 时，对「此前不存在对应定时任务」的配置额外安排一次 15s 后的立即检查；
        对已有定时任务（例如设置保存触发的重启）不再重复安排，避免每次保存都触发一次立即运行
        从而绕过每日配额预算。该立即检查同样走 _fetch_search_videos，会计入跨配置每日预算。
        """
        logger.info("开始启动所有自动调度的监控任务")
        
        configs = self.get_monitor_configs()
        auto_configs = [config for config in configs if config['enabled'] and config['schedule_type'] == 'auto']
        
        logger.info(f"找到 {len(auto_configs)} 个启用的自动调度配置")
        
        for config in auto_configs:
            # 尊重用户配置的调度间隔（而非强制每日），配额安全由共享每日预算兜底
            job_existed = self.scheduler.get_job(f"monitor_{config['id']}") is not None
            run_interval = self._resolve_schedule_interval_minutes(config)
            logger.info(f"启动调度: {config['name']} (ID: {config['id']}), 间隔: {run_interval}分钟")
            self._schedule_monitor(config['id'], run_interval)
            # 仅当定时任务此前不存在时才安排「启动后立即检查」，避免每次保存都绕过每日预算
            if immediate and not job_existed:
                try:
                    self.scheduler.add_job(
                        func=self.run_monitor,
                        trigger='date',
                        run_date=datetime.now() + timedelta(seconds=15),
                        args=[config['id']],
                        id=f"monitor_immediate_{config['id']}",
                        replace_existing=True,
                        misfire_grace_time=600,
                    )
                    logger.info(f"已安排启动后立即检查: {config['name']} (ID: {config['id']})")
                except Exception as e:
                    logger.error(f"安排启动后立即检查失败: {str(e)}")
        
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("调度器已启动")
        
        logger.info(f"所有自动调度任务启动完成，共 {len(auto_configs)} 个任务")
    
    def stop_all_schedules(self):
        """停止所有调度任务"""
        try:
            jobs = self.scheduler.get_jobs()
            for job in jobs:
                self.scheduler.remove_job(job.id)
            logger.info(f"已停止所有监控调度任务，共移除 {len(jobs)} 个任务")
        except Exception as e:
            logger.error(f"停止所有调度任务失败: {str(e)}")
    
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
