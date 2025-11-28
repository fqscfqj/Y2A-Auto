#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import logging
import gc  # 添加垃圾回收模块以优化内存使用
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
import concurrent.futures
from threading import Lock
from .utils import get_app_subdir, strip_reasoning_thoughts

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
        
        # 文件处理器 - 减少文件大小以降低内存使用
        file_handler = RotatingFileHandler(log_file, maxBytes=5242880, backupCount=3, encoding='utf-8')
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
    batch_size: int = 3  # 减少批次大小以降低内存使用
    max_retries: int = 3
    retry_delay: int = 2
    max_workers: int = 2  # 减少最大并发线程数以降低内存使用

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
        """读取SRT字幕文件（兼容更宽松的SRT变体与ASR输出）"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw = f.read()

            content = raw.strip()
            if not content:
                return []

            # 标准化换行
            content = content.replace('\r\n', '\n').replace('\r', '\n')

            # 先尝试严格格式：带编号的块
            # 小时位放宽为1-2位，兼容 0:00:01,920 与 00:00:01,920
            pattern_strict = r'(\d+)\n(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\d+\n|\Z)'
            matches = re.findall(pattern_strict, content, re.DOTALL)

            blocks: List[SubtitleItem] = []
            if matches:
                for index, start_time, end_time, text in matches:
                    processed_text = SubtitleReader._preprocess_subtitle_text(text)
                    if processed_text:
                        # 统一时间为SRT逗号毫秒
                        st = start_time.replace('.', ',')
                        et = end_time.replace('.', ',')
                        blocks.append(SubtitleItem(
                            index=int(index),
                            start_time=st,
                            end_time=et,
                            source_text=processed_text
                        ))
            else:
                # 回退解析：部分ASR会输出无编号的SRT块，仅时间行 + 文本
                pattern_loose = r'(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\d{1,2}:\d{2}:\d{2}|\Z)'
                loose_matches = re.findall(pattern_loose, content, re.DOTALL)
                for i, (start_time, end_time, text) in enumerate(loose_matches, 1):
                    processed_text = SubtitleReader._preprocess_subtitle_text(text)
                    if processed_text:
                        st = start_time.replace('.', ',')
                        et = end_time.replace('.', ',')
                        blocks.append(SubtitleItem(
                            index=i,
                            start_time=st,
                            end_time=et,
                            source_text=processed_text
                        ))

            logger.info(f"SRT文件读取完成，共{len(blocks)}条字幕（已进行前处理）")
            return blocks
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
    
    _TRAILING_PUNCTUATION = re.compile(r'[,.，。．]+$')

    @classmethod
    def _strip_trailing_punctuation(cls, text: str) -> str:
        """移除行尾的逗号或句号，保留其他标点。"""
        if not text:
            return text
        cleaned_lines: List[str] = []
        for line in text.split('\n'):
            stripped_line = line.rstrip()
            if not stripped_line:
                cleaned_lines.append(stripped_line)
                continue
            # 仅当去除逗号/句号后仍有内容时才执行，避免将省略号等特殊标记清空
            candidate = cls._TRAILING_PUNCTUATION.sub('', stripped_line)
            cleaned_lines.append(candidate if candidate.strip() else stripped_line)
        return '\n'.join(cleaned_lines)

    @staticmethod
    def write_srt(items: List[SubtitleItem], output_path: str, translated: bool = True):
        """写入SRT字幕文件"""
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                for item in items:
                    text = item.translated_text if translated and item.translated_text else item.source_text
                    text = SubtitleWriter._strip_trailing_punctuation(text)
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
                    text = SubtitleWriter._strip_trailing_punctuation(text)
                    start_time = item.start_time.replace(',', '.')
                    end_time = item.end_time.replace(',', '.')
                    f.write(f"{start_time} --> {end_time}\n")
                    f.write(f"{text}\n\n")
            logger.info(f"VTT文件已保存: {output_path}")
        except Exception as e:
            logger.error(f"写入VTT文件失败: {e}")

class LLMRequester:
    """LLM请求处理器 (与ai_enhancer.py保持一致的调用方式)"""
    
    def __init__(self, openai_config, task_id: Optional[str] = None):
        self.openai_config = openai_config
        self.task_id = task_id or "unknown"
        self.logger = setup_task_logger(self.task_id)
        self.client = None
        self._init_client()
        
        # 线程锁，用于线程安全的日志记录
        self._log_lock = Lock()
        self._batch_counter = 0
        self._batch_log_interval = 10
    
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
            self._batch_counter += 1
            log_as_info = self._should_log_batch(batch_id)
            # 构建翻译提示词
            system_prompt = self._build_structured_system_prompt(target_language)
            user_prompt = self._build_structured_user_prompt(texts)
            
            model_name = self.openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
            
            start_time = time.time()
            
            with self._log_lock:
                self.logger.log(
                    logging.INFO if log_as_info else logging.DEBUG,
                    f"开始翻译批次 {batch_id}，包含 {len(texts)} 条字幕"
                )
            
            # 使用与ai_enhancer.py相同的API调用方式，添加JSON输出格式
            response = self.client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=4096,
                response_format={"type": "json_object"}  # 强制JSON输出
            )
            
            response_time = time.time() - start_time
            
            with self._log_lock:
                self.logger.log(
                    logging.INFO if log_as_info else logging.DEBUG,
                    f"批次 {batch_id} 翻译完成，耗时: {response_time:.2f}秒"
                )
            
            message = response.choices[0].message
            # 优先使用最终答案；缺失时回退到 reasoning_content，并屏蔽 <think>
            result = (message.content or getattr(message, 'reasoning_content', None) or '')
            result = strip_reasoning_thoughts(result)
            
            # 解析结构化翻译结果
            return self._parse_structured_translation_result(result, len(texts), batch_id)
            
        except Exception as e:
            with self._log_lock:
                self.logger.error(f"批次 {batch_id} 翻译请求失败: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
            return texts  # 返回原文本

    def _should_log_batch(self, batch_id: str) -> bool:
        """控制批次日志的详细程度，减少日志文件体积。"""
        try:
            if batch_id.startswith('repair'):
                return True
            if self._batch_counter <= 2:
                return True
            return (self._batch_counter % self._batch_log_interval) == 0
        except Exception:
            return True

    def translate_batch_strict(self, texts: List[str], target_language: str, batch_id: str = "") -> List[str]:
        """严格模式批量翻译：用于补救仍未译的条目，强制全中文输出。"""
        if not self.client or not texts:
            return texts
        try:
            system_prompt = self._build_strict_structured_system_prompt(target_language)
            user_prompt = self._build_structured_user_prompt(texts)
            model_name = self.openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
            with self._log_lock:
                self.logger.info(f"开始严格模式翻译批次 {batch_id}，包含 {len(texts)} 条字幕")
            response = self.client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=4096,
                response_format={"type": "json_object"}
            )
            message = response.choices[0].message
            result = (message.content or getattr(message, 'reasoning_content', None) or '')
            result = strip_reasoning_thoughts(result)
            return self._parse_structured_translation_result(result, len(texts), batch_id)
        except Exception as e:
            with self._log_lock:
                self.logger.error(f"严格模式批次 {batch_id} 翻译失败: {e}")
            return texts
    
    def _build_structured_system_prompt(self, target_language: str) -> str:
        """构建结构化系统提示词 - 优化版：精简规则，减少token消耗"""
        target_lang_map = {
            "zh": "中文",
            "en": "English",
            "ja": "日本語",
            "ko": "한국어",
        }
        target_lang_name = target_lang_map.get(target_language, "中文")

        return f"""你是字幕翻译器。将每条字幕翻译成{target_lang_name}，返回JSON。

