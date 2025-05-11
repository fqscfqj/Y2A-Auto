#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import time
from logging.handlers import RotatingFileHandler

from alibabacloud_green20220302.client import Client
from alibabacloud_green20220302 import models
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util.models import RuntimeOptions

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
    logger = logging.getLogger(f'content_moderator_{task_id}')
    
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

class AlibabaCloudModerator:
    """阿里云内容审核类"""
    
    def __init__(self, aliyun_config, task_id=None):
        """
        初始化阿里云内容审核器
        
        Args:
            aliyun_config (dict): 阿里云配置字典，包含access_key_id、access_key_secret和region
            task_id (str, optional): 任务ID，用于日志记录
        """
        self.access_key_id = aliyun_config.get('ALIYUN_ACCESS_KEY_ID')
        self.access_key_secret = aliyun_config.get('ALIYUN_ACCESS_KEY_SECRET')
        self.region = aliyun_config.get('ALIYUN_CONTENT_MODERATION_REGION', 'cn-shanghai')
        self.logger = setup_task_logger(task_id or "unknown")
        
        # 初始化日志
        self.logger.info(f"初始化阿里云内容审核模块，区域：{self.region}")
        
        # 创建阿里云客户端
        self.client = self._create_client()
    
    def _create_client(self):
        """
        创建阿里云内容安全客户端
        
        Returns:
            Client: 阿里云内容安全客户端
        """
        try:
            config = Config(
                access_key_id=self.access_key_id,
                access_key_secret=self.access_key_secret,
                region_id=self.region,
                endpoint=f'green-cip.{self.region}.aliyuncs.com',
                connect_timeout=10000,
                read_timeout=10000
            )
            
            return Client(config)
        except Exception as e:
            self.logger.error(f"创建阿里云客户端失败: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return None
    
    def moderate_text(self, text_content, service_type='comment_detection'):
        """
        审核文本内容
        
        Args:
            text_content (str): 待审核的文本内容
            service_type (str): 审核服务类型，默认为UGC内容检测
                可选值：
                - ugc_moderation_byllm：UGC内容检测（推荐）
                - nickname_detection_pro：用户昵称检测
                - chat_detection_pro：私聊互动内容检测
                - comment_detection_pro：公聊评论内容检测
                - ad_compliance_detection_pro：广告法合规检测
        
        Returns:
            dict: 审核结果，格式为
            {
                "pass": True/False, 
                "details": [
                    {
                        "label": "违规标签", 
                        "suggestion": "block/review/pass", 
                        "reason": "具体原因"
                    }, 
                    ...
                ]
            }
        """
        if not self.client:
            self.logger.error("阿里云客户端未初始化")
            return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": "内容审核服务未初始化"}]}
        
        if not text_content or not text_content.strip():
            self.logger.warning("文本内容为空，跳过审核")
            return {"pass": True, "details": []}
            
        # 首先检查文本是否包含潜在敏感词汇
        sensitive_keywords = [
            "订阅", "关注", "点击链接", "私信", "微信", "联系我", "更多资源",
            "加我", "添加", "群号", "公众号", "频道", "欢迎", "来撩", "加+",
            "投稿", "打赏", "赞助", "咨询", "购买", "出售", "售卖", "广告",
            "优惠", "抽奖", "免费", "特价", "淘宝", "店铺", "联系方式", "联系电话",
            "客服", "营销", "推广", "引流", "商务合作", "官网", "活动", "链接"
        ]
        
        # 检查文本中是否包含敏感词
        has_sensitive_words = False
        detected_words = []
        for keyword in sensitive_keywords:
            if keyword in text_content:
                has_sensitive_words = True
                detected_words.append(keyword)
                
        if has_sensitive_words:
            self.logger.warning(f"文本包含潜在引流/广告词汇: {', '.join(detected_words)}")
        
        # 记录原始文本长度
        self.logger.info(f"开始审核文本，长度: {len(text_content)}")
        self.logger.info(f"文本内容预览: {text_content[:100]}...")
        
        try:
            # 处理超长文本，阿里云文本审核有600字符限制
            if len(text_content) > 600:
                return self._process_long_text(text_content, service_type)
            
            # 准备服务参数
            service_parameters = {
                "content": text_content
            }
            
            # 创建请求
            request = models.TextModerationPlusRequest(
                service=service_type,
                service_parameters=json.dumps(service_parameters)
            )
            
            # 设置运行时选项
            runtime = RuntimeOptions()
            
            # 发送请求
            start_time = time.time()
            response = self.client.text_moderation_plus_with_options(request, runtime)
            response_time = time.time() - start_time
            
            self.logger.info(f"文本审核完成，耗时: {response_time:.2f}秒")
            
            # 记录原始响应以便调试
            response_json = json.dumps(response.body.to_map(), ensure_ascii=False)
            self.logger.info(f"原始响应: {response_json}")
            
            # 解析响应
            if response.status_code == 200 and response.body.code == 200:
                # 提取审核结果
                moderation_result = self._parse_text_moderation_response(response.body)
                self.logger.info(f"文本审核结果: {json.dumps(moderation_result, ensure_ascii=False)}")
                
                # 如果阿里云未检测出问题，但我们的敏感词检测出问题，仍然标记为需要审核
                if moderation_result["pass"] and has_sensitive_words:
                    self.logger.warning("阿里云审核通过，但检测到潜在引流/广告词汇，标记为需要人工审核")
                    moderation_result["pass"] = False
                    moderation_result["details"].append({
                        "label": "pt_to_contact",
                        "description": "疑似引流广告词汇",
                        "confidence": 95,
                        "suggestion": "review",
                        "reason": f"检测到潜在引流/广告词汇: {', '.join(detected_words[:5])}" + ("..." if len(detected_words) > 5 else "")
                    })
                
                return moderation_result
            else:
                error_msg = f"文本审核请求失败，状态码: {response.status_code}, 错误消息: {response.body.message if hasattr(response.body, 'message') else '未知错误'}"
                self.logger.error(error_msg)
                return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": error_msg}]}
        except Exception as e:
            error_msg = f"文本审核过程中发生错误: {str(e)}"
            self.logger.error(error_msg)
            import traceback
            self.logger.error(traceback.format_exc())
            return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": error_msg}]}
    
    def _process_long_text(self, text_content, service_type):
        """
        处理长文本审核，将长文本分段审核
        
        Args:
            text_content (str): 待审核的长文本
            service_type (str): 审核服务类型
            
        Returns:
            dict: 审核结果
        """
        self.logger.info(f"文本长度超过600字符限制，分段处理，总长度: {len(text_content)}")
        
        # 以600字符为单位分段
        text_segments = []
        segment_size = 500  # 稍小于600，确保句子不被截断
        
        for i in range(0, len(text_content), segment_size):
            segment = text_content[i:i+segment_size]
            text_segments.append(segment)
            
        self.logger.info(f"文本分为 {len(text_segments)} 段进行审核")
        
        # 存储所有段落的审核结果
        segment_results = []
        all_pass = True
        
        # 逐段审核
        for index, segment in enumerate(text_segments):
            self.logger.info(f"审核第 {index+1}/{len(text_segments)} 段文本")
            result = self.moderate_text(segment, service_type)
            segment_results.append(result)
            
            # 只要有一段不通过，整体就不通过
            if not result["pass"]:
                all_pass = False
                self.logger.warning(f"第 {index+1} 段文本审核不通过")
        
        # 合并审核结果
        merged_result = {
            "pass": all_pass,
            "details": []
        }
        
        # 收集所有不通过的详细信息
        for result in segment_results:
            if not result["pass"]:
                for detail in result["details"]:
                    merged_result["details"].append(detail)
        
        # 如果通过但没有详细信息，添加默认详情
        if merged_result["pass"] and not merged_result["details"]:
            merged_result["details"].append({
                "label": "normal",
                "description": "长文本内容正常",
                "confidence": None,
                "suggestion": "pass",
                "reason": "所有文本段落审核通过"
            })
            
        return merged_result
    
    def _parse_text_moderation_response(self, response):
        """
        解析文本审核响应
        
        Args:
            response: 阿里云文本审核响应
            
        Returns:
            dict: 解析后的审核结果
        """
        result = {
            "pass": True,
            "details": []
        }
        
        try:
            response_map = response.to_map()
            self.logger.info(f"响应结构: {json.dumps(response_map, ensure_ascii=False)}")
            
            risk_level = "unknown"
            if hasattr(response, "data") and response.data and hasattr(response.data, "risk_level"):
                risk_level = response.data.risk_level
                self.logger.info(f"风险等级: {risk_level}")
            
            if risk_level in ["high", "middle"]:
                result["pass"] = False
            
            if hasattr(response, "data") and response.data and hasattr(response.data, "result") and response.data.result:
                for item_obj in response.data.result: # 重命名避免与外层result冲突
                    item = item_obj.to_map() # 将SDK对象转为字典方便处理
                    self.logger.info(f"处理结果项: {json.dumps(item, ensure_ascii=False)}")
                    
                    label = item.get("Label", "unknown")
                    if label == "nonLabel":
                        continue
                    
                    if label not in ["nonLabel", "normal"]:
                        result["pass"] = False
                    
                    label_desc = ""
                    confidence = item.get("Confidence")
                    detected_keywords = []

                    if item.get("CustomizedHit"):
                        for hit_obj in item.get("CustomizedHit", []):
                            hit = hit_obj.to_map() if hasattr(hit_obj, 'to_map') else hit_obj
                            if hit.get('Keywords'):
                                kw = hit.get('Keywords')
                                if isinstance(kw, list):
                                    detected_keywords.extend(kw)
                                elif isinstance(kw, str):
                                    detected_keywords.extend([k.strip() for k in kw.split(',') if k.strip()])
                    
                    api_risk_words_value = item.get("RiskWords")
                    if api_risk_words_value:
                        if isinstance(api_risk_words_value, str):
                            detected_keywords.extend([k.strip() for k in api_risk_words_value.split(',') if k.strip()])
                        elif isinstance(api_risk_words_value, list):
                            detected_keywords.extend(api_risk_words_value)
                    
                    api_item_description = item.get("Description")
                    
                    if detected_keywords:
                        label_desc = "命中的风险词: " + "，".join(list(set(detected_keywords)))
                    elif api_item_description:
                        label_desc = api_item_description

                    suggestion = "pass"
                    if risk_level == "high":
                        suggestion = "block"
                    elif risk_level == "middle":
                        suggestion = "review"
                    
                    detail = {
                        "label": label,
                        "description": label_desc,
                        "confidence": confidence if confidence is not None else None,
                        "suggestion": suggestion,
                        "reason": f"风险等级: {risk_level}"
                    }
                    result["details"].append(detail)
            
            if not result["pass"] and not result["details"]:
                self.logger.warning(f"审核未通过但没有详细信息: {response_map}")
                result["details"].append({
                    "label": "unknown",
                    "suggestion": "review",
                    "reason": f"未明确原因的风险，风险等级: {risk_level}"
                })
            
            if result["pass"] and not result["details"]:
                result["details"].append({
                    "label": "nonLabel",
                    "suggestion": "pass",
                    "reason": f"内容正常，风险等级: {risk_level}"
                })
            
            return result
            
        except Exception as e:
            self.logger.error(f"解析文本审核响应时出错: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            result["pass"] = False
            result["details"].append({
                "label": "parse_error",
                "suggestion": "review",
                "reason": f"解析审核结果出错: {str(e)}"
            })
            return result 