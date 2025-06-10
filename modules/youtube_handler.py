#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import uuid
import shutil
import logging
import subprocess
import time
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

def test_video_availability(youtube_url, yt_dlp_path, cookies_path=None, logger=None):
    """
    测试视频可用性和格式
    
    Args:
        youtube_url: YouTube视频URL
        yt_dlp_path: yt-dlp可执行文件路径
        cookies_path: Cookie文件路径
        logger: 日志记录器
        
    Returns:
        tuple: (是否可用, 可用格式列表, 错误信息)
    """
    if not logger:
        logger = logging.getLogger(__name__)
        
    cmd = [
        yt_dlp_path,
        youtube_url,
        '--list-formats',
        '--no-warnings',
        '--simulate'
    ]
    
    if cookies_path and os.path.exists(cookies_path):
        cmd.extend(['--cookies', cookies_path])
    
    try:
        logger.info("测试视频可用性和格式...")
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if process.returncode == 0:
            formats = process.stdout
            logger.info("视频格式检查成功")
            return True, formats, None
        else:
            logger.warning(f"格式检查返回非零状态码: {process.returncode}")
            return False, None, process.stderr
            
    except subprocess.TimeoutExpired:
        logger.error("格式检查超时")
        return False, None, "格式检查超时"
    except Exception as e:
        logger.error(f"格式检查出错: {str(e)}")
        return False, None, str(e)

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
        
        # 在Linux/Docker环境中，首先检查系统PATH中的yt-dlp
        try:
            # 尝试使用which命令查找yt-dlp
            result = subprocess.run(['which', 'yt-dlp'], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                yt_dlp_path = result.stdout.strip()
                logger.info(f"找到系统中的yt-dlp: {yt_dlp_path}")
        except:
            # 如果which命令失败，检查常见的yt-dlp安装位置
            possible_paths = [
                '/home/y2a/.local/bin/yt-dlp',  # Docker环境中的用户安装路径
                '/usr/local/bin/yt-dlp',        # 系统全局安装路径
                '/usr/bin/yt-dlp',              # 系统安装路径
                'yt-dlp'                        # 回退到PATH查找
            ]
            
            for path in possible_paths:
                if os.path.exists(path) or path == 'yt-dlp':
                    yt_dlp_path = path
                    if path != 'yt-dlp':
                        logger.info(f"使用yt-dlp路径: {yt_dlp_path}")
                    break
        
        # Windows环境的特殊处理（保持兼容性）
        if os.name == 'nt':  # Windows系统
            # 检查虚拟环境中的yt-dlp
            venv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.venv', 'Scripts', 'yt-dlp.exe')
            if os.path.exists(venv_path):
                yt_dlp_path = venv_path
                logger.info(f"使用虚拟环境中的yt-dlp: {yt_dlp_path}")
            
            # 检查python目录下的yt-dlp
            try:
                python_dir = os.path.dirname(os.path.dirname(subprocess.run(['where', 'python'], 
                                                           capture_output=True, 
                                                           text=True, 
                                                           shell=True).stdout.strip()))
                python_scripts = os.path.join(python_dir, 'Scripts', 'yt-dlp.exe')
                if os.path.exists(python_scripts):
                    yt_dlp_path = python_scripts
                    logger.info(f"使用Python Scripts目录中的yt-dlp: {yt_dlp_path}")
            except:
                pass
        
        # 处理cookies路径
        cookies_path = None
        if cookies_file_path:
            cookies_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), cookies_file_path)
            if os.path.exists(cookies_path):
                logger.info(f"使用cookies文件: {cookies_path}")
            else:
                logger.warning(f"指定的YouTube Cookies文件不存在: {cookies_path}")
                cookies_path = None
        
        # 首先测试视频可用性
        available, formats_info, error_msg = test_video_availability(youtube_url, yt_dlp_path, cookies_path, logger)
        if not available:
            # 检查是否是cookie相关的错误
            bot_indicators = [
                "Sign in to confirm",
                "not a bot", 
                "Signature extraction failed",
                "Some formats may be missing",
                "HTTP Error 403",
                "Requested format is not available",
                "player",
                "decodeURIComponent"
            ]
            if error_msg and any(indicator in str(error_msg) for indicator in bot_indicators):
                logger.warning("检测到YouTube反机器人验证，可能需要更新Cookie")
                # 发送cookie更新通知到Web界面
                try:
                    import requests
                    # 尝试通知Web界面显示cookie更新提示
                    requests.post('http://localhost:5000/api/cookies/refresh-needed', 
                                json={'reason': 'bot_detection', 'video_url': youtube_url}, 
                                timeout=1)
                    logger.info("已发送Cookie刷新通知")
                except:
                    pass  # 忽略请求失败，不影响主流程
            
            logger.error(f"视频不可用或无法访问: {error_msg}")
            return False, f"视频不可用或无法访问: {error_msg}"
        
        # 准备yt-dlp命令
        cmd = [
            yt_dlp_path,
            youtube_url,
            '--output', video_output,  # 输出视频文件
            '--force-ipv4',  # 强制使用IPv4
            '--no-check-certificates',  # 不检查SSL证书
            '--geo-bypass',  # 尝试绕过地理限制
            '--extractor-retries', '10',  # 增加提取器重试次数
            '--fragment-retries', '10',  # 增加片段重试次数
            '--retry-sleep', '3',  # 重试间隔
            '--ignore-errors',  # 忽略错误继续下载
            '--no-playlist',  # 不下载播放列表，仅下载单个视频
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',  # 设置User-Agent
        ]
        
        # 添加格式选择策略 - 改进的格式选择
        if not skip_download:
            # 优先选择高质量视频格式，回退到可用格式
            cmd.extend([
                '--format', 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4][height<=1080]/bestvideo+bestaudio/best',
                '--merge-output-format', 'mp4'
            ])
        
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
        if cookies_path:
            cmd.extend(['--cookies', cookies_path])
        
        # 重试机制
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"执行命令 (尝试 {attempt + 1}/{max_retries}): {' '.join(cmd)}")
                process = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=300)
                break  # 成功执行，跳出重试循环
                
            except subprocess.CalledProcessError as e:
                logger.warning(f"尝试 {attempt + 1} 失败: {str(e)}")
                logger.warning(f"标准输出: {e.stdout}")
                logger.warning(f"标准错误: {e.stderr}")
                
                # 检查是否是格式问题
                if "Requested format is not available" in e.stderr or "Only images are available" in e.stderr:
                    if attempt < max_retries - 1:
                        # 降级格式选择策略
                        if '--format' in cmd:
                            format_index = cmd.index('--format')
                            if attempt == 0:
                                # 第二次尝试：使用更宽松的格式
                                cmd[format_index + 1] = 'best[ext=mp4]/best'
                                logger.info("使用降级格式策略: best[ext=mp4]/best")
                            elif attempt == 1:
                                # 第三次尝试：移除格式限制
                                cmd.pop(format_index + 1)  # 移除格式参数
                                cmd.pop(format_index)      # 移除--format
                                if '--merge-output-format' in cmd:
                                    merge_index = cmd.index('--merge-output-format')
                                    cmd.pop(merge_index + 1)
                                    cmd.pop(merge_index)
                                logger.info("移除格式限制，使用默认格式")
                        
                        time.sleep(2)  # 等待2秒后重试
                        continue
                
                if attempt == max_retries - 1:
                    # 最后一次尝试也失败
                    error_msg = f"yt-dlp执行错误: {str(e)}"
                    logger.error(error_msg)
                    logger.error(f"标准输出: {e.stdout}")
                    logger.error(f"标准错误: {e.stderr}")
                    return False, error_msg
                    
            except subprocess.TimeoutExpired:
                logger.warning(f"尝试 {attempt + 1} 超时")
                if attempt == max_retries - 1:
                    error_msg = "下载超时"
                    logger.error(error_msg)
                    return False, error_msg
                time.sleep(3)  # 超时后等待更长时间
                continue
                
            except Exception as e:
                logger.warning(f"尝试 {attempt + 1} 出现异常: {str(e)}")
                if attempt == max_retries - 1:
                    error_msg = f"下载过程中发生错误: {str(e)}"
                    logger.error(error_msg)
                    return False, error_msg
                time.sleep(2)
                continue
        
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
        
    except Exception as e:
        error_msg = f"下载过程中发生未预期的错误: {str(e)}"
        logger.error(error_msg)
        return False, error_msg 