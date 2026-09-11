#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import logging
import gc  # 添加垃圾回收模块以优化内存使用
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
import concurrent.futures
from threading import Lock
from modules.task_manager import TaskCancelledError
from .speech_pipeline_settings import SPEECH_PIPELINE_DEFAULTS, coerce_bool
from .utils import (
    get_app_subdir,
    openai_chat_create_with_thinking_control,
    extract_chat_message_json,
    extract_json_from_text,
    get_chat_message_text,
)

logger = logging.getLogger('subtitle_translator')

# Pre-compiled regex for Chinese character detection (performance optimization)
_CHINESE_CHAR_RE = re.compile(r'[\u4e00-\u9fff]')
SUBTITLE_RESIDUAL_UNTRANSLATED_RATIO_THRESHOLD = 0.15
SUBTITLE_RESIDUAL_UNTRANSLATED_COUNT_THRESHOLD = 3

# 配对/批次契约的默认值：与 SPEECH_PIPELINE_DEFAULTS 中的注册值保持单一来源，
# 缺失或非法时回退到规格默认（未译残留不容忍 / 单批 2000 字符）。
SUBTITLE_ALLOW_PARTIAL_DEFAULT = bool(
    SPEECH_PIPELINE_DEFAULTS.get('SUBTITLE_TRANSLATION_ALLOW_PARTIAL', False)
)
try:
    SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT = int(
        SPEECH_PIPELINE_DEFAULTS.get('SUBTITLE_TRANSLATION_MAX_CHARS_PER_BATCH', 2000) or 2000
    )
except Exception:
    SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT = 2000
if SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT <= 0:
    SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT = 2000

# 行首「编号 + 分隔符」前缀：用于识别模型自行附加的序号。
# 点号后紧跟数字（如 "10.5%"）视为小数点，不认作序号，避免破坏数字信息。
_LEADING_INDEX_PREFIX_RE = re.compile(
    r'^(?:[\(（]?\s*\d{1,4}\s*[\)）:：、]\s*|\d{1,4}\s*[.)](?!\d)\s*|[-–—·•]\s+)'
)

# 「原文照留」类条目的判定用正则：这类内容 prompt 明确允许保留原文，
# 命中它们不算「未翻译」，否则会让一条 URL / 型号丢掉整份译文。
_URL_LIKE_RE = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)
_PURE_NUMBER_RE = re.compile(r'\d+(?:[.,:]\d+)*%?')
_SYMBOL_ONLY_RE = re.compile(r'[\W_]+', re.UNICODE)
_ALNUM_ONLY_RE = re.compile(r'[A-Za-z0-9]+')
_CAPS_ACRONYM_RE = re.compile(r'[A-Z]{2,}\d*')
_CJK_ONLY_RE = re.compile(r'[\u3400-\u9fff]+')
_COMPACT_TAG_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9+._/-]*')
# 「像专有名词/代码而非普通英文词」的形态判据：
# 含数字（v1、x86、mp4）、或首字母之后仍出现大写（iPhone、YouTube、iOS）。
_LOWER_TO_UPPER_RE = re.compile(r'[a-z][A-Z]')


def _looks_like_name_or_code(compact: str) -> bool:
    """单 token 是否像专有名词/型号/缩写，而不是一个普通英文单词。

    为什么不能只看「短且是 ASCII」：``hello``/``bravo`` 这类普通小写单词同样
    满足该条件，会被误判为「不可译」→ 模型整句照抄英文原文时残留计数归零，
    「整批未译」的反向守卫彻底失效。

    因此只认三种可被 prompt 正当保留的形态：含数字的型号/版本、全大写缩写、
    词中出现大小写转换的专有名词。普通小写英文词一律要求翻译。
    """
    if not compact or not compact.isascii():
        return False
    if any(ch.isdigit() for ch in compact):
        return True
    if _CAPS_ACRONYM_RE.fullmatch(compact):
        return True
    return bool(_LOWER_TO_UPPER_RE.search(compact))


def _is_preservable_verbatim(text: str) -> bool:
    """判断文本是否属于「原文照留即可」的不可译条目。

    字幕翻译 prompt 明确允许保留数字、代码、URL、占位符和无公认译名的
    专有名词；这些条目的译文与原文相同是**正确结果**，不能算未译残留。

    覆盖：URL、纯数字/百分比、纯符号、纯 CJK、缩写/型号/专有名词
    （``NVIDIA``/``v1.2.3``/``iPhone``）。

    刻意**不**覆盖普通英文词与英文句子：那些是真正该翻译、模型却照抄的
    情形，必须继续计为残留 —— 否则「整批未译」会被放行。
    """
    s = str(text or '').strip()
    if not s:
        return False
    if _URL_LIKE_RE.fullmatch(s):
        return True
    if _SYMBOL_ONLY_RE.fullmatch(s):
        return True
    if not re.search(r'[A-Za-z0-9]', s):
        # 没有拉丁字母/数字：纯 CJK（原本就无需翻译）或纯符号 → 可保留
        return True
    if _CJK_ONLY_RE.fullmatch(re.sub(r'[\s\W_]+', '', s)):
        return True

    compact = re.sub(r'[\s\W_]+', '', s)
    if not compact:
        return True
    if _PURE_NUMBER_RE.fullmatch(compact):
        # 纯数字/百分比或纯数字串（"12345"、"1 2 3"、"60%"）
        return True

    tokens = s.split()
    if len(tokens) > 1:
        # 多 token：单 token 必须足够"不可译"，否则判为需要翻译的句子
        return all(_is_preservable_token(token) for token in tokens)
    if _ALNUM_ONLY_RE.fullmatch(compact):
        # 无空格单 token：必须是专有名词/型号/缩写，普通小写英文词不算
        return _looks_like_name_or_code(compact)
    if len(compact) <= 12 and _COMPACT_TAG_RE.fullmatch(compact):
        # 含符号的型号/短代码（C++、x86_64、A/B）
        return _looks_like_name_or_code(compact)
    return False


