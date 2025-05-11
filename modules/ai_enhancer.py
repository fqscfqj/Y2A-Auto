#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import time
from logging.handlers import RotatingFileHandler

import openai

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
    logger = logging.getLogger(f'ai_enhancer_{task_id}')
    
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

def get_openai_client(openai_config):
    """
    创建OpenAI客户端 (兼容 API 1.x 版本)
    
    Args:
        openai_config (dict): OpenAI配置信息，包含api_key, base_url等
        
    Returns:
        OpenAI客户端实例
    """
    # 配置选项
    api_key = openai_config.get('OPENAI_API_KEY', '')
    options = {}
    
    # 如果提供了base_url，添加到选项中
    if openai_config.get('OPENAI_BASE_URL'):
        options['base_url'] = openai_config.get('OPENAI_BASE_URL')
    
    # 创建并返回新版客户端实例
    return openai.OpenAI(api_key=api_key, **options)

def translate_text(text, target_language="zh-CN", openai_config=None, task_id=None):
    """
    使用OpenAI翻译文本
    
    Args:
        text (str): 待翻译的文本
        target_language (str): 目标语言代码，默认为简体中文
        openai_config (dict): OpenAI配置信息，包含api_key, base_url, model_name等
        task_id (str, optional): 任务ID，用于日志记录
        
    Returns:
        str or None: 翻译后的文本，出错时返回None
    """
    if not text or not text.strip():
        return text
    
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始翻译文本，目标语言: {target_language}")
    # 仅日志中显示部分内容，实际翻译用完整文本
    logger.info(f"原始文本 (截取前100字符用于显示): {text[:100]}...")
    logger.info(f"原始文本总长度: {len(text)} 字符")
    
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.error("缺少OpenAI配置或API密钥")
        return None
    
    try:
        # 获取OpenAI客户端 (1.x版本)
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        language_map = {
            'zh-CN': '简体中文',
            'zh-TW': '繁体中文',
            'en': '英语',
            'ja': '日语',
            'ko': '韩语',
            'es': '西班牙语',
            'fr': '法语',
            'de': '德语',
            'ru': '俄语'
        }
        
        target_language_name = language_map.get(target_language, target_language)
        
        # 构建翻译提示 - 使用完整文本
        prompt = f"""我需要你将以下文本翻译成{target_language_name}。请遵循以下严格要求：

1. **纯净译文：** 直接输出翻译结果，不含任何额外解释、注释、元数据或前缀（如"翻译："）。
2. **语言风格：**
   * 使用规范、正式的书面语，避免过度口语化表达
   * 不使用"开整"、"搞起"等网络流行语
   * 不添加不必要的感叹词或表情符号
   * 保持专业、清晰的表达方式
3. **AcFun规范：**
   * 移除所有URL、邮箱地址、广告及社交媒体推广信息
   * 使用符合中文习惯的表达，但避免过度本地化改写
4. **格式保留：** 保持原文的段落结构
5. **内容限制：** 最终标题需<50字，简介需<1000字

原文:
{text}
"""
        
        start_time = time.time()
        
        # 使用新版API调用格式
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一个专业翻译工具。你的工作是提供准确、规范的翻译，使用正式的书面语言风格，避免过度口语化和网络流行语。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=4096
        )
        
        response_time = time.time() - start_time
        
        # 新版API响应格式
        translated_text = response.choices[0].message.content.strip()
        
        # 检查并移除可能的前缀和注释
        prefixes_to_remove = ["翻译：", "译文：", "这是翻译：", "以下是译文：", "以下是我的翻译："]
        for prefix in prefixes_to_remove:
            if translated_text.startswith(prefix):
                translated_text = translated_text[len(prefix):].strip()
        
        # 移除可能的注释部分 (通常在括号内，且包含"注："等提示词)
        import re
        translated_text = re.sub(r'（注：.*?）', '', translated_text)
        translated_text = re.sub(r'\(注：.*?\)', '', translated_text)
        translated_text = re.sub(r'【注：.*?】', '', translated_text)
        
        logger.info(f"翻译完成，耗时: {response_time:.2f}秒")
        logger.info(f"翻译结果总长度: {len(translated_text)} 字符")
        logger.info(f"翻译结果 (截取前100字符用于显示): {translated_text[:100]}...")
        
        # 过滤URL和邮箱地址
        import re
        # URL正则表达式
        url_pattern = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+'
        # 邮箱正则表达式
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        
        # 替换URL和邮箱
        translated_text = re.sub(url_pattern, '', translated_text)
        translated_text = re.sub(email_pattern, '', translated_text)
        # 删除可能存在的多余空行
        translated_text = re.sub(r'\n{3,}', '\n\n', translated_text)
        
        logger.info("已过滤URL和邮箱地址")
        
        # 处理字符限制
        if task_id and "title" in task_id.lower():
            # 如果是标题，限制为50个字符
            if len(translated_text) > 50:
                logger.info(f"标题超过AcFun限制(50字符)，将被截断: {len(translated_text)} -> 50")
                translated_text = translated_text[:50]
        else:
            # 如果是描述，限制为1000个字符
            if len(translated_text) > 1000:
                logger.info(f"描述超过AcFun限制(1000字符)，将被截断: {len(translated_text)} -> 1000")
                translated_text = translated_text[:997] + "..."
        
        return translated_text
    
    except Exception as e:
        logger.error(f"翻译过程中发生错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def generate_acfun_tags(title, description, openai_config=None, task_id=None):
    """
    使用OpenAI生成AcFun风格的标签
    
    Args:
        title (str): 视频标题
        description (str): 视频描述
        openai_config (dict): OpenAI配置信息，包含api_key, base_url, model_name等
        task_id (str, optional): 任务ID，用于日志记录
        
    Returns:
        list: 标签列表，出错时返回空列表
    """
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始生成AcFun标签")
    logger.info(f"视频标题: {title}")
    logger.info(f"视频描述 (截取前100字符用于显示): {description[:100]}...")
    logger.info(f"视频描述总长度: {len(description)} 字符")
    
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.error("缺少OpenAI配置或API密钥")
        return []
    
    try:
        # 获取OpenAI客户端 (1.x版本)
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        # 构建标签生成提示
        prompt = f"""根据以下视频的标题和描述，生成恰好6个适合AcFun平台的标签。
        要求:
        - 必须生成6个标签，不多不少
        - 每个标签长度不超过10个汉字或20个字符
        - 标签应反映视频的核心内容、类型或情感
        - 避免过于宽泛的标签如"搞笑"、"有趣"等
        - 包含1-2个与视频主题相关的基础关键词
        
        视频标题:
        {title}
        
        视频描述:
        {description}
        
        JSON格式返回6个标签，例如:
        ["标签1", "标签2", "标签3", "标签4", "标签5", "标签6"]
        
        只返回JSON数组，不要有其他内容:
        """
        
        start_time = time.time()
        
        # 使用新版API调用格式
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一个内容标签生成工具。你的任务是为视频内容生成恰当的标签，以帮助用户更好地发现和分类内容。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=800
        )
        
        response_time = time.time() - start_time
        logger.info(f"标签生成完成，耗时: {response_time:.2f}秒")
        
        # 提取响应内容
        tags_response = response.choices[0].message.content.strip()
        
        # 尝试解析JSON
        import json
        import re
        
        # 清理响应文本，确保它是有效的JSON
        # 有时API可能返回带有额外文本的JSON，尝试提取JSON部分
        json_pattern = r'\[.*?\]'
        json_match = re.search(json_pattern, tags_response, re.DOTALL)
        
        if json_match:
            try:
                tags = json.loads(json_match.group())
                # 确保我们有6个标签
                if len(tags) > 6:
                    tags = tags[:6]
                elif len(tags) < 6:
                    # 如果少于6个，用空字符串填充
                    tags.extend([''] * (6 - len(tags)))
                
                # 确保每个标签不超过长度限制
                tags = [tag[:20] for tag in tags]
                
                logger.info(f"生成标签: {tags}")
                return tags
            except json.JSONDecodeError as e:
                logger.error(f"解析标签JSON时出错: {str(e)}")
                logger.error(f"原始响应: {tags_response}")
        else:
            logger.error(f"无法从响应中提取JSON数组: {tags_response}")
        
        return []
    
    except Exception as e:
        logger.error(f"生成标签过程中发生错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return []

def flatten_partitions(id_mapping_data):
    """
    将id_mapping_data扁平化为分区列表
    
    Args:
        id_mapping_data (list): id_mapping.json解析后的数据
        
    Returns:
        list: 分区列表，每个元素包含id, name等信息
    """
    if not id_mapping_data:
        return []
        
    partitions = []
    
    for category_item in id_mapping_data:
        # 兼容两种格式："name"或"category"作为分类名称
        category_name = category_item.get('name', '') or category_item.get('category', '')
        for partition in category_item.get('partitions', []):
            # 记录一级分区信息
            partition_id = partition.get('id')
            partition_name = partition.get('name', '')
            partition_desc = partition.get('description', '')
            
            if partition_id:
                partitions.append({
                    'id': partition_id,
                    'name': partition_name,
                    'description': partition_desc,
                    'parent_name': category_name
                })
            
            # 处理二级分区
            for sub_partition in partition.get('sub_partitions', []):
                sub_id = sub_partition.get('id')
                sub_name = sub_partition.get('name', '')
                sub_desc = sub_partition.get('description', '')
                
                if sub_id:
                    partitions.append({
                        'id': sub_id,
                        'name': sub_name,
                        'description': sub_desc,
                        'parent_name': partition_name
                    })
    
    return partitions

def recommend_acfun_partition(title, description, id_mapping_data, openai_config=None, task_id=None):
    """
    使用OpenAI推荐AcFun视频分区
    
    Args:
        title (str): 视频标题
        description (str): 视频描述
        id_mapping_data (list): 分区ID映射数据
        openai_config (dict): OpenAI配置信息
        task_id (str, optional): 任务ID，用于日志记录
        
    Returns:
        str or None: 推荐分区ID，出错时返回None
    """
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始推荐AcFun视频分区")
    
    # 检查必要信息
    if not title and not description:
        logger.warning("缺少标题和描述，无法推荐分区")
        return None
    
    if not id_mapping_data:
        logger.warning("缺少分区映射数据 (id_mapping_data is empty or None)，无法推荐分区")
        return None
    
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.warning("缺少OpenAI配置或API密钥，无法推荐分区")
        return None
    
    # 将分区数据扁平化为易于处理的列表
    partitions = flatten_partitions(id_mapping_data)
    if not partitions:
        logger.warning("分区映射数据格式错误或为空 (flatten_partitions returned empty list)，无法推荐分区")
        return None
    
    try:
        # 获取OpenAI客户端 (1.x版本)
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        # 准备分区描述信息
        partitions_info = []
        for p in partitions:
            parent_name = p.get('parent_name', '') 
            prefix = f"{parent_name} - " if parent_name else ""
            partitions_info.append(f"{prefix}{p['name']} (ID: {p['id']}): {p.get('description', '无描述')}")
        
        partitions_text = "\n".join(partitions_info)
        
        # 构建提示内容
        prompt = f"""请根据以下视频的标题和描述，从给定的AcFun分区列表中，选择最合适的一个分区。

视频标题: {title}

视频描述: {description[:500] + '...' if len(description) > 500 else description}

AcFun分区列表:
{partitions_text}

要求:
1. 只能选择上述列表中的一个分区
2. 分析视频内容与分区的匹配度
3. 只返回一个分区ID，格式为:
{{"id": "分区ID", "reason": "简要推荐理由"}}

不要返回任何其他格式或额外内容。
"""
        
        # 使用新版API调用格式
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一个专业视频分类助手，擅长将视频内容匹配到最合适的分区。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=800
        )
        
        result = response.choices[0].message.content.strip()
        logger.info(f"分区推荐原始响应: {result}")
        
        # 解析结果
        import json
        import re
        
        available_partition_ids = [p['id'] for p in partitions]

        # 尝试直接解析JSON
        try:
            data = json.loads(result)
            if 'id' in data:
                # 验证ID是否存在于分区列表中
                partition_id = str(data['id'])
                if partition_id in available_partition_ids:
                    logger.info(f"推荐分区: ID {partition_id}, 理由: {data.get('reason', '无')}")
                    # 直接返回分区ID字符串，而不是整个字典
                    return partition_id
                else:
                    logger.warning(f"推荐的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。原始响应: {result}")
        except json.JSONDecodeError as e_direct:
            logger.warning(f"直接解析JSON响应失败: {e_direct}. 原始响应: {result}")
            # 如果直接解析失败，尝试从文本中提取JSON
            match = re.search(r'\\{.*\\}', result, re.DOTALL)
            if match:
                extracted_json_text = match.group(0)
                try:
                    data = json.loads(extracted_json_text)
                    if 'id' in data:
                        # 验证ID是否存在于分区列表中
                        partition_id = str(data['id'])
                        if partition_id in available_partition_ids:
                            logger.info(f"从提取内容中推荐分区: ID {partition_id}, 理由: {data.get('reason', '无')}")
                            # 直接返回分区ID字符串，而不是整个字典
                            return partition_id
                        else:
                            logger.warning(f"提取内容中推荐的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。提取的文本: {extracted_json_text}")
                except json.JSONDecodeError as e_extract:
                    logger.warning(f"无法从提取的文本中解析JSON: {e_extract}. 提取的文本: {extracted_json_text}")
        
        # 如果上述方法都失败，尝试提取ID
        id_match = re.search(r'"id"\\s*:\\s*"?(\\d+)"?', result)
        if id_match:
            partition_id = id_match.group(1)
            if partition_id in available_partition_ids:
                reason_match = re.search(r'"reason"\\s*:\\s*"([^"]+)"', result)
                reason = reason_match.group(1) if reason_match else "未提供理由 (正则提取)"
                logger.info(f"正则提取的推荐分区: ID {partition_id}, 理由: {reason}")
                return partition_id
            else:
                logger.warning(f"正则提取的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。原始响应: {result}")
        
        logger.warning(f"无法从OpenAI响应中解析或验证有效的分区ID。最终原始响应: {result}")
        return None
        
    except Exception as e:
        logger.error(f"推荐分区过程中发生严重错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None 