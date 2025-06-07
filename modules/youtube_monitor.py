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
from modules.task_manager import add_task

logger = logging.getLogger('Y2A-Auto.YouTube-Monitor')

class YouTubeMonitor:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.youtube = None
        self.scheduler = BackgroundScheduler()
        self.db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'db', 'youtube_monitor.db')
        self._init_database()
        self._init_youtube_api()
        
    def _init_database(self):
        """初始化数据库"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
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
                    min_view_count INTEGER DEFAULT 1000,
                    min_like_count INTEGER DEFAULT 0,
                    min_comment_count INTEGER DEFAULT 0,
                    keywords TEXT DEFAULT '',
                    exclude_keywords TEXT DEFAULT '',
                    channel_ids TEXT DEFAULT '',
                    exclude_channel_ids TEXT DEFAULT '',
                    min_duration INTEGER DEFAULT 0,
                    max_duration INTEGER DEFAULT 0,
                    schedule_type TEXT DEFAULT 'manual',
                    schedule_interval INTEGER DEFAULT 60,
                    order_by TEXT DEFAULT 'viewCount',
                    start_date TEXT DEFAULT '',
                    rate_limit_requests INTEGER DEFAULT 100,
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
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO monitor_configs (
                    name, enabled, region_code, category_id, time_period, max_results,
                    min_view_count, min_like_count, min_comment_count, keywords,
                    exclude_keywords, channel_ids, exclude_channel_ids,
                    min_duration, max_duration, schedule_type, schedule_interval,
                    order_by, start_date, rate_limit_requests, rate_limit_window, auto_add_to_tasks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                config_data.get('name'),
                config_data.get('enabled', True),
                config_data.get('region_code', 'US'),
                config_data.get('category_id', '0'),
                config_data.get('time_period', 7),
                config_data.get('max_results', 10),
                config_data.get('min_view_count', 1000),
                config_data.get('min_like_count', 0),
                config_data.get('min_comment_count', 0),
                config_data.get('keywords', ''),
                config_data.get('exclude_keywords', ''),
                config_data.get('channel_ids', ''),
                config_data.get('exclude_channel_ids', ''),
                config_data.get('min_duration', 0),
                config_data.get('max_duration', 0),
                config_data.get('schedule_type', 'manual'),
                config_data.get('schedule_interval', 60),
                config_data.get('order_by', 'viewCount'),
                config_data.get('start_date', ''),
                config_data.get('rate_limit_requests', 100),
                config_data.get('rate_limit_window', 60),
                config_data.get('auto_add_to_tasks', False)
            ))
            
            config_id = cursor.lastrowid
            conn.commit()
            
            # 保存配置到文件
            self._save_config_to_file(config_id, config_data)
            
            # 如果是自动调度，添加到调度器
            if config_data.get('schedule_type') == 'auto':
                self._schedule_monitor(config_id, config_data.get('schedule_interval', 60))
            
            return config_id
    
    def _save_config_to_file(self, config_id, config_data):
        """保存配置到文件"""
        try:
            config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'youtube_monitor')
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
            config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'youtube_monitor')
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
            config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'youtube_monitor')
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
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE monitor_configs SET
                    name = ?, enabled = ?, region_code = ?, category_id = ?,
                    time_period = ?, max_results = ?, min_view_count = ?,
                    min_like_count = ?, min_comment_count = ?, keywords = ?,
                    exclude_keywords = ?, channel_ids = ?, exclude_channel_ids = ?,
                    min_duration = ?, max_duration = ?, schedule_type = ?,
                    schedule_interval = ?, order_by = ?, start_date = ?,
                    rate_limit_requests = ?, rate_limit_window = ?, auto_add_to_tasks = ?,
                    updated_time = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                config_data.get('name'),
                config_data.get('enabled', True),
                config_data.get('region_code', 'US'),
                config_data.get('category_id', '0'),
                config_data.get('time_period', 7),
                config_data.get('max_results', 10),
                config_data.get('min_view_count', 1000),
                config_data.get('min_like_count', 0),
                config_data.get('min_comment_count', 0),
                config_data.get('keywords', ''),
                config_data.get('exclude_keywords', ''),
                config_data.get('channel_ids', ''),
                config_data.get('exclude_channel_ids', ''),
                config_data.get('min_duration', 0),
                config_data.get('max_duration', 0),
                config_data.get('schedule_type', 'manual'),
                config_data.get('schedule_interval', 60),
                config_data.get('order_by', 'viewCount'),
                config_data.get('start_date', ''),
                config_data.get('rate_limit_requests', 100),
                config_data.get('rate_limit_window', 60),
                config_data.get('auto_add_to_tasks', False),
                config_id
            ))
            
            conn.commit()
            
            # 保存配置到文件
            self._save_config_to_file(config_id, config_data)
            
            # 更新调度
            self._update_schedule(config_id, config_data)
    
    def delete_monitor_config(self, config_id):
        """删除监控配置"""
        # 移除调度
        self._remove_schedule(config_id)
        
        # 删除配置文件
        self._delete_config_file(config_id)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM monitor_configs WHERE id = ?', (config_id,))
            cursor.execute('DELETE FROM monitor_history WHERE config_id = ?', (config_id,))
            conn.commit()
    
    def run_monitor(self, config_id):
        """执行监控任务"""
        if not self.youtube:
            logger.error("YouTube API未初始化")
            return False, "YouTube API未初始化"
        
        config = self.get_monitor_config(config_id)
        if not config:
            logger.error(f"监控配置不存在: {config_id}")
            return False, "监控配置不存在"
        
        try:
            # 获取热门视频
            videos = self._fetch_trending_videos(config)
            
            # 筛选视频
            filtered_videos = self._filter_videos(videos, config)
            
            # 保存到历史记录
            added_count = 0
            auto_add_enabled = config.get('auto_add_to_tasks', False)
            
            for video in filtered_videos:
                # 检查是否已经处理过
                if not self._is_video_processed(video['id'], config_id):
                    # 保存到历史记录，如果启用自动添加则直接添加到任务队列
                    self._save_video_history(video, config_id, auto_add_to_tasks=auto_add_enabled)
                    
                    if auto_add_enabled:
                        added_count += 1
            
            # 更新最后运行时间
            self._update_last_run_time(config_id)
            
            logger.info(f"监控任务完成，共添加 {added_count} 个视频")
            return True, f"监控完成，添加了 {added_count} 个视频到任务队列"
            
        except Exception as e:
            logger.error(f"监控任务执行失败: {str(e)}")
            return False, f"监控失败: {str(e)}"
    
    def _fetch_trending_videos(self, config):
        """获取热门视频"""
        try:
            # 设置时间范围
            published_after = None
            if config.get('start_date'):
                # 如果设置了开始日期，使用开始日期
                start_date = datetime.strptime(config['start_date'], '%Y-%m-%d')
                published_after = start_date.isoformat() + 'Z'
            else:
                # 否则使用时间段
                published_after = (datetime.now() - timedelta(days=config['time_period'])).isoformat() + 'Z'
            
            # 如果指定了频道，优先使用频道搜索
            if config.get('channel_ids') and config['channel_ids'].strip():
                return self._fetch_channel_videos(config, published_after)
            else:
                return self._fetch_search_videos(config, published_after)
                
        except HttpError as e:
            logger.error(f"YouTube API错误: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"获取视频数据失败: {str(e)}")
            raise
    
    def _fetch_search_videos(self, config, published_after):
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
    
    def _fetch_channel_videos(self, config, published_after):
        """从指定频道获取视频"""
        all_videos = []
        channel_ids = [ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()]
        
        # 实现请求速率限制
        request_count = 0
        max_requests = config.get('rate_limit_requests', 100)
        request_window = config.get('rate_limit_window', 60)
        
        for channel_id in channel_ids:
            if request_count >= max_requests:
                logger.warning(f"达到请求限制 {max_requests}/{request_window}秒，跳过剩余频道")
                break
                
            try:
                # 获取频道的上传播放列表ID
                channel_response = self.youtube.channels().list(
                    part='contentDetails',
                    id=channel_id
                ).execute()
                request_count += 1
                
                if not channel_response['items']:
                    logger.warning(f"找不到频道: {channel_id}")
                    continue
                
                upload_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
                
                # 获取播放列表中的视频
                playlist_params = {
                    'part': 'snippet',
                    'playlistId': upload_playlist_id,
                    'maxResults': config['max_results'],
                    'order': 'date'  # 按日期排序获取最新视频
                }
                
                playlist_response = self.youtube.playlistItems().list(**playlist_params).execute()
                request_count += 1
                
                # 筛选时间范围内的视频
                video_ids = []
                for item in playlist_response['items']:
                    video_published = item['snippet']['publishedAt']
                    if video_published >= published_after:
                        video_ids.append(item['snippet']['resourceId']['videoId'])
                
                if video_ids:
                    # 获取视频详细信息
                    videos_response = self.youtube.videos().list(
                        part='id,snippet,statistics,contentDetails',
                        id=','.join(video_ids)
                    ).execute()
                    request_count += 1
                    
                    all_videos.extend(videos_response['items'])
                
                # 简单的速率限制
                if request_count >= max_requests:
                    break
                    
            except Exception as e:
                logger.error(f"获取频道 {channel_id} 视频失败: {str(e)}")
                continue
        
        return all_videos
    
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
                self._add_video_to_tasks(video_info)
    
    def _add_video_to_tasks(self, video_info):
        """将视频添加到任务队列并自动启动处理"""
        try:
            video_url = f"https://www.youtube.com/watch?v={video_info['id']}"
            task_id = add_task(video_url)
            
            if task_id:
                logger.info(f"视频已自动添加到任务队列: {video_info['title']}, 任务ID: {task_id}")
                
                # 自动启动任务处理
                try:
                    from modules.task_manager import start_task
                    
                    # 加载配置
                    import os
                    import json
                    config_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'config.json')
                    config = {}
                    if os.path.exists(config_file):
                        with open(config_file, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                    
                    # 启动任务处理
                    success = start_task(task_id, config)
                    if success:
                        logger.info(f"任务已自动启动处理: {task_id}")
                    else:
                        logger.warning(f"任务添加成功但启动处理失败: {task_id}")
                        
                except Exception as e:
                    logger.error(f"自动启动任务处理失败: {str(e)}")
                
                return True
            else:
                logger.error("添加任务失败，未返回任务ID")
                return False
                
        except Exception as e:
            logger.error(f"添加视频到任务队列失败: {str(e)}")
            return False
    
    def add_video_to_tasks_manually(self, video_id, config_id):
        """手动将视频添加到任务队列"""
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
                return False, "视频不存在"
            
            if row[8]:  # added_to_tasks
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
            
            # 添加到任务队列
            if self._add_video_to_tasks(video_info):
                self._mark_video_added_to_tasks(video_id, config_id)
                return True, "视频已成功添加到任务队列"
            else:
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
                
            logger.info(f"添加监控调度任务: {job_id}, 间隔: {interval_minutes}分钟")
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
    
    def start_all_schedules(self):
        """启动所有自动调度的监控任务"""
        configs = self.get_monitor_configs()
        
        for config in configs:
            if config['enabled'] and config['schedule_type'] == 'auto':
                self._schedule_monitor(config['id'], config['schedule_interval'])
        
        if not self.scheduler.running:
            self.scheduler.start()
    
    def stop_all_schedules(self):
        """停止所有调度任务"""
        if self.scheduler.running:
            self.scheduler.shutdown()

# 全局监控实例
youtube_monitor = YouTubeMonitor()