核心规则：
1. 等价翻译：不解释、不扩写、不改写，保持原意
2. 一一对应：输入N条，输出N条，顺序不变
3. 保留原样：数字/代码/占位符/专有名词（无固定译名时）
4. 自然表达：用{target_lang_name}口语，适合字幕阅读
5. 完整翻译：除专有名词外，不保留整句原文

仅返回JSON：
{{"translations":["译文1","译文2",...]}}"""

    def _build_strict_structured_system_prompt(self, target_language: str) -> str:
        """严格模式提示词：强制完整翻译，用于补救未译条目"""
        target_lang_map = {
            "zh": "中文",
            "en": "English",
            "ja": "日本語",
            "ko": "한국어",
        }
        target_lang_name = target_lang_map.get(target_language, "中文")
        
        return f"""你是字幕翻译器（严格模式）。将每条字幕完整翻译成{target_lang_name}。

强制要求：
1. 每条必须完整翻译，禁止保留原文
2. 一一对应：输入N条输出N条
3. 仅保留数字和代码占位符

仅返回：{{"translations":["译文1","译文2",...]}}"""
    
    def _build_structured_user_prompt(self, texts: List[str]) -> str:
        """构建结构化用户提示词 - 优化版：系统提示已包含规则，此处仅提供数据"""
        return json.dumps({"texts": texts}, ensure_ascii=False)

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
                # 回退到简单解析
                return self._fallback_parse_translation_result(result, expected_count)
            
            # 确保返回的翻译数量正确
            while len(translations) < expected_count:
                translations.append("")  # 用空字符串填充
            
            # 截断多余的翻译
            final_translations = translations[:expected_count]
            
            with self._log_lock:
                self.logger.info(f"批次 {batch_id}: 成功解析 {len(final_translations)} 条翻译")
            
            return final_translations
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
    
    def __init__(self, config: TranslationConfig, task_id: Optional[str] = None):
        self.config = config
        self.task_id = task_id or "unknown"
        self.logger = setup_task_logger(self.task_id)
        
        # 添加调试日志：检查配置值是否为 None
        self.logger.debug(f"配置参数检查 - api_key: {config.api_key is None}, base_url: {config.base_url is None}, model_name: {config.model_name is None}")
        
        # 构建与ai_enhancer.py兼容的openai_config，确保不为 None
        self.openai_config = {
            'OPENAI_API_KEY': config.api_key or '',
            'OPENAI_BASE_URL': config.base_url or 'https://api.openai.com/v1',
            'OPENAI_MODEL_NAME': config.model_name or 'gpt-3.5-turbo'
        }
        
        self.llm_requester = LLMRequester(self.openai_config, task_id)
        self.reader = SubtitleReader()
        self.writer = SubtitleWriter()

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        try:
            for ch in str(text):
                code = ord(ch)
                if 0x4E00 <= code <= 0x9FFF:
                    return True
            return False
        except Exception:
            return False

    def quick_repair_translated_file(self, input_path: str, output_path: Optional[str] = None) -> bool:
        """最小改动修复：仅补译已翻译文件中仍为英文/未译的行，避免整文件重翻译。

        - 读取 SRT/VTT 文件。
        - 找出文本中不含中文且含英文字母/数字的条目。
        - 以严格模式仅翻译这些条目，写回文件（默认覆盖原文件）。
        """
        try:
            from pathlib import Path as _Path
            fp = _Path(input_path)
            ext = fp.suffix.lower()
            if ext == '.srt':
                items = self.reader.read_srt(input_path)
            elif ext == '.vtt':
                items = self.reader.read_vtt(input_path)
            else:
                self.logger.error(f"不支持的字幕格式: {ext}")
                return False

            if not items:
                self.logger.warning("文件为空或解析失败，跳过修复")
                return False

            # 挑出仍为英文的行（无中文且包含拉丁字母/数字）
            targets: List[int] = []
            for i, it in enumerate(items):
                t = (it.source_text or '').strip()
                if not t:
                    continue
                if self._contains_chinese(t):
                    continue
                # 若包含字母或数字则判定为待修复
                if re.search(r"[A-Za-z0-9]", t):
                    targets.append(i)

            if not targets:
                self.logger.info("未发现需要修复的英文行，跳过")
                return True

            texts = [items[i].source_text for i in targets]
            self.logger.info(f"快速修复：共 {len(texts)} 条待补译")

            # 使用严格模式批量翻译，尽量输出全中文
            translations = self.llm_requester.translate_batch_strict(
                texts, self.config.target_language, batch_id=f"quick_repair_{self.task_id}"
            )

            # 写回对应条目（只改这些行）
            for j, idx in enumerate(targets):
                try:
                    tr = translations[j] if j < len(translations) else ''
                    if tr:
                        items[idx].translated_text = self._sanitize_translated_text(tr)
                except Exception:
                    pass

            # 输出到目标文件（默认覆盖原文件）
            out_path = str(output_path or input_path)
            
            # 强制转换为 SRT 格式输出
            if out_path.lower().endswith('.vtt'):
                out_path = out_path[:-4] + '.srt'
                
            self.writer.write_srt(items, out_path, translated=True)

            self.logger.info(f"快速修复完成：{out_path}")
            return True

        except Exception as e:
            self.logger.error(f"快速修复失败: {e}")
            import traceback as _tb
            self.logger.error(_tb.format_exc())
            return False
    
    def translate_file(self, input_path: str, output_path: str,
                      progress_callback: Optional[Callable[[float, int, int], None]] = None) -> bool:
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
                            progress_callback: Optional[Callable[[float, int, int], None]] = None) -> bool:
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
            
            # 内存感知处理：在高内存使用时降低并发数
            try:
                import psutil  # type: ignore
            except Exception:
                psutil = None

            if psutil:
                try:
                    memory = psutil.virtual_memory()
                    if memory.percent > 80.0:
                        max_workers = max(1, max_workers // 2)
                        self.logger.info(f"检测到高内存使用({memory.percent:.1f}%)，降低并发数至 {max_workers}")
                except Exception:
                    pass
            
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
                    # 始终计算 progress，避免在未传入 progress_callback 时未绑定变量
                    progress = (completed_items / total_items) * 100
                    if progress_callback:
                        progress_callback(progress, completed_items, total_items)
                    # 将逐条翻译进度降低到 debug 级别，保留网页上显示的进度
                    self.logger.debug(f"翻译进度: {completed_items}/{total_items} ({progress:.1f}%)")
            
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
            
            # 清理内存以降低系统资源占用
            try:
                gc.collect()
                self.logger.debug("翻译完成后执行垃圾回收以优化内存使用")
            except Exception:
                pass
            
            # 二次修复：补翻漏译项（例如返回空串或仍是英文）
            self._repair_untranslated_items(items)

            # 输出翻译后的文件
            return self._write_translated_file(items, output_path)
            
        except Exception as e:
            self.logger.error(f"并发翻译过程中发生错误: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False

    def _likely_untranslated(self, src: str, dst: str) -> bool:
        """判断翻译是否可能未生效：空串、与原文相同、非中文比例过高。

        非中文比例判定：仅统计“中文汉字”与“英拉丁字母/数字”，忽略空白与标点；
        当 非中文/(中文+非中文) > 0.8 时，认为疑似未翻译。
        """
        try:
            s = (src or '').strip()
            d = (dst or '').strip()
            if not d:
                return True
            if d == s:
                # 若目标语言是中文但结果与原文一致，多半未翻译
                return True
            # 计算非中文比例（仅中文汉字 vs 英数）
            chinese = 0
            non_chinese = 0
            for ch in d:
                if ch.isspace():
                    continue
                # 中文汉字范围
                code = ord(ch)
                if 0x4E00 <= code <= 0x9FFF:
                    chinese += 1
                elif re.match(r"[A-Za-z0-9]", ch):
                    non_chinese += 1
                else:
                    # 忽略标点/符号/表情，不计入分母
                    continue
            denom = chinese + non_chinese
            if denom == 0:
                return False
            non_cn_ratio = non_chinese / denom
            return non_cn_ratio > 0.8
        except Exception:
            return False

    def _repair_untranslated_items(self, items: List[SubtitleItem]):
        """对疑似未翻译的条目进行小批量补翻，最大化消除漏翻。"""
        try:
            to_fix_indices: List[int] = [i for i, it in enumerate(items) if self._likely_untranslated(it.source_text, it.translated_text)]
            if not to_fix_indices:
                return
            self.logger.info(f"检测到 {len(to_fix_indices)} 条疑似未翻译条目，开始补翻...")

            bs = max(1, int(self.config.batch_size) if self.config.batch_size else 5)
            for i in range(0, len(to_fix_indices), bs):
                chunk = to_fix_indices[i:i+bs]
                texts = [items[idx].source_text for idx in chunk]
                try:
                    translations = self.llm_requester.translate_batch(texts, self.config.target_language, batch_id=f"repair_{self.task_id}_{i//bs+1}")
                except Exception as e:
                    self.logger.warning(f"补翻批次失败，跳过该批：{e}")
                    continue
                for j, idx in enumerate(chunk):
                    try:
                        tr = translations[j] if j < len(translations) else ''
                        if tr and self._likely_untranslated(items[idx].source_text, tr) is False:
                            items[idx].translated_text = self._sanitize_translated_text(tr)
                    except Exception:
                        pass

            # 再次扫描仍未译的条目，使用严格模式再尝试一次
            still_untranslated: List[int] = [i for i, it in enumerate(items) if self._likely_untranslated(it.source_text, it.translated_text)]
            if not still_untranslated:
                return
            self.logger.info(f"仍有 {len(still_untranslated)} 条未充分翻译，启动严格模式补救...")
            bs2 = max(1, int(self.config.batch_size) if self.config.batch_size else 5)
            for i in range(0, len(still_untranslated), bs2):
                chunk = still_untranslated[i:i+bs2]
                texts = [items[idx].source_text for idx in chunk]
                try:
                    translations = self.llm_requester.translate_batch_strict(texts, self.config.target_language, batch_id=f"repair_strict_{self.task_id}_{i//bs2+1}")
                except Exception as e:
                    self.logger.warning(f"严格模式补翻批次失败，跳过该批：{e}")
                    continue
                for j, idx in enumerate(chunk):
                    try:
                        tr = translations[j] if j < len(translations) else ''
                        if tr and self._likely_untranslated(items[idx].source_text, tr) is False:
                            items[idx].translated_text = self._sanitize_translated_text(tr)
                    except Exception:
                        pass
        except Exception as e:
            self.logger.warning(f"补翻流程出现异常：{e}")

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
def create_translator_from_config(app_config: Dict, task_id: Optional[str] = None) -> Optional[SubtitleTranslator]:
    """从应用配置创建翻译器 (与ai_enhancer.py保持一致的配置格式)"""
    try:
        # 添加调试日志：检查配置值是否为 None
        logger.debug(f"create_translator_from_config 调用，task_id: {task_id}")
        
        # 确保数值配置被正确转换为整数
        batch_size = app_config.get('SUBTITLE_BATCH_SIZE', 3)  # 降低默认批次大小
        if isinstance(batch_size, str):
            batch_size = int(batch_size)
        
        max_retries = app_config.get('SUBTITLE_MAX_RETRIES', 3)
        if isinstance(max_retries, str):
            max_retries = int(max_retries)
        
        retry_delay = app_config.get('SUBTITLE_RETRY_DELAY', 2)
        if isinstance(retry_delay, str):
            retry_delay = int(retry_delay)
        
        max_workers = app_config.get('SUBTITLE_MAX_WORKERS', 2)  # 降低默认并发数
        if isinstance(max_workers, str):
            max_workers = int(max_workers)
        
        # 计算字幕翻译专用Base URL（优先使用SUBTITLE_OPENAI_BASE_URL，否则回退到OPENAI_BASE_URL）
        subtitle_base_url = app_config.get('SUBTITLE_OPENAI_BASE_URL') or app_config.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')

        # 计算字幕翻译专用Key/模型，未配置则回退通用值
        subtitle_api_key = app_config.get('SUBTITLE_OPENAI_API_KEY') or app_config.get('OPENAI_API_KEY', '')
        subtitle_model = app_config.get('SUBTITLE_OPENAI_MODEL_NAME') or app_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        # 添加调试日志：检查配置值
        logger.debug(f"配置值检查 - subtitle_base_url: {subtitle_base_url is None}, subtitle_api_key: {subtitle_api_key is None}, subtitle_model: {subtitle_model is None}")

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
        
        return SubtitleTranslator(translation_config, task_id or "unknown")
        
    except Exception as e:
        logger.error(f"创建翻译器失败: {e}")
        return None 