#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import uuid
import shutil
import logging
import subprocess
from logging.handlers import RotatingFileHandler

def setup_task_logger(task_id):
    """
    为特定任务设置日志记录器
    
    Args:
        task_id: 任务ID
        
    Returns:
        logger: 配置好的日志记录器
    """
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f'task_{task_id}.log')
    logger = logging.getLogger(f'youtube_handler_{task_id}')
    
    if not logger.handlers:  # 避免重复添加处理器
        logger.setLevel(logging.INFO)
        
        # 文件处理器
        file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        
        # 确保消息不会传播到根日志记录器
        logger.propagate = False
    
    return logger

def download_video_data(youtube_url, task_id=None, cookies_file_path=None, skip_download=False, only_video=False):
    """
    下载YouTube视频数据
    
    Args:
        youtube_url (str): YouTube视频URL
        task_id (str, optional): 任务ID，如果未提供则自动生成
        cookies_file_path (str, optional): cookies.txt文件路径
        skip_download (bool): 只采集元数据和封面，不下载视频本体
        only_video (bool): 只下载视频本体，不采集元数据和封面
    
    Returns:
        tuple: (成功标志, 结果数据或错误信息)
    """
    # 如果没有提供task_id，生成一个
    if not task_id:
        task_id = str(uuid.uuid4())
    
    # 设置任务日志记录器
    logger = setup_task_logger(task_id)
    logger.info(f"开始下载视频: {youtube_url}, 任务ID: {task_id}")
    
    # 创建任务目录
    task_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'downloads', task_id)
    if os.path.exists(task_dir):
        if only_video:
            # 当只下载视频文件时不清空目录，保留之前的元数据和封面
            logger.info(f"任务目录已存在，保留元数据和封面: {task_dir}")
        else:
            logger.info(f"任务目录已存在，正在清空: {task_dir}")
            shutil.rmtree(task_dir)
    
    # 确保目录存在
    os.makedirs(task_dir, exist_ok=True)
    logger.info(f"创建任务目录: {task_dir}")
    
    # 构建基本命令
    video_output = os.path.join(task_dir, 'video.%(ext)s')
    metadata_output = os.path.join(task_dir, 'metadata.json')
    thumbnail_output = os.path.join(task_dir, 'cover.jpg')
    
    try:
        # 获取yt-dlp路径
        yt_dlp_path = 'yt-dlp'
        
        # 检查虚拟环境中的yt-dlp
        venv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.venv', 'Scripts', 'yt-dlp.exe')
        if os.path.exists(venv_path):
            yt_dlp_path = venv_path
            logger.info(f"使用虚拟环境中的yt-dlp: {yt_dlp_path}")
        
        # 检查python目录下的yt-dlp
        python_dir = os.path.dirname(os.path.dirname(subprocess.run(['where', 'python'], 
                                                   capture_output=True, 
                                                   text=True, 
                                                   shell=True).stdout.strip()))
        python_scripts = os.path.join(python_dir, 'Scripts', 'yt-dlp.exe')
        if os.path.exists(python_scripts):
            yt_dlp_path = python_scripts
            logger.info(f"使用Python Scripts目录中的yt-dlp: {yt_dlp_path}")
        
        # 准备yt-dlp命令
        cmd = [
            yt_dlp_path,
            youtube_url,
            '--output', video_output,  # 输出视频文件
            '--force-ipv4',  # 强制使用IPv4
            '--no-check-certificates',  # 不检查SSL证书
            '--geo-bypass',  # 尝试绕过地理限制
            '--extractor-retries', '5',  # 提取器重试次数
            '--ignore-errors',  # 忽略错误继续下载
            '--no-playlist',  # 不下载播放列表，仅下载单个视频
        ]
        # 根据参数调整命令
        if skip_download:
            cmd.extend([
                '--skip-download',
                '--write-info-json',
                '--write-thumbnail',
                '--convert-thumbnails', 'jpg',
                '--write-subs',
                '--all-subs',
            ])
        elif only_video:
            cmd.extend([
                '--no-write-info-json',
                '--no-write-thumbnail',
                '--no-write-subs',
            ])
        else:
            # 默认全下载
            cmd.extend([
                '--write-info-json',
                '--write-thumbnail',
                '--convert-thumbnails', 'jpg',
                '--write-subs',
                '--all-subs',
            ])
        
        # 如果提供了cookie文件，添加到命令中
        if cookies_file_path:
            cookies_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), cookies_file_path)
            if os.path.exists(cookies_path):
                logger.info(f"使用cookies文件: {cookies_path}")
                cmd.extend(['--cookies', cookies_path])
            else:
                logger.warning(f"指定的YouTube Cookies文件不存在: {cookies_path}")
        
        # 执行yt-dlp命令
        logger.info(f"执行命令: {' '.join(cmd)}")
        process = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        # 处理输出结果
        if process.returncode == 0:
            logger.info("下载完成，正在收集文件信息")
            
            # 获取下载的文件信息
            video_path = None
            metadata_path = None
            cover_path = None
            subtitles_paths = []
            
            # 查找实际的视频文件
            for file in os.listdir(task_dir):
                file_path = os.path.join(task_dir, file)
                if os.path.isfile(file_path):
                    if file.startswith('video.') and not (file.endswith('.info.json') or '.vtt' in file or '.srt' in file or file.endswith('.jpg')):
                        video_path = file_path
                    elif file.endswith('.info.json'):
                        metadata_path = file_path
                    elif file.endswith('.jpg'):
                        # 将视频封面重命名为cover.jpg
                        if file != 'cover.jpg':
                            cover_path = os.path.join(task_dir, 'cover.jpg')
                            shutil.copy(file_path, cover_path)
                            logger.info(f"将封面图片 {file} 重命名为 cover.jpg")
                        else:
                            cover_path = file_path
                    elif file.startswith('video.') and ('.vtt' in file or '.srt' in file):
                        subtitles_paths.append(file_path)
            
            # 重命名metadata文件
            if metadata_path and os.path.exists(metadata_path):
                shutil.copy(metadata_path, metadata_output)
                metadata_path = metadata_output
            
            # 结果处理
            result = {
                "video_path": video_path,
                "metadata_path": metadata_path,
                "cover_path": cover_path,
                "subtitles_paths": subtitles_paths,
                "task_id": task_id,
                "task_dir": task_dir
            }
            # skip_download 时允许 video_path 为空
            if skip_download:
                logger.info(f"仅采集信息成功: {json.dumps(result, ensure_ascii=False)}")
                return True, result
            # only_video 时只关心视频文件
            if only_video:
                if not video_path:
                    logger.error("未找到下载的视频文件")
                    return False, "未找到下载的视频文件"
                
                # 即使在only_video模式下，也检查元数据和封面是否存在
                if not metadata_path:
                    metadata_path_default = os.path.join(task_dir, 'metadata.json')
                    if os.path.exists(metadata_path_default):
                        metadata_path = metadata_path_default
                
                if not cover_path:
                    cover_path_default = os.path.join(task_dir, 'cover.jpg')
                    if os.path.exists(cover_path_default):
                        cover_path = cover_path_default
                
                result["metadata_path"] = metadata_path
                result["cover_path"] = cover_path
                
                logger.info(f"仅下载视频文件成功: {json.dumps(result, ensure_ascii=False)}")
                return True, result
            # 默认全下载
            if not video_path:
                logger.error("未找到下载的视频文件")
                return False, "未找到下载的视频文件"
            logger.info(f"下载成功: {json.dumps(result, ensure_ascii=False)}")
            return True, result
        else:
            error_msg = f"yt-dlp返回非零状态码: {process.returncode}"
            logger.error(error_msg)
            logger.error(f"标准输出: {process.stdout}")
            logger.error(f"标准错误: {process.stderr}")
            return False, error_msg
        
    except subprocess.CalledProcessError as e:
        error_msg = f"yt-dlp执行错误: {str(e)}"
        logger.error(error_msg)
        logger.error(f"标准输出: {e.stdout}")
        logger.error(f"标准错误: {e.stderr}")
        return False, error_msg
        
    except Exception as e:
        error_msg = f"下载过程中发生错误: {str(e)}"
        logger.error(error_msg)
        return False, error_msg 