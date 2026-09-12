#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 智能分段模块。

基于 ASR 返回的字级时间戳（words），将相邻 VAD 窗口合并为长上下文批次，
调用独立配置的 AI 模型做语义重分段，输出节奏自然、显示时长不过短的字幕条目。

三级降级策略（封装在 segment() 内部）：
  1. 字级时间戳可用 → 字级 AI 分段（精度最高，时间精确到词边界）
  2. 字级缺失或失败 → 段级 AI 分段（仅能在段边界拆分/合并）
  3. 两者均失败     → 该批次回退到基线对齐（按段直转 cue），不阻断主流程

模型配置支持独立覆盖（AI_SEGMENTATION_BASE_URL/API_KEY/MODEL_NAME），
留空时继承全局 OPENAI_* 配置，与 SUBTITLE_OPENAI_* 模式一致。
"""

import json
import logging
import re
import string
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .ai_enhancer import _request_json_object, _request_raw_text, get_openai_client
from .prompt_manager import get_smart_segment_system_prompt, get_boundary_refine_system_prompt
from .speech_pipeline_settings import coerce_bool
from .subtitle_pipeline_types import (
    AlignedSubtitleCue,
    AsrSegmentTiming,
    AsrTranscriptionResult,
    AsrWordTiming,
    DetectedSpeechWindow,
)


# 句末/停顿标点，用于过长短目拆分的安全网
_SENTENCE_SPLIT_RE = re.compile(r'([.!?。！？；;]+\s*)')
_CLAUSE_SPLIT_RE = re.compile(r'([,，、]+\s*)')  # 次级切分标点：逗号/顿号
_CJK_CHAR_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
_SOFT_BREAK_PUNCTUATION = frozenset('.!?。！？；;')
# 中文虚词/连接词/助词——用于无标点时的兜底切分点（在其前面切分）
_CJK_FUNCTION_WORDS = frozenset(
    '的了在是和与而但也还就都把被从到会能要想过着'  # 助词/连词/介词/能愿动词/动态助词
    '而且但是因为所以如果虽然不过然后或者而且因此'  # 双字连接词（优先在前面切）
)
# 句末标点——用于判断一条 cue 是否在句子边界结束
_SENTENCE_END_PUNCTS = set('.!?。！？')
_CLAUSE_END_PUNCTS = set(';:；：，、')
# 段级 AI 时间戳吸附容差：超出该容差视为"该时间点不属于任何输入段边界"
_BOUNDARY_SNAP_TOL_S = 0.3


def _words_to_text(words) -> str:
    """把词序列还原为 cue 文本（原生方式：优先从 ASR 原始 segment 文本切片）。

    每个 word 携带它所属 segment 的原始文本（source_text）和字符偏移
    [char_start, char_end)——由 _flatten_words 在展平时计算。
    若所有 word 都有有效偏移且来自同一 segment，直接从原始文本切片，
    完整保留空格、标点和大小写，无需任何 CJK/拉丁判断。
    跨 segment 或偏移缺失时回退到 _join_word_texts 兜底。
    """
    if not words:
        return ''
    # 原生路径：所有 word 有偏移 → 从原始文本切片
    if all(getattr(w, 'char_start', -1) >= 0 and getattr(w, 'source_text', '') for w in words):
        # 按 source_text 分组（同一 segment 的连续 word 一起切片）
        parts: List[str] = []
        i = 0
        while i < len(words):
            j = i
            src = words[i].source_text
            while j + 1 < len(words) and words[j + 1].source_text is src:
                j += 1
            char_start = words[i].char_start
            char_end = words[j].char_end
            parts.append(src[char_start:char_end])
            i = j + 1
        return ' '.join(parts).strip()
    # 兜底：无偏移信息时按 CJK/拉丁判断加空格
    return _join_word_texts(words)


def _join_word_texts(words) -> str:
    """兜底：无原始文本偏移时，按 CJK/拉丁判断拼接词序列。

    空格分隔语言（英文等）词之间加空格；CJK 文本不加空格。
    标点紧贴前一词，不加空格。
    """
    parts: List[str] = []
    for w in words:
        token = str(getattr(w, 'text', '') or '').strip()
        if not token:
            continue
        if not parts:
            parts.append(token)
            continue
        prev = parts[-1]
        if token[:1] in _SENTENCE_END_PUNCTS or token[:1] in _CLAUSE_END_PUNCTS or token[:1] in ',.!?;:，。！？；：':
            parts[-1] = prev + token
            continue
        if _CJK_CHAR_RE.match(token):
            parts.append(token)
            continue
        parts.append(' ' + token)
    return ''.join(parts).strip()


class AISegmentationError(Exception):
    """AI 智能分段不可恢复错误（调用方应回退到规则分段）。"""


@dataclass
class AISegmentationConfig:
    """从应用配置字典解析的 AI 分段参数。"""

    enabled: bool = False
    # 独立模型覆盖（留空继承全局 OPENAI_*）
    base_url: str = ''
    api_key: str = ''
    model_name: str = ''
    thinking_enabled: bool = False
    # 节奏阈值
    min_cue_duration_s: float = 0.8
    max_cue_duration_s: float = 5.0
    max_cps: float = 18.0
    # 批次策略
    batch_window_s: float = 120.0
    max_chars_per_batch: int = 4000
    # 请求参数
    temperature: float = 0.1
    max_retries: int = 2
    request_timeout_s: float = 600.0
    # Agent 上下文感知
    context_window: int = 3          # 前一批末尾 N 条 cue 注入下一批 prompt
    boundary_refine_enabled: bool = False   # 边界精炼 pass（索引制下通常不需要）
    boundary_window: int = 3         # 边界精炼每侧取 N 条 cue
    rhythm_enabled: bool = False     # 节奏后处理（合并过短/拆分过长）；默认关闭，直接信任 AI 分段结果
    # 解析后的实际生效模型配置（留空继承后填充）
    resolved_base_url: str = ''
    resolved_api_key: str = ''
    resolved_model_name: str = ''

    @classmethod
    def from_app_config(cls, app_config: Dict[str, Any]) -> 'AISegmentationConfig':
        cfg = cls(
            enabled=coerce_bool(app_config.get('AI_SEGMENTATION_ENABLED', False)),
            base_url=str(app_config.get('AI_SEGMENTATION_BASE_URL', '') or '').strip(),
            api_key=str(app_config.get('AI_SEGMENTATION_API_KEY', '') or '').strip(),
            model_name=str(app_config.get('AI_SEGMENTATION_MODEL_NAME', '') or '').strip(),
            thinking_enabled=coerce_bool(app_config.get('AI_SEGMENTATION_THINKING_ENABLED', False)),
            min_cue_duration_s=float(app_config.get('AI_SEGMENTATION_MIN_CUE_DURATION_S', 0.8) or 0.8),
            max_cue_duration_s=float(app_config.get('AI_SEGMENTATION_MAX_CUE_DURATION_S', 5.0) or 5.0),
            max_cps=float(app_config.get('AI_SEGMENTATION_MAX_CPS', 18.0) or 18.0),
            batch_window_s=float(app_config.get('AI_SEGMENTATION_BATCH_WINDOW_S', 120.0) or 120.0),
            max_chars_per_batch=int(app_config.get('AI_SEGMENTATION_MAX_CHARS_PER_BATCH', 4000) or 4000),
            temperature=float(app_config.get('AI_SEGMENTATION_TEMPERATURE', 0.1) or 0.1),
            max_retries=int(app_config.get('AI_SEGMENTATION_MAX_RETRIES', 2) or 2),
            request_timeout_s=float(app_config.get('OPENAI_TIMEOUT_SECONDS', 600) or 600),
            context_window=int(app_config.get('AI_SEGMENTATION_CONTEXT_WINDOW', 3) or 3),
            boundary_refine_enabled=coerce_bool(app_config.get('AI_SEGMENTATION_BOUNDARY_REFINE_ENABLED', False)),
            boundary_window=int(app_config.get('AI_SEGMENTATION_BOUNDARY_WINDOW', 3) or 3),
            rhythm_enabled=coerce_bool(app_config.get('AI_SEGMENTATION_RHYTHM_ENABLED', False)),
        )
        # 留空继承全局 OPENAI_*
        cfg.resolved_base_url = cfg.base_url or str(app_config.get('OPENAI_BASE_URL', '') or '').strip()
        cfg.resolved_api_key = cfg.api_key or str(app_config.get('OPENAI_API_KEY', '') or '').strip()
        cfg.resolved_model_name = cfg.model_name or str(app_config.get('OPENAI_MODEL_NAME', '') or '').strip()
        return cfg

    @property
    def is_model_configured(self) -> bool:
        return bool(self.resolved_api_key and self.resolved_model_name)


# ---------------------------------------------------------------------------
# 批次构建
# ---------------------------------------------------------------------------

@dataclass
class _Batch:
    """一个待送检的批次：跨 VAD 窗口合并后的词/段序列。"""

    words: List[AsrWordTiming] = field(default_factory=list)
    segments: List[AsrSegmentTiming] = field(default_factory=list)
    time_start_s: float = 0.0
    time_end_s: float = 0.0
    has_word_timestamps: bool = False

    @property
    def char_count(self) -> int:
        if self.words:
            return sum(len(str(w.text or '')) for w in self.words)
        return sum(len(str(s.text or '')) for s in self.segments)


def _compute_word_char_offsets(segment_text: str, words: List[AsrWordTiming]) -> None:
    """为每个 word 设置它在所属 segment 原始文本中的字符偏移 [char_start, char_end)。

    顺序匹配 word.text 在 segment_text 中的位置（忽略大小写兜底）。
    匹配失败的 word 保持 char_start=-1，拼接时回退到 _join_word_texts。
    这样 cue 文本直接从 ASR 返回的原始 segment 文本切片，完整保留空格和标点，
    无需根据 CJK/拉丁判断是否加空格。
    """
    if not segment_text:
        return
    pos = 0
    search_text = segment_text
    search_lower = segment_text.lower()
    for w in words:
        wtext = str(w.text or '').strip()
        if not wtext:
            continue
        idx = search_text.find(wtext, pos)
        if idx < 0:
            idx = search_lower.find(wtext.lower(), pos)
        if idx >= 0:
            w.source_text = segment_text
            w.char_start = idx
            w.char_end = idx + len(wtext)
            pos = idx + len(wtext)


def _flatten_words(
    results: List[AsrTranscriptionResult],
    apply_window_offset: bool = True,
) -> Tuple[List[AsrWordTiming], float, float]:
    """把多个 result 的所有 segment.words 按时间顺序展平，返回 (words, start, end)。

    ASR 返回的 word 时间戳是窗口内相对时间（每个窗口从 0 开始）。
    apply_window_offset=True 时加上 result.window.start_s 偏移，转为视频绝对时间，
    使跨窗口合并的批次内时间轴统一。AI 分段在统一绝对时间轴上工作，输出可直接使用。

    每个 word 附带它所属 segment 的原始文本和字符偏移（source_text/char_start/char_end），
    供 _words_to_text 从原始文本切片，完整保留空格和标点。
    """
    words: List[AsrWordTiming] = []
    for result in results:
        offset = float(result.window.start_s) if (apply_window_offset and result.window) else 0.0
        for seg in result.segments:
            seg_text = str(seg.text or '')
            seg_words: List[AsrWordTiming] = []
            for w in seg.words:
                if str(w.text or '').strip() and w.end_s > w.start_s:
                    new_w = AsrWordTiming(
                        start_s=w.start_s + offset,
                        end_s=w.end_s + offset,
                        text=w.text,
                        source_text=seg_text,
                    )
                    seg_words.append(new_w)
                    words.append(new_w)
            _compute_word_char_offsets(seg_text, seg_words)
    if not words:
        return [], 0.0, 0.0
    start = min(w.start_s for w in words)
    end = max(w.end_s for w in words)
    return words, start, end


def _flatten_segments(
    results: List[AsrTranscriptionResult],
    apply_window_offset: bool = True,
) -> Tuple[List[AsrSegmentTiming], float, float]:
    segs: List[AsrSegmentTiming] = []
    for result in results:
        offset = float(result.window.start_s) if (apply_window_offset and result.window) else 0.0
        for seg in result.segments:
            if str(seg.text or '').strip() and seg.end_s > seg.start_s:
                seg_text = str(seg.text or '')
                # 同步偏移 segment 及其 words，并计算字符偏移
                offset_words = [
                    AsrWordTiming(
                        start_s=w.start_s + offset, end_s=w.end_s + offset, text=w.text,
                        source_text=seg_text,
                    )
                    for w in seg.words
                ]
                _compute_word_char_offsets(seg_text, offset_words)
                segs.append(AsrSegmentTiming(
                    start_s=seg.start_s + offset,
                    end_s=seg.end_s + offset,
                    text=seg.text,
                    words=offset_words,
                ))
    if not segs:
        return [], 0.0, 0.0
    start = min(s.start_s for s in segs)
    end = max(s.end_s for s in segs)
    return segs, start, end


def _split_words_by_char_limit(
    words: List[AsrWordTiming], max_chars: int
) -> List[List[AsrWordTiming]]:
    """按字符上限把词序列切成多个子列表（在标点/停顿处 soft-break）。"""
    if not words:
        return []
    chunks: List[List[AsrWordTiming]] = []
    current: List[AsrWordTiming] = []
    current_chars = 0
    for w in words:
        w_chars = len(str(w.text or ''))
        if current and current_chars + w_chars > max_chars:
            # 尝试 soft-break：在当前列表末尾附近找句末标点或停顿
            cut = _find_soft_break_point(current)
            if cut > 0 and cut < len(current):
                chunks.append(current[:cut])
                current = current[cut:]
                current_chars = sum(len(str(x.text or '')) for x in current)
            else:
                chunks.append(current)
                current = []
                current_chars = 0
        current.append(w)
        current_chars += w_chars
    if current:
        chunks.append(current)
    return chunks


def _find_soft_break_point(words: List[AsrWordTiming]) -> int:
    """在词列表末尾附近寻找最佳切分点（句末标点或停顿≥0.6s）。

    从末尾向前搜索最多 36 个词，返回切分点索引（该索引及之后的词归入下一段）。
    找不到好的切分点则返回 0。
    """
    if len(words) <= 1:
        return 0
    search_depth = min(36, len(words) - 1)
    for i in range(len(words) - 1, len(words) - 1 - search_depth, -1):
        if i <= 0:
            break
        text = str(words[i].text or '')
        # 句末标点
        if any(ch in _SOFT_BREAK_PUNCTUATION for ch in text):
            return i + 1
        # 停顿 ≥ 0.6s（当前词结束后到下一词开始前的间隙）
        if i + 1 < len(words):
            pause = max(0.0, float(words[i + 1].start_s) - float(words[i].end_s))
            if pause >= 0.6:
                return i + 1
    return 0


def build_batches(
    results: List[AsrTranscriptionResult],
    batch_window_s: float,
    max_chars: int,
) -> List[_Batch]:
    """把 ASR 结果按时间窗口和字符上限合并成批次。

    同一批次内的 result 必须时间相邻（VAD 窗口顺序）。
    单个 result 超过字符上限时，按词切分为多个子批次。
    """
    batches: List[_Batch] = []
    # 按 window 起始时间排序，保证批次时间单调
    ordered = sorted(
        [r for r in results if r.segments],
        key=lambda r: (r.window.start_s if r.window else 0.0),
    )

    current: Optional[_Batch] = None

    def _flush(batch: Optional[_Batch]) -> None:
        nonlocal current
        if batch and (batch.words or batch.segments):
            batches.append(batch)
        current = None

    for result in ordered:
        win_start = result.window.start_s if result.window else 0.0
        win_end = result.window.end_s if result.window else 0.0
        words, _, _ = _flatten_words([result])
        segs, _, _ = _flatten_segments([result])
        has_word = bool(words)

        # 字符超限：单独按词切分子批次，不与邻窗合并
        result_chars = sum(len(str(w.text or '')) for w in words) if words else sum(
            len(str(s.text or '')) for s in segs
        )
        if result_chars > max_chars:
            _flush(current)
            if has_word:
                for chunk in _split_words_by_char_limit(words, max_chars):
                    if not chunk:
                        continue
                    b = _Batch(
                        words=chunk,
                        segments=[],
                        time_start_s=chunk[0].start_s,
                        time_end_s=chunk[-1].end_s,
                        has_word_timestamps=True,
                    )
                    batches.append(b)
            else:
                # 段级且超限：直接作为一个批次（段级无法精细切分），由 LLM 处理
                if segs:
                    batches.append(_Batch(
                        words=[],
                        segments=list(segs),
                        time_start_s=segs[0].start_s,
                        time_end_s=segs[-1].end_s,
                        has_word_timestamps=False,
                    ))
            continue

        # 是否需要开启新批次：批次时间跨度超限 或 字符超限 或 词级能力不一致
        open_new = False
        if current is None:
            open_new = True
        else:
            batch_span = win_end - current.time_start_s
            new_chars = current.char_count + result_chars
            if batch_span > batch_window_s and (current.time_end_s - current.time_start_s) > 0:
                # 时间跨度超过窗口上限：开启新批次（保留长上下文但不无限拉伸）
                open_new = True
            elif new_chars > max_chars and current.char_count > 0:
                open_new = True
            elif current.has_word_timestamps != has_word:
                # 词级能力变化时切批，避免批次内能力混杂
                open_new = True

        if open_new:
            _flush(current)
            current = _Batch(
                words=list(words) if has_word else [],
                segments=[] if has_word else list(segs),
                time_start_s=win_start,
                time_end_s=win_end,
                has_word_timestamps=has_word,
            )
        else:
            if current is None:
                # 防御性兜底：逻辑上 open_new=False 时 current 必非空
                current = _Batch(
                    words=list(words) if has_word else [],
                    segments=[] if has_word else list(segs),
                    time_start_s=win_start,
                    time_end_s=win_end,
                    has_word_timestamps=has_word,
                )
            else:
                current.words.extend(words)
                if not has_word:
                    current.segments.extend(segs)
                current.time_end_s = max(current.time_end_s, win_end)
                current.has_word_timestamps = current.has_word_timestamps and has_word

    _flush(current)
    return batches


# ---------------------------------------------------------------------------
# AI 调用与解析
# ---------------------------------------------------------------------------

def _build_word_payload(words: List[AsrWordTiming]) -> Dict[str, Any]:
    return {
        'words': [
            {'index': idx, 'text': str(w.text or ''), 'start': round(float(w.start_s), 3), 'end': round(float(w.end_s), 3)}
            for idx, w in enumerate(words)
        ]
    }


def _build_segment_payload(segments: List[AsrSegmentTiming]) -> Dict[str, Any]:
    return {
        'segments': [
            {'index': idx, 'text': str(s.text or ''), 'start': round(float(s.start_s), 3), 'end': round(float(s.end_s), 3)}
            for idx, s in enumerate(segments)
        ]
    }


def _serialize_context_cues(context_cues: List[AlignedSubtitleCue]) -> List[Dict[str, Any]]:
    """将已确认的上下文 cues 序列化为 payload 片段。"""
    return [
        {'start': round(float(c.start_s), 3), 'end': round(float(c.end_s), 3), 'text': str(c.text or '')}
        for c in context_cues
        if str(c.text or '').strip()
    ]


def _build_word_payload_with_context(
    words: List[AsrWordTiming],
    context_cues: List[AlignedSubtitleCue],
) -> Dict[str, Any]:
    """构建带上下文的字级 payload。"""
    payload = _build_word_payload(words)
    if context_cues:
        payload['context_cues'] = _serialize_context_cues(context_cues)
    return payload


def _build_segment_payload_with_context(
    segments: List[AsrSegmentTiming],
    context_cues: List[AlignedSubtitleCue],
) -> Dict[str, Any]:
    """构建带上下文的段级 payload。"""
    payload = _build_segment_payload(segments)
    if context_cues:
        payload['context_cues'] = _serialize_context_cues(context_cues)
    return payload


# ---------------------------------------------------------------------------
# 索引制解析（字级 AI 分段：AI 返回 [{start_index, end_index}]）
# ---------------------------------------------------------------------------

def _strip_code_fence(text: str) -> str:
    """去除 Markdown code fence 包裹。"""
    stripped = text.strip()
    if not stripped.startswith('```'):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].strip().startswith('```'):
        return '\n'.join(lines[1:-1]).strip()
    return stripped


def _find_balanced_json(text: str, open_char: str, close_char: str) -> str:
    """在文本中查找第一个平衡的 JSON 片段（数组或对象）。"""
    in_string = False
    escaped = False
    depth = 0
    start = -1
    for index, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == '\\' and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            if depth == 0:
                start = index
            depth += 1
            continue
        if ch == close_char and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : index + 1]
    return ''


def _load_json_candidate(raw_text: str) -> Any:
    """从 AI 响应中鲁棒提取 JSON（去 code fence + balanced bracket）。"""
    text = raw_text.replace('\ufeff', '').strip()
    candidates: List[str] = []
    base = _strip_code_fence(text)
    if base:
        candidates.append(base)
    for open_char, close_char in (('[', ']'), ('{', '}')):
        snippet = _find_balanced_json(base, open_char, close_char)
        if snippet:
            candidates.append(snippet)

    seen = set()
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return json.loads(normalized)
        except Exception:
            continue
    raise AISegmentationError('智能分段结果不是有效 JSON')


def _coerce_index(value: Any) -> Optional[int]:
    """将值转换为非负整数索引，失败返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        token = value.strip()
        if not token:
            return None
        try:
            number = float(token)
        except Exception:
            return None
        return int(number) if number.is_integer() and number >= 0 else None
    return None