def _is_preservable_token(token: str) -> bool:
    """多词短语里的单个 token 是否"不可译"。

    只认三类：纯数字、大写的技术缩写/型号（`NVIDIA`/`RTX`/`USB`/`GPU`）、
    纯 CJK。普通小写英文词（`hello`/`world`/`the`）一律不算 —— 否则英文句子
    照抄会被整句放行，整批未译就失去了拦截能力。
    """
    compact = re.sub(r'[\W_]+', '', token)
    if not compact:
        return True
    if _PURE_NUMBER_RE.fullmatch(compact):
        return True
    if _CAPS_ACRONYM_RE.fullmatch(compact):
        return True
    return bool(_CJK_ONLY_RE.fullmatch(compact))


def _should_fail_translation_residue(
    total_items: int,
    unresolved_count: int,
    allow_partial: bool = False,
) -> bool:
    """字幕翻译验收：判定是否必须整体失败。

    allow_partial=False（默认）：任一条未译残留即失败，防止原文/译文混排烧录成片。
    allow_partial=True：保留旧的容忍阈值（同时超过 3 条且超过 15% 才失败）。

    注意：本函数只表达「严格/宽松」两种策略语义，不含少量残留的追认逻辑；
    真正决定是否写盘的是 SubtitleTranslator._finalize_residual_untranslated_items，
    它在 allow_partial=False 时还会对少量（<=3 条且 <=15%）残留做标记后放行，
    避免一条 URL/数字条目丢掉整份译文。
    """
    if total_items <= 0 or unresolved_count <= 0:
        return False
    if not allow_partial:
        return True
    unresolved_ratio = unresolved_count / max(1, total_items)
    return (
        unresolved_count > SUBTITLE_RESIDUAL_UNTRANSLATED_COUNT_THRESHOLD
        and unresolved_ratio > SUBTITLE_RESIDUAL_UNTRANSLATED_RATIO_THRESHOLD
    )


def _normalize_batch_size(value, default: int = 3) -> int:
    """批次条数上限归一化：非法/空值回退默认值；<=0 表示不限制条数。"""
    try:
        if value is None or str(value).strip() == '':
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def _normalize_chars_per_batch(
    value,
    default: int = SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT,
) -> int:
    """批次字符预算归一化：非法/非正值回退默认值（预算必须为正）。"""
    try:
        if value is None or str(value).strip() == '':
            return default
        number = int(float(str(value).strip()))
    except Exception:
        return default
    return number if number > 0 else default


def _split_by_budget(entries: List[Tuple[object, int]], size_limit: int, char_limit: int) -> List[list]:
    """按「条数上限 + 字符预算」把 (载荷, 字符数) 列表切分成批。

    规则：批内条数不超过 size_limit（<=0 表示不限条数）；
    批内字符数之和不超过 char_limit；单条自身超过 char_limit 时该条独占一批
    （不丢弃、不截断），避免超长 cue 撑爆单次请求导致整批失败。
    """
    batches: List[list] = []
    current: list = []
    current_chars = 0
    for payload, char_count in entries:
        if current:
            over_count = size_limit > 0 and len(current) >= size_limit
            over_chars = char_limit > 0 and (current_chars + char_count) > char_limit
            if over_count or over_chars:
                batches.append(current)
                current = []
                current_chars = 0
        current.append(payload)
        current_chars += char_count
    if current:
        batches.append(current)
    return batches


