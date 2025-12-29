#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import time
import logging
import requests
import re
import ssl
from hashlib import sha1
from math import ceil
from logging.handlers import RotatingFileHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Optional, List, Union, Tuple
from .utils import get_app_subdir

from modules.utils import process_cover


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
    logger = logging.getLogger(f'acfun_uploader_{task_id}')
    
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


class AcfunUploader:
    """AcFun视频上传模块 - 现代化版本，支持Cookie登录"""
    
    def __init__(self, acfun_username=None, acfun_password=None, cookie_file=None):
        """
        初始化AcFun上传器
        
        Args:
            acfun_username (str, optional): AcFun账号用户名
            acfun_password (str, optional): AcFun账号密码
            cookie_file (str, optional): Cookie文件路径
        """
        self.username = acfun_username
        self.password = acfun_password
        self.cookie_file = cookie_file or "cookies/ac_cookies.txt"
        self.session = requests.Session()
        self.logger = None  # 需要在上传时设置
        
        # 设置通用请求头
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://member.acfun.cn",
            "Referer": "https://member.acfun.cn/"
        })
        
        # API端点
        self.LOGIN_URL = "https://id.app.acfun.cn/rest/web/login/signin"
        self.TOKEN_URL = "https://member.acfun.cn/video/api/getKSCloudToken"
        self.FRAGMENT_URL = "https://upload.kuaishouzt.com/api/upload/fragment"
        self.COMPLETE_URL = "https://upload.kuaishouzt.com/api/upload/complete"
        self.FINISH_URL = "https://member.acfun.cn/video/api/uploadFinish"
        self.C_VIDEO_URL = "https://member.acfun.cn/video/api/createVideo"
        self.C_DOUGA_URL = "https://member.acfun.cn/video/api/createDouga"
        self.QINIU_URL = "https://member.acfun.cn/common/api/getQiniuToken"
        self.COVER_URL = "https://member.acfun.cn/common/api/getUrlAfterUpload"
    
    def log(self, *msg):
        """记录日志信息"""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        message = " ".join(str(m) for m in msg)
        
        if self.logger:
            self.logger.info(message)
        else:
            print(f'[{timestamp}] {message}')
    
    def calc_sha1(self, data: bytes) -> str:
        """计算数据的SHA1哈希值"""
        sha1_obj = sha1()
        sha1_obj.update(data)
        return sha1_obj.hexdigest()
    
    def load_cookies(self, cookie_file: Optional[str] = None) -> bool:
        """从文件加载cookie，支持Netscape和JSON格式"""
        if cookie_file is None:
            cookie_file = self.cookie_file
            
        try:
            if not os.path.exists(cookie_file):
                self.log(f"Cookie文件不存在: {cookie_file}")
                return False
            
            with open(cookie_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            if not content:
                self.log(f"Cookie文件为空: {cookie_file}")
                return False
            
            # 判断文件格式
            if content.startswith('# Netscape HTTP Cookie File') or '\t' in content:
                # Netscape格式
                cookie_count = self._load_netscape_cookies(content)
                self.log(f"从Netscape格式文件加载了 {cookie_count} 个cookie")
                if cookie_count == 0:
                    self.log("警告: Netscape格式文件中没有有效的cookie条目")
                    return False
            else:
                try:
                    # JSON格式
                    cookies_data = json.loads(content)
                    if not cookies_data:
                        self.log("JSON格式cookies文件为空数组")
                        return False
                    for cookie in cookies_data:
                        self.session.cookies.set(
                            cookie['name'], 
                            cookie['value'], 
                            domain=cookie.get('domain', ''),
                            path=cookie.get('path', '/')
                        )
                    self.log(f"从JSON格式文件加载了 {len(cookies_data)} 个cookie")
                except json.JSONDecodeError as e:
                    self.log(f"JSON格式解析失败: {e}")
                    return False
            
            # 测试cookie是否有效
            login_success = self.test_login()
            if login_success:
                self.log("Cookie登录测试成功")
            else:
                self.log("Cookie登录测试失败，可能已过期或无效")
            return login_success
            
        except Exception as e:
            self.log(f"加载cookie文件失败: {e}")
            return False
    
    def _load_netscape_cookies(self, content: str) -> int:
        """加载Netscape格式的cookie"""
        lines = content.split('\n')
        cookie_count = 0
        
        for line in lines:
            line = line.strip()
            # 跳过注释和空行
            if not line or line.startswith('#'):
                continue
            
            # Netscape格式: domain	flag	path	secure	expiration	name	value
            parts = line.split('\t')
            if len(parts) >= 7:
                domain = parts[0]
                path = parts[2]
                secure = parts[3].upper() == 'TRUE'
                name = parts[5]
                value = parts[6]
                
                # 设置cookie
                self.session.cookies.set(
                    name=name,
                    value=value,
                    domain=domain,
                    path=path,
                    secure=secure
                )
                cookie_count += 1
        
        return cookie_count
    
    def save_cookies(self, cookie_file: Optional[str] = None):
        """保存cookie到文件"""
        if cookie_file is None:
            cookie_file = self.cookie_file
            
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(cookie_file), exist_ok=True)
            
            cookies_data = []
            for cookie in self.session.cookies:
                cookies_data.append({
                    'name': cookie.name,
                    'value': cookie.value,
                    'domain': cookie.domain,
                    'path': cookie.path
                })
            
            with open(cookie_file, 'w', encoding='utf-8') as f:
                json.dump(cookies_data, f, ensure_ascii=False, indent=2)
            
            self.log(f"Cookie已保存到: {cookie_file}")
        except Exception as e:
            self.log(f"保存cookie失败: {e}")
    
    def test_network_connectivity(self) -> bool:
        """测试网络连接"""
        test_urls = [
            "https://www.acfun.cn",
            "https://member.acfun.cn",
            "https://upload.kuaishouzt.com"
        ]
        
        for url in test_urls:
            try:
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    self.log(f"网络连接正常: {url}")
                else:
                    self.log(f"网络连接异常: {url} (状态码: {response.status_code})")
                    return False
            except Exception as e:
                self.log(f"网络连接失败: {url} ({e})")
                return False
        
        return True
    
    def test_login(self) -> bool:
        """测试登录状态"""
        try:
            # 使用一个简单的API来测试登录状态
            response = self.session.get("https://member.acfun.cn/video/api/getMyChannels")
            
            if response.status_code == 200:
                try:
                    result = response.json()
                    return result.get('result') == 0
                except:
                    # 如果不是JSON，可能是HTML页面，检查是否是登录页面
                    if "login" in response.text.lower() or "登录" in response.text:
                        return False
                    # 如果是其他HTML页面（如创作中心），说明已登录
                    return True
            return False
        except Exception as e:
            self.log(f"测试登录状态失败: {e}")
            return False
    
    def login(self) -> bool:
        """
        登录AcFun账号，优先使用cookie，其次使用用户名密码
        
        Returns:
            bool: 登录是否成功
        """
        # 首先尝试使用cookie登录
        if self.load_cookies():
            self.log("使用Cookie登录成功")
            return True
        
        # 如果cookie登录失败，尝试用户名密码登录
        if not self.username or not self.password:
            self.log("没有提供用户名和密码，无法登录")
            return False
        
        self.log("正在使用用户名密码登录AcFun账号...")
        try:
            response = self.session.post(
                self.LOGIN_URL,
                data={
                    'username': self.username,
                    'password': self.password,
                    'key': '',
                    'captcha': ''
                }
            )
            
            result = response.json()
            if result.get('result') == 0:
                self.log('用户名密码登录成功')
                # 保存新的cookie
                self.save_cookies()
                return True
            else:
                self.log(f"登录失败: {result.get('error_msg', '账号密码错误')}")
                return False
        except Exception as e:
            self.log(f"登录过程中出错: {e}")
            return False
    
    def get_token(self, filename: str, filesize: int) -> Tuple[str, str, int]:
        """获取上传token"""
        response = self.session.post(
            self.TOKEN_URL,
            data={
                "fileName": filename,
                "size": filesize,
                "template": "1"
            }
        )
        try:
            result = response.json()
        except (ValueError, TypeError) as e:
            self.log(f"获取token响应JSON解析失败: {response.text}, 错误: {str(e)}")
            raise RuntimeError(f"获取上传token失败: JSON解析错误 - {str(e)}")
        
        if not isinstance(result, dict):
            raise RuntimeError(f"获取token API返回格式异常: {response.text}")
        
        task_id = result.get("taskId")
        token = result.get("token")
        upload_config = result.get("uploadConfig", {})
        part_size = upload_config.get("partSize") if isinstance(upload_config, dict) else None
        
        if not task_id or not token or not part_size:
            raise RuntimeError(f"获取token失败，缺少必需字段: {response.text}")
        
        return task_id, token, part_size
    
    def upload_chunk(self, block: bytes, fragment_id: int, upload_token: str) -> bool:
        """上传分块"""
        # 创建专用的上传session
        upload_session = requests.Session()
        
        # 配置重试策略
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        # 配置适配器
        adapter = HTTPAdapter(max_retries=retry_strategy)
        upload_session.mount("http://", adapter)
        upload_session.mount("https://", adapter)
        
        # 设置请求头
        headers = {
            "Content-Type": "application/octet-stream",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive"
        }
        
        for attempt in range(3):
            try:
                # 添加延迟避免请求过快
                if attempt > 0:
                    time.sleep(2 ** attempt)  # 指数退避
                
                # 第一次尝试使用标准SSL
                verify_ssl = True if attempt == 0 else False
                
                response = upload_session.post(
                    self.FRAGMENT_URL,
                    params={
                        "fragment_id": fragment_id,
                        "upload_token": upload_token
                    },
                    data=block,
                    headers=headers,
                    timeout=(30, 120),  # (连接超时, 读取超时)
                    verify=verify_ssl,  # 第一次验证SSL，后续尝试跳过验证
                    stream=False
                )
                
                # 检查响应
                if response.status_code == 200:
                    result = response.json()
                    if result.get("result") == 1:
                        # 成功的分块上传不再写入任务INFO日志，改为debug级别
                        if hasattr(self, 'logger') and self.logger:
                            try:
                                self.logger.debug(f"分块 {fragment_id + 1} 上传成功")
                            except Exception:
                                pass
                        # 如果没有task级logger则不打印成功消息，避免终端或文件过多噪声
                        return True
                    else:
                        self.log(f"分块 {fragment_id + 1} 上传失败: {result}")
                else:
                    self.log(f"分块 {fragment_id + 1} HTTP错误: {response.status_code}")
                    
            except ssl.SSLError as e:
                self.log(f"分块 {fragment_id + 1} SSL错误，重试第 {attempt + 1} 次: {e}")
                if attempt == 2:  # 最后一次尝试
                    self.log("SSL连接持续失败，可能是网络问题或防火墙阻拦")
            except requests.exceptions.Timeout as e:
                self.log(f"分块 {fragment_id + 1} 超时，重试第 {attempt + 1} 次: {e}")
            except requests.exceptions.ConnectionError as e:
                self.log(f"分块 {fragment_id + 1} 连接错误，重试第 {attempt + 1} 次: {e}")
            except Exception as e:
                self.log(f"分块 {fragment_id + 1} 未知错误，重试第 {attempt + 1} 次: {e}")
        
        return False
    
    def complete_upload(self, fragment_count: int, upload_token: str):
        """完成上传"""
        # 创建专用的上传session
        upload_session = requests.Session()
        
        # 配置重试策略
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        # 配置适配器
        adapter = HTTPAdapter(max_retries=retry_strategy)
        upload_session.mount("http://", adapter)
        upload_session.mount("https://", adapter)
        
        headers = {
            "Content-Length": "0",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Connection": "keep-alive"
        }
        
        for attempt in range(3):
            try:
                if attempt > 0:
                    time.sleep(2 ** attempt)
                
                response = upload_session.post(
                    self.COMPLETE_URL,
                    params={
                        "fragment_count": fragment_count,
                        "upload_token": upload_token
                    },
                    headers=headers,
                    timeout=(30, 60),
                    verify=True
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("result") == 1:
                        self.log("上传完成确认成功")
                        return
                    else:
                        self.log(f"完成上传失败: {result}")
                else:
                    self.log(f"完成上传HTTP错误: {response.status_code}")
                    
            except Exception as e:
                self.log(f"完成上传出错，重试第 {attempt + 1} 次: {e}")
                if attempt == 2:
                    self.log("完成上传失败，但文件可能已上传成功")
    
    def upload_finish(self, task_id: int):
        """上传完成处理"""
        response = self.session.post(
            self.FINISH_URL,
            data={"taskId": task_id}
        )
        
        try:
            result = response.json()
        except (ValueError, TypeError) as e:
            self.log(f"上传完成处理响应JSON解析失败: {response.text}, 错误: {str(e)}")
            return
        
        if not isinstance(result, dict) or result.get("result") != 0:
            self.log(f"上传完成处理失败: {response.text}")
    
    def create_video(self, video_key: int, filename: str) -> Optional[int]:
        """创建视频"""
        response = self.session.post(
            self.C_VIDEO_URL,
            data={
                "videoKey": video_key,
                "fileName": filename,
                "vodType": "ksCloud"
            },
            headers={
                "origin": "https://member.acfun.cn",
                "referer": "https://member.acfun.cn/upload-video"
            }
        )
        
        try:
            result = response.json()
        except (ValueError, TypeError) as e:
            self.log(f"创建视频API响应JSON解析失败: {response.text}, 错误: {str(e)}")
            return None
        
        if not isinstance(result, dict):
            self.log(f"创建视频API返回格式异常，响应不是字典类型: {response.text}")
            return None
        
        if result.get("result") != 0:
            self.log(f"创建视频失败: {response.text}")
            return None
        
        video_id = result.get("videoId")
        if not video_id:
            self.log(f"创建视频成功但未获取到videoId: {response.text}")
            return None
        
        self.upload_finish(video_key)
        return video_id
    
    def upload_cover(self, image_path: str, mode='crop') -> str:
        """上传封面图片（支持 jpg/png/webp，尽量保留原格式）"""
        self.log(f"处理封面图片: {image_path}")

        # 创建临时目录用于处理封面
        temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'temp')
        os.makedirs(temp_dir, exist_ok=True)

        # 推断源扩展名
        src_ext = os.path.splitext(image_path)[1].lower()
        if src_ext not in {'.jpg', '.jpeg', '.png', '.webp'}:
            # 不识别的格式默认处理为 jpg
            src_ext = '.jpg'

        # 处理后文件名与扩展名保持一致
        processed_image = os.path.join(temp_dir, f'processed_cover{src_ext}')

        try:
            process_cover(image_path, processed_image, mode)
        except Exception as e:
            self.log(f"调用 process_cover 失败，使用原始封面: {e}")
            processed_image = image_path

        # 如果处理封面失败或未生成文件，使用原始封面
        if not os.path.exists(processed_image):
            processed_image = image_path
            src_ext = os.path.splitext(processed_image)[1].lower() or '.jpg'

        # 生成随机文件名（与扩展名匹配）
        import random
        import string
        file_name = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        token_file_name = f"{file_name}{src_ext if src_ext != '.jpeg' else '.jpg'}"

        # 获取七牛token（使用匹配的扩展名）
        response = self.session.post(
            self.QINIU_URL,
            data={"fileName": token_file_name}
        )

        try:
            result = response.json()
        except (ValueError, TypeError) as e:
            self.log(f"获取七牛token响应JSON解析失败: {response.text}, 错误: {str(e)}")
            raise RuntimeError(f"获取封面上传token失败: JSON解析错误 - {str(e)}")
        
        token = result.get("info", {}).get("token") if isinstance(result, dict) else None
        if not token:
            raise RuntimeError(f"获取封面上传token失败: {response.text}")

        # 上传图片
        with open(processed_image, "rb") as f:
            chunk_data = f.read()

        self.upload_chunk(chunk_data, 0, token)
        self.complete_upload(1, token)

        # 获取上传后的URL
        response = self.session.post(
            self.COVER_URL,
            data={"bizFlag": "web-douga-cover", "token": token}
        )
        
        try:
            result = response.json()
        except (ValueError, TypeError) as e:
            self.log(f"获取封面URL响应JSON解析失败: {response.text}, 错误: {str(e)}")
            raise RuntimeError(f"获取封面URL失败: JSON解析错误 - {str(e)}")
        
        cover_url = result.get("url") if isinstance(result, dict) else None
        if not cover_url:
            raise RuntimeError(f"获取封面URL失败: {response.text}")

        # 清理临时文件
        try:
            if os.path.exists(processed_image) and processed_image != image_path:
                os.remove(processed_image)
        except Exception as e:
            self.log(f"临时封面清理失败: {e}")

        return cover_url
    
    def create_douga(self, file_path: str, title: str, channel_id: int, cover_path: str,
                     desc: str = "", tags: Optional[List[str]] = None,
                     creation_type: int = 3, original_url: str = "", is_sync_ks: str = "0") -> Tuple[bool, Union[dict, str]]:
        """创建投稿"""
        if tags is None:
            tags = []

        def _compact_text_local(text: str, max_len: int) -> str:
            text = (text or "").strip()
            if not text or max_len <= 0:
                return ""
            text = re.sub(r"\s+", " ", text)
            if len(text) <= max_len:
                return text
            if max_len <= 3:
                return text[:max_len]
            return text[: max_len - 3] + "..."
        
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        
        # 获取上传token
        task_id, token, part_size = self.get_token(file_name, file_size)
        fragment_count = ceil(file_size / part_size)
        
        self.log(f"开始上传 {file_name}，共 {fragment_count} 个分块")
        
        # 上传视频文件
        with open(file_path, "rb") as f:
            for fragment_id in range(fragment_count):
                chunk_data = f.read(part_size)
                if not chunk_data:
                    break
                
                if not self.upload_chunk(chunk_data, fragment_id, token):
                    self.log(f"分块 {fragment_id + 1} 上传失败")
                    return False, "分块上传失败"
                
                # 计算上传进度并更新网页显示进度（不再把每次进度写为 INFO 到任务日志）
                progress = ((fragment_id + 1) / fragment_count) * 100
                if hasattr(self, 'logger') and self.logger:
                    try:
                        self.logger.debug(f"上传进度: {progress:.1f}% ({fragment_id + 1}/{fragment_count})")
                    except Exception:
                        pass

                # 静默更新任务进度到数据库（如果有task_id），用于网页显示
                if hasattr(self, 'task_id') and self.task_id:
                    from modules.task_manager import update_task
                    update_task(self.task_id, upload_progress=f"{progress:.1f}%", silent=True)

        
        # 完成上传
        self.complete_upload(fragment_count, token)
        
        # 创建视频
        video_id = self.create_video(int(task_id), file_name)
        if not video_id:
            return False, "创建视频失败"
        
        # 上传封面
        cover_url = self.upload_cover(cover_path)
        
        def _submit_create_douga(submit_desc: str) -> Tuple[bool, Union[dict, str], Optional[int], str]:
            data = {
                "title": title,
                "description": submit_desc,
                "tagNames": json.dumps(tags),
                "creationType": creation_type,
                "channelId": channel_id,
                "coverUrl": cover_url,
                "videoInfos": json.dumps([{"videoId": video_id, "title": title}]),
                "isJoinUpCollege": "0",
                "isSyncKs": str(is_sync_ks)
            }

            if creation_type == 1:  # 转载
                data["originalLinkUrl"] = original_url
                data["originalDeclare"] = "0"
            else:  # 原创
                data["originalDeclare"] = "1"

            response = self.session.post(
                self.C_DOUGA_URL,
                data=data,
                headers={
                    "origin": "https://member.acfun.cn",
                    "referer": "https://member.acfun.cn/upload-video"
                }
            )

            try:
                result = response.json()
            except (ValueError, TypeError) as e:
                self.log(f"API响应JSON解析失败: {response.text}, 错误: {str(e)}")
                return False, f"API响应JSON解析失败: {str(e)}", None, ""

            if not isinstance(result, dict):
                self.log(f"API返回格式异常，响应不是字典类型: {response.text}")
                return False, "API返回格式异常: 响应不是字典类型", None, ""

            # 成功结构：{result:0, dougaId:...}
            if result.get("result") == 0 and "dougaId" in result:
                self.log(f"视频投稿成功！AC号：{result['dougaId']}")
                return True, {
                    "ac_number": result["dougaId"],
                    "title": title,
                    "cover_url": cover_url
                }, 0, ""

            # 失败结构A：{result:xxx, error_msg/msg}
            if "result" in result:
                result_code = result.get("result")
                error_msg = result.get("error_msg") or result.get("msg") or "未知错误"
                self.log(f"视频投稿失败: {response.text}")
                err_code_int = None
                try:
                    err_code_int = int(result_code) if result_code is not None else None
                except Exception:
                    err_code_int = None
                return False, f"视频投稿失败 (code={result_code if result_code is not None else '未知'}): {error_msg}", err_code_int, str(error_msg)

            # 失败结构B：{errMsg:{result:..., error_msg:...}, isError:true}
            err_msg_obj = result.get("errMsg")
            if not isinstance(err_msg_obj, dict):
                err_msg_obj = {}
            err_code = err_msg_obj.get("result")
            err_msg = err_msg_obj.get("error_msg") or result.get("error_msg") or result.get("msg") or "未知错误"
            if result.get("isError") or err_msg:
                self.log(f"视频投稿失败: {response.text}")
                err_code_int = None
                try:
                    err_code_int = int(err_code) if err_code is not None else None
                except Exception:
                    err_code_int = None
                return False, f"视频投稿失败 (code={err_code or '未知'}): {err_msg}", err_code_int, str(err_msg)

            self.log(f"API返回格式异常，缺少result字段: {response.text}")
            return False, f"API返回格式异常: {err_msg}", None, str(err_msg)

        ok, payload_or_err, _, _ = _submit_create_douga(desc)
        return ok, payload_or_err
    
    def upload_video(self, video_file_path, cover_file_path, title, description, tags, 
                     partition_id, original_url=None, original_uploader=None, 
                     original_upload_date=None, task_id=None, cover_mode='crop'):
        """
        上传视频到AcFun
        
        Args:
            video_file_path (str): 视频文件路径
            cover_file_path (str): 封面文件路径
            title (str): 视频标题
            description (str): 视频描述
            tags (list): 标签列表
            partition_id (str): AcFun分区ID
            original_url (str, optional): 原始视频URL
            original_uploader (str, optional): 原始上传者
            original_upload_date (str, optional): 原始上传日期
            task_id (str, optional): 任务ID
            cover_mode (str): 封面处理模式，'crop'表示裁剪，'pad'表示添加黑边
            
        Returns:
            tuple: (成功标志, 结果数据或错误信息)
        """
        # 设置任务日志和task_id
        self.task_id = task_id
        self.logger = setup_task_logger(task_id or "unknown")
        self.log(f"开始上传视频: {video_file_path}")
        
        try:
            # 尝试登录
            if not self.login():
                return False, "AcFun登录失败，请检查用户名密码或Cookie文件"
            
            # 检查文件是否存在
            if not os.path.exists(video_file_path):
                return False, f"视频文件不存在: {video_file_path}"
            
            if not os.path.exists(cover_file_path):
                return False, f"封面文件不存在: {cover_file_path}"
            
            # 应用AcFun字符限制
            # 1. 标题限制50个字符
            if len(title) > 50:
                self.log(f"标题超过限制(50字符)，将被截断: {len(title)} -> 50")
                title = title[:50]
            
            # 2. 标签限制为6个
            if len(tags) > 6:
                self.log(f"标签数量超过限制(6个)，将保留前6个: {len(tags)} -> 6")
                tags = tags[:6]
            
            def _compact_text(text: str, max_len: int) -> str:
                text = (text or "").strip()
                if not text or max_len <= 0:
                    return ""
                # 将多余空白折叠，降低长度波动（网页“字”口径按字符计）
                text = re.sub(r"\s+", " ", text)
                if len(text) <= max_len:
                    return text
                if max_len <= 3:
                    return text[:max_len]
                return text[: max_len - 3] + "..."

            def _build_repost_description(base_desc: str, max_len: int) -> str:
                # 转载：仅保留“转载声明 + 精简摘要”，不附带原简介全文
                copyright_info = "本视频转载自YouTube"
                if original_upload_date:
                    copyright_info += f"，原始上传时间：{original_upload_date}"
                if original_uploader:
                    copyright_info += f"，UP主：{original_uploader}"

                # 先裁剪声明本身，避免占满
                copyright_info = _compact_text(copyright_info, max_len)
                if not base_desc:
                    return copyright_info

                sep = "\n\n"
                available = max_len - len(copyright_info) - len(sep)
                summary = _compact_text(base_desc, max(0, available))
                if not summary:
                    return copyright_info
                return f"{copyright_info}{sep}{summary}"

            # 3. 网页端限制：简介 1000 字、粉丝动态 233 字
            max_desc = 1000
            max_fans_only_desc = 233

            if original_url or original_uploader or original_upload_date:
                full_description = _build_repost_description(description, max_desc)
            else:
                full_description = _compact_text(description, max_desc)

            # 判断视频创作类型
            creation_type = 1 if original_url else 3  # 1:转载, 3:原创
            
            # 创建投稿
            success, result = self.create_douga(
                file_path=video_file_path,
                title=title,
                channel_id=partition_id,
                cover_path=cover_file_path,
                desc=full_description,
                tags=tags,
                creation_type=creation_type,
                original_url=original_url or ""
            )

            # 失败时补充关键字段长度，便于定位是否命中服务端限制
            if not success and isinstance(result, str):
                if "109015" in result or "简介不能超过" in result:
                    self.log(
                        f"投稿失败疑似命中长度限制：description_len={len(full_description)}"
                    )
            
            return success, result
        
        except Exception as e:
            self.log(f"上传过程中发生错误: {str(e)}")
            import traceback
            self.log(traceback.format_exc())
            return False, f"上传过程中发生错误: {str(e)}" 