def _parse_index_ranges(
    raw_text: str,
    word_count: int,
) -> List[Tuple[int, int]]:
    """从 AI 响应解析索引范围数组 [{start_index, end_index}]，并做硬契约校验。

    - 每个范围为闭区间 [start, end]，必须连续、无缺口、无重叠、无乱序
    - 必须从 0 开始、到 word_count-1 结束
    - 任何违反契约的情况抛 AISegmentationError，由上层降级到段级/基线
    """
    if word_count <= 0:
        return []

    parsed = _load_json_candidate(raw_text)
    # 兼容 {"ranges": [...]} 等包裹格式
    if isinstance(parsed, dict):
        for key in ('ranges', 'segments', 'items', 'data', 'output'):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                parsed = candidate
                break
    if not isinstance(parsed, list):
        raise AISegmentationError('智能分段结果不是 JSON 数组')

    raw_ranges: List[Tuple[int, int]] = []
    for item in parsed:
        start: Optional[int] = None
        end: Optional[int] = None
        if isinstance(item, list) and len(item) >= 2:
            start = _coerce_index(item[0])
            end = _coerce_index(item[1])
        elif isinstance(item, dict):
            start = _coerce_index(
                item.get('start_index', item.get('start', item.get('from', item.get('begin'))))
            )
            end = _coerce_index(
                item.get('end_index', item.get('end', item.get('to', item.get('stop'))))
            )
            if (start is None or end is None) and isinstance(item.get('indices'), list) and len(item['indices']) >= 2:
                start = _coerce_index(item['indices'][0])
                end = _coerce_index(item['indices'][1])
        if start is None or end is None:
            raise AISegmentationError('智能分段结果包含非法索引')
        if start > end:
            raise AISegmentationError(f'智能分段索引非法: start={start} > end={end}')
        raw_ranges.append((start, end))

    if not raw_ranges:
        raise AISegmentationError('智能分段结果为空')

    return _fill_gap_ranges(raw_ranges, word_count)