def _split_items_into_batches(
    items: List['SubtitleItem'],
    batch_size: int = 3,
    max_chars_per_batch: int = SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT,
) -> List[List['SubtitleItem']]:
    """字幕条目分批：条数与字符预算双约束（见 _split_by_budget）。"""
    return _split_by_budget(
        [(item, len(str(getattr(item, 'source_text', '') or ''))) for item in items],
        _normalize_batch_size(batch_size),
        _normalize_chars_per_batch(max_chars_per_batch),
    )

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

    统一走 modules.ai_fallback_client.get_ai_client，主端点（OPENAI_*）不可用时
    自动切换到 FALLBACK_OPENAI_* 兜底端点；未配置兜底则退化为单端点，行为不变。

    Args:
        openai_config (dict): OpenAI配置信息，包含api_key, base_url等

    Returns:
        OpenAI客户端实例
    """
    from modules.ai_fallback_client import get_ai_client
    return get_ai_client(openai_config)

@dataclass
class SubtitleItem:
    """字幕条目"""
    index: int
    start_time: str
    end_time: str
    source_text: str
    translated_text: str = ""
    # 未译残留标记：仅在 SUBTITLE_TRANSLATION_ALLOW_PARTIAL=True 且验收阶段
    # 判定该条仍未翻译时置位；默认 False，不影响既有构造方式与时间轴字段。
    residual_untranslated: bool = False

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
    thinking_enabled: bool = False
    timeout_seconds: int = 600  # API请求超时秒数；思考模型输出可达64k token，建议不低于300
    # Prompt 中心配置
    prompt_mode: str = "builtin"
    prompt_text: str = ""         # 字幕翻译主 Prompt 用户文本
    prompt_strict_mode: str = "builtin"  # 字幕翻译严格补救 Prompt 模式
    prompt_strict_text: str = ""  # 字幕翻译严格补救 Prompt 用户文本
    # 未译残留策略：False（默认）时任一条未译即整体失败，不写盘；
    # True 时保留旧的少量残留容忍行为（标记 + 警告 + 写盘回退原文）。
    allow_partial: bool = SUBTITLE_ALLOW_PARTIAL_DEFAULT
    # 单批字符预算：批内 source_text 长度之和上限，超限即另起一批。
    max_chars_per_batch: int = SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT

class SubtitleReader:
    """字幕文件读取器"""

    # Punctuation sets used when merging multi-line subtitles into one line.
    _TRAILING_PUNCT = frozenset(".,!?;:)]}，。！？；：）】》」』")
    _LEADING_PUNCT = frozenset(".,!?;:([{，。！？；：（【《「『")
    
    @staticmethod
    def _is_cjk_char(char: str) -> bool:
        """Check if a character is CJK (Chinese/Japanese/Korean)."""
        if not char:
            return False
        cp = ord(char)
        return (
            0x4E00 <= cp <= 0x9FFF        # CJK Unified Ideographs
            or 0x3400 <= cp <= 0x4DBF     # CJK Extension A
            or 0x3000 <= cp <= 0x303F     # CJK Symbols and Punctuation
            or 0x3040 <= cp <= 0x309F     # Hiragana
            or 0x30A0 <= cp <= 0x30FF     # Katakana
            or 0xAC00 <= cp <= 0xD7AF     # Hangul Syllables
            or 0xFF00 <= cp <= 0xFFEF     # Fullwidth Forms
            or 0xFE30 <= cp <= 0xFE4F     # CJK Compatibility Forms
            or 0x20000 <= cp <= 0x2A6DF   # CJK Extension B
        )

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
        
        # 合并多行，智能处理标点符号和 CJK 字符
        merged_text = ""
        for i, line in enumerate(lines):
            if i == 0:
                merged_text = line
            else:
                prev_char = merged_text[-1] if merged_text else ""
                curr_char = line[0] if line else ""
                
                # CJK 字符之间不需要空格
                if (SubtitleReader._is_cjk_char(prev_char)
                        and SubtitleReader._is_cjk_char(curr_char)):
                    merged_text += line
                # 标点符号附近直接连接
                elif (prev_char in SubtitleReader._TRAILING_PUNCT
                        or curr_char in SubtitleReader._LEADING_PUNCT):
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

    @staticmethod
    def _strip_terminal_full_stop(text: str) -> str:
        """移除每行结尾的句号/英文句点，保留其他标点。"""
        if not text:
            return text

        normalized_lines: List[str] = []
        for raw_line in str(text).split('\n'):
            line = raw_line.rstrip()
            if line.endswith('。'):
                line = line[:-1].rstrip()
            elif line.endswith('.') and not line.endswith('..'):
                line = line[:-1].rstrip()
            normalized_lines.append(line)
        return '\n'.join(normalized_lines)

    @staticmethod
    def write_srt(items: List[SubtitleItem], output_path: str, translated: bool = True):
        """写入SRT字幕文件"""
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                for item in items:
                    # 空译文回退原文的语义：
                    # - allow_partial=False（默认）：存在未译残留会在验收阶段直接失败，
                    #   不会走到写盘，因此这里只等同于「译文恰好为空串」的兜底；
                    # - allow_partial=True：未译条目在验收阶段被标记
                    #   （SubtitleItem.residual_untranslated）并清空译文，此处回退为原文，
                    #   属于显式容忍的原文/译文混排行为，已在验收阶段记录 warning。
                    text = item.translated_text if translated and item.translated_text else item.source_text
                    if translated:
                        text = SubtitleWriter._strip_terminal_full_stop(text)
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
                    # 空译文回退原文的语义同 write_srt（见上方注释）
                    text = item.translated_text if translated and item.translated_text else item.source_text
                    if translated:
                        text = SubtitleWriter._strip_terminal_full_stop(text)
                    start_time = item.start_time.replace(',', '.')
                    end_time = item.end_time.replace(',', '.')
                    f.write(f"{start_time} --> {end_time}\n")
                    f.write(f"{text}\n\n")
            logger.info(f"VTT文件已保存: {output_path}")
        except Exception as e:
            logger.error(f"写入VTT文件失败: {e}")

class SubtitleAlignmentError(RuntimeError):
    """译文与原文无法按下标严格配对（缺项/合并/增项/条数不符）时抛出。

    由批次重试逻辑（config.max_retries）接管；重试耗尽后沿用既有「整批失败」语义：
    该批译文全部置空并交由补翻/验收处理，绝不按位置回填错位译文。
    """


# 下标键与译文键的候选名（兼容不同网关/模型对下标对象数组的命名差异）
_INDEX_KEY_CANDIDATES = ('index', 'idx', 'i', 'id', 'n', 'no', 'seq')
_TRANSLATION_KEY_CANDIDATES = (
    'translation', 'translated_text', 'text', 't', 'output', 'content', '译文',
)
# 常见包装键：{"items": [...]} / {"translations": [...]} / {"results": [...]} 等
_INDEXED_WRAPPER_KEYS = (
    'items', 'item', 'translations', 'results', 'result', 'data', 'output', 'list', 'segments',
)


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
        self._capability_lock = Lock()
        self._json_mode_disabled = False
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
        if not texts:
            return []
        if not self.client:
            raise RuntimeError("OpenAI客户端未初始化")
        
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
            
            translations = self._request_translation_result(
                model_name=model_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                expected_count=len(texts),
                batch_id=batch_id,
                scene_name='subtitle_translate_batch',
            )
            
            response_time = time.time() - start_time
            
            with self._log_lock:
                self.logger.log(
                    logging.INFO if log_as_info else logging.DEBUG,
                    f"批次 {batch_id} 翻译完成，耗时: {response_time:.2f}秒"
                )
            
            return translations
            
        except Exception as e:
            with self._log_lock:
                self.logger.error(f"批次 {batch_id} 翻译请求失败: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
            raise

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
        if not texts:
            return []
        if not self.client:
            raise RuntimeError("OpenAI客户端未初始化")
        try:
            system_prompt = self._build_strict_structured_system_prompt(target_language)
            user_prompt = self._build_structured_user_prompt(texts)
            model_name = self.openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
            with self._log_lock:
                self.logger.info(f"开始严格模式翻译批次 {batch_id}，包含 {len(texts)} 条字幕")
            return self._request_translation_result(
                model_name=model_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                expected_count=len(texts),
                batch_id=batch_id,
                scene_name='subtitle_translate_batch_strict',
            )
        except Exception as e:
            with self._log_lock:
                self.logger.error(f"严格模式批次 {batch_id} 翻译失败: {e}")
            raise

    def _create_translation_completion(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        scene_name: str,
        json_mode: bool,
    ):
        create_kwargs = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_mode:
            create_kwargs["response_format"] = {"type": "json_object"}
        return openai_chat_create_with_thinking_control(
            client=self.client,
            create_kwargs=create_kwargs,
            thinking_enabled=self.openai_config.get('OPENAI_THINKING_ENABLED', False),
            logger=self.logger,
            scene_name=scene_name,
        )

    def _request_translation_result(
        self,
        *,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        expected_count: int,
        batch_id: str,
        scene_name: str,
    ) -> List[str]:
        """请求并解析字幕；JSON 模式产出不可解析时自动改用纯文本 JSON 重试。

        配对契约：只有能证明「译文与输入逐条严格对齐」时才返回结果；
        条数不符、下标缺项/重复/增项一律判定该批失败并抛 SubtitleAlignmentError，
        由调用方的 max_retries 重试逻辑接管，绝不补齐、绝不按位置回填。
        """
        with self._capability_lock:
            json_mode = not self._json_mode_disabled
        response = self._create_translation_completion(
            model_name=model_name,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            scene_name=scene_name,
            json_mode=json_mode,
        )
        if not getattr(response, 'choices', None):
            with self._log_lock:
                self.logger.warning(f"批次 {batch_id}: API返回空的choices列表")
            return [""] * expected_count

        (
            translations,
            parsed_successfully,
        ) = self._parse_structured_translation_result_with_status(
            response.choices[0].message, expected_count, batch_id
        )
        if parsed_successfully:
            return translations

        if not json_mode:
            raise SubtitleAlignmentError(
                f"批次 {batch_id}: 响应无法与输入按下标严格配对（期望 {expected_count} 条），判定该批失败"
            )

        with self._log_lock:
            self.logger.warning(
                f"批次 {batch_id}: JSON模式响应不可用，改用纯文本JSON模式重试"
            )
        retry_response = self._create_translation_completion(
            model_name=model_name,
            system_prompt=(
                system_prompt
                + "\n重要：只返回 JSON 对象，不要使用 Markdown 代码块或添加解释。"
                + "\n必须按输入下标逐条返回："
                + '{"translations":[{"index":0,"translation":"译文1"},{"index":1,"translation":"译文2"}]}；'
                + "index 与输入条目编号一一对应，不得缺项、不得合并、不得增项；"
                + "若只能输出纯文本，则每行一条译文，行数必须与输入条数完全相同且顺序一致。"
            ),
            user_prompt=user_prompt,
            scene_name=f'{scene_name}_plain_json_retry',
            json_mode=False,
        )
        if not getattr(retry_response, 'choices', None):
            raise SubtitleAlignmentError(
                f"批次 {batch_id}: 纯文本JSON重试返回空的choices列表，判定该批失败"
            )
        (
            retried,
            retry_parsed_successfully,
        ) = self._parse_structured_translation_result_with_status(
            retry_response.choices[0].message,
            expected_count,
            f'{batch_id}_plain_retry',
        )
        if retry_parsed_successfully:
            with self._capability_lock:
                self._json_mode_disabled = True
            return retried
        raise SubtitleAlignmentError(
            f"批次 {batch_id}: JSON模式与纯文本模式均无法与输入按下标严格配对"
            f"（期望 {expected_count} 条），判定该批失败"
        )
    
    def _build_structured_system_prompt(self, target_language: str) -> str:
        """构建结构化系统提示词（委托给统一 Prompt 中心）。"""
        from .prompt_manager import get_subtitle_system_prompt
        return get_subtitle_system_prompt(
            mode=self.openai_config.get('PROMPT_MODE', 'builtin'),
            user_text=self.openai_config.get('PROMPT_TEXT', ''),
            target_language=target_language,
        )

    def _build_strict_structured_system_prompt(self, target_language: str) -> str:
        """严格模式提示词（委托给统一 Prompt 中心）。"""
        from .prompt_manager import get_subtitle_strict_system_prompt
        return get_subtitle_strict_system_prompt(
            mode=self.openai_config.get(
                'PROMPT_STRICT_MODE',
                self.openai_config.get('PROMPT_MODE', 'builtin'),
            ),
            user_text=self.openai_config.get('PROMPT_STRICT_TEXT', ''),
            target_language=target_language,
        )
    
    def _build_structured_user_prompt(self, texts: List[str]) -> str:
        """构建结构化用户提示词。"""
        return json.dumps(
            {
                "task": "subtitle_translation",
                "requirements": {
                    "one_to_one_alignment": True,
                    "no_cross_item_carryover": True,
                    "keep_fragment_boundaries": True,
                    # 输出契约：下游按下标回填，缺项/合并/增项都会让整批判定失败
                    "output_format": '{"translations":[{"index":0,"translation":"..."}]}',
                    "output_index_rule": "index 必须完整覆盖 0..N-1 且与 texts 下标一一对应，不得缺项、不得合并、不得增项",
                },
                "texts": texts,
            },
            ensure_ascii=False,
        )

    def _parse_structured_translation_result(self, message, expected_count: int, batch_id: str) -> Optional[List[str]]:
        """解析结构化翻译结果（配对不成立时返回 None）"""
        translations, _ = self._parse_structured_translation_result_with_status(
            message,
            expected_count,
            batch_id,
        )
        return translations

    def _parse_structured_translation_result_with_status(
        self,
        message,
        expected_count: int,
        batch_id: str,
    ) -> Tuple[Optional[List[str]], bool]:
        """解析结构化翻译结果，并区分解析成功与配对失败。

        配对优先级：
        1) 带下标的对象数组（{"items":[{"index":0,"translation":"..."}]}、顶层数组、
           数字字符串键对象、下标键/译文键变体）：下标集合必须构成 0..N-1 或 1..N 的
           完整双射，否则判定失败；
        2) 无下标的纯数组：条数必须严格等于期望条数，否则判定失败；
        3) 纯文本编号行兜底：行数与编号序列都必须严格对齐，否则判定失败。
        任何情况下都不补齐、不按位置回填无法证明对齐的译文。
        """
        try:
            json_result = extract_chat_message_json(message, expected_type=None)
            # 如果首次解析失败，尝试清洗 ASS 标签后重试
            if not isinstance(json_result, (dict, list)):
                raw_text = get_chat_message_text(message)
                cleaned_text = re.sub(r'\\[hHnN]', ' ', raw_text)
                cleaned_text = re.sub(r'{\\[^}]*}', '', cleaned_text)
                json_result = extract_json_from_text(cleaned_text, expected_type=None)

            preview = get_chat_message_text(message)

            indexed, has_index_hint = self._build_translations_by_index(json_result)
            if indexed is not None:
                if set(indexed.keys()) == set(range(expected_count)):
                    ordered = [indexed[i] for i in range(expected_count)]
                elif set(indexed.keys()) == set(range(1, expected_count + 1)):
                    # 兼容 1 基下标：仍是完整双射（无缺项/无合并/无增项），按下标回填
                    ordered = [indexed[i] for i in range(1, expected_count + 1)]
                else:
                    with self._log_lock:
                        self.logger.warning(
                            "批次 %s: 译文下标集合与输入不匹配（期望 0..%s，实际 %s），判定该批失败",
                            batch_id,
                            expected_count - 1,
                            sorted(indexed.keys()),
                        )
                    return None, False
            elif has_index_hint:
                with self._log_lock:
                    self.logger.warning(
                        f"批次 {batch_id}: 响应含下标但无法构成合法映射，判定该批失败"
                    )
                return None, False
            else:
                positional = self._coerce_translation_list(json_result, expected_count)
                if positional is not None:
                    if len(positional) != expected_count:
                        with self._log_lock:
                            self.logger.warning(
                                "批次 %s: 无下标译文条数 %s 与输入条数 %s 不符，判定该批失败",
                                batch_id,
                                len(positional),
                                expected_count,
                            )
                        return None, False
                    ordered = positional
                else:
                    ordered = self._parse_plain_translation_lines(preview, expected_count)
                    if ordered is None:
                        with self._log_lock:
                            self.logger.warning(
                                f"批次 {batch_id}: 未解析到可严格对齐的翻译列表，响应预览: {preview[:200]}"
                            )
                        return None, False

            # 条数与顺序已由上面的配对校验保证，这里只做清洗
            _ass_tag_re = re.compile(r'\\[hHnN]|{\\[^}]*}')
            final_translations = []
            for t in ordered:
                cleaned = _ass_tag_re.sub('', str(t or '')).strip()
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                final_translations.append(cleaned)
            
            with self._log_lock:
                self.logger.info(f"批次 {batch_id}: 成功解析 {len(final_translations)} 条翻译")
            
            return final_translations, True
        except Exception as e:
            with self._log_lock:
                self.logger.error(f"批次 {batch_id}: 解析翻译结果失败: {e}")
            return None, False

    @staticmethod
    def _has_index_key(element) -> bool:
        """元素是否显式携带下标键。"""
        if not isinstance(element, dict):
            return False
        return any(key in element for key in _INDEX_KEY_CANDIDATES)

    @staticmethod
    def _extract_indexed_item(element) -> Optional[Tuple[int, str]]:
        """从单个对象元素中取出 (下标, 译文)；无法确定时返回 None。"""
        if not isinstance(element, dict):
            return None
        index_value = None
        for key in _INDEX_KEY_CANDIDATES:
            if key in element:
                index_value = element[key]
                break
        if index_value is None or isinstance(index_value, (dict, list)):
            return None
        try:
            index_int = int(str(index_value).strip())
        except Exception:
            return None
        if index_int < 0:
            return None

        text_value = None
        for key in _TRANSLATION_KEY_CANDIDATES:
            if key in element:
                text_value = element[key]
                break
        if text_value is None or isinstance(text_value, (dict, list)):
            return None
        return index_int, text_value

    @staticmethod
    def _collect_numeric_keyed_mapping(value) -> Optional[Dict[int, str]]:
        """把 {"0": "甲", "1": "乙"} 这类数字字符串键对象转成下标字典。"""
        if not isinstance(value, dict) or not value:
            return None
        mapping: Dict[int, str] = {}
        for key, item in value.items():
            try:
                index_int = int(str(key).strip())
            except Exception:
                return None
            if index_int in mapping or isinstance(item, (dict, list)):
                return None
            mapping[index_int] = item
        return mapping

    @staticmethod
    def _build_translations_by_index(json_result, _depth: int = 0) -> Tuple[Optional[Dict[int, str]], bool]:
        """尝试把 JSON 结果解析为「下标 → 译文」映射。

        返回值 `(mapping, has_index_hint)`：
        - mapping 非 None：成功解析出下标映射（是否合法由调用方校验）；
        - (None, True)：响应里出现下标线索却无法构成合法映射（契约破坏，必须失败）；
        - (None, False)：响应完全没有下标线索（可退回按位置配对，但要求条数严格相等）。

        支持的形态：
        - {"items": [{"index": 0, "translation": "..."}]} 等包装键
        - 顶层直接是 [{"index": 0, "translation": "..."}]
        - 下标键变体 index/idx/i/id/n/no/seq，译文键变体 translation/translated_text/text/t/...
        - 数字字符串键对象 {"0": "...", "1": "..."}
        - 单个条目对象 {"index": 0, "translation": "..."}
        """
        value = json_result
        if isinstance(value, dict):
            numeric_mapping = LLMRequester._collect_numeric_keyed_mapping(value)
            if numeric_mapping is not None:
                return numeric_mapping, True

            single = LLMRequester._extract_indexed_item(value)
            if single is not None:
                return {single[0]: single[1]}, True

            if _depth < 3:
                for key in _INDEXED_WRAPPER_KEYS:
                    if key in value:
                        inner_indexed, inner_hint = LLMRequester._build_translations_by_index(
                            value[key], _depth + 1
                        )
                        if inner_indexed is not None or inner_hint:
                            return inner_indexed, inner_hint

            if LLMRequester._has_index_key(value):
                return None, True
            return None, False

        if isinstance(value, list):
            if not value:
                return None, False
            objects = [element for element in value if isinstance(element, dict)]
            if len(objects) != len(value):
                # 混入非对象元素：只有出现下标线索时才判定为契约破坏
                hint = any(LLMRequester._has_index_key(element) for element in value)
                return None, hint
            flags = [LLMRequester._has_index_key(element) for element in objects]
            if not any(flags):
                return None, False
            if not all(flags):
                # 部分带下标、部分不带：无法证明对齐，判定为契约破坏
                return None, True
            mapping: Dict[int, str] = {}
            for element in objects:
                extracted = LLMRequester._extract_indexed_item(element)
                if extracted is None:
                    return None, True
                index_int, text_value = extracted
                if index_int in mapping:
                    return None, True
                mapping[index_int] = text_value
            return mapping, True

        return None, False

    @staticmethod
    def _coerce_translation_list(json_result, expected_count: int):
        """兼容常见网关/模型的 JSON 包装差异，并保持原始顺序。

        注意：本函数只做「无下标线索」场景下的按位置展开，不校验下标；
        调用方（_parse_structured_translation_result_with_status）会强制要求
        展开后的条数严格等于输入条数，否则判定该批失败。
        """
        value = json_result
        if isinstance(value, dict):
            for key in ('translations', 'translation', 'results', 'result', 'data', 'output'):
                if key in value:
                    value = value[key]
                    break
            else:
                numeric_items = []
                for key, item in value.items():
                    try:
                        numeric_items.append((int(str(key)), item))
                    except Exception:
                        numeric_items = []
                        break
                if numeric_items:
                    value = [item for _, item in sorted(numeric_items)]
                else:
                    return None

        if isinstance(value, str):
            return [value] if expected_count == 1 else None
        if not isinstance(value, list):
            return None

        translations = []
        for item in value:
            if isinstance(item, dict):
                item_value = None
                for key in ('translation', 'translated_text', 'text', 'output', 'content'):
                    if key in item:
                        item_value = item[key]
                        break
                if item_value is None:
                    return None
                translations.append(item_value)
            else:
                translations.append(item)
        return translations

    @staticmethod
    def _parse_plain_translation_lines(text: str, expected_count: int):
        """最后兜底：兼容只返回编号逐行译文、但不返回 JSON 的小模型。

        严格契约（与下标配对同一标准）：
        - 编号行条数必须严格等于输入条数；
        - 行首编号必须构成 0..N-1 或 1..N 的连续序列（缺项/合并/重复一律拒绝）；
        - 若全部是无编号的项目符号行，则要求条数严格相等并保持顺序；
        - 首个编号行之前的开场白/解释会被忽略，其后出现无编号行即判为结构不可信。
        """
        raw = str(text or '').strip()
        if not raw:
            return None
        if expected_count == 1 and '\n' not in raw:
            # 单条请求只给一行裸译文：仅在明确是「序号+点/括号+空白」前缀时剥离，
            # 避免把 "10.5% 的人…" 这类数字文本当成编号清单损坏。
            single_prefix = re.match(r'^\s*\d{1,4}\s*[.)]\s+(?=\S)(.+)$', raw)
            if single_prefix:
                return [single_prefix.group(1).strip()]
            return [raw]

        pattern = re.compile(r'^\s*(?:(\d+)\s*[.)、:：-]|[-*•])\s*(.+?)\s*$')
        index_texts: List[Tuple[Optional[int], str]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            match = pattern.match(line)
            if not match:
                if not index_texts:
                    # 首个编号行之前的开场白/解释，忽略
                    continue
                return None
            body = (match.group(2) or '').strip()
            if not body:
                return None
            index_texts.append(
                (int(match.group(1)) if match.group(1) is not None else None, body)
            )

        if len(index_texts) != expected_count:
            return None
        if all(index is None for index, _ in index_texts):
            return [body for _, body in index_texts]
        if any(index is None for index, _ in index_texts):
            return None
        indices = [index for index, _ in index_texts]
        if indices != list(range(expected_count)) and indices != list(range(1, expected_count + 1)):
            return None
        return [body for _, body in index_texts]

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
            'OPENAI_MODEL_NAME': config.model_name or 'gpt-3.5-turbo',
            'OPENAI_THINKING_ENABLED': str(config.thinking_enabled).strip().lower() in ('true', '1', 'on', 'yes'),
            'OPENAI_TIMEOUT_SECONDS': config.timeout_seconds,
            # Prompt 中心配置（快照，避免热修改影响进行中的翻译）
            'PROMPT_MODE': getattr(config, 'prompt_mode', 'builtin'),
            'PROMPT_TEXT': getattr(config, 'prompt_text', ''),
            'PROMPT_STRICT_MODE': getattr(config, 'prompt_strict_mode', 'builtin'),
            'PROMPT_STRICT_TEXT': getattr(config, 'prompt_strict_text', ''),
        }
        # 兜底端点（FALLBACK_OPENAI_*）来自全局配置；统一客户端 get_ai_client 也会兜底补齐，
        # 这里显式透传以保证字幕翻译路径必然拿到兜底端点。
        try:
            from modules.ai_fallback_client import _global_fallback_fields
            self.openai_config.update(_global_fallback_fields())
        except Exception:
            pass
        
        self.llm_requester = LLMRequester(self.openai_config, task_id)
        self.reader = SubtitleReader()
        self.writer = SubtitleWriter()

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        """Check if text contains Chinese characters using pre-compiled regex (optimized)."""
        try:
            return bool(_CHINESE_CHAR_RE.search(str(text)))
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
                      progress_callback: Optional[Callable[[float, int, int], None]] = None,
                      cancel_event=None) -> bool:
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
            return self._translate_concurrent(items, output_path, progress_callback, cancel_event)
            
        except Exception as e:
            self.logger.error(f"翻译字幕文件失败: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
    
    def _translate_concurrent(self, items: List[SubtitleItem], output_path: str,
                            progress_callback: Optional[Callable[[float, int, int], None]] = None,
                            cancel_event=None) -> bool:
        """使用多线程并发翻译"""
        try:
            total_items = len(items)
            batch_size = _normalize_batch_size(self.config.batch_size)
            # 批次同时受条数与字符预算约束（E4）：避免单条超长 cue 或长句密集批次
            # 把整批 texts JSON 化后撑爆单次请求（超时/输出截断 → 整批失败）。
            char_budget = _normalize_chars_per_batch(
                getattr(self.config, 'max_chars_per_batch', SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT)
            )
            item_batches = _split_items_into_batches(items, batch_size, char_budget)
            # 允许不设上限：当配置为0或小于1时，按实际批次数动态分配
            required_workers = max(1, len(item_batches))
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
            
            # 创建批次（条数上限 + 字符预算双约束，单条超预算时独占一批）
            batches = []
            start_index = 0
            for batch_no, batch_items in enumerate(item_batches, 1):
                batches.append({
                    'batch_id': f"{self.task_id}_{batch_no}",
                    'start_index': start_index,
                    'items': batch_items,
                    'texts': [item.source_text for item in batch_items]
                })
                start_index += len(batch_items)
            
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
                        if cancel_event is not None and cancel_event.is_set():
                            return False
                        translations = self.llm_requester.translate_batch(
                            batch_texts, 
                            self.config.target_language,
                            batch_id=batch_id
                        )
                        
                        # 配对契约：译文必须与输入逐条严格对齐。条数不符说明模型
                        # 漏项/合并/增项，直接判定该批失败（交由重试），
                        # 绝不按位置回填错位译文，也不补齐空串。
                        if not isinstance(translations, (list, tuple)) or len(translations) != len(batch_items):
                            raise SubtitleAlignmentError(
                                f"批次 {batch_id}: 译文条数 {len(translations) if isinstance(translations, (list, tuple)) else 'invalid'}"
                                f" != 输入条数 {len(batch_items)}，判定该批失败"
                            )
                        
                        # 将翻译结果赋值给字幕项（条数已严格相等）
                        for j, translation in enumerate(translations):
                            batch_items[j].translated_text = self._sanitize_translated_text(translation)

                        invalid_translations = [
                            idx for idx, batch_item in enumerate(batch_items)
                            if self._likely_untranslated(
                                batch_item.source_text,
                                batch_item.translated_text,
                            )
                        ]
                        if len(invalid_translations) == len(batch_items):
                            raise RuntimeError("整批译文均未通过有效性检查")
                        
                        # 更新进度
                        update_progress(len(batch_items))
                        
                        return True
                        
                    except TaskCancelledError:
                        raise
                    except Exception as e:
                        self.logger.warning(f"批次 {batch_id} 翻译失败 (重试 {retry + 1}/{self.config.max_retries}): {e}")
                        if retry < self.config.max_retries - 1:
                            time.sleep(self.config.retry_delay)
                        else:
                            # 最后一次重试失败，保留空译文，交由后续补翻/验收决定是否继续
                            for j in range(len(batch_items)):
                                batch_items[j].translated_text = ""
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
                        if cancel_event is not None and cancel_event.is_set():
                            self.logger.info("检测到任务取消请求，终止字幕翻译")
                            return False
                        success = future.result()
                        if success:
                            successful_batches += 1
                    except TaskCancelledError:
                        raise
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

            if not self._finalize_residual_untranslated_items(items):
                return False

            # 输出翻译后的文件
            return self._write_translated_file(items, output_path)
            
        except TaskCancelledError:
            self.logger.info("字幕翻译检测到任务取消请求")
            raise
        except Exception as e:
            self.logger.error(f"并发翻译过程中发生错误: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False

    def _likely_untranslated(self, src: str, dst: str) -> bool:
        """判断翻译是否可能未生效：空串、与原文相同、非中文比例过高。

        非中文比例判定：仅统计“中文汉字”与“英拉丁字母/数字”，忽略空白与标点；
        当 非中文/(中文+非中文) > 0.8 时，认为疑似未翻译。

        例外：URL、纯数字、代码/版本号、大写缩写、短专有名词等**不可译条目**
        的译文等于原文是 prompt 允许的正确结果（见 _is_preservable_verbatim），
        直接判为已翻译，避免一条残留导致整份译文被丢弃。
        """
        try:
            s = (src or '').strip()
            d = (dst or '').strip()
            if not d:
                return True
            if d == s:
                # 若目标语言是中文但结果与原文一致，多半未翻译；
                # 但不可译条目（URL/型号/纯数字/专有名词）保留原文是正确行为。
                return not _is_preservable_verbatim(d)
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
            if non_cn_ratio > 0.8:
                # 非中文占绝大多数：只有不可译条目才容忍，其余判为未译
                return not _is_preservable_verbatim(d)
            return False
        except Exception:
            return False

    def _collect_untranslated_indices(self, items: List[SubtitleItem]) -> List[int]:
        try:
            return [
                i for i, item in enumerate(items)
                if self._likely_untranslated(item.source_text, item.translated_text)
            ]
        except Exception:
            return []

    def _finalize_residual_untranslated_items(self, items: List[SubtitleItem]) -> bool:
        """字幕翻译验收：决定未译残留条目是否可继续写盘。

        - allow_partial=False（默认，SUBTITLE_TRANSLATION_ALLOW_PARTIAL）：
          整批未译（残留超过 3 条或超过 15%）仍然整体失败并返回 False，调用方
          不写盘；但**少量残留**（<=3 条且 <=15%）按「标记 + 回退原文」放行 ——
          False 的语义是「不把原文当译文写盘」，而不是「因为一条 URL/型号丢掉
          整份译文」。标记后的条目译文置空，写盘阶段回退原文。
        - allow_partial=True：保留旧的少量残留容忍阈值；未译条目打
          residual_untranslated 标记、译文置空并记录 warning，
          写盘阶段按 SubtitleWriter 的回退语义输出原文。
        """
        unresolved_indices = self._collect_untranslated_indices(items)
        unresolved_count = len(unresolved_indices)
        total_items = len(items)
        if unresolved_count == 0:
            return True

        allow_partial = bool(getattr(self.config, 'allow_partial', SUBTITLE_ALLOW_PARTIAL_DEFAULT))
        unresolved_ratio = unresolved_count / max(1, total_items)
        sample_indices = unresolved_indices[:5]
        if _should_fail_translation_residue(total_items, unresolved_count, allow_partial=allow_partial):
            if allow_partial or not self._residual_within_tolerance(unresolved_count, total_items):
                self.logger.error(
                    "字幕翻译验收失败：仍有 %s/%s 条疑似未翻译（%.1f%%），样本索引=%s（allow_partial=%s）",
                    unresolved_count,
                    total_items,
                    unresolved_ratio * 100.0,
                    sample_indices,
                    allow_partial,
                )
                return False
            self.logger.warning(
                "字幕翻译存在少量未译残留（%s/%s，%.1f%%），按「标记 + 回退原文」放行以避免丢弃整份译文",
                unresolved_count,
                total_items,
                unresolved_ratio * 100.0,
            )

        self.logger.warning(
            "字幕翻译验收保留未译残留：%s/%s 条仍疑似未翻译（%.1f%%），下标=%s；"
            "已标记 residual_untranslated，写盘时将回退为原文",
            unresolved_count,
            total_items,
            unresolved_ratio * 100.0,
            unresolved_indices,
        )
        for idx in unresolved_indices:
            try:
                items[idx].residual_untranslated = True
                # 保留原译文为空（不再用原文伪造译文），由写盘阶段决定回退
                items[idx].translated_text = ""
            except Exception:
                pass
        return True

    @staticmethod
    def _residual_within_tolerance(unresolved_count: int, total_items: int) -> bool:
        """少量残留的追认条件：不超过 3 条且不超过 15%。

        两个条件必须同时满足，保证「整批未译」（例如模型把英文原文全部照抄）
        必然超阈值失败：10 条里残留 2 条 → 20% > 15% → 判失败。
        """
        if unresolved_count <= 0 or total_items <= 0:
            return True
        return (
            unresolved_count <= SUBTITLE_RESIDUAL_UNTRANSLATED_COUNT_THRESHOLD
            and (unresolved_count / total_items) <= SUBTITLE_RESIDUAL_UNTRANSLATED_RATIO_THRESHOLD
        )

    def _repair_untranslated_items(self, items: List[SubtitleItem]):
        """对疑似未翻译的条目进行小批量补翻，最大化消除漏翻。"""
        try:
            to_fix_indices = self._collect_untranslated_indices(items)
            if not to_fix_indices:
                return
            self.logger.info(f"检测到 {len(to_fix_indices)} 条疑似未翻译条目，开始补翻...")

            bs = _normalize_batch_size(self.config.batch_size, default=5) or 5
            char_budget = _normalize_chars_per_batch(
                getattr(self.config, 'max_chars_per_batch', SUBTITLE_MAX_CHARS_PER_BATCH_DEFAULT)
            )
            for chunk_no, chunk in enumerate(
                _split_by_budget(
                    [(idx, len(str(items[idx].source_text or ''))) for idx in to_fix_indices],
                    bs,
                    char_budget,
                ),
                1,
            ):
                texts = [items[idx].source_text for idx in chunk]
                try:
                    translations = self.llm_requester.translate_batch(texts, self.config.target_language, batch_id=f"repair_{self.task_id}_{chunk_no}")
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
            still_untranslated = self._collect_untranslated_indices(items)
            if not still_untranslated:
                return
            self.logger.info(f"仍有 {len(still_untranslated)} 条未充分翻译，启动严格模式补救...")
            bs2 = _normalize_batch_size(self.config.batch_size, default=5) or 5
            for chunk_no, chunk in enumerate(
                _split_by_budget(
                    [(idx, len(str(items[idx].source_text or ''))) for idx in still_untranslated],
                    bs2,
                    char_budget,
                ),
                1,
            ):
                texts = [items[idx].source_text for idx in chunk]
                try:
                    translations = self.llm_requester.translate_batch_strict(texts, self.config.target_language, batch_id=f"repair_strict_{self.task_id}_{chunk_no}")
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
            stripped_lines = [line for line in lines if line]

            # 仅当「本次响应的全部非空行都以编号+分隔符开头」且存在多行时，
            # 才认定整体是编号清单并剥离行首编号；否则原样保留，避免把
            # "10.5% 的人…"、"3、4 号方案" 这类数字开头的正文损坏成 "5% 的人…"。
            is_numbered_list = (
                len(stripped_lines) >= 2
                and all(_LEADING_INDEX_PREFIX_RE.match(line) for line in stripped_lines)
            )

            cleaned_lines: List[str] = []
            previous_key = None

            for line in lines:
                if not line:
                    continue
                if is_numbered_list:
                    # 反复移除前置编号或项目符号（最多10次防止无限循环）
                    for _ in range(10):
                        new_line = _LEADING_INDEX_PREFIX_RE.sub('', line)
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

                # 去重**只作用于相邻行**（模型偶尔重复输出同一行）：全局去重会把
                # 同一译文里本来就重复出现的行（歌词、复读句）当成冗余删掉，在严格
                # 配对契约下等于凭空丢内容，因此这里只折叠紧邻的重复行。
                key = line.strip().lower()
                if previous_key is not None and key == previous_key:
                    continue
                previous_key = key
                cleaned_lines.append(line)

            sanitized = '\n'.join(cleaned_lines).strip()
            return SubtitleWriter._strip_terminal_full_stop(sanitized)
        except Exception:
            return SubtitleWriter._strip_terminal_full_stop(text.strip())
    
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

        # 未译残留策略：默认关闭部分容忍（任一条未译即整体失败，不写盘）
        allow_partial = coerce_bool(
            app_config.get(
                'SUBTITLE_TRANSLATION_ALLOW_PARTIAL',
                SUBTITLE_ALLOW_PARTIAL_DEFAULT,
            )
        )
        # 单批字符预算：非法/非正值回退 2000
        max_chars_per_batch = _normalize_chars_per_batch(
            app_config.get('SUBTITLE_TRANSLATION_MAX_CHARS_PER_BATCH')
        )
        
        # 计算字幕翻译专用Base URL（优先使用SUBTITLE_OPENAI_BASE_URL，否则回退到OPENAI_BASE_URL）
        subtitle_base_url = app_config.get('SUBTITLE_OPENAI_BASE_URL') or app_config.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')

        # 计算字幕翻译专用Key/模型，未配置则回退通用值
        subtitle_api_key = app_config.get('SUBTITLE_OPENAI_API_KEY') or app_config.get('OPENAI_API_KEY', '')
        subtitle_model = app_config.get('SUBTITLE_OPENAI_MODEL_NAME') or app_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        # 添加调试日志：检查配置值
        logger.debug(f"配置值检查 - subtitle_base_url: {subtitle_base_url is None}, subtitle_api_key: {subtitle_api_key is None}, subtitle_model: {subtitle_model is None}")

        # 读取 Prompt 中心配置
        prompt_mode = 'builtin'
        prompt_text = ''
        prompt_strict_mode = 'builtin'
        prompt_strict_text = ''
        try:
            from .prompt_manager import read_prompt_config_from_app_config
            prompt_mode, prompt_text = read_prompt_config_from_app_config(app_config, 'SUBTITLE_TRANSLATE')
            prompt_strict_mode, prompt_strict_text = read_prompt_config_from_app_config(app_config, 'SUBTITLE_TRANSLATE_STRICT')
        except Exception as exc:
            logger.debug(f"读取 Prompt 中心配置失败，将回退 builtin: {exc}")

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
            max_workers=max_workers,
            thinking_enabled=app_config.get('SUBTITLE_OPENAI_THINKING_ENABLED', False),
            timeout_seconds=int(app_config.get('OPENAI_TIMEOUT_SECONDS', 600)),
            prompt_mode=prompt_mode,
            prompt_text=prompt_text,
            prompt_strict_mode=prompt_strict_mode,
            prompt_strict_text=prompt_strict_text,
            allow_partial=allow_partial,
            max_chars_per_batch=max_chars_per_batch,
        )
        
        if not translation_config.api_key:
            logger.error("未配置API密钥，无法创建翻译器")
            return None
        
        return SubtitleTranslator(translation_config, task_id or "unknown")
        
    except Exception as e:
        logger.error(f"创建翻译器失败: {e}")
        return None 
