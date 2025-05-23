#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import uuid
import time
import sqlite3
import logging
import shutil
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
import re

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError
from apscheduler.executors.pool import ThreadPoolExecutor

# 导入其他模块
# 这些导入会在函数内部使用，避免循环导入问题
# from modules.youtube_handler import download_video_data
# from modules.ai_enhancer import translate_text, generate_acfun_tags, recommend_acfun_partition
# from modules.content_moderator import AlibabaCloudModerator
# from modules.acfun_uploader import AcfunUploader

# 全局变量
DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'db')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'tasks.db')
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'downloads')

# 确保目录存在
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# 任务状态定义
TASK_STATES = {
    'PENDING': 'pending',                 # 等待处理
    'DOWNLOADING': 'downloading',         # 正在下载
    'DOWNLOADED': 'downloaded',           # 下载完成
    'TRANSLATING': 'translating',         # 正在翻译
    'TAGGING': 'tagging',                 # 正在生成标签
    'PARTITIONING': 'partitioning',       # 正在推荐分区
    'MODERATING': 'moderating',           # 正在内容审核
    'AWAITING_REVIEW': 'awaiting_manual_review',  # 等待人工审核
    'UPLOADING': 'uploading',             # 正在上传
    'COMPLETED': 'completed',             # 任务完成
    'FAILED': 'failed'                    # 任务失败
}

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
        
        # 文件处理器
        file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5, encoding='utf-8')
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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 创建tasks表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        youtube_url TEXT NOT NULL,
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
        cover_path_local TEXT,
        video_path_local TEXT,
        metadata_json_path_local TEXT,
        moderation_result TEXT,  -- JSON
        error_message TEXT,
        acfun_upload_response TEXT
    )
    ''')
    
    conn.commit()
    conn.close()
    
    logger.info("数据库初始化完成")

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 返回字典形式的结果
    return conn

def add_task(youtube_url):
    """
    添加新任务到数据库
    
    Args:
        youtube_url: YouTube视频URL
        
    Returns:
        task_id: 新创建的任务ID
    """
    task_id = str(uuid.uuid4())
    conn = get_db_connection()
    
    try:
        conn.execute(
            'INSERT INTO tasks (id, youtube_url, status) VALUES (?, ?, ?)',
            (task_id, youtube_url, TASK_STATES['PENDING'])
        )
        conn.commit()
        logger.info(f"新任务添加成功, ID: {task_id}, URL: {youtube_url}")
    except Exception as e:
        logger.error(f"添加任务失败: {str(e)}")
        task_id = None
    finally:
        conn.close()
    
    return task_id

def update_task(task_id, **kwargs):
    """
    更新任务信息
    
    Args:
        task_id: 任务ID
        **kwargs: 要更新的字段及其值
    
    Returns:
        success: 更新是否成功
    """
    if not kwargs:
        return False
    
    # 添加更新时间
    kwargs['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 构建SQL更新语句
    set_clause = ', '.join([f"{key} = ?" for key in kwargs.keys()])
    values = list(kwargs.values())
    values.append(task_id)
    
    conn = get_db_connection()
    try:
        conn.execute(
            f'UPDATE tasks SET {set_clause} WHERE id = ?',
            values
        )
        conn.commit()
        logger.info(f"任务 {task_id} 更新成功: {kwargs}")
        return True
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
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        task = cursor.fetchone()
        return dict(task) if task else None
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
    
    # 删除任务文件
    if delete_files:
        delete_task_files(task_id)
    
    # 删除任务记录
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        conn.commit()
        logger.info(f"任务 {task_id} 删除成功")
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
    if delete_files:
        # 获取所有任务ID
        tasks = get_all_tasks()
        for task in tasks:
            delete_task_files(task['id'])
    
    # 清空任务表
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM tasks')
        conn.commit()
        logger.info("所有任务已清空")
        return True
    except Exception as e:
        logger.error(f"清空任务失败: {str(e)}")
        return False
    finally:
        conn.close()

def delete_task_files(task_id):
    """
    删除任务相关文件
    
    Args:
        task_id: 任务ID
    
    Returns:
        success: 删除是否成功
    """
    # 删除下载目录
    task_dir = os.path.join(DOWNLOADS_DIR, task_id)
    if os.path.exists(task_dir):
        try:
            shutil.rmtree(task_dir)
            logger.info(f"任务 {task_id} 的下载目录已删除: {task_dir}")
        except Exception as e:
            logger.error(f"删除任务 {task_id} 的下载目录失败: {str(e)}")
            # 不直接返回False，尝试继续删除其他文件
    
    # 删除 static/covers 目录下的封面图片
    # 假设封面文件名是 task_id 加上常见的图片后缀
    # 获取项目根目录的父目录，再拼接 static/covers
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    covers_dir = os.path.join(base_dir, 'static', 'covers')
    
    possible_extensions = ['.jpg', '.jpeg', '.png']
    cover_deleted = False
    for ext in possible_extensions:
        cover_file_name = f"{task_id}{ext}"
        cover_file_path = os.path.join(covers_dir, cover_file_name)
        if os.path.exists(cover_file_path):
            try:
                os.remove(cover_file_path)
                logger.info(f"任务 {task_id} 的封面图片已删除: {cover_file_path}")
                cover_deleted = True
                break # 假设只有一个封面文件
            except Exception as e:
                logger.error(f"删除任务 {task_id} 的封面图片 {cover_file_path} 失败: {str(e)}")
    
    if not cover_deleted:
        logger.info(f"任务 {task_id} 在 static/covers 目录下未找到对应的封面图片或删除失败 (尝试的后缀: {', '.join(possible_extensions)})")

    return True

# 任务处理逻辑
class TaskProcessor:
    """任务处理器，负责任务的执行和状态管理"""
    
    def __init__(self, config=None):
        """
        初始化任务处理器
        
        Args:
            config: 配置字典，包含各种API的配置信息
        """
        self.config = config or {}
        self.scheduler = BackgroundScheduler(
            executors={
                'default': ThreadPoolExecutor(max_workers=3)
            },
            job_defaults={
                'coalesce': False,
                'max_instances': 3
            }
        )
        self.scheduler.start()
        logger.info("任务处理器初始化完成")
    
    def shutdown(self):
        """安全关闭调度器"""
        self.scheduler.shutdown()
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
            job = self.scheduler.add_job(
                self.process_task,
                args=[task_id],
                id=f"task_{task_id}"
            )
            logger.info(f"任务 {task_id} 已调度, 作业ID: {job.id}")
            return job.id
        except Exception as e:
            logger.error(f"调度任务 {task_id} 失败: {str(e)}")
            # 更新任务状态为失败
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"调度失败: {str(e)}")
            return None
    
    def process_task(self, task_id):
        """
        处理任务，包括采集信息、内容审核、下载、上传等步骤
        """
        task_logger = setup_task_logger(task_id)
        task_logger.info(f"开始处理任务 {task_id}")
        task = get_task(task_id)
        if not task:
            logger.error(f"任务 {task_id} 不存在")
            return
        try:
            # 1. 采集视频信息（只获取元数据和封面，不下载视频文件）
            self._fetch_video_info(task_id, task['youtube_url'], task_logger)
            task = get_task(task_id)
            if task['status'] == TASK_STATES['FAILED']:
                task_logger.error("采集视频信息失败，终止任务处理")
                return
            # 2. 翻译/标签/分区推荐（如有需要）
            if self.config.get('TRANSLATE_TITLE', True) or self.config.get('TRANSLATE_DESCRIPTION', True):
                self._translate_content(task_id, task_logger)
            if self.config.get('GENERATE_TAGS', True):
                self._generate_tags(task_id, task_logger)
            if self.config.get('RECOMMEND_PARTITION', True):
                self._recommend_partition(task_id, task_logger)
                task = get_task(task_id)
                if not task.get('selected_partition_id') and task.get('recommended_partition_id'):
                    update_task(task_id, selected_partition_id=task.get('recommended_partition_id'))
                    task_logger.info(f"自动使用推荐分区: {task.get('recommended_partition_id')}")
            # 3. 内容审核（如启用）
            if self.config.get('CONTENT_MODERATION_ENABLED', False):
                self._moderate_content(task_id, task_logger)
                task = get_task(task_id)
                if task['status'] == TASK_STATES['AWAITING_REVIEW']:
                    task_logger.info("内容需要人工审核，暂停任务处理")
                    return
            # 4. 审核通过后才下载视频文件
            self._download_video_file(task_id, task['youtube_url'], task_logger)
            task = get_task(task_id)
            if task['status'] == TASK_STATES['FAILED']:
                task_logger.error("下载视频文件失败，终止任务处理")
                return
            # 5. 上传
            if self.config.get('AUTO_MODE_ENABLED', False):
                self._upload_to_acfun(task_id, task_logger)
            
            # 任务处理完成后，根据是否已上传到AcFun决定状态
            task = get_task(task_id)
            if task['status'] != TASK_STATES['COMPLETED'] and task['status'] != TASK_STATES['FAILED']:
                # 如果没有开启自动上传或者上传失败，则标记为"待上传"
                if not self.config.get('AUTO_MODE_ENABLED', False) or not task.get('acfun_upload_response'):
                    update_task(task_id, status=TASK_STATES['PENDING'])
                    task_logger.info("任务处理完成，标记为待上传")
                else:
                    # 只有成功上传到AcFun的视频才会被标记为"已完成"
                    update_task(task_id, status=TASK_STATES['COMPLETED'])
                    task_logger.info("任务处理并上传完成")
        except Exception as e:
            task_logger.error(f"任务处理过程中发生错误: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=str(e))
    
    def _fetch_video_info(self, task_id, youtube_url, task_logger):
        """只采集视频元数据和封面，不下载视频文件"""
        from modules.youtube_handler import download_video_data
        task_logger.info(f"采集视频信息: {youtube_url}")
        update_task(task_id, status='fetching_info')
        
        # 获取cookies文件路径，优先使用config目录下的文件
        cookies_filename = os.path.basename(self.config.get('YOUTUBE_COOKIES_PATH', 'cookies.txt'))
        # 首先尝试在config目录中查找
        config_cookies_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', cookies_filename)
        if os.path.exists(config_cookies_path):
            cookies_path = config_cookies_path
            task_logger.info(f"使用config目录中的cookies文件: {cookies_path}")
        else:
            # 如果config目录中不存在，则尝试使用配置中指定的路径
            cookies_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), self.config.get('YOUTUBE_COOKIES_PATH', ''))
            if not os.path.exists(cookies_path):
                task_logger.warning(f"指定的YouTube Cookies文件不存在: {cookies_path}")
                cookies_path = None
                
        # 只采集信息
        success, result = download_video_data(youtube_url, task_id, cookies_path, skip_download=True)
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
        
        # 获取cookies文件路径，优先使用config目录下的文件
        cookies_filename = os.path.basename(self.config.get('YOUTUBE_COOKIES_PATH', 'cookies.txt'))
        # 首先尝试在config目录中查找
        config_cookies_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', cookies_filename)
        if os.path.exists(config_cookies_path):
            cookies_path = config_cookies_path
            task_logger.info(f"使用config目录中的cookies文件: {cookies_path}")
        else:
            # 如果config目录中不存在，则尝试使用配置中指定的路径
            cookies_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), self.config.get('YOUTUBE_COOKIES_PATH', ''))
            if not os.path.exists(cookies_path):
                task_logger.warning(f"指定的YouTube Cookies文件不存在: {cookies_path}")
                cookies_path = None
                
        # 只下载视频文件
        success, result = download_video_data(youtube_url, task_id, cookies_path, only_video=True)
        if success:
            task_logger.info("视频文件下载成功")
            
            # 获取当前任务信息
            task = get_task(task_id)
            
            update_data = {
                'status': TASK_STATES['DOWNLOADED'],
                'video_path_local': result.get('video_path', '')
            }
            
            # 如果结果中包含元数据和封面信息，保存这些信息
            # 这是因为我们修改了download_video_data函数，使其在only_video=True时也能返回之前保存的元数据和封面
            if result.get('metadata_path') and not task.get('metadata_json_path_local'):
                update_data['metadata_json_path_local'] = result.get('metadata_path')
                
            if result.get('cover_path') and not task.get('cover_path_local'):
                update_data['cover_path_local'] = result.get('cover_path')
                
            update_task(task_id, **update_data)
        else:
            task_logger.error(f"视频文件下载失败: {result}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"下载视频失败: {result}")
    
    def _translate_content(self, task_id, task_logger):
        """翻译视频标题和描述"""
        from modules.ai_enhancer import translate_text
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始翻译视频标题和描述")
        update_task(task_id, status=TASK_STATES['TRANSLATING'])
        
        # 构建OpenAI配置
        openai_config = {
            'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''),
            'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''),
            'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
        }
        
        # 翻译标题
        task_logger.info("翻译视频标题")
        if self.config.get('TRANSLATE_TITLE', True) and task.get('video_title_original'):
            title_translated = translate_text(
                task['video_title_original'], 
                target_language="zh-CN", 
                openai_config=openai_config,
                task_id=task_id
            )
            if title_translated:
                update_task(task_id, video_title_translated=title_translated)
            else:
                task_logger.warning("标题翻译失败，将使用原始标题")
        
        # 翻译描述
        task_logger.info("翻译视频描述")
        if self.config.get('TRANSLATE_DESCRIPTION', True) and task.get('description_original'):
            description_translated = translate_text(
                task['description_original'], 
                target_language="zh-CN", 
                openai_config=openai_config,
                task_id=task_id
            )
            if description_translated:
                update_task(task_id, description_translated=description_translated)
            else:
                task_logger.warning("描述翻译失败，将使用原始描述")
        
        task_logger.info("翻译完成")
        return True
    
    def _generate_tags(self, task_id, task_logger):
        """生成视频标签"""
        from modules.ai_enhancer import generate_acfun_tags
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始生成视频标签")
        update_task(task_id, status=TASK_STATES['TAGGING'])
        
        # 优先使用翻译后的标题和描述
        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        
        # 构建OpenAI配置
        openai_config = {
            'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''),
            'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''),
            'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
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
        """推荐视频分区"""
        from modules.ai_enhancer import recommend_acfun_partition
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始推荐视频分区")
        update_task(task_id, status=TASK_STATES['PARTITIONING'])
        
        # 优先使用翻译后的标题和描述
        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        
        # 读取分区ID映射
        id_mapping_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'acfunid', 'id_mapping.json')
        task_logger.info(f"尝试读取分区映射文件: {id_mapping_path}")
        
        try:
            if not os.path.exists(id_mapping_path):
                task_logger.error(f"分区映射文件不存在: {id_mapping_path}")
                id_mapping_data = []
            else:
                with open(id_mapping_path, 'r', encoding='utf-8') as f:
                    id_mapping_data = json.load(f)
                category_count = len(id_mapping_data)
                task_logger.info(f"成功读取分区映射文件，包含 {category_count} 个分类")
        except Exception as e:
            task_logger.error(f"读取分区ID映射失败: {str(e)}")
            id_mapping_data = []
        
        # 构建OpenAI配置
        openai_config = {
            'OPENAI_API_KEY': self.config.get('OPENAI_API_KEY', ''),
            'OPENAI_BASE_URL': self.config.get('OPENAI_BASE_URL', ''),
            'OPENAI_MODEL_NAME': self.config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
        }
        
        task_logger.info(f"RECOMMEND_PARTITION设置: {self.config.get('RECOMMEND_PARTITION', True)}")
        task_logger.info(f"标题长度: {len(title)}, 描述长度: {len(description)}")
        task_logger.info(f"分区数据长度: {len(id_mapping_data)}")
        
        recommended_partition_id = None
        if self.config.get('RECOMMEND_PARTITION', True) and (title or description) and id_mapping_data:
            task_logger.info("满足推荐分区的所有条件，开始调用推荐函数")
            
            # 调用推荐函数，现在它返回分区ID字符串或None
            recommended_partition_id = recommend_acfun_partition(
                title, 
                description, 
                id_mapping_data,
                openai_config=openai_config,
                task_id=task_id
            )
            
            if recommended_partition_id:
                task_logger.info(f"获取到推荐分区ID: {recommended_partition_id}")
                
                # 保存推荐分区ID
                update_task(task_id, recommended_partition_id=recommended_partition_id)
                
                # 如果还没有选定分区，也将推荐分区设为选定分区
                if not task.get('selected_partition_id'):
                    update_task(task_id, selected_partition_id=recommended_partition_id)
                    task_logger.info(f"已自动选择推荐分区: {recommended_partition_id}")
            else:
                task_logger.warning("分区推荐失败")
        else:
            conditions = []
            if not self.config.get('RECOMMEND_PARTITION', True):
                conditions.append("分区推荐功能已禁用")
            if not (title or description):
                conditions.append("缺少标题和描述")
            if not id_mapping_data:
                conditions.append("分区映射数据为空")
            
            task_logger.warning(f"分区推荐已禁用或缺少必要信息: {', '.join(conditions)}")
            
        task_logger.info(f"分区推荐完成: {recommended_partition_id}")
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
        filtered_description = re.sub(url_pattern, '', description)
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
            update_task(task_id, status=TASK_STATES['AWAITING_REVIEW'])
    
    def _upload_to_acfun(self, task_id, task_logger):
        """上传到AcFun"""
        from modules.acfun_uploader import AcfunUploader
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        task_logger.info("开始上传到AcFun")
        update_task(task_id, status=TASK_STATES['UPLOADING'])
        
        # 获取任务信息
        video_path = task.get('video_path_local', '')
        cover_path = task.get('cover_path_local', '')
        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        partition_id = task.get('selected_partition_id', '') or task.get('recommended_partition_id', '')
        
        # 解析标签
        tags = []
        try:
            tags_json = task.get('tags_generated', '[]')
            tags = json.loads(tags_json)
        except Exception as e:
            task_logger.error(f"解析标签失败: {str(e)}")
        
        # 获取元数据
        metadata_path = task.get('metadata_json_path_local', '')
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
        
        # AcFun配置
        acfun_config = {
            'acfun_username': self.config.get('ACFUN_USERNAME', ''),
            'acfun_password': self.config.get('ACFUN_PASSWORD', '')
        }
        
        # 获取封面处理模式
        cover_mode = self.config.get('COVER_PROCESSING_MODE', 'crop')
        
        # 检查必要参数
        if not all([video_path, cover_path, title, partition_id, acfun_config['acfun_username'], acfun_config['acfun_password']]):
            task_logger.error("上传参数不完整，取消上传")
            update_task(task_id, error_message="上传参数不完整")
            return
        
        # 创建上传器
        uploader = AcfunUploader(acfun_config['acfun_username'], acfun_config['acfun_password'])
        
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
            task_id=task_id,
            cover_mode=cover_mode
        )
        
        if success:
            task_logger.info(f"视频上传成功: {result}")
            update_task(
                task_id,
                status=TASK_STATES['COMPLETED'],
                acfun_upload_response=json.dumps(result, ensure_ascii=False)
            )
        else:
            task_logger.error(f"视频上传失败: {result}")
            update_task(
                task_id,
                status=TASK_STATES['FAILED'],
                error_message=f"上传失败: {result}"
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
    
    # 创建任务处理器
    processor = TaskProcessor(config)
    
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
    
    # 允许状态为"等待人工审核"、"已完成"或"等待处理"的任务进行上传
    allowed_states = [TASK_STATES['AWAITING_REVIEW'], TASK_STATES['COMPLETED'], TASK_STATES['PENDING']]
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
    
    # 创建任务处理器
    processor = TaskProcessor(config)
    
    # 创建任务日志记录器
    task_logger = setup_task_logger(task_id)
    
    try:
        # 直接执行上传步骤
        processor._upload_to_acfun(task_id, task_logger)
        return True
    except Exception as e:
        task_logger.error(f"强制上传任务 {task_id} 失败: {str(e)}")
        import traceback
        task_logger.error(traceback.format_exc())
        update_task(task_id, error_message=f"强制上传失败: {str(e)}")
        return False

# 初始化数据库
init_db() 