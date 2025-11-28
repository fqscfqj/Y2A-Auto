#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import time
from logging.handlers import RotatingFileHandler
from .utils import get_app_subdir, strip_reasoning_thoughts, safe_str

import openai

# --- Helpers: logger/client/cleaner (restored) ---
def setup_task_logger(task_id):
    """
    为特定任务设置日志记录器。
    """
    log_dir = get_app_subdir('logs')
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f'task_{task_id}.log')
    logger = logging.getLogger(f'ai_enhancer_{task_id}')

    if not logger.handlers:
        logger.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        logger.propagate = False

    return logger

def get_openai_client(openai_config):
    """
    创建OpenAI客户端。
    """
    api_key = openai_config.get('OPENAI_API_KEY', '')
    options = {}
    if openai_config.get('OPENAI_BASE_URL'):
        options['base_url'] = openai_config.get('OPENAI_BASE_URL')
    return openai.OpenAI(api_key=api_key, **options)

def _pre_clean(text: str) -> str:
    """在发送给模型前做基础去噪：去URL/邮箱/社交句柄/明显CTA等。"""
    if not text:
        return text
    import re
    cleaned = text
    # URLs / domains
    url_patterns = [
        r'https?://[^\s\u4e00-\u9fff]+',
        r'www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
        r'ftp://[^\s\u4e00-\u9fff]+'
    ]
    for pat in url_patterns:
        cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE)
    # emails
    cleaned = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '', cleaned)
    # social handles/hashtags
    cleaned = re.sub(r'@[A-Za-z0-9_]+', '', cleaned)
    cleaned = re.sub(r'#[A-Za-z0-9_]+', '', cleaned)
    # Common CTAs
    ctas = [
        r'订阅[我们的]*[频道]*', r'关注[我们]*', r'点赞[这个]*[视频]*', r'分享[给]*[朋友们]*', r'评论[区]*[见]*',
        r'更多[内容]*请访问', r'详情见[链接]*', r'链接在[描述]*[中]*', r'访问[我们的]*[网站]*', r'查看[完整]*[版本]*',
        r'下载[链接]*', r'购买[链接]*', r'subscribe\s+to\s+[our\s]*channel', r'follow\s+[us\s]*',
        r'like\s+[this\s]*video', r'share\s+[with\s]*[friends\s]*', r'check\s+out\s+[our\s]*[website\s]*',
        r'visit\s+[our\s]*[site\s]*', r'download\s+[link\s]*', r'buy\s+[link\s]*', r'more\s+info\s+at',
        r'see\s+[full\s]*[version\s]*',
    ]
    for pat in ctas:
        cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE)
    # whitespace normalize (keep newlines)
    cleaned = cleaned.replace('\r\n', '\n').replace('\r', '\n')
    cleaned = re.sub(r'[ \t\f\v]+', ' ', cleaned)
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

