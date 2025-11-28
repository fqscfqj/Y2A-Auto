#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import time
import uuid
import shutil
import logging
import subprocess
from pathlib import Path
from modules.config_manager import load_config
from logging.handlers import RotatingFileHandler
from .utils import get_app_subdir, get_app_root_dir
from .ffmpeg_manager import get_ffmpeg_path, is_ffmpeg_usable
from shutil import which as _which

# 其他导入和常量定义
logger = logging.getLogger(__name__)


def is_docker_env() -> bool:
    """粗略判断是否运行在 Docker 中"""
    try:
        if os.path.exists('/.dockerenv'):
            return True
        cgroup_path = '/proc/1/cgroup'
        if os.path.exists(cgroup_path):
            with open(cgroup_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().lower()
                return 'docker' in content or 'kubepods' in content
    except Exception:
        pass
    return False


def merge_streams_with_ffmpeg(task_dir: str, ffmpeg_path: str | None = None, logger: logging.Logger | None = None) -> str | None:
    """Fallback merger for cases where yt-dlp fails during the internal merging phase."""
    log = logger or logging.getLogger(__name__)
    task_path = Path(task_dir)
    if not task_path.exists():
        log.warning("手动合并失败: 任务目录不存在")
        return None

    output_file = task_path / 'video.mp4'
    try:
        if output_file.exists() and output_file.stat().st_size > 0:
            log.info("检测到已存在的合并视频文件: %s", output_file)
            return str(output_file)
    except OSError as exc:
        log.debug("检查视频输出文件出错: %s", exc)

    def _pick_largest(paths):
        valid = [p for p in paths if p.exists() and p.is_file()]
        if not valid:
            return None
        return max(valid, key=lambda p: p.stat().st_size)

    video_exts = {'.mp4', '.webm', '.mkv', '.mov'}
    audio_exts = {'.m4a', '.weba', '.webm', '.opus', '.mp3'}

    video_candidates = [p for p in task_path.glob('video.*')
                        if p.suffix.lower() in video_exts and p.name not in {'video.mp4'} and '.info' not in p.name]
    audio_candidates = [p for p in task_path.glob('video.*')
                        if p.suffix.lower() in audio_exts and '.info' not in p.name]

    video_file = _pick_largest(video_candidates)
    audio_file = _pick_largest(audio_candidates)

    if not video_file or not audio_file:
        log.warning("手动合并失败: 未找到可用的视频/音频分离文件")
        return None

    candidates = []
    if ffmpeg_path:
        candidates.append(ffmpeg_path)

    which_target = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    path_ffmpeg = _which(which_target)
    if path_ffmpeg and path_ffmpeg not in candidates:
        candidates.append(path_ffmpeg)

    # 最后尝试项目内置目录（防止绝对路径因为被移动而失效）
    bundled = os.path.join(get_app_root_dir(), 'ffmpeg', 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    if bundled not in candidates:
        candidates.append(bundled)

    for candidate in candidates:
        if not candidate or not is_ffmpeg_usable(candidate, log):
            continue

        cmd = [candidate, '-y', '-i', str(video_file), '-i', str(audio_file), '-c', 'copy', str(output_file)]
        log.info("尝试通过ffmpeg手动合并音视频: %s", ' '.join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
        except Exception as exc:
            log.warning(f"手动合并过程中发生异常: {exc}")
            continue

        if result.returncode == 0:
            log.info("已通过ffmpeg成功合并音视频: %s", output_file)
            return str(output_file)

        log.warning("手动合并失败，ffmpeg输出: %s", result.stderr)

    log.warning("所有可用ffmpeg路径均无法完成手动合并")
    return None


def build_proxy_url(config):
    """
    构建代理URL，包含认证信息（如果有）
    
    Args:
        config (dict): 配置字典
        
    Returns:
        str: 完整的代理URL，如果没有启用代理则返回None
    """
    if not config.get('YOUTUBE_PROXY_ENABLED', False):
        return None
        
    proxy_url = config.get('YOUTUBE_PROXY_URL', '').strip()
    if not proxy_url:
        return None
        
    proxy_username = config.get('YOUTUBE_PROXY_USERNAME', '').strip()
    proxy_password = config.get('YOUTUBE_PROXY_PASSWORD', '').strip()
    
    # 如果有用户名和密码，构建认证代理URL
    if proxy_username and proxy_password:
        # 解析原始代理URL
        if '://' in proxy_url:
            protocol, rest = proxy_url.split('://', 1)
            # 构建包含认证的代理URL
            auth_proxy_url = f"{protocol}://{proxy_username}:{proxy_password}@{rest}"
            return auth_proxy_url
        else:
            # 如果没有协议前缀，默认添加http://
            return f"http://{proxy_username}:{proxy_password}@{proxy_url}"
    
    return proxy_url

def setup_task_logger(task_id):
    """
    为特定任务设置日志记录器
    
    Args:
        task_id: 任务ID
        
    Returns:
        logger: 配置好的日志记录器
    """
    log_dir = get_app_subdir('logs')
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
    
    # 检查是否需要使用代理
    config = load_config()
    proxy_url = build_proxy_url(config)
    if proxy_url:
        cmd.extend(['--proxy', proxy_url])
        if logger:
            logger.info(f"测试视频可用性时使用代理: {proxy_url}")
    
    if cookies_path and os.path.exists(cookies_path):
        cmd.extend(['--cookies', cookies_path])
    
    try:
        logger.info("测试视频可用性和格式...")
        process = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=30,
            encoding='utf-8',
            errors='replace'  # 遇到无法解码的字符时用?替换
        )
        
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

def download_video_data(youtube_url, task_id=None, cookies_file_path=None, skip_download=False, only_video=False, progress_callback=None):
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
    task_dir = os.path.join(get_app_subdir('downloads'), task_id)
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
        logger.info("开始查找yt-dlp可执行文件路径...")
        
        # 优先使用跨平台方式在PATH中查找 yt-dlp
        try:
            import shutil as _shutil
            found = _shutil.which('yt-dlp')
            if found:
                yt_dlp_path = found
                logger.info(f"找到系统中的yt-dlp: {yt_dlp_path}")
            else:
                logger.debug("PATH 未找到 yt-dlp，尝试常见安装位置")
                # 检查常见的yt-dlp安装位置（Linux/Docker）
                possible_paths = [
                    '/home/y2a/.local/bin/yt-dlp',  # Docker环境中的用户安装路径
                    '/usr/local/bin/yt-dlp',        # 系统全局安装路径
                    '/usr/bin/yt-dlp',              # 系统安装路径
                ]
                logger.debug(f"检查常见yt-dlp安装位置: {possible_paths}")
                for path in possible_paths:
                    if os.path.exists(path):
                        yt_dlp_path = path
                        logger.info(f"找到存在的yt-dlp路径: {yt_dlp_path}")
                        break
        except Exception as e:
            logger.debug(f"通过PATH查找 yt-dlp 异常: {e}")
        
        # Windows环境的特殊处理（保持兼容性）
        if os.name == 'nt':  # Windows系统
            # 检查虚拟环境中的yt-dlp
            venv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.venv', 'Scripts', 'yt-dlp.exe')
            if os.path.exists(venv_path):
                yt_dlp_path = venv_path
                logger.info(f"使用虚拟环境中的yt-dlp: {yt_dlp_path}")
            
            # 检查python目录下的yt-dlp
            try:
                python_dir = os.path.dirname(os.path.dirname(subprocess.run(
                    ['where', 'python'], 
                    capture_output=True, 
                    text=True, 
                    shell=True,
                    timeout=10,  # 添加10秒超时
                    encoding='utf-8',
                    errors='replace'
                ).stdout.strip()))
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
        
        # 验证yt-dlp路径有效性
        logger.info(f"最终确定的yt-dlp路径: {yt_dlp_path}")
        if yt_dlp_path != 'yt-dlp' and not os.path.exists(yt_dlp_path):
            logger.error(f"yt-dlp路径不存在: {yt_dlp_path}")
            return False, f"yt-dlp可执行文件不存在: {yt_dlp_path}"
        
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
        
        # 预先检测 ffmpeg（内部会在未提供config时自行加载配置）
        ffmpeg_location = get_ffmpeg_path(logger=logger)

        # 准备yt-dlp命令
        cmd = [
            yt_dlp_path,
            youtube_url,
            '--output', video_output,  # 输出视频文件
            '--no-check-certificates',  # 不检查SSL证书
            '--geo-bypass',  # 尝试绕过地理限制
            '--extractor-retries', '10',  # 增加提取器重试次数
            '--fragment-retries', '10',  # 增加片段重试次数
            '--retry-sleep', '3',  # 重试间隔
            '--ignore-errors',  # 忽略错误继续下载
            '--no-playlist',  # 不下载播放列表，仅下载单个视频
            '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',  # 设置User-Agent
        ]
        
        # 检查是否需要使用代理
        config = load_config()
        proxy_url = build_proxy_url(config)
        if proxy_url:
            cmd.extend(['--proxy', proxy_url])
            logger.info(f"使用代理: {proxy_url}")
        
        # 配置下载线程数
        download_threads = config.get('YOUTUBE_DOWNLOAD_THREADS', 4)
        cmd.extend(['--concurrent-fragments', str(download_threads)])
        logger.info(f"使用下载线程数: {download_threads}")
        
        # 配置下载速度限制
        throttled_rate = config.get('YOUTUBE_THROTTLED_RATE', '').strip()
        if throttled_rate:
            cmd.extend(['--throttled-rate', throttled_rate])
            logger.info(f"启用下载速度限制: {throttled_rate}")
        
        # 添加格式选择策略 - 改进的格式选择
        if not skip_download:
            has_ffmpeg = bool(ffmpeg_location) or is_docker_env()
            if has_ffmpeg:
                # 有可用 ffmpeg（内置或 Docker 环境）：选择分离的视频+音频并合并
                cmd.extend([
                    '--format', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best',
                    '--merge-output-format', 'mp4'
                ])
            else:
                # 无 ffmpeg：避免触发合并，直接选单路流
                cmd.extend([
                    '--format', 'best[ext=mp4]/best'
                ])
        
        # 根据参数调整命令（不再进行缩略图格式转换，直接使用原生格式）
        if skip_download:
            cmd.extend([
                '--skip-download',
                '--write-info-json',
                '--write-thumbnail',
                '--write-subs',
                '--all-subs',
                '--convert-subs', 'srt',
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
                '--write-subs',
                '--all-subs',
                '--convert-subs', 'srt',
            ])

        # 传入 ffmpeg 位置（若检测到本地路径；在 Docker 中让 yt-dlp 走 PATH）
        if ffmpeg_location and os.path.isabs(ffmpeg_location):
            cmd.extend(['--ffmpeg-location', ffmpeg_location])
        
        # 如果提供了cookie文件，添加到命令中
        if cookies_path:
            cmd.extend(['--cookies', cookies_path])

        # 添加进度显示选项
        if progress_callback and not skip_download:
            cmd.extend(['--progress'])

        # 重试机制
        max_retries = 3
        # 预先初始化，避免在异常分支中未绑定导致静态分析报错
        process = None
        output = ""
        manual_merge_success = False

        for attempt in range(max_retries):
            try:
                logger.info(f"执行命令 (尝试 {attempt + 1}/{max_retries}): {' '.join(cmd)}")
                
                if progress_callback and not skip_download:
                    # 使用Popen实时获取进度，设置UTF-8编码
                    logger.info(f"准备执行yt-dlp命令，路径: {yt_dlp_path}")
                    logger.debug(f"完整命令: {' '.join(cmd)}")
                    
                    try:
                        process = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            bufsize=1,
                            universal_newlines=True,
                            encoding='utf-8',
                            errors='replace'  # 遇到无法解码的字符时用?替换
                        )
                        logger.info(f"subprocess.Popen创建成功，PID: {process.pid}")
                    except Exception as e:
                        logger.error(f"subprocess.Popen创建失败: {str(e)}")
                        raise
                    
                    # 检查process.stdout是否为None
                    if process.stdout is None:
                        logger.error("process.stdout为None，无法读取输出")
                        raise RuntimeError("进程创建成功但stdout为None")
                    
                    output_lines = []
                    logger.info("开始读取yt-dlp输出...")
                    
                    # 确保process.stdout不为None且可迭代
                    if process.stdout is None:
                        logger.error("process.stdout为None，无法读取输出")
                        raise RuntimeError("进程创建成功但stdout为None")
                    
                    for line in process.stdout:
                        output_lines.append(line)
                        line = line.strip()
                        logger.debug(f"yt-dlp输出: {line}")
                        
                        # 解析进度信息
                        if '[download]' in line and '%' in line:
                            try:
                                # 解析进度百分比，例如: [download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:30
                                if 'of' in line and 'at' in line:
                                    parts = line.split()
                                    for i, part in enumerate(parts):
                                        if part.endswith('%'):
                                            percent_str = part.replace('%', '')
                                            progress_percent = float(percent_str)
                                            
                                            # 提取文件大小和下载速度
                                            file_size = ""
                                            download_speed = ""
                                            eta = ""
                                            
                                            if i + 2 < len(parts) and parts[i + 1] == 'of':
                                                file_size = parts[i + 2]
                                            
                                            for j in range(i + 3, len(parts)):
                                                if parts[j] == 'at' and j + 1 < len(parts):
                                                    download_speed = parts[j + 1]
                                                elif parts[j] == 'ETA' and j + 1 < len(parts):
                                                    eta = parts[j + 1]
                                                    break
                                            
                                            progress_info = {
                                                'percent': progress_percent,
                                                'file_size': file_size,
                                                'speed': download_speed,
                                                'eta': eta
                                            }
                                            
                                            progress_callback(progress_info)
                                            break
                            except (ValueError, IndexError) as e:
                                logger.debug(f"解析进度信息失败: {e}")
                    
                    process.wait()
                    output = ''.join(output_lines)
                    
                    if process.returncode != 0:
                        raise subprocess.CalledProcessError(process.returncode, cmd, output)
                else:
                    # 不需要进度回调时使用原来的方式，设置UTF-8编码
                    process = subprocess.run(
                        cmd, 
                        capture_output=True, 
                        text=True, 
                        check=True, 
                        timeout=300,
                        encoding='utf-8',
                        errors='replace'  # 遇到无法解码的字符时用?替换
                    )
                    output = process.stdout
                
                break  # 成功执行，跳出重试循环
                
            except subprocess.CalledProcessError as e:
                logger.warning(f"尝试 {attempt + 1} 失败: {str(e)}")
                # 使用已初始化的 output 优先，其次回退到异常对象中的 stdout/stderr
                error_output = output or getattr(e, 'stdout', "")
                error_stderr = getattr(e, 'stderr', "") or ""
                logger.warning(f"标准输出: {error_output}")
                logger.warning(f"标准错误: {error_stderr}")
                
                # 检查是否是格式问题
                combined_error = error_output + error_stderr

                if "'NoneType' object has no attribute 'lower'" in combined_error:
                    merged_file = merge_streams_with_ffmpeg(task_dir, ffmpeg_location, logger)
                    if merged_file:
                        manual_merge_success = True
                        logger.info("yt-dlp在合并阶段触发NoneType错误，已使用ffmpeg手动合并成功")
                        break
                    logger.warning("手动合并失败，将继续进行下载重试")

                if "Requested format is not available" in combined_error or "Only images are available" in combined_error:
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
                    logger.error(f"标准输出: {error_output}")
                    logger.error(f"标准错误: {error_stderr}")
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
        
        # 处理输出结果 — 使用安全检查以防 process 未被创建
        download_success = manual_merge_success or (process is not None and getattr(process, 'returncode', None) == 0)
        if download_success:
            logger.info("下载完成，正在收集文件信息")
            
            # 获取下载的文件信息
            video_path = None
            metadata_path = None
            cover_path = None
            subtitles_paths = []
            
            # 查找实际的视频文件与封面
            for file in os.listdir(task_dir):
                file_path = os.path.join(task_dir, file)
                if os.path.isfile(file_path):
                    if file.startswith('video.') and not (file.endswith('.info.json') or '.vtt' in file or '.srt' in file or file.endswith('.jpg') or file.endswith('.webp') or file.endswith('.png')):
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
                    elif (file.lower().endswith('.webp') or file.lower().endswith('.png')) and file.startswith('video'):
                        # 直接使用 webp/png 作为封面（AcFun 已支持，不强制转 jpg）
                        if not cover_path:
                            cover_path = file_path
                            logger.info(f"检测到封面 {os.path.basename(file_path)}，将直接作为封面文件")
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
                    # 优先 jpg，其次 webp、png
                    for name in ['cover.jpg', 'video.webp', 'video.png', 'cover.webp', 'cover.png']:
                        p = os.path.join(task_dir, name)
                        if os.path.exists(p):
                            cover_path = p
                            break
                
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
            proc_code = getattr(process, 'returncode', None)
            error_msg = f"yt-dlp返回非零状态码: {proc_code}"
            logger.error(error_msg)
            final_output = output or (getattr(process, 'stdout', "") if process is not None else "")
            final_stderr = (getattr(process, 'stderr', "") or "") if process is not None else ""
            logger.error(f"标准输出: {final_output}")
            logger.error(f"标准错误: {final_stderr}")
            return False, error_msg
        
    except Exception as e:
        error_msg = f"下载过程中发生未预期的错误: {str(e)}"
        logger.error(error_msg)
        return False, error_msg

def extract_video_urls_from_playlist(playlist_url, cookies_file_path=None):
    """
    提取YouTube播放列表中的所有视频URL
    Args:
        playlist_url (str): 播放列表URL
        cookies_file_path (str, optional): cookies.txt文件路径
    Returns:
        list: 视频URL列表
    """
    import subprocess
    import sys
    video_urls = []
    try:
        yt_dlp_path = 'yt-dlp'
        # 处理cookies路径
        cookies_path = None
        if cookies_file_path:
            cookies_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), cookies_file_path)
            if not os.path.exists(cookies_path):
                cookies_path = None
        cmd = [
            yt_dlp_path,
            '--flat-playlist',
            '--dump-single-json',
            playlist_url
        ]
        if cookies_path:
            cmd.extend(['--cookies', cookies_path])
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode == 0:
            data = json.loads(result.stdout)
            entries = data.get('entries', [])
            for entry in entries:
                video_id = entry.get('id')
                if video_id:
                    video_urls.append(f'https://www.youtube.com/watch?v={video_id}')
        else:
            logger.error(f"yt-dlp提取播放列表失败: {result.stderr}")
    except Exception as e:
        logger.error(f"extract_video_urls_from_playlist异常: {str(e)}")
    return video_urls