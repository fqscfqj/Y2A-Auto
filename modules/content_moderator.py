#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import time

# 尝试导入阿里云依赖，如果失败则设置标记
ALIBABA_CLOUD_AVAILABLE = True
try:
    from alibabacloud_green20220302.client import Client as Green20220302Client
    from alibabacloud_green20220302 import models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util.models import RuntimeOptions
except ImportError as e:
    ALIBABA_CLOUD_AVAILABLE = False
    _import_error = str(e)
    # Provide fallbacks for static analyzers (Pylance) so these names are always defined
    from typing import Any
    Green20220302Client: Any = None
    models: Any = None
    open_api_models: Any = None
    RuntimeOptions: Any = None

def setup_task_logger(task_id):
    """
    使用现有的任务日志器，不创建单独的内容审核日志文件
    
    Args:
        task_id: 任务ID
        
    Returns:
        logging.Logger: 任务日志器
    """
    # 导入task_manager中的setup_task_logger
    from modules.task_manager import setup_task_logger as task_setup_logger
    return task_setup_logger(task_id)

class AlibabaCloudModerator:
    """阿里云内容审核类"""
    
    def __init__(self, aliyun_config, task_id=None):
        """
        初始化阿里云内容审核客户端
        
        Args:
            aliyun_config (dict): 阿里云配置信息
            task_id (str, optional): 任务ID，用于日志
        """
        self.aliyun_config = aliyun_config
        self.task_id = task_id
        
        # 设置日志器
        if task_id:
            self.logger = setup_task_logger(task_id)
        else:
            self.logger = logging.getLogger('content_moderator')
        
        # 检查阿里云依赖是否可用
        if not ALIBABA_CLOUD_AVAILABLE:
            self.logger.warning(f"阿里云内容审核依赖未安装: {_import_error}")
            self.logger.warning("内容审核功能将被跳过")
            self.client = None
            return
        
        # 初始化客户端
        self.client = None
        self._create_client()
    
    def _create_client(self):
        """创建阿里云客户端"""
        if not ALIBABA_CLOUD_AVAILABLE:
            return
            
        try:
            # 兼容不同的配置键名格式
            access_key_id = (self.aliyun_config.get('access_key_id') or 
                           self.aliyun_config.get('ALIYUN_ACCESS_KEY_ID') or '')
            access_key_secret = (self.aliyun_config.get('access_key_secret') or 
                               self.aliyun_config.get('ALIYUN_ACCESS_KEY_SECRET') or '')
            
            if not self.aliyun_config or not access_key_id or not access_key_secret:
                self.logger.error("阿里云配置信息不完整，内容审核功能将不可用")
                self.logger.error(f"当前配置: access_key_id={'***有值***' if access_key_id else '无'}, access_key_secret={'***有值***' if access_key_secret else '无'}")
                return
            
            config = open_api_models.Config(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                endpoint='green-cip.cn-shanghai.aliyuncs.com'
            )
            
            self.client = Green20220302Client(config)
            self.logger.info("阿里云内容审核客户端初始化成功")
            
        except Exception as e:
            self.logger.error(f"创建阿里云客户端失败: {str(e)}")
            self.logger.error(f"调试信息：异常类型: {type(e)}")
            import traceback
            self.logger.error(f"调试信息：异常堆栈: {traceback.format_exc()}")
            self.client = None
    
    def moderate_text(self, text_content, service_type='comment_detection_pro'):
        """
        文本内容审核
        
        Args:
            text_content (str): 待审核的文本内容
            service_type (str): 审核服务类型，默认为'comment_detection'
            
        Returns:
            dict: 审核结果
        """
        # 如果阿里云依赖不可用，直接跳过审核
        if not ALIBABA_CLOUD_AVAILABLE:
            self.logger.info("阿里云依赖不可用，跳过内容审核")
            return {
                "pass": True, 
                "details": [{"label": "skipped", "suggestion": "pass", "reason": "阿里云内容审核依赖未安装"}]
            }
        
        if not self.client:
            self.logger.warning("阿里云客户端未初始化，跳过内容审核")
            return {
                "pass": True, 
                "details": [{"label": "skipped", "suggestion": "pass", "reason": "阿里云客户端未初始化"}]
            }
        
        if not text_content or not text_content.strip():
            self.logger.warning("文本内容为空，跳过审核")
            return {"pass": True, "details": []}
        
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
            
            # 检查客户端是否可用
            if self.client is None:
                error_msg = "阿里云客户端未初始化，无法进行文本审核"
                self.logger.error(error_msg)
                return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": error_msg}]}
            
            # 检查方法是否存在
            if not hasattr(self.client, 'text_moderation_plus_with_options'):
                error_msg = "阿里云客户端不支持text_moderation_plus_with_options方法，可能是SDK版本问题"
                self.logger.error(error_msg)
                return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": error_msg}]}
            
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
                
                # 完全依赖阿里云的审核结果，不做额外检测
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
            result = self._moderate_text_segment(segment, service_type)
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
    
    def _moderate_text_segment(self, text_content, service_type):
        """
        审核单个文本段，不进行长度检查和递归处理
        
        Args:
            text_content (str): 待审核的文本内容
            service_type (str): 审核服务类型
            
        Returns:
            dict: 审核结果
        """
        try:
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
            
            # 检查客户端是否可用
            if self.client is None:
                error_msg = "阿里云客户端未初始化，无法进行文本段审核"
                self.logger.error(error_msg)
                return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": error_msg}]}
            
            # 检查方法是否存在
            if not hasattr(self.client, 'text_moderation_plus_with_options'):
                error_msg = "阿里云客户端不支持text_moderation_plus_with_options方法，可能是SDK版本问题"
                self.logger.error(error_msg)
                return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": error_msg}]}
            
            response = self.client.text_moderation_plus_with_options(request, runtime)
            response_time = time.time() - start_time
            
            self.logger.info(f"文本段审核完成，耗时: {response_time:.2f}秒")
            
            # 解析响应
            if response.status_code == 200 and response.body.code == 200:
                # 提取审核结果
                moderation_result = self._parse_text_moderation_response(response.body)
                return moderation_result
            else:
                error_msg = f"文本段审核请求失败，状态码: {response.status_code}, 错误消息: {response.body.message if hasattr(response.body, 'message') else '未知错误'}"
                self.logger.error(error_msg)
                return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": error_msg}]}
                
        except Exception as e:
            error_msg = f"文本段审核过程中发生错误: {str(e)}"
            self.logger.error(error_msg)
            return {"pass": False, "details": [{"label": "error", "suggestion": "review", "reason": error_msg}]}
    
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