def translate_text(text, target_language="zh-CN", openai_config=None, task_id=None, content_type: str = "description"):
    """
    使用OpenAI翻译文本（移除温度，强制结构化JSON输出）。
    返回清洗后的翻译文本，失败返回None。
    """
    if not text or not text.strip():
        return text

    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始翻译文本，目标语言: {target_language}")
    logger.info(f"原始文本 (截取前100字符用于显示): {text[:100]}...")
    logger.info(f"原始文本总长度: {len(text)} 字符")

    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.error("缺少OpenAI配置或API密钥")
        return None

    try:
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')

        cleaned_source_text = _pre_clean(text)
        if cleaned_source_text != text:
            logger.info("已在提示阶段前预清洗推广信息与链接等噪声")

        purpose = "标题" if str(content_type).lower() == "title" else "描述"
        prompt = f"""翻译视频{purpose}为{target_language}，移除推广信息，返回JSON。

规则：
1. 移除：URL/邮箱/社交账号/CTA（关注订阅点赞分享等）/联系方式
2. 等价翻译：不解释、不扩写、保持原意和风格
3. 保留：数字/代码/专有名词（无固定译名时）

原文：
{cleaned_source_text}

返回：{{"translation":"译文"}}"""

        start_time = time.time()
        create_kwargs = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": 'JSON翻译器。仅输出{"translation":"..."}，无其他内容。'
                },
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 4096,
        }
        try:
            create_kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass

        response = client.chat.completions.create(**create_kwargs)
        response_time = time.time() - start_time

        message = response.choices[0].message
        # 优先尝试结构化 parsed（部分SDK/服务商在json_object下提供parsed）
        raw = ''
        translation_from_parsed = None
        try:
            parsed = getattr(message, 'parsed', None)
            if isinstance(parsed, dict) and 'translation' in parsed:
                translation_from_parsed = parsed.get('translation')
        except Exception:
            pass
        # 提取文本内容（兼容部分供应商将 content 组织为 list[{type,text}]）
        if not translation_from_parsed:
            mc = getattr(message, 'content', None)
            if isinstance(mc, list):
                try:
                    raw = ''.join([seg.get('text', '') for seg in mc if isinstance(seg, dict)])
                except Exception:
                    raw = ''
            else:
                raw = (mc or getattr(message, 'reasoning_content', None) or '')
        raw = strip_reasoning_thoughts(raw).strip()

        # 去除可能的Markdown围栏
        if raw.startswith('```'):
            try:
                import re as _re
                raw = _re.sub(r'^```[a-zA-Z0-9]*\s*', '', raw)
                raw = _re.sub(r'\s*```$', '', raw)
            except Exception:
                raw = raw.strip('`')

        import json as _json
        import re
        translation_value = translation_from_parsed
        if translation_value is None:
            try:
                data = _json.loads(raw)
                translation_value = data.get('translation') if isinstance(data, dict) else None
            except Exception:
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if m:
                    try:
                        data = _json.loads(m.group(0))
                        translation_value = data.get('translation') if isinstance(data, dict) else None
                    except Exception:
                        pass

        translated_text = (translation_value if isinstance(translation_value, str) else (raw or '')).strip()

        # 移除常见前缀
        for prefix in ["翻译：", "译文：", "这是翻译：", "以下是译文：", "以下是我的翻译："]:
            if translated_text.startswith(prefix):
                translated_text = translated_text[len(prefix):].strip()

        # 清理提示性注释
        for pattern in [
            r'（注：.*?）', r'\(注：.*?\)', r'【注：.*?】', r'（.*?已移除）', r'\(.*?已移除\)',
            r'（.*?联系方式.*?）', r'\(.*?联系方式.*?\)', r'（.*?社交媒体.*?）', r'\(.*?社交媒体.*?\)',
            r'（.*?标签.*?）', r'\(.*?标签.*?\)', r'（.*?链接.*?）', r'\(.*?链接.*?\)',
            r'（.*?推广.*?）', r'\(.*?推广.*?\)', r'（.*?广告.*?）', r'\(.*?广告.*?\)',
            r'（.*?removed.*?）', r'\(.*?removed.*?\)', r'（.*?filtered.*?）', r'\(.*?filtered.*?\)'
        ]:
            translated_text = re.sub(pattern, '', translated_text, flags=re.IGNORECASE)

        logger.info(f"翻译完成，耗时: {response_time:.2f}秒")
        logger.info(f"翻译结果总长度: {len(translated_text)} 字符")
        logger.info(f"翻译结果 (截取前100字符用于显示): {translated_text[:100]}...")

        # 再次防御性清理 URL/邮箱/社交引用
        url_patterns = [
            r'https?://[^\s\u4e00-\u9fff]+',
            r'www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            r'ftp://[^\s\u4e00-\u9fff]+'
        ]
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        social_patterns = [r'@[A-Za-z0-9_]+', r'#[A-Za-z0-9_]+' ]
        for pattern in url_patterns:
            translated_text = re.sub(pattern, '', translated_text, flags=re.IGNORECASE)
        translated_text = re.sub(email_pattern, '', translated_text)
        for pattern in social_patterns:
            translated_text = re.sub(pattern, '', translated_text)

        # 互动提示词
        for pattern in [
            r'订阅[我们的]*[频道]*', r'关注[我们]*', r'点赞[这个]*[视频]*', r'分享[给]*[朋友们]*', r'评论[区]*[见]*',
            r'更多[内容]*请访问', r'详情见[链接]*', r'链接在[描述]*[中]*', r'访问[我们的]*[网站]*', r'查看[完整]*[版本]*',
            r'下载[链接]*', r'购买[链接]*', r'subscribe\s+to\s+[our\s]*channel', r'follow\s+[us\s]*',
            r'like\s+[this\s]*video', r'share\s+[with\s]*[friends\s]*', r'check\s+out\s+[our\s]*[website\s]*',
            r'visit\s+[our\s]*[site\s]*', r'download\s+[link\s]*', r'buy\s+[link\s]*', r'more\s+info\s+at',
            r'see\s+[full\s]*[version\s]*',
        ]:
            translated_text = re.sub(pattern, '', translated_text, flags=re.IGNORECASE)

        # 规范空白但保留换行
        translated_text = translated_text.replace('\r\n', '\n').replace('\r', '\n')
        translated_text = re.sub(r'[ \t\f\v]+', ' ', translated_text)
        translated_text = re.sub(r'[ \t]+\n', '\n', translated_text)
        translated_text = re.sub(r'\n{3,}', '\n\n', translated_text)
        translated_text = translated_text.strip()

        # 若为空或与清理后的原文一致，则尝试一次严格模式重试（强制中文与JSON）
        needs_retry = (not translated_text) or (translated_text.strip() == cleaned_source_text.strip())
        if needs_retry:
            logger.info("首次翻译为空或未改变，进行严格模式重试")
            strict_prompt = f"""翻译为简体中文，移除推广信息，仅返回JSON。

原文：{cleaned_source_text}

返回：{{"translation":"译文"}}"""
            strict_kwargs = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": '仅输出{"translation":"..."}，中文。'},
                    {"role": "user", "content": strict_prompt},
                ],
                "max_tokens": 2048,
            }
            try:
                strict_kwargs["response_format"] = {"type": "json_object"}
            except Exception:
                pass
            try:
                resp2 = client.chat.completions.create(**strict_kwargs)
                msg2 = resp2.choices[0].message
                parsed2 = getattr(msg2, 'parsed', None)
                trans2 = None
                if isinstance(parsed2, dict) and 'translation' in parsed2:
                    trans2 = parsed2.get('translation')
                if not trans2:
                    mc2 = getattr(msg2, 'content', None)
                    if isinstance(mc2, list):
                        try:
                            raw2 = ''.join([seg.get('text', '') for seg in mc2 if isinstance(seg, dict)])
                        except Exception:
                            raw2 = ''
                    else:
                        raw2 = (mc2 or getattr(msg2, 'reasoning_content', None) or '')
                    raw2 = strip_reasoning_thoughts(raw2).strip()
                    import json as _json
                    import re as _re
                    try:
                        data2 = _json.loads(raw2)
                        if isinstance(data2, dict):
                            trans2 = data2.get('translation')
                    except Exception:
                        m2 = _re.search(r'\{.*\}', raw2, _re.DOTALL)
                        if m2:
                            try:
                                data2 = _json.loads(m2.group(0))
                                if isinstance(data2, dict):
                                    trans2 = data2.get('translation')
                            except Exception:
                                pass
                if isinstance(trans2, str) and trans2.strip():
                    translated_text = trans2.strip()
            except Exception as _e2:
                logger.warning(f"严格模式重试失败: {_e2}")

        # 若仍为空，则回退为已清理的原文，避免返回空串
        if not translated_text:
            translated_text = cleaned_source_text

        # 平台长度限制
        ct_lower = str(content_type).lower()
        if ct_lower == 'title':
            if len(translated_text) > 50:
                logger.info(f"标题超过AcFun限制(50字符)，将被截断: {len(translated_text)} -> 50")
                translated_text = translated_text[:50]
        else:
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
    # 防御性处理：确保 title/description 为字符串，避免 None 导致切片/len 时抛出异常
    title = safe_str(title)
    description = safe_str(description)
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

        # 构建标签生成提示（优化版：精简提示词）
        # 截取描述前200字符以减少token
        short_desc = description[:200] if len(description) > 200 else description
        prompt = f"""为视频生成6个AcFun标签（每个≤10汉字）。

标题：{title}
描述：{short_desc}

返回JSON：{{"tags":["标签1","标签2","标签3","标签4","标签5","标签6"]}}"""
        
        start_time = time.time()

        # 使用新版API调用格式
        create_kwargs = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": '标签生成器。仅输出{"tags":[...]}格式的6个标签。'},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 300,
        }
        # 尝试启用结构化JSON输出
        try:
            create_kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass

        response = client.chat.completions.create(**create_kwargs)
        response_time = time.time() - start_time
        logger.info(f"标签生成完成，耗时: {response_time:.2f}秒")

        # 提取响应内容并屏蔽思考
        message = response.choices[0].message
        tags_response = (message.content or getattr(message, 'reasoning_content', None) or '')
        tags_response = strip_reasoning_thoughts(tags_response).strip()

        # 尝试解析JSON
        import json
        import re

        # 去除可能的代码块围栏
        if tags_response.startswith("```"):
            try:
                tags_response = re.sub(r'^```[a-zA-Z0-9]*\s*', '', tags_response)
                tags_response = re.sub(r'\s*```$', '', tags_response)
            except Exception:
                pass

        # 优先解析对象JSON {"tags": [...]} 
        try:
            data = json.loads(tags_response)
            if isinstance(data, dict) and isinstance(data.get('tags'), list):
                tags = data['tags']
            else:
                # 兼容旧格式：直接数组
                tags = data if isinstance(data, list) else None
        except Exception:
            # 兼容：从文本中提取JSON对象或数组
            obj_match = re.search(r'\{[^{}]*\}', tags_response, re.DOTALL)
            arr_match = re.search(r'\[[^\[\]]*\]', tags_response, re.DOTALL)
            raw_json = obj_match.group(0) if obj_match else (arr_match.group(0) if arr_match else None)
            tags = None
            if raw_json:
                try:
                    data = json.loads(raw_json)
                    if isinstance(data, dict) and isinstance(data.get('tags'), list):
                        tags = data['tags']
                    elif isinstance(data, list):
                        tags = data
                except Exception:
                    pass

        if not tags:
            logger.error(f"无法从响应中提取标签: {tags_response}")
            return []

        # 归一化并确保我们有6个标签
        tags = [str(tag).strip() for tag in tags]
        if len(tags) > 6:
            tags = tags[:6]
        elif len(tags) < 6:
            tags.extend([''] * (6 - len(tags)))

        # 确保每个标签不超过长度限制
        tags = [str(tag)[:20] for tag in tags]

        logger.info(f"生成标签: {tags}")
        return tags
    
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
    from typing import Optional
    
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始推荐AcFun视频分区")

    # 防御性归一化
    title = safe_str(title)
    description = safe_str(description)

    # 检查必要信息
    if not title and not description:
        logger.warning("缺少标题和描述，无法推荐分区")
        return None
    
    if not id_mapping_data:
        logger.warning("缺少分区映射数据 (id_mapping_data is empty or None)，无法推荐分区")
        return None
    
    # 将分区数据扁平化为易于处理的列表
    partitions = flatten_partitions(id_mapping_data)
    if not partitions:
        logger.warning("分区映射数据格式错误或为空 (flatten_partitions returned empty list)，无法推荐分区")
        return None
    
    # 如果没有OpenAI配置，直接尝试规则匹配
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.info("缺少OpenAI配置或API密钥，尝试使用规则匹配")
        
        def rule_based_fallback(t: str, d: str) -> Optional[str]:
            """基于简单关键词的回退分类策略。"""
            text = f"{t or ''}\n{d or ''}".lower()
            # 音乐相关
            if any(k in text for k in [' mv', '官方mv', 'official video', 'music', '歌曲', '演唱', '单曲', '专辑', 'mv']):
                for p in partitions:
                    if '综合音乐' in p.get('name', '') or '原创·翻唱' in p.get('name', '') or '演奏·乐器' in p.get('name', ''):
                        return p['id']
            # 舞蹈相关
            if any(k in text for k in ['舞蹈', 'dance', '编舞', '翻跳']):
                for p in partitions:
                    if '综合舞蹈' in p.get('name', '') or '宅舞' in p.get('name', ''):
                        return p['id']
            # 影视预告/花絮
            if any(k in text for k in ['预告', '花絮', 'trailer', 'behind the scenes']):
                for p in partitions:
                    if '预告·花絮' in p.get('name', ''):
                        return p['id']
            # 游戏相关
            if any(k in text for k in ['game', '游戏', '实况', '攻略', '电竞']):
                for p in partitions:
                    if '主机单机' in p.get('name', '') or '电子竞技' in p.get('name', '') or '网络游戏' in p.get('name', ''):
                        return p['id']
            # 科技/数码
            if any(k in text for k in ['科技', '数码', '评测', '开箱', '测评']):
                for p in partitions:
                    if '数码家电' in p.get('name', '') or '科技制造' in p.get('name', ''):
                        return p['id']
            # 生活
            if any(k in text for k in ['vlog', '生活', '美食', '旅行', '宠物']):
                for p in partitions:
                    if '生活日常' in p.get('name', '') or '美食' in p.get('name', '') or '旅行' in p.get('name', ''):
                        return p['id']
            return None
        
        fallback_result = rule_based_fallback(title or '', description or '')
        if fallback_result:
            logger.info(f"规则匹配成功，推荐分区ID: {fallback_result}")
            return fallback_result
        else:
            logger.warning("规则匹配未找到合适的分区")
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
        # 在构造 prompt 时防护 description 的切片
        short_desc = (description[:500] + '...') if len(description) > 500 else description
        prompt = f"""从分区列表选择最匹配的分区。

标题：{title}
描述：{short_desc[:200] if len(short_desc) > 200 else short_desc}

分区列表：
{partitions_text}

返回JSON：{{"id":"分区ID","reason":"理由"}}"""
        
        # 如果配置指定固定分区ID，优先返回
        fixed_pid = (openai_config or {}).get('FIXED_PARTITION_ID')
        if fixed_pid and fixed_pid in [p['id'] for p in partitions]:
            logger.info(f"根据配置固定分区ID直接返回: {fixed_pid}")
            return fixed_pid

        # 先尝试规则直推，尽量一次成功
        pre_rule_id = None
        def _pre_rule_based(title_in, desc_in):
            text = f"{title_in or ''}\n{desc_in or ''}".lower()
            if any(k in text for k in [' mv', '官方mv', 'official video', 'music', '歌曲', '演唱', '单曲', '专辑', 'mv']):
                return (
                    next((p['id'] for p in partitions if '综合音乐' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '原创·翻唱' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '演奏·乐器' in p.get('name','')), None)
                )
            if any(k in text for k in ['舞蹈', 'dance', '编舞', '翻跳']):
                return (
                    next((p['id'] for p in partitions if '综合舞蹈' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '宅舞' in p.get('name','')), None)
                )
            if any(k in text for k in ['预告', '花絮', 'trailer', 'behind the scenes']):
                return next((p['id'] for p in partitions if '预告·花絮' in p.get('name','')), None)
            if any(k in text for k in ['game', '游戏', '实况', '攻略', '电竞']):
                return (
                    next((p['id'] for p in partitions if '主机单机' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '电子竞技' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '网络游戏' in p.get('name','')), None)
                )
            if any(k in text for k in ['科技', '数码', '评测', '开箱', '测评']):
                return (
                    next((p['id'] for p in partitions if '数码家电' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '科技制造' in p.get('name','')), None)
                )
            if any(k in text for k in ['vlog', '生活', '美食', '旅行', '宠物']):
                return (
                    next((p['id'] for p in partitions if '生活日常' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '美食' in p.get('name','')), None) or
                    next((p['id'] for p in partitions if '旅行' in p.get('name','')), None)
                )
            return None
        pre_rule_id = _pre_rule_based(title, description)
        if pre_rule_id:
            logger.info(f"规则优先直接命中分区ID: {pre_rule_id}")
            return pre_rule_id

        # 使用新版API调用格式（尽量结构化）
        create_kwargs = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": '视频分区选择器。仅输出{"id":"...","reason":"..."}。'},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 200,
        }
        try:
            create_kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass

        response = client.chat.completions.create(**create_kwargs)
        
        message = response.choices[0].message
        result = (message.content or getattr(message, 'reasoning_content', None) or '')
        result = strip_reasoning_thoughts(result).strip()
        # 处理可能的Markdown代码块围栏
        if result.startswith('```'):
            # 去除围栏与语言标记
            tmp = result.strip('`')
            tmp = tmp.replace('\njson\n', '\n').replace('\njson', '\n').replace('json\n', '\n')
            result = tmp.strip()
        logger.info(f"分区推荐原始响应: {result}")
        
        # 解析结果
        import json
        import re
        from typing import Optional
        
        available_partition_ids = [p['id'] for p in partitions]

        def extract_first_json_object(text: str) -> Optional[str]:
            """从文本中提取第一个完整的JSON对象（使用括号计数，忽略引号内的括号）。"""
            if not text:
                return None
            start = text.find('{')
            if start == -1:
                return None
            brace = 0
            in_str = False
            esc = False
            for i, ch in enumerate(text[start:], start):
                if esc:
                    esc = False
                    continue
                if ch == '\\':
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                if not in_str:
                    if ch == '{':
                        brace += 1
                    elif ch == '}':
                        brace -= 1
                        if brace == 0:
                            return text[start:i+1]
            return None

        def find_partition_id_by_name(name_sub: str) -> Optional[str]:
            """根据分区名称包含关系查找ID。"""
            name_sub = (name_sub or '').strip()
            if not name_sub:
                return None
            for p in partitions:
                if name_sub in p.get('name', ''):
                    return p['id']
            return None

        def rule_based_fallback(t: str, d: str) -> Optional[str]:
            """基于简单关键词的回退分类策略。"""
            text = f"{t or ''}\n{d or ''}".lower()
            # 音乐相关
            if any(k in text for k in [' mv', '官方mv', 'official video', 'music', '歌曲', '演唱', '单曲', '专辑', 'mv']):
                # 优先 综合音乐 -> 原创·翻唱 -> 演奏·乐器
                return (
                    find_partition_id_by_name('综合音乐') or
                    find_partition_id_by_name('原创·翻唱') or
                    find_partition_id_by_name('演奏·乐器')
                )
            # 舞蹈相关
            if any(k in text for k in ['舞蹈', 'dance', '编舞', '翻跳']):
                return find_partition_id_by_name('综合舞蹈') or find_partition_id_by_name('宅舞')
            # 影视预告/花絮
            if any(k in text for k in ['预告', '花絮', 'trailer', 'behind the scenes']):
                return find_partition_id_by_name('预告·花絮')
            # 游戏相关
            if any(k in text for k in ['game', '游戏', '实况', '攻略', '电竞']):
                return find_partition_id_by_name('主机单机') or find_partition_id_by_name('电子竞技') or find_partition_id_by_name('网络游戏')
            # 科技/数码
            if any(k in text for k in ['科技', '数码', '评测', '开箱', '测评']):
                return find_partition_id_by_name('数码家电') or find_partition_id_by_name('科技制造')
            # 生活
            if any(k in text for k in ['vlog', '生活', '美食', '旅行', '宠物']):
                return find_partition_id_by_name('生活日常') or find_partition_id_by_name('美食') or find_partition_id_by_name('旅行')
            return None

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
            # 如果直接解析失败，尝试从文本中提取JSON（使用括号计数）
            extracted_json_text = extract_first_json_object(result)
            if not extracted_json_text:
                # 退而求其次，使用简单正则（不跨嵌套）
                match = re.search(r'\{[^{}]*\}', result, re.DOTALL)
                extracted_json_text = match.group(0) if match else None
            if extracted_json_text:
                try:
                    data = json.loads(extracted_json_text)
                    if 'id' in data:
                        partition_id = str(data['id'])
                        if partition_id in available_partition_ids:
                            logger.info(f"从提取内容中推荐分区: ID {partition_id}, 理由: {data.get('reason', '无')}")
                            return partition_id
                        else:
                            logger.warning(f"提取内容中推荐的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。提取的文本: {extracted_json_text}")
                except json.JSONDecodeError as e_extract:
                    logger.warning(f"无法从提取的文本中解析JSON: {e_extract}. 提取的文本: {extracted_json_text}")
        
        # 如果上述方法都失败，尝试提取ID
        id_match = re.search(r'"id"\s*:\s*"?(\d+)"?', result)
        if id_match:
            partition_id = id_match.group(1)
            if partition_id in available_partition_ids:
                reason_match = re.search(r'"reason"\s*:\s*"([^"]+)"', result)
                reason = reason_match.group(1) if reason_match else "未提供理由 (正则提取)"
                logger.info(f"正则提取的推荐分区: ID {partition_id}, 理由: {reason}")
                return partition_id
            else:
                logger.warning(f"正则提取的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。原始响应: {result}")

        # 最后尝试：在文本中直接匹配已知ID集合
        joined_ids = '|'.join(re.escape(pid) for pid in available_partition_ids)
        id_any_match = re.search(rf'\b({joined_ids})\b', result)
        if id_any_match:
            pid = id_any_match.group(1)
            logger.info(f"在响应文本中直接匹配到合法分区ID: {pid}")
            return pid

        # 规则回退
        fallback_id = rule_based_fallback(title or '', description or '')
        if fallback_id and fallback_id in available_partition_ids:
            logger.warning(f"无法从OpenAI响应可靠解析，启用规则回退，得到分区ID: {fallback_id}")
            return fallback_id
        
        logger.warning(f"无法从OpenAI响应中解析或验证有效的分区ID。最终原始响应: {result}")
        return None
        
    except Exception as e:
        logger.error(f"推荐分区过程中发生严重错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None 