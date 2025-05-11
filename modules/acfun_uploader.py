#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import time
import logging
import shutil
from base64 import b64decode
from hashlib import sha1
from math import ceil
from mimetypes import guess_type
from logging.handlers import RotatingFileHandler

import requests
import js2py
from modules.utils import process_cover

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
    """AcFun视频上传模块"""
    
    def __init__(self, acfun_username, acfun_password):
        """
        初始化AcFun上传器
        
        Args:
            acfun_username (str): AcFun账号用户名
            acfun_password (str): AcFun账号密码
        """
        self.username = acfun_username
        self.password = acfun_password
        self.context = js2py.EvalJs()
        self.session = requests.session()
        self.logger = None  # 需要在上传时设置
        
        # API端点
        self.LOGIN_URL    = "https://id.app.acfun.cn/rest/web/login/signin"
        self.TOKEN_URL    = "https://member.acfun.cn/video/api/getKSCloudToken"
        self.FRAGMENT_URL = "https://upload.kuaishouzt.com/api/upload/fragment"
        self.COMPLETE_URL = "https://upload.kuaishouzt.com/api/upload/complete"
        self.FINISH_URL   = "https://member.acfun.cn/video/api/uploadFinish"
        self.C_VIDEO_URL  = "https://member.acfun.cn/video/api/createVideo"
        self.C_DOUGA_URL  = "https://member.acfun.cn/video/api/createDouga"
        self.QINIU_URL    = "https://member.acfun.cn/common/api/getQiniuToken"
        self.COVER_URL    = "https://member.acfun.cn/common/api/getUrlAfterUpload"
        self.IMAGE_URL    = "https://imgs.aixifan.com/"
    
    def log(self, *msg):
        """
        记录日志
        
        Args:
            msg: 要记录的消息
        """
        if self.logger:
            self.logger.info(" ".join(str(m) for m in msg))
        else:
            print(f'[{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}]', *msg)
    
    def calc_sha1(self, data: bytes) -> str:
        """
        计算数据的SHA1哈希值
        
        Args:
            data (bytes): 要计算哈希值的数据
            
        Returns:
            str: SHA1哈希值
        """
        sha1_obj = sha1()
        sha1_obj.update(data)
        return sha1_obj.hexdigest()
    
    def login(self):
        """
        登录AcFun账号
        
        Returns:
            bool: 登录是否成功
        """
        self.log("正在登录AcFun账号...")
        r = self.session.post(
            url = self.LOGIN_URL,
            data = {
                'username': self.username,
                'password': self.password,
                'key': '',
                'captcha': ''
            }
        )
        
        response = r.json()
        if response['result'] == 0:
            self.log('登录成功')
            return True
        else:
            self.log(f"登录失败: {response.get('error_msg', '账号密码错误')}")
            return False
    
    def get_token(self, filename: str, filesize: int) -> tuple:
        """
        获取上传令牌
        
        Args:
            filename (str): 文件名
            filesize (int): 文件大小
            
        Returns:
            tuple: (任务ID, 令牌, 分块大小)
        """
        r = self.session.post(
            url=self.TOKEN_URL,
            data={
                "fileName": filename,
                "size": filesize,
                "template": "1"
            }
        )
        response = r.json()
        return response["taskId"], response["token"], response["uploadConfig"]["partSize"]
    
    def upload_chunk(self, block: bytes, fragment_id: int, upload_token: str):
        """
        上传文件分块
        
        Args:
            block (bytes): 分块数据
            fragment_id (int): 分块ID
            upload_token (str): 上传令牌
        """
        # 创建一个新的session用于上传
        upload_session = requests.Session()
        # 配置session
        upload_session.mount('https://', requests.adapters.HTTPAdapter(
            max_retries=3,
            pool_connections=10,
            pool_maxsize=10
        ))
        
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/octet-stream",
            "Origin": "https://member.acfun.cn",
            "Referer": "https://member.acfun.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        }
        
        for attempt in range(3):
            try:
                r = upload_session.post(
                    url=self.FRAGMENT_URL,
                    params={
                        "fragment_id": fragment_id,
                        "upload_token": upload_token
                    },
                    data=block,
                    headers=headers,
                    timeout=30  # 设置超时时间
                )
                
                response = r.json()
                if response["result"] == 1:
                    self.log(f"分块{fragment_id+1}上传成功")
                    return
                else:
                    self.log(f"分块{fragment_id+1}上传失败，重试第{attempt+1}次", r.text)
            except Exception as e:
                self.log(f"分块{fragment_id+1}上传出错，重试第{attempt+1}次", str(e))
        
        self.log(f"分块{fragment_id+1}上传失败，已达到最大重试次数")
    
    def complete(self, fragment_count: int, upload_token: str):
        """
        完成上传
        
        Args:
            fragment_count (int): 分块数量
            upload_token (str): 上传令牌
        """
        # 创建一个新的session用于上传
        upload_session = requests.Session()
        # 配置session
        upload_session.mount('https://', requests.adapters.HTTPAdapter(
            max_retries=3,
            pool_connections=10,
            pool_maxsize=10
        ))
        
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Length": "0",
            "Origin": "https://member.acfun.cn",
            "Referer": "https://member.acfun.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        }
        
        try:
            r = upload_session.post(
                url=self.COMPLETE_URL,
                params={
                    "fragment_count": fragment_count,
                    "upload_token": upload_token
                },
                headers=headers,
                timeout=30  # 设置超时时间
            )
            
            response = r.json()
            if response["result"] != 1:
                self.log(f"完成上传失败: {r.text}")
                return False
            return True
        except Exception as e:
            self.log(f"完成上传出错: {str(e)}")
            return False
    
    def upload_finish(self, taskId: int):
        """
        通知上传完成
        
        Args:
            taskId (int): 任务ID
        """
        r = self.session.post(
            url=self.FINISH_URL,
            data={
                "taskId": taskId
            }
        )
        
        response = r.json()
        if response["result"] != 0:
            self.log(f"上传完成通知失败: {r.text}")
            return False
        return True
    
    def create_video(self, video_key: int, filename: str) -> int:
        """
        创建视频
        
        Args:
            video_key (int): 视频密钥
            filename (str): 文件名
            
        Returns:
            int: 视频ID
        """
        r = self.session.post(
            url=self.C_VIDEO_URL,
            data={
                "videoKey": video_key,
                "fileName": filename,
                "vodType": "ksCloud"
            },
            headers={"origin": "https://member.acfun.cn", "referer": "https://member.acfun.cn/upload-video"}
        )
        
        response = r.json()
        self.log(f"创建视频结果: {r.text}")
        
        if response["result"] != 0:
            self.log(f"创建视频失败: {r.text}")
            return None
        
        self.upload_finish(video_key)
        return response.get("videoId")
    
    def upload_cover(self, image_path: str, mode='crop'):
        """
        处理并上传封面
        
        Args:
            image_path (str): 封面图片路径
            mode (str): 处理模式，'crop'表示裁剪，'pad'表示添加黑边
            
        Returns:
            str: 上传后的封面URL
        """
        self.log(f"处理封面图片: {image_path}")
        
        # 创建临时目录用于处理封面
        temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        # 处理封面
        processed_image = os.path.join(temp_dir, 'processed_cover.jpg')
        process_cover(image_path, processed_image, mode)
        
        # 如果处理封面失败，使用原始封面
        if not os.path.exists(processed_image):
            processed_image = image_path
        
        # 获取文件类型
        image_type = guess_type(processed_image)[0]
        suffix = image_type.split("/")[-1] if image_type else "jpeg"
        
        # 读取图片数据
        with open(processed_image, "rb") as f:
            file_data = f.read()
        
        # 计算SHA1
        file_sha1 = self.calc_sha1(file_data)
        
        # 获取七牛云上传令牌
        def get_qiniu_token(fileName):
            self.log(f"获取七牛云上传令牌: {fileName}")
            r = self.session.post(
                url=self.QINIU_URL,
                data={"fileName": fileName + ".jpeg"}
            )
            response = r.json()
            self.log(f"七牛云上传令牌响应: {r.text}")
            return response["info"]["token"]
        
        # 生成随机文件名
        self.context.execute("""
            function u() {
                var e, t = 0, n = (new Date).getTime().toString(32);
                for (e = 0; e < 5; e++)
                    n += Math.floor(65535 * Math.random()).toString(32);
                return "o_" + n + (t++).toString(32)
            }
        """)
        
        fileName = self.context.u()
        token = get_qiniu_token(fileName)
        
        # 上传封面
        file_size = os.path.getsize(processed_image)
        with open(processed_image, "rb") as f:
            chunk_data = f.read(file_size)
            self.upload_chunk(chunk_data, 0, token)
        
        # 完成上传
        self.complete(1, token)
        
        # 获取上传后的URL
        r = self.session.post(
            url=self.COVER_URL,
            data={"bizFlag": "web-douga-cover", "token": token}
        )
        
        response = r.json()
        cover_url = response.get("url", "")
        
        # 清理临时文件
        try:
            if os.path.exists(processed_image) and processed_image != image_path:
                os.remove(processed_image)
        except:
            pass
        
        return cover_url
    
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
        # 设置任务日志
        self.logger = setup_task_logger(task_id or "unknown")
        self.log(f"开始上传视频: {video_file_path}")
        
        try:
            # 尝试登录
            if not self.login():
                return False, "AcFun登录失败，请检查用户名和密码"
            
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
            
            # 3. 简介限制为1000字符
            if original_url or original_uploader or original_upload_date:
                copyright_info = "本视频转载自YouTube"
                
                if original_upload_date:
                    copyright_info += f"，原始上传时间：{original_upload_date}"
                
                if original_uploader:
                    copyright_info += f"，UP主：{original_uploader}"
                
                full_description = f"{copyright_info}\n\n---原简介---\n{description}"
                
                # 如果超过1000字符，裁剪原始描述部分
                if len(full_description) > 1000:
                    self.log(f"完整简介超过限制(1000字符): {len(full_description)} -> 1000")
                    # 计算版权信息长度
                    copyright_length = len(f"{copyright_info}\n\n---原简介---\n")
                    # 计算剩余可用字符数
                    available_chars = 1000 - copyright_length
                    if available_chars > 0:
                        # 裁剪描述
                        description = description[:available_chars] + "..."
                        full_description = f"{copyright_info}\n\n---原简介---\n{description}"
                    else:
                        # 版权信息已经接近限制，大幅裁剪原始描述
                        self.log("版权信息占用空间较大，原始描述将被大幅裁剪")
                        full_description = full_description[:997] + "..."
            else:
                # 原创视频，只检查简介长度
                if len(description) > 1000:
                    self.log(f"简介超过限制(1000字符)，将被截断: {len(description)} -> 1000")
                    description = description[:997] + "..."
                full_description = description
            
            # 获取文件名和大小
            file_name = os.path.basename(video_file_path)
            file_size = os.path.getsize(video_file_path)
            
            # 获取上传令牌
            task_id, token, part_size = self.get_token(file_name, file_size)
            fragment_count = ceil(file_size / part_size)
            self.log(f"开始上传视频 {file_name}，共 {fragment_count} 个分块")
            
            # 分块上传
            with open(video_file_path, "rb") as f:
                for fragment_id in range(fragment_count):
                    chunk_data = f.read(part_size)
                    if not chunk_data:
                        break
                    self.upload_chunk(chunk_data, fragment_id, token)
            
            # 完成上传
            if not self.complete(fragment_count, token):
                return False, "完成视频上传失败"
            
            # 上传封面
            cover_url = self.upload_cover(cover_file_path, cover_mode)
            if not cover_url:
                return False, "上传封面失败"
            
            # 创建视频
            video_id = self.create_video(task_id, file_name)
            if not video_id:
                return False, "创建视频失败"
            
            # 判断视频创作类型
            creation_type = 1  # 转载
            original_link_url = ""
            
            if original_url:
                creation_type = 1  # 转载
                original_link_url = original_url
            else:
                creation_type = 3  # 原创
            
            # 发布视频
            self.log(f"发布视频: {title}")
            douga_data = {
                "title": title,
                "description": full_description,
                "tagNames": json.dumps(tags),
                "creationType": creation_type,
                "channelId": partition_id,
                "coverUrl": cover_url,
                "videoInfos": json.dumps([{"videoId": video_id, "title": title}]),
                "isJoinUpCollege": "0"
            }
            
            if creation_type == 1:
                douga_data["originalLinkUrl"] = original_link_url
                douga_data["originalDeclare"] = "0"
            else:
                douga_data["originalDeclare"] = "1"
            
            r = self.session.post(
                url=self.C_DOUGA_URL,
                data=douga_data,
                headers={"origin": "https://member.acfun.cn", "referer": "https://member.acfun.cn/upload-video"}
            )
            
            response = r.json()
            if response["result"] == 0 and "dougaId" in response:
                self.log(f"视频投稿成功！AC号：{response['dougaId']}")
                return True, {
                    "ac_number": response['dougaId'],
                    "title": title,
                    "cover_url": cover_url
                }
            else:
                self.log(f"视频投稿失败: {r.text}")
                return False, f"视频投稿失败: {response.get('error_msg', '未知错误')}"
        
        except Exception as e:
            self.log(f"上传过程中发生错误: {str(e)}")
            import traceback
            self.log(traceback.format_exc())
            return False, f"上传过程中发生错误: {str(e)}" 