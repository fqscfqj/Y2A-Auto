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
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError
from apscheduler.executors.pool import ThreadPoolExecutor
import queue
from .utils import get_app_subdir

# 导入其他模块
# 这些导入会在函数内部使用，避免循环导入问题
# from modules.youtube_handler import download_video_data
# from modules.ai_enhancer import translate_text, generate_acfun_tags, recommend_acfun_partition
# from modules.content_moderator import AlibabaCloudModerator
# from modules.acfun_uploader import AcfunUploader

# 全局变量
DB_DIR = get_app_subdir('db')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'tasks.db')
LOGS_DIR = get_app_subdir('logs')
DOWNLOADS_DIR = get_app_subdir('downloads')

# 确保目录存在
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# 任务状态定义
TASK_STATES = {
    'PENDING': 'pending',                 # 等待处理
    'DOWNLOADING': 'downloading',         # 正在下载
    'DOWNLOADED': 'downloaded',           # 下载完成
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
        subtitle_path_original TEXT,  -- 原始字幕文件路径
        subtitle_path_translated TEXT,  -- 翻译后字幕文件路径
        subtitle_language_detected TEXT,  -- 检测到的字幕语言
        metadata_json_path_local TEXT,
        moderation_result TEXT,  -- JSON
        error_message TEXT,
        upload_progress TEXT,  -- 上传进度
        acfun_upload_response TEXT
    )
    ''')
    
    conn.commit()
    
    # 检查并添加新字段（用于数据库升级）
    try:
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'upload_progress' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN upload_progress TEXT")
            logger.info("数据库升级：添加upload_progress字段")
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
        if not silent:  # 只有非静默模式才记录到主日志
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
    
    # 封面图片现在保存在downloads目录中，无需单独删除

    return True

# 全局上传队列锁
upload_queue_lock = threading.Lock()
upload_semaphore = None

def init_upload_semaphore(max_concurrent_uploads=1):
    """初始化上传信号量"""
    global upload_semaphore
    upload_semaphore = threading.Semaphore(max_concurrent_uploads)

def reset_stuck_tasks():
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
            WHERE status IN (?, ?, ?, ?, ?, ?, ?) 
            AND datetime(updated_at) < datetime('now', '-30 minutes')
        ''', ('processing', 'downloading', 'uploading', 'fetching_info', 'translating', 'translating_subtitle', 'encoding_video'))
        
        stuck_tasks = cursor.fetchall()
        
        if stuck_tasks:
            logger.warning(f"发现 {len(stuck_tasks)} 个可能卡住的任务，正在重置...")
            
            for task in stuck_tasks:
                task_id = task[0]
                old_status = task[1]
                updated_at = task[2]
                
                # 重置为失败状态
                conn.execute('''
                    UPDATE tasks 
                    SET status = ?, error_message = ?, updated_at = ?
                    WHERE id = ?
                ''', (TASK_STATES['FAILED'], 
                      f"任务超时重置 (原状态: {old_status})",
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      task_id))
                
                logger.info(f"重置任务 {task_id[:8]}... 从 {old_status} 到 failed")
            
            conn.commit()
            return len(stuck_tasks)
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
            # Netscape格式
            lines = [line for line in content.split('\n') if line.strip() and not line.startswith('#')]
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
            logger.warning(f"{service_name} Cookies文件格式不明")
            return False, "Cookies文件格式不明"
        
        return True, "Cookies文件格式正确"
        
    except Exception as e:
        logger.error(f"验证{service_name} Cookies文件时出错: {str(e)}")
        return False, f"验证cookies文件出错: {str(e)}"

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
        
        # 获取并发配置
        max_concurrent_tasks = self.config.get('MAX_CONCURRENT_TASKS', 3)
        max_concurrent_uploads = self.config.get('MAX_CONCURRENT_UPLOADS', 1)
        
        # 初始化上传信号量
        init_upload_semaphore(max_concurrent_uploads)
        
        self.scheduler = BackgroundScheduler(
            executors={
                'default': ThreadPoolExecutor(max_workers=max_concurrent_tasks)
            },
            job_defaults={
                'coalesce': False,
                'max_instances': 1  # 每个任务只能有一个实例在运行，避免重复执行同一任务
            }
        )
        self.scheduler.start()
        logger.info(f"任务处理器初始化完成 - 最大并发任务: {max_concurrent_tasks}, 最大并发上传: {max_concurrent_uploads}")
    
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
            # 检查任务是否已在调度器中
            existing_job_id = f"task_{task_id}"
            existing_job = None
            try:
                existing_job = self.scheduler.get_job(existing_job_id)
            except:
                pass
            
            if existing_job:
                logger.warning(f"任务 {task_id} 已在调度器中，跳过重复调度")
                return existing_job_id
            
            # 检查任务当前状态
            task = get_task(task_id)
            if not task:
                logger.error(f"任务 {task_id} 不存在")
                return None
                
            if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['FAILED']]:
                logger.warning(f"任务 {task_id} 状态为 {task['status']}，不能调度")
                return None
            
            # 直接在新线程中执行，避免调度器冲突
            import threading
            
            def run_task_wrapper():
                try:
                    logger.info(f"任务 {task_id} 开始在线程中执行")
                    self.process_task(task_id)
                except Exception as e:
                    logger.error(f"任务 {task_id} 执行出错: {str(e)}")
                    import traceback
                    logger.error(traceback.format_exc())
                    update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"执行出错: {str(e)}")
            
            # 启动后台线程
            thread = threading.Thread(
                target=run_task_wrapper, 
                name=f"task_{task_id}",
                daemon=True
            )
            thread.start()
            
            logger.info(f"任务 {task_id} 已在后台线程启动")
            return f"thread_{task_id}"
            
        except Exception as e:
            logger.error(f"调度任务 {task_id} 失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
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
            
            # 5. 字幕翻译（如启用）
            if self.config.get('SUBTITLE_TRANSLATION_ENABLED', False):
                self._translate_subtitle(task_id, task_logger)
                task = get_task(task_id)
                if task['status'] == TASK_STATES['FAILED']:
                    task_logger.error("字幕翻译失败，继续执行后续步骤")
            
            # 6. 上传
            if self.config.get('AUTO_MODE_ENABLED', False):
                self._upload_to_acfun(task_id, task_logger)
            
            # 任务处理完成后，根据是否已上传到AcFun决定状态
            task = get_task(task_id)
            if task['status'] != TASK_STATES['COMPLETED'] and task['status'] != TASK_STATES['FAILED']:
                # 如果没有开启自动上传或者上传失败，则标记为"准备上传"
                if not self.config.get('AUTO_MODE_ENABLED', False) or not task.get('acfun_upload_response'):
                    update_task(task_id, status=TASK_STATES['READY_FOR_UPLOAD'])
                    task_logger.info("任务处理完成，标记为准备上传")
                else:
                    # 只有成功上传到AcFun的视频才会被标记为"已完成"
                    update_task(task_id, status=TASK_STATES['COMPLETED'])
                    task_logger.info("任务处理并上传完成")
        except Exception as e:
            task_logger.error(f"任务处理过程中发生错误: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=str(e))
        finally:
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
            # 获取所有pending任务
            pending_tasks = get_tasks_by_status(TASK_STATES['PENDING'])
            
            if not pending_tasks:
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
                TASK_STATES['TRANSLATING_SUBTITLE'],
                TASK_STATES['ENCODING_VIDEO'],
                TASK_STATES['UPLOADING']
            ]
            
            running_tasks = []
            for state in processing_states:
                running_tasks.extend(get_tasks_by_status(state))
            
            # 如果有任务正在运行且并发限制为1，则不启动新任务
            max_concurrent = self.config.get('MAX_CONCURRENT_TASKS', 3)
            if len(running_tasks) >= max_concurrent:
                logger.info(f"当前有 {len(running_tasks)} 个任务正在运行，达到并发限制 {max_concurrent}，暂不启动新任务")
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
            success, result = download_video_data(youtube_url, task_id, cookies_path, skip_download=True)
        except Exception as e:
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
            
            task_logger.info(f"下载进度: {detailed_msg}")
            
            # 更新任务的上传进度字段用于显示（只显示百分比）
            update_task(task_id, upload_progress=progress_msg, silent=True)
        
        # 只下载视频文件
        try:
            success, result = download_video_data(youtube_url, task_id, cookies_path, only_video=True, progress_callback=progress_callback)
        except Exception as e:
            task_logger.error(f"下载视频文件时发生异常: {str(e)}")
            update_task(task_id, status=TASK_STATES['FAILED'], error_message=f"下载异常: {str(e)}")
            return
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
    
    def _translate_subtitle(self, task_id, task_logger):
        """翻译字幕文件"""
        from modules.subtitle_translator import create_translator_from_config
        import glob
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return False
        
        task_logger.info("开始字幕翻译")
        update_task(task_id, status=TASK_STATES['TRANSLATING_SUBTITLE'])
        
        try:
            # 查找字幕文件
            task_dir = os.path.join(DOWNLOADS_DIR, task_id)
            subtitle_files = []
            
            # 查找SRT和VTT字幕文件
            for ext in ['*.srt', '*.vtt']:
                subtitle_files.extend(glob.glob(os.path.join(task_dir, ext)))
            
            if not subtitle_files:
                task_logger.info("未找到字幕文件，跳过字幕翻译")
                return True
            
            # 选择第一个字幕文件进行翻译
            subtitle_file = subtitle_files[0]
            task_logger.info(f"找到字幕文件: {os.path.basename(subtitle_file)}")
            
            # 创建翻译器，传递task_id参数
            translator = create_translator_from_config(self.config, task_id)
            if not translator:
                task_logger.error("无法创建字幕翻译器，请检查API配置")
                return False
            
            # 检测字幕语言（简单实现）
            subtitle_lang = self._detect_subtitle_language(subtitle_file)
            task_logger.info(f"检测到字幕语言: {subtitle_lang}")
            
            # 生成翻译后的文件路径
            subtitle_ext = os.path.splitext(subtitle_file)[1]
            translated_subtitle_path = os.path.join(
                task_dir, 
                f"translated_{task_id}{subtitle_ext}"
            )
            
            # 定义进度回调函数
            def progress_callback(progress, current, total):
                task_logger.info(f"字幕翻译进度: {progress:.1f}% ({current}/{total})")
                # 更新任务进度显示到网页
                update_task(task_id, upload_progress=f"{progress:.1f}%", silent=True)
            
            # 执行翻译
            success = translator.translate_file(
                subtitle_file, 
                translated_subtitle_path,
                progress_callback=progress_callback
            )
            
            if success:
                task_logger.info("字幕翻译完成")
                # 清除翻译进度显示
                update_task(task_id, upload_progress=None, silent=True)
                
                # 如果配置了将字幕嵌入视频
                if self.config.get('SUBTITLE_EMBED_IN_VIDEO', True):
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
                            subtitle_language_detected=subtitle_lang
                        )
                    else:
                        task_logger.warning("字幕嵌入失败，保留原视频和字幕文件")
                        update_task(
                            task_id,
                            subtitle_path_original=subtitle_file,
                            subtitle_path_translated=translated_subtitle_path,
                            subtitle_language_detected=subtitle_lang
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
                task_logger.error("字幕翻译失败")
                # 清除进度显示
                update_task(task_id, upload_progress=None, silent=True)
                return False
                
        except Exception as e:
            task_logger.error(f"字幕翻译过程中发生错误: {str(e)}")
            import traceback
            task_logger.error(traceback.format_exc())
            # 清除进度显示
            update_task(task_id, upload_progress=None, silent=True)
            return False
    
    def _detect_subtitle_language(self, subtitle_path):
        """简单的字幕语言检测"""
        try:
            with open(subtitle_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 简单的语言检测逻辑
            if any(ord(char) >= 0x4e00 and ord(char) <= 0x9fff for char in content):
                return "zh"  # 中文
            elif any(ord(char) >= 0x3040 and ord(char) <= 0x309f for char in content):
                return "ja"  # 日语平假名
            elif any(ord(char) >= 0x30a0 and ord(char) <= 0x30ff for char in content):
                return "ja"  # 日语片假名
            elif any(ord(char) >= 0xac00 and ord(char) <= 0xd7af for char in content):
                return "ko"  # 韩语
            else:
                return "en"  # 默认英语
                
        except Exception:
            return "auto"
    
    def _convert_vtt_to_srt(self, vtt_path, task_logger):
        """将VTT字幕文件转换为SRT格式（FFmpeg对SRT支持更好）"""
        try:
            import re
            import os
            
            # 生成SRT文件路径
            srt_path = vtt_path.replace('.vtt', '.srt')
            
            with open(vtt_path, 'r', encoding='utf-8') as vtt_file:
                vtt_content = vtt_file.read()
            
            # 删除VTT头部
            vtt_content = re.sub(r'^WEBVTT\n\n?', '', vtt_content)
            
            # 删除NOTE行
            vtt_content = re.sub(r'NOTE.*\n', '', vtt_content)
            
            # 删除样式和位置信息
            vtt_content = re.sub(r'<[^>]*>', '', vtt_content)
            vtt_content = re.sub(r'{[^}]*}', '', vtt_content)
            
            # 处理时间戳格式：VTT使用 "." 分隔毫秒，SRT使用 ","
            vtt_content = re.sub(r'(\d{2}:\d{2}:\d{2})\.(\d{3})', r'\1,\2', vtt_content)
            
            # 分割成字幕块
            subtitle_blocks = re.split(r'\n\s*\n', vtt_content.strip())
            
            srt_content = []
            subtitle_index = 1
            
            for block in subtitle_blocks:
                if not block.strip():
                    continue
                    
                lines = block.strip().split('\n')
                if len(lines) >= 2:
                    # 第一行应该是时间戳
                    time_line = lines[0]
                    if '-->' in time_line:
                        # 添加序号
                        srt_content.append(str(subtitle_index))
                        # 添加时间戳（已转换格式）
                        srt_content.append(time_line)
                        # 添加字幕文本
                        srt_content.extend(lines[1:])
                        srt_content.append('')  # 空行分隔
                        subtitle_index += 1
            
            # 写入SRT文件
            with open(srt_path, 'w', encoding='utf-8') as srt_file:
                srt_file.write('\n'.join(srt_content))
            
            task_logger.info(f"VTT转换为SRT成功: {srt_path}")
            return srt_path
            
        except Exception as e:
            task_logger.error(f"VTT转SRT转换失败: {str(e)}")
            return None

    def _convert_srt_to_ass(self, srt_path, ass_path, task_logger):
        """将SRT字幕文件转换为ASS格式（FFmpeg对ASS支持最好）"""
        try:
            import re
            import os
            
            with open(srt_path, 'r', encoding='utf-8') as srt_file:
                srt_content = srt_file.read()
            
            # ASS文件头部
            ass_header = """[Script Info]
Title: Subtitle
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00ffffff,&H000000ff,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
            
            # 解析SRT字幕
            subtitle_blocks = re.split(r'\n\s*\n', srt_content.strip())
            ass_lines = []
            
            for block in subtitle_blocks:
                if not block.strip():
                    continue
                    
                lines = block.strip().split('\n')
                if len(lines) >= 3:  # 序号、时间戳、文本
                    time_line = lines[1]
                    text_lines = lines[2:]
                    
                    # 解析时间戳
                    time_match = re.match(r'(\d{2}:\d{2}:\d{2}),(\d{3}) --> (\d{2}:\d{2}:\d{2}),(\d{3})', time_line)
                    if time_match:
                        start_time = f"{time_match.group(1)}.{time_match.group(2)[:2]}"  # ASS使用两位毫秒
                        end_time = f"{time_match.group(3)}.{time_match.group(4)[:2]}"
                        
                        # 合并文本行
                        text = '\\N'.join(text_lines)  # ASS使用\N作为换行符
                        
                        # 创建ASS事件行
                        ass_line = f"Dialogue: 0,{start_time},{end_time},Default,,0,0,0,,{text}"
                        ass_lines.append(ass_line)
            
            # 写入ASS文件
            with open(ass_path, 'w', encoding='utf-8') as ass_file:
                ass_file.write(ass_header)
                ass_file.write('\n'.join(ass_lines))
            
            task_logger.info(f"SRT转换为ASS成功: {ass_path}")
            return True
            
        except Exception as e:
            task_logger.error(f"SRT转ASS转换失败: {str(e)}")
            return False

    def _embed_subtitle_in_video(self, task_id, video_path, subtitle_path, task_logger):
        """使用FFmpeg将字幕嵌入视频（修复版本 - 添加超时机制）"""
        # 保存当前状态，稍后恢复
        task_before_encoding = get_task(task_id)
        previous_status = task_before_encoding['status'] if task_before_encoding else TASK_STATES['TRANSLATING_SUBTITLE']
        
        try:
            import subprocess
            import os
            import tempfile
            import shutil
            import re
            import time
            import threading
            import queue
            
            # 如果是VTT格式，先转换为SRT
            if subtitle_path.lower().endswith('.vtt'):
                task_logger.info("检测到VTT格式字幕，转换为SRT格式以提高兼容性")
                srt_path = self._convert_vtt_to_srt(subtitle_path, task_logger)
                if srt_path and os.path.exists(srt_path):
                    subtitle_path = srt_path
                    task_logger.info(f"使用转换后的SRT文件: {subtitle_path}")
                else:
                    task_logger.warning("VTT转SRT失败，尝试直接使用VTT文件")
            
            # 生成嵌入字幕后的视频文件路径
            video_dir = os.path.dirname(video_path)
            video_name = os.path.splitext(os.path.basename(video_path))[0]
            embedded_video_path = os.path.join(video_dir, f"{video_name}_with_subtitle.mp4")
            
            task_logger.info("开始将字幕嵌入视频...")
            task_logger.info(f"视频路径: {video_path}")
            task_logger.info(f"字幕路径: {subtitle_path}")
            
            # 设置任务状态为转码视频中
            update_task(task_id, status=TASK_STATES['ENCODING_VIDEO'])
            
            # 获取视频时长用于计算进度
            video_duration = self._get_video_duration(video_path, task_logger)
            
            # 使用简化路径方式（已测试成功）
            temp_dir = tempfile.mkdtemp()
            simple_video = os.path.join(temp_dir, "input.mp4")
            simple_subtitle = os.path.join(temp_dir, "sub.srt")
            simple_output = os.path.join(temp_dir, "output.mp4")
            
            try:
                # 复制文件到临时目录
                shutil.copy2(video_path, simple_video)
                shutil.copy2(subtitle_path, simple_subtitle)
                
                cmd = [
                    'ffmpeg', '-y',
                    '-i', 'input.mp4',
                    '-vf', 'subtitles=sub.srt',  # 简化语法（已验证有效）
                    '-c:v', 'libx264',
                    '-crf', '23.5',
                    '-c:a', 'copy',
                    '-preset', 'medium',
                    '-progress', 'pipe:1',  # 输出进度信息到stdout
                    'output.mp4'
                ]
                
                task_logger.info(f"FFmpeg命令: {' '.join(cmd)}")
                task_logger.info(f"临时目录: {temp_dir}")
                
                # 设置超时时间（根据视频时长计算，最少30分钟，最多3小时）
                if video_duration:
                    # 估算处理时间：实际时长的2-5倍，取决于视频长度
                    estimated_time = video_duration * 3 if video_duration < 1800 else video_duration * 2
                    timeout = max(1800, min(estimated_time, 10800))  # 最少30分钟，最多3小时
                else:
                    timeout = 3600  # 默认1小时
                
                task_logger.info(f"设置处理超时时间: {timeout//60} 分钟")
                
                # 执行FFmpeg命令并实时获取进度
                process = subprocess.Popen(
                    cmd, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE, 
                    text=True, 
                    cwd=temp_dir,  # 在临时目录执行
                    encoding='utf-8',
                    errors='replace'  # 遇到无法解码的字符时用?替换
                )
                
                # 创建线程来读取输出，避免管道阻塞
                output_queue = queue.Queue()
                error_queue = queue.Queue()
                
                def read_output():
                    try:
                        for line in process.stdout:
                            output_queue.put(('stdout', line.strip()))
                    except:
                        pass
                    finally:
                        output_queue.put(('stdout', None))
                
                def read_error():
                    try:
                        for line in process.stderr:
                            error_queue.put(('stderr', line.strip()))
                    except:
                        pass
                    finally:
                        error_queue.put(('stderr', None))
                
                # 启动读取线程
                output_thread = threading.Thread(target=read_output, daemon=True)
                error_thread = threading.Thread(target=read_error, daemon=True)
                output_thread.start()
                error_thread.start()
                
                # 实时解析进度
                last_time = 0
                start_time = time.time()
                last_progress_time = start_time
                error_messages = []
                
                while True:
                    # 检查超时
                    current_time = time.time()
                    if current_time - start_time > timeout:
                        task_logger.error(f"FFmpeg处理超时（{timeout//60}分钟），强制终止")
                        process.terminate()
                        time.sleep(5)
                        if process.poll() is None:
                            process.kill()
                        break
                    
                    # 检查进程状态
                    if process.poll() is not None:
                        break
                    
                    # 读取输出
                    try:
                        msg_type, line = output_queue.get(timeout=1)
                        if line is None:
                            break
                        
                        if line.startswith('out_time_us='):
                            try:
                                # 解析当前处理时间（微秒）
                                time_us = int(line.split('=')[1])
                                current_time = time_us / 1000000.0  # 转换为秒
                                
                                if video_duration and current_time > last_time:
                                    progress = min((current_time / video_duration) * 100, 100)
                                    progress_msg = f"转码进度: {progress:.1f}%"
                                    task_logger.info(progress_msg)
                                    
                                    # 更新任务进度显示
                                    update_task(task_id, upload_progress=f"{progress:.1f}%", silent=True)
                                    last_time = current_time
                                    last_progress_time = time.time()
                            except (ValueError, IndexError):
                                continue
                    except queue.Empty:
                        # 检查是否长时间没有进度更新（可能卡死了）
                        if time.time() - last_progress_time > 300:  # 5分钟没有进度更新
                            task_logger.warning("长时间没有进度更新，可能处理卡死")
                        continue
                    
                    # 读取错误信息
                    try:
                        msg_type, error_line = error_queue.get_nowait()
                        if error_line:
                            error_messages.append(error_line)
                            if len(error_messages) > 50:  # 限制错误信息数量
                                error_messages.pop(0)
                    except queue.Empty:
                        pass
                
                # 等待进程完成
                try:
                    process.wait(timeout=30)  # 最多等待30秒
                except subprocess.TimeoutExpired:
                    task_logger.error("进程未能在30秒内正常结束，强制终止")
                    process.kill()
                
                if process.returncode == 0 and os.path.exists(simple_output):
                    # 成功！复制输出文件回原位置
                    shutil.copy2(simple_output, embedded_video_path)
                    task_logger.info("字幕嵌入完成（使用简化路径方式）")
                    task_logger.info(f"嵌入字幕的视频已保存: {embedded_video_path}")
                    
                    # 清除进度显示并恢复之前的状态
                    update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                    return embedded_video_path
                else:
                    # 收集错误信息
                    error_output = '\n'.join(error_messages[-10:]) if error_messages else "无详细错误信息"
                    task_logger.error(f"字幕嵌入失败 (返回码: {process.returncode})")
                    task_logger.error(f"错误信息: {error_output}")
                    update_task(task_id, upload_progress=None, status=previous_status, silent=True)
                    return None
            
            finally:
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
            import subprocess
            
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json', 
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
        except Exception as e:
            task_logger.warning(f"获取视频时长失败: {str(e)}")
            return None

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
        from .utils import get_app_subdir
        id_mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
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
        """上传到AcFun - 带并发控制"""
        from modules.acfun_uploader import AcfunUploader
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        # 使用信号量控制并发上传
        global upload_semaphore
        if upload_semaphore is None:
            task_logger.warning("上传信号量未初始化，使用默认值")
            init_upload_semaphore(1)
        
        task_logger.info("等待获取上传锁...")
        with upload_semaphore:
            task_logger.info("获得上传锁，开始上传到AcFun")
            self._do_upload_to_acfun(task_id, task_logger)
            task_logger.info("释放上传锁")
    
    def _do_upload_to_acfun(self, task_id, task_logger):
        """实际执行上传到AcFun的逻辑"""
        from modules.acfun_uploader import AcfunUploader
        
        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return
        
        update_task(task_id, status=TASK_STATES['UPLOADING'])
        
        # 获取任务信息
        video_path = task.get('video_path_local', '')
        cover_path = task.get('cover_path_local', '')
        title = task.get('video_title_translated', '') or task.get('video_title_original', '')
        description = task.get('description_translated', '') or task.get('description_original', '')
        partition_id = task.get('selected_partition_id', '') or task.get('recommended_partition_id', '')
        
        # 如果没有视频文件，先下载视频
        if not video_path or not os.path.exists(video_path):
            task_logger.info("检测到视频文件缺失，开始下载视频文件...")
            youtube_url = task.get('youtube_url', '')
            if not youtube_url:
                task_logger.error("无法获取YouTube URL，无法下载视频")
                update_task(task_id, error_message="无法获取YouTube URL，无法下载视频")
                return
            
            # 下载视频文件
            self._download_video_file(task_id, youtube_url, task_logger)
            
            # 重新获取任务信息
            task = get_task(task_id)
            video_path = task.get('video_path_local', '')
        
        # 重新设置状态为上传中（字幕翻译已在主流程中完成）
        update_task(task_id, status=TASK_STATES['UPLOADING'])
        
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
        acfun_username = self.config.get('ACFUN_USERNAME', '')
        acfun_password = self.config.get('ACFUN_PASSWORD', '')
        acfun_cookies_path = self.config.get('ACFUN_COOKIES_PATH', 'cookies/ac_cookies.txt')
        
        # 获取封面处理模式
        cover_mode = self.config.get('COVER_PROCESSING_MODE', 'crop')
        
        # 检查必要参数
        missing_params = []
        if not video_path or not os.path.exists(video_path):
            missing_params.append("video_path (视频文件)")
        if not cover_path or not os.path.exists(cover_path):
            missing_params.append("cover_path (封面文件)")
        if not title:
            missing_params.append("title (视频标题)")
        if not partition_id:
            missing_params.append("partition_id (分区ID)")
        
        if missing_params:
            error_msg = f"上传参数不完整，缺少: {', '.join(missing_params)}"
            task_logger.error(error_msg)
            update_task(task_id, error_message=error_msg)
            return
        
        # 检查登录凭据 - Cookie文件或用户名密码至少要有一个
        cookie_file_exists = os.path.exists(acfun_cookies_path)
        has_credentials = acfun_username and acfun_password
        
        # 验证cookies文件（如果存在）
        cookies_valid = False
        if cookie_file_exists:
            is_valid, error_msg = validate_cookies(acfun_cookies_path, "AcFun")
            if is_valid:
                cookies_valid = True
                task_logger.info("AcFun Cookies文件验证通过")
            else:
                task_logger.warning(f"AcFun Cookies文件验证失败: {error_msg}")
                if not has_credentials:
                    task_logger.error("AcFun Cookies无效且没有提供用户名密码")
                    update_task(task_id, error_message=f"AcFun Cookies无效且没有提供用户名密码: {error_msg}")
                    return
        
        if not cookies_valid and not has_credentials:
            task_logger.error("AcFun登录信息不完整，需要有效的Cookie文件或用户名密码")
            update_task(task_id, error_message="AcFun登录信息不完整，需要有效的Cookie文件或用户名密码")
            return
        
        # 创建上传器
        uploader = AcfunUploader(
            acfun_username=acfun_username,
            acfun_password=acfun_password,
            cookie_file=acfun_cookies_path
        )
        
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
        processor._upload_to_acfun(task_id, task_logger)
        return True
    except Exception as e:
        task_logger.error(f"强制上传任务 {task_id} 失败: {str(e)}")
        import traceback
        task_logger.error(traceback.format_exc())
        update_task(task_id, error_message=f"强制上传失败: {str(e)}")
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
    elif config and config != _global_task_processor.config:
        # 如果配置发生变化，重新创建处理器
        logger.info("配置已更新，重新创建全局任务处理器实例")
        _global_task_processor.shutdown()
        _global_task_processor = TaskProcessor(config)
    
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