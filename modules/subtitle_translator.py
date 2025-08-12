#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
import concurrent.futures
from threading import Lock
from .utils import get_app_subdir

logger = logging.getLogger('subtitle_translator')

def setup_task_logger(task_id):
    """
    为特定任务设置日志记录器 (与ai_enhancer.py保持一致)
    
    Args:
        task_id: 任务ID
        
    Returns:
        logger: 配置好的日志记录器
    """
    log_dir = get_app_subdir('logs')
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f'task_{task_id}.log')
    logger = logging.getLogger(f'subtitle_translator_{task_id}')
    
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
    创建OpenAI客户端 (与ai_enhancer.py保持一致)
    
    Args:
        openai_config (dict): OpenAI配置信息，包含api_key, base_url等
        
    Returns:
        OpenAI客户端实例
    """
    import openai
    
    # 配置选项
    api_key = openai_config.get('OPENAI_API_KEY', '')
    options = {}
    
    # 如果提供了base_url，添加到选项中
    if openai_config.get('OPENAI_BASE_URL'):
        options['base_url'] = openai_config.get('OPENAI_BASE_URL')
    
    # 创建并返回新版客户端实例
    return openai.OpenAI(api_key=api_key, **options)

@dataclass
class SubtitleItem:
    """字幕条目"""
    index: int
    start_time: str
    end_time: str
    source_text: str
    translated_text: str = ""
    
    @property
    def time_range(self):
        return f"{self.start_time} --> {self.end_time}"

@dataclass
class TranslationConfig:
    """翻译配置"""
    source_language: str = "auto"
    target_language: str = "zh"
    api_provider: str = "openai"  # 仅支持openai
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-3.5-turbo"
    batch_size: int = 5
    max_retries: int = 3
    retry_delay: int = 2
    max_workers: int = 3  # 新增：最大并发线程数

class SubtitleReader:
    """字幕文件读取器"""
    
    @staticmethod
    def _preprocess_subtitle_text(text: str) -> str:
        """
        前处理字幕文本：将双行或多行字幕改为单行字幕
        
        Args:
            text: 原始字幕文本
            
        Returns:
            str: 处理后的单行字幕文本
        """
        if not text:
            return text
        
        # 移除首尾空白
        text = text.strip()
        
        # 将多行文本合并为单行
        # 使用空格连接不同行，但保留必要的标点符号间距
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        if len(lines) <= 1:
            return text
        
        # 合并多行，智能处理标点符号
        merged_text = ""
        for i, line in enumerate(lines):
            if i == 0:
                merged_text = line
            else:
                # 如果前一行以标点符号结尾，或当前行以标点符号开始，直接连接
                # 否则添加空格
                prev_char = merged_text[-1] if merged_text else ""
                curr_char = line[0] if line else ""
                
                if prev_char in ".,!?;:)]}" or curr_char in ".,!?;:([{":
                    merged_text += line
                else:
                    merged_text += " " + line
        
        logger.info(f"字幕前处理：多行合并为单行")
        logger.debug(f"原文本: {repr(text)}")
        logger.debug(f"处理后: {repr(merged_text)}")
        
        return merged_text
    
    @staticmethod
    def read_srt(file_path: str) -> List[SubtitleItem]:
        """读取SRT字幕文件"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            # SRT格式解析
            pattern = r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\d+\n|\Z)'
            matches = re.findall(pattern, content, re.DOTALL)
            
            items = []
            for match in matches:
                index, start_time, end_time, text = match
                
                # 前处理字幕文本：将多行改为单行
                processed_text = SubtitleReader._preprocess_subtitle_text(text)
                
                if processed_text:  # 只保留有文本内容的条目
                    items.append(SubtitleItem(
                        index=int(index),
                        start_time=start_time,
                        end_time=end_time,
                        source_text=processed_text
                    ))
            
            logger.info(f"SRT文件读取完成，共{len(items)}条字幕（已进行前处理）")
            return items
        except Exception as e:
            logger.error(f"读取SRT文件失败: {e}")
            return []
    
    @staticmethod
    def read_vtt(file_path: str) -> List[SubtitleItem]:
        """读取VTT字幕文件"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            # 移除WEBVTT头部
            lines = content.split('\n')
            if lines[0].startswith('WEBVTT'):
                lines = lines[1:]
            
            # VTT格式解析
            content = '\n'.join(lines)
            pattern = r'(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})\n(.*?)(?=\n\d{2}:\d{2}|\Z)'
            matches = re.findall(pattern, content, re.DOTALL)
            
            items = []
            for i, match in enumerate(matches, 1):
                start_time, end_time, text = match
                
                # 前处理字幕文本：将多行改为单行
                processed_text = SubtitleReader._preprocess_subtitle_text(text)
                
                if processed_text:
                    items.append(SubtitleItem(
                        index=i,
                        start_time=start_time.replace('.', ','),  # 转换为SRT格式
                        end_time=end_time.replace('.', ','),
                        source_text=processed_text
                    ))
            
            logger.info(f"VTT文件读取完成，共{len(items)}条字幕（已进行前处理）")
            return items
        except Exception as e:
            logger.error(f"读取VTT文件失败: {e}")
            return []

class SubtitleWriter:
    """字幕文件输出器"""
    
    @staticmethod
    def write_srt(items: List[SubtitleItem], output_path: str, translated: bool = True):
        """写入SRT字幕文件"""
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                for item in items:
                    text = item.translated_text if translated and item.translated_text else item.source_text
                    f.write(f"{item.index}\n")
                    f.write(f"{item.time_range}\n")
                    f.write(f"{text}\n\n")
            logger.info(f"SRT文件已保存: {output_path}")
        except Exception as e:
            logger.error(f"写入SRT文件失败: {e}")
    
    @staticmethod
    def write_vtt(items: List[SubtitleItem], output_path: str, translated: bool = True):
        """写入VTT字幕文件"""
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write("WEBVTT\n\n")
                for item in items:
                    text = item.translated_text if translated and item.translated_text else item.source_text
                    start_time = item.start_time.replace(',', '.')
                    end_time = item.end_time.replace(',', '.')
                    f.write(f"{start_time} --> {end_time}\n")
                    f.write(f"{text}\n\n")
            logger.info(f"VTT文件已保存: {output_path}")
        except Exception as e:
            logger.error(f"写入VTT文件失败: {e}")

class LLMRequester:
    """LLM请求处理器 (与ai_enhancer.py保持一致的调用方式)"""
    
    def __init__(self, openai_config, task_id=None):
        self.openai_config = openai_config
        self.task_id = task_id or "unknown"
        self.logger = setup_task_logger(self.task_id)
        self.client = None
        self._init_client()
        
        # 线程锁，用于线程安全的日志记录
        self._log_lock = Lock()
    
    def _init_client(self):
        """初始化OpenAI客户端"""
        try:
            if not self.openai_config or not self.openai_config.get('OPENAI_API_KEY'):
                self.logger.error("缺少OpenAI配置或API密钥")
                return
            
            # 使用与ai_enhancer.py相同的客户端创建方式
            self.client = get_openai_client(self.openai_config)
            self.logger.info("OpenAI客户端初始化成功")
            
        except Exception as e:
            self.logger.error(f"初始化OpenAI客户端失败: {e}")
    
    def translate_batch(self, texts: List[str], target_language: str, batch_id: str = "") -> List[str]:
        """批量翻译文本，使用结构化JSON输出"""
        if not self.client or not texts:
            return texts
        
        try:
            # 构建翻译提示词
            system_prompt = self._build_structured_system_prompt(target_language)
            user_prompt = self._build_structured_user_prompt(texts)
            
            model_name = self.openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
            
            start_time = time.time()
            
            with self._log_lock:
                self.logger.info(f"开始翻译批次 {batch_id}，包含 {len(texts)} 条字幕")
            
            # 使用与ai_enhancer.py相同的API调用方式，添加JSON输出格式
            response = self.client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1,  # 降低温度以提高一致性
                max_tokens=4096,
                response_format={"type": "json_object"}  # 强制JSON输出
            )
            
            response_time = time.time() - start_time
            
            with self._log_lock:
                self.logger.info(f"批次 {batch_id} 翻译完成，耗时: {response_time:.2f}秒")
            
            result = response.choices[0].message.content
            
            # 解析结构化翻译结果
            return self._parse_structured_translation_result(result, len(texts), batch_id)
            
        except Exception as e:
            with self._log_lock:
                self.logger.error(f"批次 {batch_id} 翻译请求失败: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
            return texts  # 返回原文本
    
    def _build_structured_system_prompt(self, target_language: str) -> str:
        """构建结构化系统提示词"""
        target_lang_map = {
            "zh": "中文",
            "en": "英文", 
            "ja": "日文",
            "ko": "韩文"
        }
        target_lang_name = target_lang_map.get(target_language, "中文")
        
        return f"""你是一个专业的字幕翻译专家。请将提供的字幕文本逐条翻译成{target_lang_name}。