def _fill_gap_ranges(
    ranges: List[Tuple[int, int]],
    word_count: int,
) -> List[Tuple[int, int]]:
    """索引契约硬校验：要求 ranges 恰好连续覆盖 [0, word_count-1]，原样返回。

    历史实现会把缺口"并入前一段"、把尾部缺口"追加到最后"——这条修补逻辑是错的：
    输入 [(0,5),(7,9)] 会被整批塌缩成 [(0,9)]，输入乱序 [(6,10),(0,5)] 会产出
    时间重叠且文本重复的两条 cue。修补掩盖了模型返回非法索引的事实，比直接降级更危险。

    当前语义（保留函数名以兼容调用方）：
    - ranges 为空 → 抛错
    - 任一段 start > end，或索引值非法 → 抛错
    - 按 start 升序排序后，第一段必须 start == 0
    - 每段必须 start == prev_end + 1（缺口 = 内容静默丢失，判非法）
    - 任意重叠/乱序/非连续 → 抛错
    - 最后一段必须 end == word_count - 1（尾部未覆盖 = 内容静默丢失，判非法）

    校验通过时原样返回（不做任何修补），失败一律抛 AISegmentationError，
    由 AISegmenter._segment_batch_with_context 的 except 转为降级。
    """
    if not ranges:
        raise AISegmentationError('索引范围为空')

    ordered: List[Tuple[int, int]] = []
    for item in ranges:
        try:
            start = int(item[0])
            end = int(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            raise AISegmentationError(f'索引范围格式非法: {item!r}')
        if start < 0 or end < 0:
            raise AISegmentationError(f'索引范围含负数: [{start}, {end}]')
        if start > end:
            raise AISegmentationError(f'索引范围非法: start={start} > end={end}')
        ordered.append((start, end))

    ordered.sort(key=lambda r: r[0])

    if ordered[0][0] != 0:
        raise AISegmentationError(
            f'索引范围未从 0 开始: 首段 start={ordered[0][0]}（应为 0）'
        )

    previous_end = -1
    for start, end in ordered:
        if start != previous_end + 1:
            raise AISegmentationError(
                f'索引范围不连续: 段 [{start}, {end}] 与上一段 end={previous_end} 之间存在'
                f'缺口或重叠（要求每段 start == 上一段 end + 1）'
            )
        previous_end = end

    if previous_end != word_count - 1:
        raise AISegmentationError(
            f'索引范围未覆盖全部内容: 末段 end={previous_end}，词总数={word_count}'
            f'（应为 {word_count - 1}）'
        )

    return ordered


def _cues_from_index_ranges(
    ranges: List[Tuple[int, int]],
    words: List[AsrWordTiming],
    provider: str,
    logger=None,
) -> List[AlignedSubtitleCue]:
    """将索引范围映射回 AlignedSubtitleCue（使用原始词时间戳）。

    任一索引范围越界即整批拒绝（抛 AISegmentationError）：
    旧实现只 warning + continue，会静默丢弃该区间的字幕内容，而"部分内容消失"
    比整批降级到段级/基线更糟——降级至少保证内容完整。
    空文本区间仍然跳过（安全）：文本由 _words_to_text 从 ASR 原文切片，
    空文本意味着该区间内没有有效词，不构成内容丢失。
    """
    cues: List[AlignedSubtitleCue] = []
    for start_idx, end_idx in ranges:
        if start_idx < 0 or end_idx >= len(words):
            raise AISegmentationError(
                f'索引范围越界: [{start_idx}, {end_idx}], 词总数: {len(words)}'
            )
        cue_words = words[start_idx:end_idx + 1]
        text = _words_to_text(cue_words)
        if not text:
            continue
        cues.append(AlignedSubtitleCue(
            start_s=cue_words[0].start_s,
            end_s=cue_words[-1].end_s,
            text=text,
            provider=provider,
            timing_source='ai',
            # 置信度按来源可靠度分层（见 _cues_from_response/_baseline_align_batch 注释）：
            # 字级 AI 的时间戳取自 ASR 词边界，只受分段决策影响，故最高。
            alignment_confidence=0.90,
        ))
    return cues


# 允许 AI 新增的标点（不视为"凭空造字"），含 ASCII 与常见全角标点
# 常见全角/中文/日文标点与纯符号字符。
# 覆盖面不足会造成**误杀**：模型把 `《标题》` 换成 `【标题】`、把 `・` 删掉，
# 或把 `©` 改写掉时，未被识别为标点的字符会被算成"造字/丢字" → 整批降级。
# 字符只需"不承载实词内容"，因此用码位区间而非逐个枚举：
#   U+2010-U+2015 各类连字符与破折号；U+2018-U+201F 引号；U+2026 省略号
#   U+3001-U+3003 、。〃；U+3008-U+3011 〈〉《》「」『』【】〕〖〗；U+3014-U+301B
#   U+30FB 片假名中点；U+FF01-U+FF0F / U+FF1A-U+FF20 / U+FF3B-U+FF40 / U+FF5B-U+FF65
#   为全角标点（**必须**避开 U+FF10-U+FF19 全角数字与 U+FF21-U+FF5A 全角字母）
#   U+00A9 ©、U+00AE ®、U+2122 ™、U+00B0 °、U+2116 № 等纯符号
_EXTRA_PUNCTUATION = frozenset(
    '，。！？；：、（）「」『』【】…·—～'
    '\u3000\u2018\u2019\u201c\u201d\uFF01\uFF1F\uFF1B\uFF1A\uFF0C\uFF0E'
    '\u2010\u2011\u2012\u2013\u2014\u2015\u2026\u2018\u2019\u201a\u201b'
    '\u201c\u201d\u201e\u201f\u3001\u3002\u3003\u3008\u3009\u300a\u300b'
    '\u300c\u300d\u300e\u300f\u3010\u3011\u3014\u3015\u3016\u3017\u3018'
    '\u3019\u301a\u301b\u30fb\uFF0D\uFF5E\uFF5F\uFF60\uFF61\uFF62\uFF63'
    '\uFF64\uFF65\u00a9\u00ae\u2122\u00b0\u2116\u301c\uFF5B\uFF5D\uFF3B'
    '\uFF3D\uFF5C\uFF1C\uFF1E\uFF20\uFF3E\uFF40\uFF3F'
)


def _is_punctuation_char(ch: str) -> bool:
    """判断是否为标点（ASCII 标点、常见全角标点或纯符号）。"""
    if not ch:
        return False
    if ch in string.punctuation or ch in _EXTRA_PUNCTUATION:
        return True
    code = ord(ch)
    return (
        0x2010 <= code <= 0x2015
        or 0x2018 <= code <= 0x201F
        or 0x3001 <= code <= 0x3003
        or 0x3008 <= code <= 0x3011
        or 0x3014 <= code <= 0x301B
        or 0xFF01 <= code <= 0xFF0F
        or 0xFF1A <= code <= 0xFF20
        or 0xFF3B <= code <= 0xFF40
        or 0xFF5B <= code <= 0xFF65
    )


def _strip_for_coverage(text: str) -> str:
    """去空白与标点，得到用于覆盖率比较的纯内容字符序列。

    先去空白/标点，再做 NFKC 归一化：模型把全角数字 `１２３` 写成 `123`、
    或把兼容字符写成基本字符，都属于等价书写，不构成内容变更。
    """
    filtered = ''.join(
        ch for ch in str(text or '')
        if not ch.isspace() and not _is_punctuation_char(ch)
    )
    return unicodedata.normalize('NFKC', filtered)


# 中文数字写法归一：ASR 常输出「一二三」，模型归一时可能写成「123」
# （或反之）。两者是同一信息，不应判为丢字或造字 —— 这正是数字密集内容
# （年份/价格/公式）被判「造字」而整批降级的高发来源。
_CJK_DIGIT_MAP = {
    '零': 0, '〇': 0,
    '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
}
_CJK_UNIT_MAP = {'十': 10, '百': 100, '千': 1000}
_CJK_SECTION_UNIT_MAP = {'万': 10000, '亿': 100000000}
_CJK_NUMERAL_RUN_RE = re.compile(
    '[' + ''.join(_CJK_DIGIT_MAP) + ''.join(_CJK_UNIT_MAP) + ''.join(_CJK_SECTION_UNIT_MAP) + ']+'
)


def _cjk_numeral_run_to_arabic(run: str) -> str:
    """把一段纯中文数字串转换成阿拉伯数字串；不是数字表达式时原样返回。

    两种读法必须区分，否则会引入新的误判：

    - 不含单位（十/百/千/万/亿）：按**逐位**读，`一二三` → `123`；
    - 含单位：按**数值**读，`三百六十五` → `365`、`两千零二十四` → `2024`。

    统一按数值读会把 `一二三` 变成 `3`，那是真实的丢字，不能容忍。

    另一条前置条件：整串必须**至少含一个中文数字**，否则原样返回。
    没有这条，孤立的单位字会被塌成数字：`百分之六十` 里的 `百`（词素，不是
    数量）会变成 `100`、`1.2 万` 里的 `万` 会变成 `0` —— 后者甚至会让完全不同
    的内容被判为相同，反而掩盖真实的丢字。
    """
    if not any(ch in _CJK_DIGIT_MAP for ch in run):
        return run
    if not any(ch in _CJK_UNIT_MAP or ch in _CJK_SECTION_UNIT_MAP for ch in run):
        return ''.join(str(_CJK_DIGIT_MAP[ch]) for ch in run)

    total = 0
    section = 0
    number = 0
    for ch in run:
        if ch in _CJK_DIGIT_MAP:
            number = _CJK_DIGIT_MAP[ch]
        elif ch in _CJK_UNIT_MAP:
            unit = _CJK_UNIT_MAP[ch]
            section += (number or 1) * unit
            number = 0
        else:  # 万 / 亿
            section = (section + number) * _CJK_SECTION_UNIT_MAP[ch]
            total += section
            section = 0
            number = 0
    return str(total + section + number)


def _fold_digits(text: str) -> str:
    """数字写法归一：中文数字统一折叠为阿拉伯数字，便于逐字符比较。

    只做替换、不改变与数字无关的字符，因此对源文本与输出文本是对称的：
    误折叠最多让两个不同写法被判为相同，不会凭空判出缺失。
    """
    return _CJK_NUMERAL_RUN_RE.sub(
        lambda match: _cjk_numeral_run_to_arabic(match.group(0)), str(text or '')
    )


# 可被 AI 安全删除的填充词/口吃（英文与中文语气词）。
# 注意：只覆盖无实义的填充词，不覆盖任何实词，避免把正常改写当"允许丢弃"。
# 前后用非单词字符（而不是 \b）做边界，保证 `world` 里的 `or`/`er` 不会被误删。
_FILLER_DELETABLE_RE = re.compile(
    r'(?<![A-Za-z])(?:um|uh|er|ah|hmm|mm|uhm|erm)(?![A-Za-z])|[嗯啊呃哦唔]',
    re.IGNORECASE,
)


def _strip_deletable_fillers(text: str) -> str:
    """先从待比较文本中移除填充词，得到"实词骨架"。

    填充词（`um`/`uh`/`hmm`/`嗯`…）在 ASR 原文中真实存在，模型按 prompt
    清理它们是允许的行为，不应被判成"丢字"。因此覆盖率比较前把两侧文本
    都做同样的预删除 —— 这样比较的才是实词骨架，而不是把阈值整体放宽。
    """
    return _FILLER_DELETABLE_RE.sub('', str(text or ''))


def _deletable_filler_chars(text: str) -> int:
    """统计文本中填充词的字符数（仅用于诊断日志）。"""
    return sum(len(match.group(0)) for match in _FILLER_DELETABLE_RE.finditer(str(text or '')))


def _coverage_metrics(src_text: str, out_text: str) -> tuple:
    """按多重集比较源文本与输出文本，返回 (覆盖率, 缺失字符样本)。

    为什么必须是多重集而不是集合：集合只判断"字符是否出现过"，
    源 `abab` → 输出 `ab` 这种丢掉一半内容的输出会被判成 100% 覆盖。
    多重集用 Counter 逐字符计次（min(源计数, 输出计数) 求和 / 源长度）
    才能反映真实内容留存率。

    填充词预删除：ASR 原文中的 `um`/`uh`/`嗯` 是被 prompt 允许清理的填充词，
    因此比较前对源与输出做同样的预删除，只在"实词骨架"上要求 0.9 留存率。
    预删除清单固定且很短，不会掩盖实词丢失：`abab`→`ab`、`a`*100→`a`*50
    这类输出预删除后覆盖率仍为 0.5，照样拒绝。
    """
    src_chars = _fold_digits(_strip_for_coverage(_fold_case(_strip_deletable_fillers(src_text))))
    out_chars = _fold_digits(_strip_for_coverage(_fold_case(_strip_deletable_fillers(out_text))))
    total = len(src_chars)
    if total == 0:
        return 1.0, ''

    src_counter = Counter(src_chars)
    out_counter = Counter(out_chars)
    covered = sum(min(count, out_counter[ch]) for ch, count in src_counter.items())
    coverage = covered / total

    missing = src_counter - out_counter
    missing_sample = ''.join(sorted(missing.elements()))[:40]
    return coverage, missing_sample


_ASCII_LOWER_TABLE = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _fold_case(text: str) -> str:
    """把 ASCII 大写折叠为小写，用于内容比较。

    大小写不是字幕内容：模型把句首字母大写（`hello` → `Hello`）是最基本的
    书写规范，绝不构成「丢字」或「造字」。此前的比较是大小写敏感的，导致
    任何一句被正确大写的输出都会命中「输出含原文不存在的非标点字符」而被整批
    拒绝并降级 —— 这是覆盖率闸门最常见的假阳性来源。

    只折叠 ASCII A-Z（长度不变），不使用 str.lower()/casefold()：
    后者对个别字符（如 `İ`、`ß`）会改变长度，破坏基于字符数的比率。
    """
    return str(text or '').translate(_ASCII_LOWER_TABLE)


def _assert_text_coverage(
    src_text: str,
    out_cues: List[AlignedSubtitleCue],
    *,
    context: str,
    logger=None,
) -> None:
    """文本覆盖率闸门：AI 只能重排/重标点，不能丢字或造字。

    两条硬约束（任一违反抛 AISegmentationError，由上层转降级）：
    1. 覆盖率按**多重集**计算（见 _coverage_metrics）：必须 >= 0.9。
       逐字符计次的留存率能抓住"源 abab → 输出 ab"这类丢一半内容的输出，
       而集合归属判定会把它们判成 100% 覆盖。
       比较前做 NFKC 归一化、ASCII 大小写折叠与中文数字折叠，避免把
       `１２３`→`123`、`hello`→`Hello`、`一年`→`1年` 这类等价书写
       误判为丢字/造字。
       删除配额：允许 AI 删除无实义填充词与重复口吃（ASR 清理的正常行为）。
       原文为空时直接放行（无内容可校验）。
    2. 输出中原文不存在的非标点字符必须为空（只允许 AI 新增标点），
       否则说明输出出现了原文不存在的字词（幻觉/串批）。
       同样先做填充词预删除 + NFKC + 大小写折叠 + 数字折叠。

    权衡说明：字级出口的原文来自 ASR 词表，填充词（`uh`/`嗯` 等）确实存在
    于原文中，模型清理它们是 prompt 明确允许的行为；因此这里用"两侧同步预删除
    填充词"而不是把阈值整体放宽 —— 预删除只作用于固定填充词清单，实词丢失
    仍按 0.9 严格拒绝（`abab`→`ab`、`a`*100→`a`*50 均被拒）。
    """
    out_text = ''.join(str(c.text or '') for c in out_cues or [])
    src_raw = str(src_text or '')
    if not _strip_for_coverage(_strip_deletable_fillers(src_raw)):
        # 无实词内容（空批次/纯标点/纯语气词）→ 无内容可校验
        return

    coverage, missing_sample = _coverage_metrics(src_raw, out_text)
    if coverage < 0.9:
        message = (
            f'[{context}] 文本覆盖率不足: {coverage:.3f} < 0.9 '
            f'(原文 {len(_strip_for_coverage(src_raw))} 字符 / '
            f'输出 {len(_strip_for_coverage(out_text))} 字符, '
            f'可删填充词字符={_deletable_filler_chars(_strip_for_coverage(src_raw))}, '
            f'缺失样本={missing_sample!r}, cue {len(out_cues or [])} 条)'
        )
        if logger:
            logger.warning(message)
        raise AISegmentationError(message)

    src_set = set(_fold_digits(_strip_for_coverage(_fold_case(src_raw))))
    extra = set(_fold_digits(_strip_for_coverage(_fold_case(out_text)))) - src_set
    extra.discard('')
    if extra:
        sample = ' '.join(sorted(extra))[:40]
        message = f'[{context}] 输出含原文不存在的非标点字符: {sample}'
        if logger:
            logger.warning(message)
        raise AISegmentationError(message)


def _snap_or_interpolate(
    value: float,
    spans: List[Tuple[float, float]],
    interpolation_points: List[float],
) -> Optional[Tuple[float, str]]:
    """把单个时间点解析成 ``(合法时间, 来源)``；无法解析时返回 ``None``。

    ``来源`` 为 ``'boundary'``（吸附到输入段边界，权威时间轴）或
    ``'segment_interp'``（段内切点，非输入边界）—— 下游需要区分二者：
    段内切点是模型给出的近似值，而边界吸附是可信时间。

    解析优先级：
    1. 吸附到最近的输入段边界（容差 `_BOUNDARY_SNAP_TOL_S` 内）；
    2. 命中的"段内插值点"原样保留（该点必须是输出时间点之一，且严格落在
       某个真实输入区间内部）；
    3. 两者都不满足 → ``None``（调用方整批拒绝并降级）。

    注意：插值点不能放宽为"任意落在区间内部的时间点"，否则等于取消边界吸附 ——
    模型输出的 2.5s 会被原样放行，越界与乱跳防护同时失效。
    """
    value = float(value)
    nearest = None
    nearest_distance = None
    for lo, hi in spans:
        for boundary in (lo, hi):
            distance = abs(boundary - value)
            if nearest_distance is None or distance < nearest_distance:
                nearest = boundary
                nearest_distance = distance
    if nearest is not None and nearest_distance is not None \
            and nearest_distance <= _BOUNDARY_SNAP_TOL_S:
        return float(nearest), 'boundary'
    for point in interpolation_points:
        if abs(float(point) - value) <= _BOUNDARY_SNAP_TOL_S:
            return float(point), 'segment_interp'
    return None


def _segment_boundaries(segments: List[AsrSegmentTiming]) -> List[float]:
    """从输入段构造允许的时间边界集合（所有 start/end，去重升序）。"""
    boundaries = set()
    for seg in segments:
        boundaries.add(round(float(seg.start_s), 3))
        boundaries.add(round(float(seg.end_s), 3))
    return sorted(boundaries)


def _boundaries_to_spans(boundaries: List[float]) -> List[Tuple[float, float]]:
    """把有序边界列表还原成最小输入段区间。

    边界的相邻两项构成一个"段跨度"，这是"输出时间必须落在输入内容覆盖范围内"
    这一约束的最紧近似：跨段跳出去的时间点会落在没有任何输入内容的空隙里。
    """
    spans: List[Tuple[float, float]] = []
    for left, right in zip(boundaries, boundaries[1:]):
        if right > left:
            spans.append((float(left), float(right)))
    return spans


def _accepts_time(value: float, span: Tuple[float, float]) -> bool:
    """判断一个时间点是否可能是该段跨度的"段内切点"。

    条件：落在跨度闭区间内，且与跨度端点的距离大于吸附容差 —— 端点附近的
    时间点已经由 `_snap_or_interpolate` 吸附处理，这里只保留真正的段内切点。
    """
    start, end = span
    if not (start < value < end):
        return False
    return (value - start) > _BOUNDARY_SNAP_TOL_S and (end - value) > _BOUNDARY_SNAP_TOL_S


def _legal_interpolation_points(values: List[float], span: Tuple[float, float]) -> List[float]:
    """在段跨度内找出合法段内切点。

    切点条件（与段级 prompt 的"可将一段拆为多条"一致）：
    - 该跨度的起止时间都出现在输出时间点里，说明这一段被输出**完整覆盖**，
      而不是只覆盖一半就丢掉剩余内容（丢内容另有文本覆盖率闸门兜底）；
    - 该点严格落在跨度内部，且与跨度端点的距离大于吸附容差 —— 端点附近的
      时间点已由 `_snap_or_interpolate` 吸附处理。

    为什么不能再加"附近必须有另一个锚点"这类配对条件：段内切点按定义就是
    孤立的。把 30s 的段拆成两条时，切点 14.8s 与最近端点相距 14.8s/15.2s，
    永远不存在容差量级内的邻居 —— 加上该条件会让"拆长段"这一**唯一**需要
    段内切点的用途整批失败，插值支持形同虚设（实测 A/B/F 三类拆段全部被拒）。

    乱跳与越界不靠邻居判据拦截，而由两道更强的约束保证：
    1. 切点必须严格落在**某个输入跨度内部** —— 落在 VAD 判定无语音的空隙里的
       时间点（例如跨段的 15s）不属于任何跨度，仍被拒绝；
    2. `_interpolate_segment_cues` 末尾的内容覆盖范围交叉校验。
    """
    start, end = span
    if start not in values or end not in values:
        return []
    return [v for v in values if _accepts_time(v, span)]

def _interpolate_segment_cues(
    cleaned: List[Dict[str, Any]],
    spans: List[Tuple[float, float]],
    logger,
) -> List[Dict[str, Any]]:
    """段级时间校验：边界吸附优先，段内切点按同一跨度的连续证据放行。

    返回 [] 表示整批拒绝（越界/逆序/跨段乱跳/时间重叠），由上层降级。

    为什么需要段内切点：段级行为层明确要求"可将一段拆为多条"，而单段本身可能
    长于 `AI_SEGMENTATION_MAX_CUE_DURATION_S`，用输入段边界表达段内切点在数学上
    不可能（容差 0.3s 永远比不上 15s 的段内距离），容忍策略会让"拆长段"这一
    主要用途整批失败并降级。

    放行条件严格限定为"该跨度被完整覆盖 + 跨度内成对出现的连续切点"：
    - 一条 cue 只覆盖半个段（没有与之配对的切分）→ 仍判拒绝；
    - 时间落在所有输入段之外（越界）→ 拒绝；
    - 时间与输入段完全没有交集（跨段乱跳）→ 拒绝。
    """
    if not cleaned:
        return []
    values = sorted({float(c['start_s']) for c in cleaned} | {float(c['end_s']) for c in cleaned})

    # 段内插值点必须同时是"输出时间点之一 + 满足跨度内切点条件"
    interpolation_points: List[float] = []
    for span in spans:
        for point in _legal_interpolation_points(values, span):
            if point not in interpolation_points:
                interpolation_points.append(point)

    snapped: List[Dict[str, Any]] = []
    for c in cleaned:
        resolved_start = _snap_or_interpolate(c['start_s'], spans, interpolation_points)
        resolved_end = _snap_or_interpolate(c['end_s'], spans, interpolation_points)
        if resolved_start is None or resolved_end is None:
            bad_time = c['start_s'] if resolved_start is None else c['end_s']
            bad_field = 'start_s' if resolved_start is None else 'end_s'
            logger.warning(
                '段级 AI 时间既不在输入段边界上、也不落在任何输入区间内部: %s=%.3f '
                '（输入区间 %d 个），整批拒绝并降级',
                bad_field, bad_time, len(spans),
            )
            return []
        new_start, start_source = resolved_start
        new_end, end_source = resolved_end
        if new_end <= new_start:
            # 逆序/零长度：非"未吸附"类问题，直接整批拒绝（由上层降级）
            logger.warning(
                '段级 AI 输出时间逆序: [%.3f, %.3f]，整批拒绝并降级', new_start, new_end,
            )
            return []
        item = {'start_s': new_start, 'end_s': new_end, 'text': c['text']}
        # 任一端取自段内切点就标记：该时间是模型给出的近似值，不是输入段边界，
        # 下游据此可以区分「可信的边界吸附」与「段内插值」。
        if 'segment_interp' in (start_source, end_source):
            item['timing_source'] = 'segment_interp'
        snapped.append(item)
    if not snapped:
        return []

    # 交叉校验：所有输出时间必须落在输入内容覆盖的范围内（越界/乱跳一律拒绝）
    covered_lo = min(span[0] for span in spans)
    covered_hi = max(span[1] for span in spans)
    for item in snapped:
        if item['start_s'] < covered_lo - _BOUNDARY_SNAP_TOL_S or \
                item['end_s'] > covered_hi + _BOUNDARY_SNAP_TOL_S:
            logger.warning(
                '段级 AI 输出时间越出输入内容覆盖范围: [%.3f, %.3f] 不在 [%.3f, %.3f] 内，'
                '整批拒绝并降级',
                item['start_s'], item['end_s'], covered_lo, covered_hi,
            )
            return []
    return snapped


def _parse_cues_response(
    parsed: Optional[Dict[str, Any]],
    batch_start_s: float,
    batch_end_s: float,
    input_count: int,
    input_boundaries: Optional[List[float]] = None,
    total_duration_s: Optional[float] = None,
    input_spans: Optional[List[Tuple[float, float]]] = None,
) -> List[Dict[str, Any]]:
    """校验并清洗 AI 返回的 cues。

    基础校验：
    - 必须是 {"cues": [...]}
    - 每条 start_s < end_s，且落在批次时间范围内（允许 0.5s 容差）
    - 按时间升序、去重叠
    - 数量合理（1 ~ input_count*2 + 4，防异常膨胀）

    增强校验（input_boundaries / total_duration_s 提供时启用）：
    - 时间吸附：start_s / end_s 优先吸附到最近的输入段边界（±_BOUNDARY_SNAP_TOL_S）。
      时间是 AI 生成的近似值，不能直接当权威时间轴；吸附保证输出边界尽量取
      输入段的真实边界。找不到容差内边界的点，只有在该点严格落在**某个输入区间
      内部**时，才按"段内插值切点"放行（标记 timing_source='segment_interp'）
      ——段级 prompt 允许把一段拆为多条，而段内切点在数学上不可能落在输入边界上。
      越界（落在输入内容覆盖范围之外）、逆序、时间重叠仍一律整批返回 []
      （触发降级），防护未被削弱。

    ``input_spans`` 必须传**真实输入区间**（段的 start/end 对）：调用方若省略，
    这里退化为用相邻边界对拼出区间，而相邻边界对里包含「段与段之间的静音空隙」
    ——空隙被当成合法区间会把"时间跳到无语音区"放行，因此能传就必须传。
    - 时长上界：total_duration_s > 0 时把 end_s 钳制到总时长、start_s 抬到 >= 0。
    - 文本覆盖率闸门不在此函数内，见 _assert_text_coverage（两级 AI 出口共用）。
    """
    if not isinstance(parsed, dict):
        return []
    raw_cues = parsed.get('cues')
    if not isinstance(raw_cues, list) or not raw_cues:
        return []

    tol = 0.5
    lo = batch_start_s - tol
    hi = batch_end_s + tol
    cleaned: List[Dict[str, Any]] = []
    for item in raw_cues:
        if not isinstance(item, dict):
            continue
        raw_start = item.get('start_s', item.get('start', item.get('start_time')))
        raw_end = item.get('end_s', item.get('end', item.get('end_time')))
        if raw_start is None or raw_end is None:
            continue
        try:
            start_s = float(raw_start)
            end_s = float(raw_end)
        except (TypeError, ValueError):
            continue
        text = str(item.get('text', '') or '').strip()
        if not text:
            continue
        if end_s <= start_s:
            continue
        # 钳制到批次范围
        start_s = max(lo, min(hi, start_s))
        end_s = max(lo, min(hi, end_s))
        if end_s <= start_s:
            continue
        cleaned.append({'start_s': start_s, 'end_s': end_s, 'text': text})

    if not cleaned:
        return []

    # 时间校验：边界吸附优先，段内切点按"落在真实输入区间内部"放行
    if input_boundaries:
        if input_spans:
            spans = sorted((float(lo), float(hi)) for lo, hi in input_spans if float(hi) > float(lo))
        else:
            spans = _boundaries_to_spans(sorted({round(float(b), 3) for b in input_boundaries}))
        cleaned = _interpolate_segment_cues(
            cleaned, spans, logging.getLogger(__name__),
        )
        if not cleaned:
            return []

    # 时长上界钳制（安全兜底：防止输出时间轴溢出视频总时长）
    if total_duration_s is not None and float(total_duration_s) > 0:
        limit = float(total_duration_s)
        clamped: List[Dict[str, Any]] = []
        for c in cleaned:
            new_start = max(0.0, min(limit, c['start_s']))
            new_end = max(0.0, min(limit, c['end_s']))
            if new_end <= new_start:
                continue
            clamped.append({'start_s': new_start, 'end_s': new_end, 'text': c['text']})
        cleaned = clamped
        if not cleaned:
            return []

    # 升序 + 去重叠
    cleaned.sort(key=lambda c: c['start_s'])
    deduped: List[Dict[str, Any]] = []
    for c in cleaned:
        if deduped and c['start_s'] < deduped[-1]['end_s'] - 0.001:
            if input_boundaries:
                # 吸附模式下不做截断：截断会把 start_s 推到非边界值，
                # 违反"时间必须取自输入段边界"的契约 → 整批拒绝并降级
                logging.getLogger(__name__).warning(
                    '段级 AI 输出时间重叠: [%.3f, %.3f] 与上一条 end=%.3f 冲突，整批拒绝并降级',
                    c['start_s'], c['end_s'], deduped[-1]['end_s'],
                )
                return []
            # 重叠：跳过或截断到上一条结尾
            new_start = deduped[-1]['end_s']
            if c['end_s'] > new_start + 0.05:
                c = {'start_s': new_start, 'end_s': c['end_s'], 'text': c['text']}
            else:
                continue
        deduped.append(c)

    max_allowed = max(8, input_count * 2 + 4)
    if len(deduped) > max_allowed:
        deduped = deduped[:max_allowed]
    return deduped


def _cues_from_response(
    cues_data: List[Dict[str, Any]],
    timing_source: str,
    provider: str,
) -> List[AlignedSubtitleCue]:
    # 置信度按来源可靠度分层，避免常量决定下游冲突仲裁：
    # 段级 AI 只能在粗粒度段边界上重排（0.70），低于字级 AI（0.90），
    # 高于无字级时的按段直转（0.60）；下游 resolve_overlaps 按此值仲裁。
    return [
        AlignedSubtitleCue(
            start_s=c['start_s'],
            end_s=c['end_s'],
            text=c['text'],
            provider=provider,
            timing_source=timing_source,
            alignment_confidence=0.70,
        )
        for c in cues_data
    ]


# ---------------------------------------------------------------------------
# 基线对齐（AI 失败时的批次兜底，按段直转 cue）
# ---------------------------------------------------------------------------

def _baseline_align_batch(batch: _Batch, provider: str) -> List[AlignedSubtitleCue]:
    """AI 失败时的批次兜底。

    置信度按来源可靠度分层，避免常量决定下游 resolve_overlaps 的冲突仲裁：
    有字级时间戳时按词边界聚合（0.85），无字级时只能按段直转（0.60）。
    """
    cues: List[AlignedSubtitleCue] = []
    if batch.has_word_timestamps and batch.words:
        # 按段语义不可得时，按词序列每 N 个词聚成一条（保守：每 12 词或遇句末标点切）
        unit: List[AsrWordTiming] = []
        for w in batch.words:
            unit.append(w)
            if len(unit) >= 12 or _SENTENCE_SPLIT_RE.search(str(w.text or '')):
                cues.append(AlignedSubtitleCue(
                    start_s=unit[0].start_s,
                    end_s=unit[-1].end_s,
                    text=_words_to_text(unit),
                    provider=provider,
                    timing_source='word',
                    alignment_confidence=0.85,
                ))
                unit = []
        if unit:
            cues.append(AlignedSubtitleCue(
                start_s=unit[0].start_s,
                end_s=unit[-1].end_s,
                text=_words_to_text(unit),
                provider=provider,
                timing_source='word',
                alignment_confidence=0.85,
            ))
    else:
        for seg in batch.segments:
            cues.append(AlignedSubtitleCue(
                start_s=seg.start_s,
                end_s=seg.end_s,
                text=str(seg.text or '').strip(),
                provider=provider,
                timing_source='segment',
                alignment_confidence=0.60,
            ))
    return cues


# ---------------------------------------------------------------------------
# 节奏后处理：保证显示时长不过短（用户核心诉求）
# ---------------------------------------------------------------------------

def _visual_text_length(text: str) -> float:
    """可见字符数（CJK 计 1，ASCII 字母计 0.6，近似视觉宽度）。"""
    total = 0.0
    for ch in str(text or ''):
        if ch.isspace():
            continue
        if _CJK_CHAR_RE.match(ch):
            total += 1.0
        elif ch.isascii() and ch.isalnum():
            total += 0.6
        else:
            total += 0.8
    return total


def _cps(text: str, duration_s: float) -> float:
    safe_dur = max(float(duration_s or 0.0), 0.1)
    return _visual_text_length(text) / safe_dur


def _crosses_sentence_boundary(left_text: str, right_text: str) -> bool:
    """判断合并两条 cue 是否跨越了句子边界。

    如果左 cue 以句末标点结尾（.!?。！？），且右 cue 以新句开头（大写字母或中文非标点字符），
    则认为跨越了句子边界，不应合并。
    """
    if not left_text or not right_text:
        return False
    left_end = left_text.rstrip()[-1:] if left_text.rstrip() else ''
    right_start = right_text.lstrip()[:1] if right_text.lstrip() else ''
    if left_end in _SENTENCE_END_PUNCTS:
        # 右侧以大写字母或中文字符开头 → 新句起始
        if right_start and (right_start[0].isupper() or _CJK_CHAR_RE.match(right_start)):
            return True
    return False


def _merge_short_cues(
    cues: List[AlignedSubtitleCue],
    min_duration_s: float,
    max_duration_s: float,
    max_cps: float,
) -> List[AlignedSubtitleCue]:
    """把短于 min_duration_s 的条目与相邻条目合并；无法合并则延长结尾。

    优先向前合并下一条（保留语意延续），其次向后合并上一条；
    都不满足则保留原条目（末条可延长结尾到 min_duration）。
    """
    if not cues:
        return cues
    work = list(cues)
    result: List[AlignedSubtitleCue] = []
    i = 0
    while i < len(work):
        cue = work[i]
        duration = cue.end_s - cue.start_s
        if duration >= min_duration_s:
            result.append(cue)
            i += 1
            continue

        merged_into_next = False
        # 优先向前合并下一条
        if i + 1 < len(work):
            nxt = work[i + 1]
            gap = nxt.start_s - cue.end_s
            combined_text = (cue.text + ' ' + nxt.text).strip() if cue.text and nxt.text else (cue.text or nxt.text)
            combined_dur = nxt.end_s - cue.start_s
            if (
                gap <= 0.3
                and combined_dur <= max_duration_s
                and _cps(combined_text, combined_dur) <= max_cps
                and not _crosses_sentence_boundary(cue.text or '', nxt.text or '')
            ):
                merged = AlignedSubtitleCue(
                    start_s=cue.start_s, end_s=nxt.end_s, text=combined_text,
                    provider=cue.provider, timing_source=cue.timing_source,
                    alignment_confidence=cue.alignment_confidence,
                )
                work[i] = merged
                work.pop(i + 1)
                merged_into_next = True
                # 重新评估合并后的 cue（可能仍短，可继续合并）
        if merged_into_next:
            continue

        # 其次向后合并上一条
        if result:
            prev = result[-1]
            gap = cue.start_s - prev.end_s
            combined_text = (prev.text + ' ' + cue.text).strip() if prev.text and cue.text else (prev.text or cue.text)
            combined_dur = cue.end_s - prev.start_s
            if (
                gap <= 0.3
                and combined_dur <= max_duration_s
                and _cps(combined_text, combined_dur) <= max_cps
                and not _crosses_sentence_boundary(prev.text or '', cue.text or '')
            ):
                prev.end_s = cue.end_s
                prev.text = combined_text
                i += 1
                continue

        # 无法合并：保留（末条延长结尾）
        if i == len(work) - 1:
            cue.end_s = cue.start_s + min_duration_s
        result.append(cue)
        i += 1
    return result


def _merge_suboptimal_cues(
    cues: List[AlignedSubtitleCue],
    min_duration_s: float,
    max_duration_s: float,
    max_cps: float,
    ideal_min_s: float = 2.0,
    ideal_max_s: float = 4.0,
) -> List[AlignedSubtitleCue]:
    """主动合并处于非理想区间的相邻短条目。

    区别于 _merge_short_cues（仅处理 < min_duration_s 的条目）：
    本函数处理 [min_duration_s, ideal_min_s) 区间内的"勉强达标但偏短"的条目，
    若与下一条合并后落在理想区间 [ideal_min_s, ideal_max_s] 内且不超 CPS/最长限制，则合并。

    这样可以避免「1.0-1.5s 短句堆积」这类 AI 虽满足阈值但观感不佳的情况。
    """
    if not cues:
        return cues
    work = list(cues)
    result: List[AlignedSubtitleCue] = []
    i = 0
    while i < len(work):
        cue = work[i]
        duration = cue.end_s - cue.start_s
        # 已在理想区间或更长：直接保留
        if duration >= ideal_min_s:
            result.append(cue)
            i += 1
            continue

        # 偏短（min_duration_s <= duration < ideal_min_s）：尝试与下一条合并到理想区间
        merged_into_next = False
        if i + 1 < len(work):
            nxt = work[i + 1]
            gap = nxt.start_s - cue.end_s
            combined_text = (cue.text + ' ' + nxt.text).strip() if cue.text and nxt.text else (cue.text or nxt.text)
            combined_dur = nxt.end_s - cue.start_s
            # 合并条件：间隙小、合并后不超最长、CPS 不超标、合并后落在理想区间或至少显著更长
            if (
                gap <= 0.5
                and combined_dur <= max_duration_s
                and _cps(combined_text, combined_dur) <= max_cps
                and combined_dur <= ideal_max_s
                and combined_dur > duration  # 合并后必须更长
                and not _crosses_sentence_boundary(cue.text or '', nxt.text or '')
            ):
                merged = AlignedSubtitleCue(
                    start_s=cue.start_s, end_s=nxt.end_s, text=combined_text,
                    provider=cue.provider, timing_source=cue.timing_source,
                    alignment_confidence=cue.alignment_confidence,
                )
                work[i] = merged
                work.pop(i + 1)
                merged_into_next = True
                # 重新评估（可能仍偏短，继续合并下一条）
        if merged_into_next:
            continue

        # 无法与下一条合并：尝试与上一条合并（仅当上一条也偏短）
        if result:
            prev = result[-1]
            prev_dur = prev.end_s - prev.start_s
            if prev_dur < ideal_min_s:
                gap = cue.start_s - prev.end_s
                combined_text = (prev.text + ' ' + cue.text).strip() if prev.text and cue.text else (prev.text or cue.text)
                combined_dur = cue.end_s - prev.start_s
                if (
                    gap <= 0.5
                    and combined_dur <= max_duration_s
                    and _cps(combined_text, combined_dur) <= max_cps
                    and combined_dur <= ideal_max_s
                    and not _crosses_sentence_boundary(prev.text or '', cue.text or '')
                ):
                    prev.end_s = cue.end_s
                    prev.text = combined_text
                    i += 1
                    continue

        # 都不合并：保留原条目
        result.append(cue)
        i += 1
    return result


def _split_long_cue(cue: AlignedSubtitleCue, max_duration_s: float) -> List[AlignedSubtitleCue]:
    """过长度条目按句末标点切分；切不动则按逗号/顿号切分；最后按虚词/中点兜底。"""
    duration = cue.end_s - cue.start_s
    if duration <= max_duration_s:
        return [cue]
    text = str(cue.text or '')

    # 第一级：按句末标点切分（.!?。！？；;）
    result = _split_at_pattern(text, duration, cue, _SENTENCE_SPLIT_RE, max_duration_s)
    if result:
        return result

    # 第二级：按逗号/顿号切分（,，、）
    result = _split_at_pattern(text, duration, cue, _CLAUSE_SPLIT_RE, max_duration_s)
    if result:
        return result

    # 第三级：兜底——优先在中文虚词前切分，其次空格，最后中点
    mid_pos = _find_best_fallback_split(text)
    mid_s = cue.start_s + duration * mid_pos / max(1, len(text))
    left_text = text[:mid_pos].strip()
    right_text = text[mid_pos:].strip()
    if not left_text or not right_text:
        return [cue]
    return [
        AlignedSubtitleCue(start_s=cue.start_s, end_s=mid_s, text=left_text,
                           provider=cue.provider, timing_source=cue.timing_source,
                           alignment_confidence=cue.alignment_confidence),
        AlignedSubtitleCue(start_s=mid_s, end_s=cue.end_s, text=right_text,
                           provider=cue.provider, timing_source=cue.timing_source,
                           alignment_confidence=cue.alignment_confidence),
    ]


def _split_at_pattern(
    text: str, duration: float, cue: AlignedSubtitleCue, pattern: 're.Pattern',
    max_duration_s: float = 6.0,
) -> Optional[List[AlignedSubtitleCue]]:
    """尝试按正则模式切分文本，成功则递归返回左右 cue 列表。"""
    parts = [p for p in pattern.split(text) if p.strip()]
    if len(parts) < 2:
        return None
    total_len = sum(len(p) for p in parts)
    acc_len = 0
    cut_idx = 0
    for i, p in enumerate(parts):
        acc_len += len(p)
        if acc_len >= total_len / 2:
            cut_idx = i + 1
            break
    if not (0 < cut_idx < len(parts)):
        return None
    left_text = ''.join(parts[:cut_idx]).strip()
    right_text = ''.join(parts[cut_idx:]).strip()
    if not left_text or not right_text:
        return None
    ratio = len(left_text) / max(1, len(left_text) + len(right_text))
    mid_s = cue.start_s + duration * ratio
    left = AlignedSubtitleCue(
        start_s=cue.start_s, end_s=mid_s, text=left_text,
        provider=cue.provider, timing_source=cue.timing_source,
        alignment_confidence=cue.alignment_confidence,
    )
    right = AlignedSubtitleCue(
        start_s=mid_s, end_s=cue.end_s, text=right_text,
        provider=cue.provider, timing_source=cue.timing_source,
        alignment_confidence=cue.alignment_confidence,
    )
    return _split_long_cue(left, max_duration_s) + _split_long_cue(right, max_duration_s)


def _find_best_fallback_split(text: str) -> int:
    """在文本中找最佳兜底切分位置：虚词前 > 空格 > CJK 字符边界 > 中点。"""
    mid_pos = len(text) // 2
    search_start = max(0, mid_pos - 20)
    search_end = min(len(text), mid_pos + 20)

    # 优先在中文虚词前切分
    for pos in range(search_start, min(search_end, len(text))):
        ch = text[pos]
        # 双字虚词：在第二个字的位置切（即虚词整体归入右侧）
        if pos + 1 < len(text) and text[pos:pos + 2] in _CJK_FUNCTION_WORDS:
            if pos > 0:
                return pos
        # 单字虚词：在虚词前切分
        if ch in _CJK_FUNCTION_WORDS and pos > 0:
            return pos

    # 其次在空格处切分
    for pos in range(search_start, search_end):
        if text[pos].isspace():
            return pos + 1

    # 再次在 CJK 字符边界处切分
    for pos in range(search_start, search_end):
        ch = text[pos]
        if _CJK_CHAR_RE.match(ch) and pos > 0 and _CJK_CHAR_RE.match(text[pos - 1]):
            return pos

    # 最后兜底中点
    return mid_pos


def enforce_rhythm(
    cues: List[AlignedSubtitleCue],
    config: AISegmentationConfig,
) -> List[AlignedSubtitleCue]:
    """节奏后处理：合并过短、拆分过长。"""
    if not cues:
        return cues
    # 排序、去重叠
    cues = sorted(cues, key=lambda c: c.start_s)
    deduped: List[AlignedSubtitleCue] = []
    for c in cues:
        if deduped and c.start_s < deduped[-1].end_s - 0.001:
            new_start = deduped[-1].end_s
            if c.end_s > new_start + 0.05:
                c = AlignedSubtitleCue(
                    start_s=new_start, end_s=c.end_s, text=c.text,
                    provider=c.provider, timing_source=c.timing_source,
                    alignment_confidence=c.alignment_confidence,
                )
            else:
                continue
        deduped.append(c)

    # 拆分过长
    split_applied: List[AlignedSubtitleCue] = []
    for c in deduped:
        split_applied.extend(_split_long_cue(c, config.max_cue_duration_s))

    # 合并过短
    merged = _merge_short_cues(split_applied, config.min_cue_duration_s, config.max_cue_duration_s, config.max_cps)

    # 主动合并非理想区间的偏短条目（1.5-2s 与下一条合并到 2-4s 理想区间）
    merged = _merge_suboptimal_cues(
        merged, config.min_cue_duration_s, config.max_cue_duration_s, config.max_cps,
    )

    # 再次拆分（合并可能产生过长）
    final: List[AlignedSubtitleCue] = []
    for c in merged:
        final.extend(_split_long_cue(c, config.max_cue_duration_s))

    # 修复被劈开的句子（以虚词/介词结尾的 cue 与下一条合并）
    final = _repair_broken_sentences(final, config.max_cue_duration_s, config.max_cps)
    return final


def _repair_broken_sentences(
    cues: List[AlignedSubtitleCue],
    max_duration_s: float,
    max_cps: float,
) -> List[AlignedSubtitleCue]:
    """修复被劈开的句子：以虚词/介词/连词结尾的 cue 与下一条合并。

    在 enforce_rhythm 最终输出前调用，修复后处理链中产生的语义断裂。
    """
    if len(cues) < 2:
        return cues
    result: List[AlignedSubtitleCue] = []
    i = 0
    while i < len(cues):
        cue = cues[i]
        if i + 1 >= len(cues):
            result.append(cue)
            break

        nxt = cues[i + 1]
        text = str(cue.text or '').rstrip()
        if not text:
            result.append(cue)
            i += 1
            continue

        last_char = text[-1]
        # 检查是否以虚词/助词/介词/连词结尾（不是句末标点、不是逗号/分号）
        should_merge = False
        if last_char not in _SENTENCE_END_PUNCTS and last_char not in _CLAUSE_END_PUNCTS:
            # 单字虚词结尾
            if last_char in _CJK_FUNCTION_WORDS:
                should_merge = True
            # 双字虚词结尾（检查最后两个字符）
            elif len(text) >= 2 and text[-2:] in _CJK_FUNCTION_WORDS:
                should_merge = True
            # 英文虚词结尾
            elif last_char.isalpha():
                last_word = text.split()[-1].lower() if text.split() else ''
                if last_word in ('the', 'a', 'an', 'and', 'or', 'but', 'of', 'in', 'on', 'at',
                                  'to', 'for', 'with', 'from', 'by', 'as', 'is', 'are', 'was',
                                  'were', 'has', 'have', 'had', 'be', 'been', 'being',
                                  'that', 'which', 'who', 'where', 'when', 'if', 'because',
                                  'so', 'like', 'about', 'into', 'through', 'during', 'before',
                                  'after', 'above', 'below', 'between', 'under', 'over'):
                    should_merge = True

        if should_merge:
            gap = nxt.start_s - cue.end_s
            combined_text = (cue.text + ' ' + nxt.text).strip() if cue.text and nxt.text else (cue.text or nxt.text)
            combined_dur = nxt.end_s - cue.start_s
            if gap <= 0.5 and combined_dur <= max_duration_s and _cps(combined_text, combined_dur) <= max_cps:
                merged = AlignedSubtitleCue(
                    start_s=cue.start_s, end_s=nxt.end_s, text=combined_text,
                    provider=cue.provider, timing_source=cue.timing_source,
                    alignment_confidence=cue.alignment_confidence,
                )
                result.append(merged)
                i += 2  # 跳过下一条（已合并）
                continue

        result.append(cue)
        i += 1
    return result


def _flatten_segments_from_words(words: List[AsrWordTiming]) -> Tuple[List[AsrSegmentTiming], float, float]:
    """字级失败降级段级时，把词序列按句末标点聚合成段。"""
    if not words:
        return [], 0.0, 0.0
    segs: List[AsrSegmentTiming] = []
    unit: List[AsrWordTiming] = []
    for w in words:
        unit.append(w)
        if _SENTENCE_SPLIT_RE.search(str(w.text or '')):
            text = _words_to_text(unit)
            if text:
                segs.append(AsrSegmentTiming(
                    start_s=unit[0].start_s, end_s=unit[-1].end_s, text=text, words=list(unit),
                ))
            unit = []
    if unit:
        text = _words_to_text(unit)
        if text:
            segs.append(AsrSegmentTiming(
                start_s=unit[0].start_s, end_s=unit[-1].end_s, text=text, words=list(unit),
            ))
    if not segs:
        return [], 0.0, 0.0
    return segs, segs[0].start_s, segs[-1].end_s


# ---------------------------------------------------------------------------
# AI 智能分段器（上下文感知 + 边界精炼）
# ---------------------------------------------------------------------------

def _normalize_output_cues(
    cues: List[AlignedSubtitleCue],
    total_duration_s: Optional[float] = None,
    logger=None,
) -> List[AlignedSubtitleCue]:
    """出口归一化：吸收非法 cue 的文本 → 升序 → 去重叠 → 时长钳制。

    下游 srt_transform_engine.resolve_overlaps 与 _refine_boundaries 都假定输入
    按时间有序且不重叠，而 context_cues 又取 all_cues[-N:] 作为下一批上下文；
    因此这里做统一兜底，避免未排序/重叠的 AI 输出污染上下文和落盘结果。

    被判定「时间戳不可用」的 cue **不丢文本**：它的文本按时间顺序并入相邻的
    合法 cue（没有前一条时先挂起，随后并入下一条；全批都不可用时落到一条
    最小长度的兜底 cue）。丢弃与吸收都必须记 warning —— 这是落盘前的最后一道
    归一化，丢一条就是丢一段字幕内容，静默丢失会让用户只看到字幕莫名缺句而无从
    排查；而「只记 warning、文本照丢」同样不可接受（实测 4 条输入丢 2 条）。
    """
    discarded = []
    absorbed_texts: List[str] = []
    cleaned: List[AlignedSubtitleCue] = []

    def _join_texts(left: str, right: str) -> str:
        left = str(left or '').strip()
        right = str(right or '').strip()
        if not left:
            return right
        if not right:
            return left
        return f"{left} {right}"

    for cue in cues or []:
        text = str(getattr(cue, 'text', '') or '').strip()
        if not text:
            discarded.append(('empty_text', cue))
            continue
        start_s = float(cue.start_s)
        end_s = float(cue.end_s)
        if end_s <= start_s:
            discarded.append((f'non_positive_duration:{start_s:.3f}-{end_s:.3f}', cue))
            absorbed_texts.append(text)
            continue
        cleaned.append(cue)

    cleaned.sort(key=lambda c: float(c.start_s))

    limit: Optional[float] = None
    if total_duration_s is not None and float(total_duration_s) > 0:
        limit = float(total_duration_s)

    normalized: List[AlignedSubtitleCue] = []
    previous_end: Optional[float] = None
    for cue in cleaned:
        start_s = float(cue.start_s)
        end_s = float(cue.end_s)
        if limit is not None:
            start_s = max(0.0, min(limit, start_s))
            end_s = max(0.0, min(limit, end_s))
        if previous_end is not None and start_s < previous_end:
            # 重叠：后一条抬到前一条结尾（不改变文本归属）
            start_s = previous_end
        if end_s <= start_s:
            discarded.append((f'collapsed_after_clamp:{start_s:.3f}-{end_s:.3f}', cue))
            absorbed_texts.append(str(cue.text or ''))
            continue
        previous_end = end_s
        pending_text = ' '.join(absorbed_texts)
        absorbed_texts.clear()
        if not pending_text and start_s == float(cue.start_s) and end_s == float(cue.end_s):
            # 未被修改且没有待吸收文本：复用原对象，避免无谓重建
            normalized.append(cue)
            continue
        normalized.append(AlignedSubtitleCue(
            start_s=start_s,
            end_s=end_s,
            text=_join_texts(pending_text, str(cue.text or '')),
            provider=getattr(cue, 'provider', ''),
            timing_source=getattr(cue, 'timing_source', 'segment'),
            alignment_confidence=getattr(cue, 'alignment_confidence', 0.0),
            # source_window_index 必须原样保留：下游 srt_transform_engine 的跨窗
            # 去重（_is_cross_window_dup / _window_index_of）完全依赖该字段，
            # 丢失后退化为 -1 会让跨窗去重彻底失效（同窗口判定为 -1 == -1 时跳过）。
            source_window_index=getattr(cue, 'source_window_index', -1),
            metadata=dict(getattr(cue, 'metadata', {}) or {}),
        ))

    if absorbed_texts:
        orphan_text = ' '.join(absorbed_texts)
        absorbed_texts.clear()
        if normalized:
            # 有合法 cue 时挂到最后一条：时间轴仍由真实 cue 承载，
            # 不新造一条没有依据的时间段。
            normalized[-1].text = _join_texts(str(normalized[-1].text or ''), orphan_text)
        else:
            # 一个合法时间轴都没有：用一条最小长度的 cue 承载全部文本，
            # 宁可时间不准也不把内容丢掉（并在下面记 warning 说明）。
            orphan_end = 0.05
            if limit is not None and limit > 0:
                orphan_end = min(limit, orphan_end)
            normalized.append(AlignedSubtitleCue(
                start_s=0.0,
                end_s=max(orphan_end, 0.05),
                text=orphan_text,
                provider='',
                timing_source='orphan_text_fallback',
                alignment_confidence=0.0,
                source_window_index=-1,
                metadata={},
            ))

    if discarded and logger is not None:
        try:
            samples = ' | '.join(
                f"{reason}: {str(getattr(cue, 'text', '') or '')[:40]!r}"
                for reason, cue in discarded[:3]
            )
            logger.warning(
                "出口归一化丢弃 %d/%d 条 cue 的时间戳（文本已并入相邻 cue，未丢失内容，"
                "请检查上游时间戳）：%s",
                len(discarded),
                len(cues or []),
                samples,
            )
        except Exception:
            pass
    return normalized


def _derive_total_duration_s(batches: List[_Batch]) -> Optional[float]:
    """从批次推导输出时间轴的时长上界（所有输入段的 max(end_s)）。

    AISegmentationConfig 不携带视频总时长，AISegmenter.segment() 也无法从
    ASR 结果之外拿到容器时长；用"输入覆盖到的最晚时间点"作为上界是安全近似：
    输出 cue 不应晚于输入内容的结束时间。拿不到任何时间时返回 None（不钳制）。
    """
    latest: Optional[float] = None
    for batch in batches or []:
        if batch.segments:
            candidate = max(float(s.end_s) for s in batch.segments)
        elif batch.words:
            candidate = max(float(w.end_s) for w in batch.words)
        else:
            candidate = float(batch.time_end_s or 0.0)
        if latest is None or candidate > latest:
            latest = candidate
    if latest is None or latest <= 0:
        return None
    return latest


class AISegmenter:
    """AI 智能分段器：上下文感知 + 边界精炼 + 节奏后处理。

    特性：
    1. 滑动上下文窗口：处理批次 N 时注入 N-1 的末尾 cue 作为参考
    2. 边界精炼 pass：所有批次完成后，对相邻批次边界进行二次审视（可选）
    3. 三级降级：字级 AI → 段级 AI → 基线对齐
    """

    def __init__(self, config: AISegmentationConfig, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        # 降级可观测：每次 segment() 重置并累计，供上层读取（不改变返回值类型）
        self.last_degradation_stats: Dict[str, int] = {
            'word_level': 0, 'segment_level': 0, 'baseline': 0, 'rejected': 0,
        }
        # 本次 segment() 的总时长上界（由输入推导，跨批次安全近似）
        self._total_duration_s: Optional[float] = None

    def segment(self, results: List[AsrTranscriptionResult]) -> List[AlignedSubtitleCue]:
        """主入口：构建批次 → 上下文感知 AI 分段 → 可选边界精炼 → 节奏后处理。"""
        if not self.config.enabled:
            raise AISegmentationError('AI 分段未启用')
        if not self.config.is_model_configured:
            raise AISegmentationError('AI 分段模型未配置（API_KEY/MODEL_NAME 为空且无全局 OPENAI 配置可继承）')

        valid_results = [r for r in results if r.segments]
        if not valid_results:
            raise AISegmentationError('无可用 ASR 结果')

        provider = valid_results[0].provider or 'unknown'
        batches = build_batches(
            valid_results,
            self.config.batch_window_s,
            self.config.max_chars_per_batch,
        )
        if not batches:
            self.logger.warning('Agent 分段：无有效批次')
            return []

        self.logger.info(
            'Agent 分段开始：%d 批次，上下文窗口=%d，边界精炼=%s',
            len(batches), self.config.context_window,
            '开启' if self.config.boundary_refine_enabled else '关闭',
        )

        # 总时长上界：所有输入的 max(end)。batches 由 valid_results 派生，
        # 因此这是"本次分段输入覆盖到的时间终点"，作为输出时间轴的安全上界。
        self._total_duration_s = _derive_total_duration_s(batches)
        self.last_degradation_stats = {
            'word_level': 0, 'segment_level': 0, 'baseline': 0, 'rejected': 0,
        }

        # 逐批处理，维护滑动上下文
        all_cues: List[AlignedSubtitleCue] = []
        batch_results: List[List[AlignedSubtitleCue]] = []
        ai_success_count = 0

        for idx, batch in enumerate(batches):
            # 提取前一批末尾 N 条 cue 作为上下文
            context_cues: List[AlignedSubtitleCue] = []
            if self.config.context_window > 0 and all_cues:
                context_cues = all_cues[-self.config.context_window:]

            cues = self._segment_batch_with_context(batch, provider, idx, len(batches), context_cues)
            batch_results.append(cues)
            all_cues.extend(cues)

            if any(c.timing_source == 'ai' for c in cues):
                ai_success_count += 1

        self.logger.info(
            'Agent 分段初轮完成：%d 批次，AI 成功 %d，共 %d 条 cue',
            len(batches), ai_success_count, len(all_cues),
        )

        # Phase 2：边界精炼
        if self.config.boundary_refine_enabled and len(batches) > 1:
            all_cues = self._refine_boundaries(all_cues, batches, batch_results, provider)
            self.logger.info('边界精炼完成，共 %d 条 cue', len(all_cues))

        stats = self.last_degradation_stats
        degraded = stats['baseline'] + stats['rejected']
        if degraded > 0:
            ratio = degraded / max(1, len(batches))
            if ratio > 0.3:
                self.logger.warning(
                    'Agent 分段降级比例偏高：%.1f%%（基线 %d + 拒绝 %d / 共 %d 批次）'
                    '，统计=%s',
                    ratio * 100, stats['baseline'], stats['rejected'], len(batches), stats,
                )

        if self.config.rhythm_enabled:
            self.logger.info('节奏后处理已开启，执行 enforce_rhythm')
            return _normalize_output_cues(
                enforce_rhythm(all_cues, self.config), self._total_duration_s, self.logger,
            )
        self.logger.info('节奏后处理已关闭，直接返回 AI 分段结果')
        return _normalize_output_cues(all_cues, self._total_duration_s, self.logger)

    def _segment_batch_with_context(
        self,
        batch: _Batch,
        provider: str,
        idx: int,
        total: int,
        context_cues: List[AlignedSubtitleCue],
    ) -> List[AlignedSubtitleCue]:
        """单批次三级降级，支持上下文传递。"""
        label = f'批次 {idx + 1}/{total}'
        has_ctx = bool(context_cues)
        stats = self.last_degradation_stats

        # 第一级：字级 AI（带上下文）
        if batch.has_word_timestamps and batch.words:
            try:
                cues = self._call_ai_word_level(batch, provider, context_cues)
                if cues:
                    stats['word_level'] += 1
                    self.logger.info('%s 字级 AI 分段成功%s，%d 条 cue', label, '(含上下文)' if has_ctx else '', len(cues))
                    return cues
            except Exception as exc:
                stats['rejected'] += 1
                self.logger.warning('%s 字级 AI 分段失败，降级段级：%s', label, exc)
            # 第二级：段级 AI
            if not batch.segments:
                segs, _, _ = _flatten_segments_from_words(batch.words)
                batch.segments = segs
            if batch.segments:
                try:
                    cues = self._call_ai_segment_level(batch, provider, context_cues)
                    if cues:
                        stats['segment_level'] += 1
                        self.logger.info('%s 段级 AI 分段成功%s，%d 条 cue', label, '(含上下文)' if has_ctx else '', len(cues))
                        return cues
                except Exception as exc:
                    stats['rejected'] += 1
                    self.logger.warning('%s 段级 AI 分段失败，回退基线：%s', label, exc)
        else:
            if batch.segments:
                try:
                    cues = self._call_ai_segment_level(batch, provider, context_cues)
                    if cues:
                        stats['segment_level'] += 1
                        self.logger.info('%s 段级 AI 分段成功%s，%d 条 cue', label, '(含上下文)' if has_ctx else '', len(cues))
                        return cues
                except Exception as exc:
                    stats['rejected'] += 1
                    self.logger.warning('%s 段级 AI 分段失败，回退基线：%s', label, exc)

        # 第三级：基线对齐
        stats['baseline'] += 1
        self.logger.info('%s 回退基线对齐', label)
        return _baseline_align_batch(batch, provider)

    def _call_ai_word_level(
        self,
        batch: _Batch,
        provider: str,
        context_cues: Optional[List[AlignedSubtitleCue]] = None,
    ) -> List[AlignedSubtitleCue]:
        system_prompt = get_smart_segment_system_prompt(
            has_word_timestamps=True,
            min_duration_s=self.config.min_cue_duration_s,
            max_duration_s=self.config.max_cue_duration_s,
            max_cps=self.config.max_cps,
            has_context=bool(context_cues),
        )
        payload = _build_word_payload_with_context(batch.words, context_cues or [])
        raw_text = self._call_with_retry_raw(system_prompt, payload)
        # 索引制解析：AI 返回 [{start_index, end_index}]
        ranges = _parse_index_ranges(raw_text, len(batch.words))
        cues = _cues_from_index_ranges(ranges, batch.words, provider, logger=self.logger)
        if not cues:
            raise AISegmentationError('字级 AI 返回无有效 cue')
        # 出口闸门：索引分段只能重排词，不能丢词/造词
        _assert_text_coverage(
            _words_to_text(batch.words), cues,
            context='word_level', logger=self.logger,
        )
        return cues

    def _call_ai_segment_level(
        self,
        batch: _Batch,
        provider: str,
        context_cues: Optional[List[AlignedSubtitleCue]] = None,
    ) -> List[AlignedSubtitleCue]:
        if not batch.segments:
            raise AISegmentationError('段级 AI 无段输入')
        system_prompt = get_smart_segment_system_prompt(
            has_word_timestamps=False,
            min_duration_s=self.config.min_cue_duration_s,
            max_duration_s=self.config.max_cue_duration_s,
            max_cps=self.config.max_cps,
            has_context=bool(context_cues),
        )
        payload = _build_segment_payload_with_context(batch.segments, context_cues or [])
        parsed = self._call_with_retry(system_prompt, payload)
        # 段级时间戳只信输入段边界：吸附 + 覆盖率校验 + 时长上界
        cues_data = _parse_cues_response(
            parsed, batch.time_start_s, batch.time_end_s, len(batch.segments),
            input_boundaries=_segment_boundaries(batch.segments),
            total_duration_s=self._total_duration_s,
            # 传真实段区间：相邻边界对里含段间静音空隙，空隙不能当合法区间
            input_spans=[(float(s.start_s), float(s.end_s)) for s in batch.segments],
        )
        if not cues_data:
            # 诊断：记录模型返回的原始结构，帮助定位格式不匹配
            if isinstance(parsed, dict):
                raw_cues = parsed.get('cues')
                if isinstance(raw_cues, list):
                    self.logger.warning(
                        '段级 AI 返回 cues 列表长度=%d，但全部被过滤（批次范围=%.1f-%.1f）',
                        len(raw_cues), batch.time_start_s, batch.time_end_s,
                    )
                    for i, item in enumerate(raw_cues[:3]):
                        self.logger.warning('  cue[%d]: %s', i, str(item)[:200])
                else:
                    self.logger.warning(
                        '段级 AI 返回 JSON 缺少 cues 字段，键=%s',
                        list(parsed.keys())[:5],
                    )
            else:
                self.logger.warning('段级 AI 返回非 dict: %s', str(parsed)[:200])
            raise AISegmentationError('段级 AI 返回无有效 cue')
        cues = _cues_from_response(cues_data, timing_source='ai', provider=provider)
        # 出口闸门：段级分段只能重排/补标点，不能丢字/造字
        _assert_text_coverage(
            '\n'.join(str(s.text or '') for s in batch.segments), cues,
            context='segment_level', logger=self.logger,
        )
        return cues

    def _normalize_output_cues(
        self,
        cues: List[AlignedSubtitleCue],
        total_duration_s: Optional[float] = None,
    ) -> List[AlignedSubtitleCue]:
        """实例方法包装（保持既有调用点可用），实现见模块级 _normalize_output_cues。"""
        return _normalize_output_cues(cues, total_duration_s, getattr(self, 'logger', None))

    def _call_with_retry(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        client = self._create_client()
        last_exc: Optional[Exception] = None
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                delay = min(2 ** attempt, 8)
                self.logger.info('AI 分段重试等待 %ds...', delay)
                time.sleep(delay)
            try:
                result = _request_json_object(
                    client=client,
                    model_name=self.config.resolved_model_name,
                    system_prompt=system_prompt,
                    payload=payload,
                    temperature=self.config.temperature,
                    thinking_enabled=self.config.thinking_enabled,
                    logger_obj=self.logger,
                    scene_name=f'agent_segmentation_attempt{attempt + 1}',
                    user_content=json.dumps(payload, ensure_ascii=False),
                )
                if result is not None:
                    return result
                self.logger.warning(
                    'AI 分段返回空结果（第 %d 次），重试中...',
                    attempt + 1,
                )
            except Exception as exc:
                last_exc = exc
                self.logger.warning(
                    'AI 分段请求失败（第 %d 次）：%s: %s',
                    attempt + 1, exc.__class__.__name__, exc,
                )
        if last_exc:
            raise last_exc
        return None

    def _call_with_retry_raw(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
    ) -> str:
        """调用 LLM 并返回原始文本（不做 JSON 解析），用于索引制分段。"""
        client = self._create_client()
        last_exc: Optional[Exception] = None
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                delay = min(2 ** attempt, 8)
                self.logger.info('AI 分段重试等待 %ds...', delay)
                time.sleep(delay)
            try:
                raw_text = _request_raw_text(
                    client=client,
                    model_name=self.config.resolved_model_name,
                    system_prompt=system_prompt,
                    payload=payload,
                    temperature=self.config.temperature,
                    thinking_enabled=self.config.thinking_enabled,
                    logger_obj=self.logger,
                    scene_name=f'agent_segmentation_attempt{attempt + 1}',
                    user_content=json.dumps(payload, ensure_ascii=False),
                )
                if raw_text:
                    return raw_text
                raise AISegmentationError('模型返回空文本')
            except AISegmentationError:
                raise
            except Exception as exc:
                last_exc = exc
                self.logger.warning(
                    'AI 分段请求失败（第 %d 次）：%s: %s',
                    attempt + 1, exc.__class__.__name__, exc,
                )
        if last_exc:
            raise last_exc
        raise AISegmentationError('AI 分段请求未获得有效结果')

    def _create_client(self):
        client_config = {
            'OPENAI_API_KEY': self.config.resolved_api_key,
            'OPENAI_BASE_URL': self.config.resolved_base_url,
            # 必须透传 resolved model：统一客户端（含启用兜底时的 FallbackChatClient）
            # 会以端点配置的 model 为准覆盖调用方传入的 model，缺了这里分段请求
            # 会被替换成默认 gpt-3.5-turbo，在仅支持其它模型的端点上直接失败。
            'OPENAI_MODEL_NAME': self.config.resolved_model_name,
            'OPENAI_TIMEOUT_SECONDS': self.config.request_timeout_s,
        }
        return get_openai_client(client_config)

    def _refine_boundaries(
        self,
        all_cues: List[AlignedSubtitleCue],
        batches: List[_Batch],
        batch_results: List[List[AlignedSubtitleCue]],
        provider: str,
    ) -> List[AlignedSubtitleCue]:
        """边界精炼 pass：对相邻批次边界进行二次审视。"""
        bw = self.config.boundary_window
        refined: List[AlignedSubtitleCue] = list(batch_results[0])

        for i in range(len(batches) - 1):
            prev_cues = batch_results[i]
            next_cues = batch_results[i + 1]

            # 取边界区域的 cue
            boundary_prev = prev_cues[-bw:] if len(prev_cues) > bw else prev_cues
            boundary_next = next_cues[:bw] if len(next_cues) > bw else next_cues

            if not boundary_prev or not boundary_next:
                refined.extend(next_cues if i + 1 < len(batches) else [])
                continue

            # 检查边界是否需要精炼：前批末条是否语意完整
            last_prev_text = boundary_prev[-1].text or ''
            if _is_sentence_complete(last_prev_text):
                # 边界合理，无需精炼
                refined.extend(next_cues)
                continue

            # 收集边界区域的 word 数据
            boundary_words = self._collect_boundary_words(batches, i, boundary_prev, boundary_next)
            if not boundary_words:
                refined.extend(next_cues)
                continue

            # 构建精炼 payload
            current_boundary_cues = boundary_prev + boundary_next
            payload = _build_word_payload(boundary_words)
            payload['current_cues'] = [
                {'start': round(float(c.start_s), 3), 'end': round(float(c.end_s), 3), 'text': str(c.text or '')}
                for c in current_boundary_cues
            ]

            try:
                system_prompt = get_boundary_refine_system_prompt(
                    min_duration_s=self.config.min_cue_duration_s,
                    max_duration_s=self.config.max_cue_duration_s,
                    max_cps=self.config.max_cps,
                )
                parsed = self._call_with_retry(system_prompt, payload)
                # 精炼输出同样只信输入词边界（±0.3s 吸附），并受总时长上界约束
                word_boundaries = sorted({
                    round(float(w.start_s), 3) for w in boundary_words
                } | {
                    round(float(w.end_s), 3) for w in boundary_words
                })
                refined_cues_data = _parse_cues_response(
                    parsed,
                    boundary_prev[0].start_s,
                    boundary_next[-1].end_s,
                    len(boundary_words),
                    input_boundaries=word_boundaries,
                    total_duration_s=self._total_duration_s,
                    # 字级同样传真实词区间：词与词之间的停顿不是可切分的语音内容
                    input_spans=[(float(w.start_s), float(w.end_s)) for w in boundary_words],
                )
                if refined_cues_data:
                    new_boundary_cues = _cues_from_response(refined_cues_data, timing_source='ai', provider=provider)
                    # 替换边界区域的 cues：前批去掉尾部 + 后批去掉头部
                    prev_keep = prev_cues[:max(0, len(prev_cues) - bw)]
                    next_keep = next_cues[bw:] if len(next_cues) > bw else []
                    refined = prev_keep + new_boundary_cues + next_keep
                    self.logger.info(
                        '边界 %d/%d 精炼成功：%d 条 → %d 条',
                        i + 1, i + 2, len(current_boundary_cues), len(new_boundary_cues),
                    )
                else:
                    refined.extend(next_cues)
                    self.logger.info('边界 %d/%d 精炼无调整', i + 1, i + 2)
            except Exception as exc:
                refined.extend(next_cues)
                self.logger.warning('边界 %d/%d 精炼失败：%s', i + 1, i + 2, exc)

        return refined

    def _collect_boundary_words(
        self,
        batches: List[_Batch],
        batch_idx: int,
        boundary_prev: List[AlignedSubtitleCue],
        boundary_next: List[AlignedSubtitleCue],
    ) -> List[AsrWordTiming]:
        """收集边界区域的 word 数据。"""
        time_start = boundary_prev[0].start_s
        time_end = boundary_next[-1].end_s
        words: List[AsrWordTiming] = []
        for bi in (batch_idx, batch_idx + 1):
            if bi >= len(batches):
                continue
            batch = batches[bi]
            if batch.words:
                for w in batch.words:
                    if w.start_s >= time_start - 0.5 and w.end_s <= time_end + 0.5:
                        words.append(w)
        words.sort(key=lambda w: w.start_s)
        return words


def _is_sentence_complete(text: str) -> bool:
    """检查文本是否以句末标点结尾（语意完整）。"""
    text = str(text or '').rstrip()
    if not text:
        return False
    return text[-1] in '.!?。！？；;' or text.endswith('...') or text.endswith('…')


# ---------------------------------------------------------------------------
# SRT 文件重分段适配层
# ---------------------------------------------------------------------------

def srt_to_asr_results(
    srt_path: str,
    logger: Optional[logging.Logger] = None,
) -> 'List[AsrTranscriptionResult]':
    """将 SRT 文件解析为 AsrTranscriptionResult 列表，供 AISegmenter.segment() 使用。

    每个 SRT cue 转为一个 AsrSegmentTiming，包装为独立的 AsrTranscriptionResult。
    """
    from .srt_transform_engine import SrtTransformEngine, SrtTransformConfig

    _logger = logger or logging.getLogger(__name__)

    try:
        with open(srt_path, encoding='utf-8') as f:
            srt_text = f.read()
    except FileNotFoundError:
        _logger.warning('SRT 文件不存在: %s', srt_path)
        return []
    except Exception as exc:
        _logger.warning('SRT 文件读取失败: %s — %s', srt_path, exc)
        return []

    if not srt_text.strip():
        _logger.warning('SRT 文件为空: %s', srt_path)
        return []

    engine = SrtTransformEngine(SrtTransformConfig(), logger=_logger)
    cues = engine.parse_srt(srt_text)
    if not cues:
        _logger.warning('SRT 解析无有效 cue: %s', srt_path)
        return []

    results: List[AsrTranscriptionResult] = []
    for cue in cues:
        start = float(cue.get('start', 0.0) or 0.0)
        end = float(cue.get('end', 0.0) or 0.0)
        text = str(cue.get('text') or '').strip()
        if not text or end <= start:
            continue

        seg = AsrSegmentTiming(start_s=start, end_s=end, text=text)
        window = DetectedSpeechWindow(
            start_s=start, end_s=end,
            ownership_start_s=start, ownership_end_s=end,
        )
        result = AsrTranscriptionResult(
            provider='srt_file',
            response_format='srt',
            timestamp_mode='segment',
            text=text,
            segments=[seg],
            window=window,
        )
        results.append(result)

    _logger.info('SRT 转换为 %d 个 AsrTranscriptionResult: %s', len(results), srt_path)
    return results


def resegment_srt_file(
    srt_path: str,
    config: 'AISegmentationConfig',
    logger: Optional[logging.Logger] = None,
) -> 'Optional[str]':
    """对已有 SRT 文件进行 AI 重分段，返回新 SRT 文件路径。

    失败时返回 None（不阻断流程）。
    """
    from .srt_transform_engine import SrtTransformEngine, SrtTransformConfig

    _logger = logger or logging.getLogger(__name__)

    try:
        results = srt_to_asr_results(srt_path, _logger)
        if not results:
            _logger.warning('SRT 重分段：无法解析输入文件，跳过')
            return None

        segmenter = AISegmenter(config, logger=_logger)
        cues = segmenter.segment(results)
        if not cues:
            _logger.warning('SRT 重分段：AI 分段返回空结果，跳过')
            return None

        # 落盘前兜底：SRT 适配层没有其它下游校验，这里再归一化一次
        # （segment() 已归一化，此处幂等，防配置/调用路径变化导致漏网）
        latest = max(
            (float(seg.end_s) for r in results for seg in r.segments),
            default=0.0,
        )
        total_duration_s = latest if latest > 0 else None
        cues = _normalize_output_cues(cues, total_duration_s)
        if not cues:
            _logger.warning('SRT 重分段：归一化后无有效 cue，跳过')
            return None

        engine = SrtTransformEngine(SrtTransformConfig(), logger=_logger)
        srt_text = engine.render_srt(cues)
        if not srt_text:
            _logger.warning('SRT 重分段：render_srt 返回空，跳过')
            return None

        # 写入临时文件，路径与原始 SRT 同目录
        import os
        base, ext = os.path.splitext(srt_path)
        new_path = f'{base}.resegmented{ext}'
        with open(new_path, 'w', encoding='utf-8') as f:
            f.write(srt_text)

        _logger.info(
            'SRT 重分段完成：%d cues → %d cues，输出: %s',
            len(results), len(cues), new_path,
        )
        return new_path

    except AISegmentationError as exc:
        _logger.warning('SRT 重分段跳过（AI 分段不可用）: %s', exc)
        return None
    except Exception as exc:
        _logger.warning('SRT 重分段异常，使用原始文件: %s', exc)
        return None