严格要求：
1. 只输出与原句对应的译文本身，不要添加任何与内容无关的信息（不得添加序号、项目符号、解释、注释、引号包裹、前后缀、额外括号等）。
2. 译文应口语自然、准确传达含义，并保持简洁以适合字幕展示。
3. 保留原文中已有的场景说明或音效标注（如“(audience laughing)”），但禁止新增未出现的说明。
4. 每个输入对应一个输出，长度与顺序严格一致。

输出格式：必须返回有效的JSON，结构如下（仅此一个对象）：
{{
  "translations": [
    "句子1的翻译",
    "句子2的翻译",
    "句子3的翻译"
  ]
}}

注意：
- 严禁输出JSON对象以外的任何文字。
- 严禁在译文前后添加序号（如“1.”、“2.”、“3:”、“1、”等）或任何列表标记。"""
    
    def _build_structured_user_prompt(self, texts: List[str]) -> str:
        """构建结构化用户提示词"""
        # 以JSON形式提供输入，避免模型受列表编号干扰而生成带编号的输出
        payload = {
            "texts": texts
        }
        return (
            "请将下面JSON中texts数组的每个元素翻译为目标语言。"
            "仅返回包含等长translations数组的JSON对象，不要输出任何其他文字。\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
    
    def _parse_structured_translation_result(self, result: str, expected_count: int, batch_id: str) -> List[str]:
        """解析结构化翻译结果"""
        try:
            # 解析JSON响应
            json_result = json.loads(result.strip())
            
            if "translations" not in json_result:
                with self._log_lock:
                    self.logger.warning(f"批次 {batch_id}: JSON响应缺少translations字段")
                return [""] * expected_count
            
            translations = json_result["translations"]
            
            if not isinstance(translations, list):
                with self._log_lock:
                    self.logger.warning(f"批次 {batch_id}: translations不是数组格式")
                return [""] * expected_count
            
            # 确保返回的翻译数量正确
            while len(translations) < expected_count:
                translations.append("")  # 用空字符串填充
            
            # 截断多余的翻译
            final_translations = translations[:expected_count]
            
            with self._log_lock:
                self.logger.info(f"批次 {batch_id}: 成功解析 {len(final_translations)} 条翻译")
            
            return final_translations
            
        except json.JSONDecodeError as e:
            with self._log_lock:
                self.logger.error(f"批次 {batch_id}: JSON解析失败: {e}")
                self.logger.error(f"原始响应: {result[:200]}...")
            
            # 回退到简单解析
            return self._fallback_parse_translation_result(result, expected_count)
        except Exception as e:
            with self._log_lock:
                self.logger.error(f"批次 {batch_id}: 解析翻译结果失败: {e}")
            return [""] * expected_count
    
    def _fallback_parse_translation_result(self, result: str, expected_count: int) -> List[str]:
        """回退解析方法，用于处理非JSON响应"""
        try:
            lines = result.strip().split('\n')
            translations = []
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # 移除可能的序号前缀
                line = re.sub(r'^(\d+\.|\d+、|\(|（)?\d+(\)|）)?\s*[-–—·•]*\s*', '', line)
                # 去除引号包裹
                if (line.startswith('"') and line.endswith('"')) or (line.startswith("'") and line.endswith("'")):
                    line = line[1:-1].strip()
                if line:
                    translations.append(line)
            
            # 确保返回的翻译数量正确
            while len(translations) < expected_count:
                translations.append("")  # 用空字符串填充
            
            return translations[:expected_count]
            
        except Exception as e:
            self.logger.error(f"回退解析翻译结果失败: {e}")
            return [""] * expected_count

class SubtitleTranslator:
    """字幕翻译器主类"""
    
    def __init__(self, config: TranslationConfig, task_id: str = None):
        self.config = config
        self.task_id = task_id or "unknown"
        self.logger = setup_task_logger(self.task_id)
        
        # 构建与ai_enhancer.py兼容的openai_config
        self.openai_config = {
            'OPENAI_API_KEY': config.api_key,
            'OPENAI_BASE_URL': config.base_url,
            'OPENAI_MODEL_NAME': config.model_name
        }
        
        self.llm_requester = LLMRequester(self.openai_config, task_id)
        self.reader = SubtitleReader()
        self.writer = SubtitleWriter()
    
    def translate_file(self, input_path: str, output_path: str, 
                      progress_callback: Optional[callable] = None) -> bool:
        """翻译字幕文件，使用多线程并发翻译"""
        try:
            # 检测文件格式并读取
            file_ext = Path(input_path).suffix.lower()
            if file_ext == '.srt':
                items = self.reader.read_srt(input_path)
            elif file_ext == '.vtt':
                items = self.reader.read_vtt(input_path)
            else:
                self.logger.error(f"不支持的字幕格式: {file_ext}")
                return False
            
            if not items:
                self.logger.error("未读取到字幕内容")
                return False
            
            self.logger.info(f"读取到 {len(items)} 条字幕")
            
            # 并发翻译
            return self._translate_concurrent(items, output_path, progress_callback)
            
        except Exception as e:
            self.logger.error(f"翻译字幕文件失败: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
    
    def _translate_concurrent(self, items: List[SubtitleItem], output_path: str, 
                            progress_callback: Optional[callable] = None) -> bool:
        """使用多线程并发翻译"""
        try:
            total_items = len(items)
            batch_size = self.config.batch_size
            # 允许不设上限：当配置为0或小于1时，按需要的批次数动态分配
            required_workers = max(1, (total_items + batch_size - 1) // batch_size)
            if isinstance(self.config.max_workers, int) and self.config.max_workers > 0:
                max_workers = min(self.config.max_workers, required_workers)
            else:
                max_workers = required_workers
            
            self.logger.info(f"开始并发翻译，批次大小: {batch_size}, 并发线程数: {max_workers}")
            
            # 创建批次
            batches = []
            for i in range(0, total_items, batch_size):
                batch_items = items[i:i + batch_size]
                batch_texts = [item.source_text for item in batch_items]
                batches.append({
                    'batch_id': f"{self.task_id}_{i//batch_size + 1}",
                    'start_index': i,
                    'items': batch_items,
                    'texts': batch_texts
                })
            
            # 进度跟踪
            completed_items = 0
            progress_lock = Lock()
            
            def update_progress(batch_size):
                nonlocal completed_items
                with progress_lock:
                    completed_items += batch_size
                    if progress_callback:
                        progress = (completed_items / total_items) * 100
                        progress_callback(progress, completed_items, total_items)
                    self.logger.info(f"翻译进度: {completed_items}/{total_items} ({progress:.1f}%)")
            
            def translate_batch_worker(batch_info):
                """单个批次翻译工作函数"""
                batch_id = batch_info['batch_id']
                start_index = batch_info['start_index']
                batch_items = batch_info['items']
                batch_texts = batch_info['texts']
                
                # 翻译当前批次，带重试机制
                for retry in range(self.config.max_retries):
                    try:
                        translations = self.llm_requester.translate_batch(
                            batch_texts, 
                            self.config.target_language,
                            batch_id=batch_id
                        )
                        
                        # 将翻译结果赋值给字幕项
                        for j, translation in enumerate(translations):
                            if j < len(batch_items):
                                batch_items[j].translated_text = self._sanitize_translated_text(translation)
                        
                        # 更新进度
                        update_progress(len(batch_items))
                        
                        return True
                        
                    except Exception as e:
                        self.logger.warning(f"批次 {batch_id} 翻译失败 (重试 {retry + 1}/{self.config.max_retries}): {e}")
                        if retry < self.config.max_retries - 1:
                            time.sleep(self.config.retry_delay)
                        else:
                            # 最后一次重试失败，使用原文
                            for j in range(len(batch_items)):
                                batch_items[j].translated_text = batch_items[j].source_text
                            update_progress(len(batch_items))
                            return False
            
            # 使用线程池执行并发翻译
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # 提交所有批次任务
                future_to_batch = {
                    executor.submit(translate_batch_worker, batch): batch
                    for batch in batches
                }
                
                # 等待所有任务完成
                successful_batches = 0
                for future in concurrent.futures.as_completed(future_to_batch):
                    batch = future_to_batch[future]
                    try:
                        success = future.result()
                        if success:
                            successful_batches += 1
                    except Exception as e:
                        self.logger.error(f"批次 {batch['batch_id']} 执行异常: {e}")
                
                self.logger.info(f"并发翻译完成，成功批次: {successful_batches}/{len(batches)}")
            
            # 输出翻译后的文件
            return self._write_translated_file(items, output_path)
            
        except Exception as e:
            self.logger.error(f"并发翻译过程中发生错误: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False

    def _sanitize_translated_text(self, text: str) -> str:
        """清洗译文：移除无关的序号/项目符号/引号，合并重复行"""
        if not text:
            return text
        try:
            # 标准化换行
            lines = [line.strip() for line in str(text).split('\n')]
            cleaned_lines: List[str] = []
            seen: set = set()

            for line in lines:
                if not line:
                    continue
                original = line
                # 反复移除前置编号或项目符号
                while True:
                    new_line = re.sub(r'^(?:[\(（]?\s*\d+\s*[\)）.:、]\s*|[-–—·•]\s+)', '', line)
                    if new_line == line:
                        break
                    line = new_line.strip()

                # 去除整行包裹引号
                if ((line.startswith('"') and line.endswith('"')) or
                    (line.startswith("'") and line.endswith("'")) or
                    (line.startswith('“') and line.endswith('”')) or
                    (line.startswith('‘') and line.endswith('’'))):
                    line = line[1:-1].strip()

                if not line:
                    continue

                # 去重（基于标准化后的小写文本）
                key = line.strip().lower()
                if key in seen:
                    continue
                seen.add(key)
                cleaned_lines.append(line)

            return '\n'.join(cleaned_lines).strip()
        except Exception:
            return text.strip()
    
    def _write_translated_file(self, items: List[SubtitleItem], output_path: str) -> bool:
        """写入翻译后的文件"""
        try:
            output_ext = Path(output_path).suffix.lower()
            if output_ext == '.srt':
                self.writer.write_srt(items, output_path, translated=True)
            elif output_ext == '.vtt':
                self.writer.write_vtt(items, output_path, translated=True)
            else:
                self.logger.error(f"不支持的输出格式: {output_ext}")
                return False
            
            self.logger.info(f"字幕翻译完成: {output_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"写入翻译文件失败: {e}")
            return False
    
    def get_subtitle_preview(self, file_path: str, max_items: int = 5) -> List[Dict]:
        """获取字幕预览"""
        try:
            file_ext = Path(file_path).suffix.lower()
            if file_ext == '.srt':
                items = self.reader.read_srt(file_path)
            elif file_ext == '.vtt':
                items = self.reader.read_vtt(file_path)
            else:
                return []
            
            preview_items = items[:max_items]
            return [
                {
                    'index': item.index,
                    'time_range': item.time_range,
                    'text': item.source_text
                }
                for item in preview_items
            ]
            
        except Exception as e:
            self.logger.error(f"获取字幕预览失败: {e}")
            return []

# 工厂函数
def create_translator_from_config(app_config: Dict, task_id: str = None) -> Optional[SubtitleTranslator]:
    """从应用配置创建翻译器 (与ai_enhancer.py保持一致的配置格式)"""
    try:
        # 确保数值配置被正确转换为整数
        batch_size = app_config.get('SUBTITLE_BATCH_SIZE', 5)
        if isinstance(batch_size, str):
            batch_size = int(batch_size)
        
        max_retries = app_config.get('SUBTITLE_MAX_RETRIES', 3)
        if isinstance(max_retries, str):
            max_retries = int(max_retries)
        
        retry_delay = app_config.get('SUBTITLE_RETRY_DELAY', 2)
        if isinstance(retry_delay, str):
            retry_delay = int(retry_delay)
        
        max_workers = app_config.get('SUBTITLE_MAX_WORKERS', 3)
        if isinstance(max_workers, str):
            max_workers = int(max_workers)
        
        # 计算字幕翻译专用Base URL（优先使用SUBTITLE_OPENAI_BASE_URL，否则回退到OPENAI_BASE_URL）
        subtitle_base_url = app_config.get('SUBTITLE_OPENAI_BASE_URL') or app_config.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')

        # 计算字幕翻译专用Key/模型，未配置则回退通用值
        subtitle_api_key = app_config.get('SUBTITLE_OPENAI_API_KEY') or app_config.get('OPENAI_API_KEY', '')
        subtitle_model = app_config.get('SUBTITLE_OPENAI_MODEL_NAME') or app_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')

        translation_config = TranslationConfig(
            source_language=app_config.get('SUBTITLE_SOURCE_LANGUAGE', 'auto'),
            target_language=app_config.get('SUBTITLE_TARGET_LANGUAGE', 'zh'),
            api_provider=app_config.get('SUBTITLE_API_PROVIDER', 'openai'),
            api_key=subtitle_api_key,
            base_url=subtitle_base_url,
            model_name=subtitle_model,
            batch_size=batch_size,
            max_retries=max_retries,
            retry_delay=retry_delay,
            max_workers=max_workers
        )
        
        if not translation_config.api_key:
            logger.error("未配置API密钥，无法创建翻译器")
            return None
        
        return SubtitleTranslator(translation_config, task_id)
        
    except Exception as e:
        logger.error(f"创建翻译器失败: {e}")
        return None 