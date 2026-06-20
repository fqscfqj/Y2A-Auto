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
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .ai_enhancer import _request_json_object, get_openai_client
from .prompt_manager import get_smart_segment_system_prompt, get_boundary_refine_system_prompt
from .speech_pipeline_settings import coerce_bool
from .subtitle_pipeline_types import (
    AlignedSubtitleCue,
    AsrSegmentTiming,
    AsrTranscriptionResult,
    AsrWordTiming,
)


# 句末/停顿标点，用于过长短目拆分的安全网
_SENTENCE_SPLIT_RE = re.compile(r'([.!?。！？；;]+\s*)')
_CJK_CHAR_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')


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
    max_cue_duration_s: float = 7.0
    max_cps: float = 18.0
    # 批次策略
    batch_window_s: float = 120.0
    max_chars_per_batch: int = 8000
    # 请求参数
    temperature: float = 0.2
    max_retries: int = 2
    request_timeout_s: float = 600.0
    # Agent 上下文感知
    context_window: int = 3          # 前一批末尾 N 条 cue 注入下一批 prompt
    boundary_refine_enabled: bool = True   # 边界精炼 pass
    boundary_window: int = 3         # 边界精炼每侧取 N 条 cue
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
            max_cue_duration_s=float(app_config.get('AI_SEGMENTATION_MAX_CUE_DURATION_S', 7.0) or 7.0),
            max_cps=float(app_config.get('AI_SEGMENTATION_MAX_CPS', 18.0) or 18.0),
            batch_window_s=float(app_config.get('AI_SEGMENTATION_BATCH_WINDOW_S', 120.0) or 120.0),
            max_chars_per_batch=int(app_config.get('AI_SEGMENTATION_MAX_CHARS_PER_BATCH', 8000) or 8000),
            temperature=float(app_config.get('AI_SEGMENTATION_TEMPERATURE', 0.2) or 0.2),
            max_retries=int(app_config.get('AI_SEGMENTATION_MAX_RETRIES', 2) or 2),
            request_timeout_s=float(app_config.get('OPENAI_TIMEOUT_SECONDS', 600) or 600),
            context_window=int(app_config.get('AI_SEGMENTATION_CONTEXT_WINDOW', 3) or 3),
            boundary_refine_enabled=coerce_bool(app_config.get('AI_SEGMENTATION_BOUNDARY_REFINE_ENABLED', True)),
            boundary_window=int(app_config.get('AI_SEGMENTATION_BOUNDARY_WINDOW', 3) or 3),
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


def _flatten_words(
    results: List[AsrTranscriptionResult],
    apply_window_offset: bool = True,
) -> Tuple[List[AsrWordTiming], float, float]:
    """把多个 result 的所有 segment.words 按时间顺序展平，返回 (words, start, end)。

    ASR 返回的 word 时间戳是窗口内相对时间（每个窗口从 0 开始）。
    apply_window_offset=True 时加上 result.window.start_s 偏移，转为视频绝对时间，
    使跨窗口合并的批次内时间轴统一。AI 分段在统一绝对时间轴上工作，输出可直接使用。
    """
    words: List[AsrWordTiming] = []
    for result in results:
        offset = float(result.window.start_s) if (apply_window_offset and result.window) else 0.0
        for seg in result.segments:
            for w in seg.words:
                if str(w.text or '').strip() and w.end_s > w.start_s:
                    words.append(AsrWordTiming(
                        start_s=w.start_s + offset,
                        end_s=w.end_s + offset,
                        text=w.text,
                    ))
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
                # 同步偏移 segment 及其 words
                offset_words = [
                    AsrWordTiming(start_s=w.start_s + offset, end_s=w.end_s + offset, text=w.text)
                    for w in seg.words
                ]
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
    """按字符上限把词序列切成多个子列表（尽量在标点/空格后切）。"""
    if not words:
        return []
    chunks: List[List[AsrWordTiming]] = []
    current: List[AsrWordTiming] = []
    current_chars = 0
    for w in words:
        w_chars = len(str(w.text or ''))
        if current and current_chars + w_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(w)
        current_chars += w_chars
    if current:
        chunks.append(current)
    return chunks


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

def _estimate_max_tokens(char_count: int) -> int:
    """根据输入字符数估算输出 max_tokens。"""
    # 输出文本≈输入文本，CJK 约 1 字/token，ASCII 约 0.5 token/字；加结构开销
    estimated = max(1024, int(char_count * 1.5) + 256)
    return min(8192, estimated)


def _build_word_payload(words: List[AsrWordTiming]) -> Dict[str, Any]:
    return {
        'words': [
            {'i': idx, 'text': str(w.text or ''), 'start_s': round(float(w.start_s), 3), 'end_s': round(float(w.end_s), 3)}
            for idx, w in enumerate(words)
        ]
    }


def _build_segment_payload(segments: List[AsrSegmentTiming]) -> Dict[str, Any]:
    return {
        'segments': [
            {'i': idx, 'text': str(s.text or ''), 'start_s': round(float(s.start_s), 3), 'end_s': round(float(s.end_s), 3)}
            for idx, s in enumerate(segments)
        ]
    }


def _serialize_context_cues(context_cues: List[AlignedSubtitleCue]) -> List[Dict[str, Any]]:
    """将已确认的上下文 cues 序列化为 payload 片段。"""
    return [
        {'start_s': round(float(c.start_s), 3), 'end_s': round(float(c.end_s), 3), 'text': str(c.text or '')}
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


def _parse_cues_response(
    parsed: Optional[Dict[str, Any]],
    batch_start_s: float,
    batch_end_s: float,
    input_count: int,
) -> List[Dict[str, Any]]:
    """校验并清洗 AI 返回的 cues。

    - 必须是 {"cues": [...]}
    - 每条 start_s < end_s，且落在批次时间范围内（允许 0.5s 容差）
    - 按时间升序、去重叠
    - 数量合理（1 ~ input_count*2 + 4，防异常膨胀）
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
        raw_start = item.get('start_s')
        raw_end = item.get('end_s')
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

    # 升序 + 去重叠
    cleaned.sort(key=lambda c: c['start_s'])
    deduped: List[Dict[str, Any]] = []
    for c in cleaned:
        if deduped and c['start_s'] < deduped[-1]['end_s'] - 0.001:
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
    return [
        AlignedSubtitleCue(
            start_s=c['start_s'],
            end_s=c['end_s'],
            text=c['text'],
            provider=provider,
            timing_source=timing_source,
            alignment_confidence=0.9,
        )
        for c in cues_data
    ]


# ---------------------------------------------------------------------------
# 基线对齐（AI 失败时的批次兜底，按段直转 cue）
# ---------------------------------------------------------------------------

def _baseline_align_batch(batch: _Batch, provider: str) -> List[AlignedSubtitleCue]:
    cues: List[AlignedSubtitleCue] = []
    if batch.has_word_timestamps and batch.words:
        # 按段语义不可得时，按词序列每 N 个词聚成一条（保守：每 12 词或遇句末标点切）
        unit: List[AsrWordTiming] = []
        for w in batch.words:
            unit.append(w)
            text_joined = ''.join(str(x.text or '') for x in unit)
            if len(unit) >= 12 or _SENTENCE_SPLIT_RE.search(str(w.text or '')):
                cues.append(AlignedSubtitleCue(
                    start_s=unit[0].start_s,
                    end_s=unit[-1].end_s,
                    text=text_joined.strip(),
                    provider=provider,
                    timing_source='word',
                    alignment_confidence=0.5,
                ))
                unit = []
        if unit:
            text_joined = ''.join(str(x.text or '') for x in unit)
            cues.append(AlignedSubtitleCue(
                start_s=unit[0].start_s,
                end_s=unit[-1].end_s,
                text=text_joined.strip(),
                provider=provider,
                timing_source='word',
                alignment_confidence=0.5,
            ))
    else:
        for seg in batch.segments:
            cues.append(AlignedSubtitleCue(
                start_s=seg.start_s,
                end_s=seg.end_s,
                text=str(seg.text or '').strip(),
                provider=provider,
                timing_source='segment',
                alignment_confidence=0.5,
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
    """过长度条目按句末标点切分；切不动则按文本中点等分（时间按比例）。"""
    duration = cue.end_s - cue.start_s
    if duration <= max_duration_s:
        return [cue]
    text = str(cue.text or '')
    # 尝试按句末标点切分
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    if len(parts) >= 2:
        total_len = sum(len(p) for p in parts)
        # 贪心累积到约一半长度切一刀
        acc_len = 0
        cut_idx = 0
        for i, p in enumerate(parts):
            acc_len += len(p)
            if acc_len >= total_len / 2:
                cut_idx = i + 1
                break
        if 0 < cut_idx < len(parts):
            left_text = ''.join(parts[:cut_idx]).strip()
            right_text = ''.join(parts[cut_idx:]).strip()
            if left_text and right_text:
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
                # 递归切分左右（防单边仍过长）
                return _split_long_cue(left, max_duration_s) + _split_long_cue(right, max_duration_s)
    # 切不动：优先按空格/CJK 边界切，兜底按中点
    mid_pos = len(text) // 2
    search_start = max(0, mid_pos - 15)
    search_end = min(len(text), mid_pos + 15)
    best_pos = mid_pos
    for pos in range(search_start, search_end):
        ch = text[pos]
        if ch.isspace():
            best_pos = pos + 1
            break
        if _CJK_CHAR_RE.match(ch) and pos > 0 and _CJK_CHAR_RE.match(text[pos - 1]):
            best_pos = pos
            break
    mid_s = cue.start_s + duration / 2
    mid_text = best_pos
    left_text = text[:mid_text].strip()
    right_text = text[mid_text:].strip()
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
    return final


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

class AISegmenter:
    """AI 智能分段器：三级降级 + 节奏后处理。"""

    def __init__(self, config: AISegmentationConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger('ai_segmentation')

    def segment(self, results: List[AsrTranscriptionResult]) -> List[AlignedSubtitleCue]:
        """对 ASR 结果做 AI 智能分段，返回重分段后的字幕 cue 列表。

        若模型未配置或所有批次均失败，抛出 AISegmentationError 由调用方回退。
        单批次失败时该批次回退到基线对齐，不影响其他批次。
        """
        if not self.config.enabled:
            raise AISegmentationError('AI 分段未启用')
        if not self.config.is_model_configured:
            raise AISegmentationError('AI 分段模型未配置（API_KEY/MODEL_NAME 为空且无全局 OPENAI 配置可继承）')

        valid_results = [r for r in results if r.segments]
        if not valid_results:
            raise AISegmentationError('无可用 ASR 结果')

        batches = build_batches(
            valid_results,
            self.config.batch_window_s,
            self.config.max_chars_per_batch,
        )
        if not batches:
            raise AISegmentationError('批次构建为空')

        provider = valid_results[0].provider if valid_results else 'ai'
        all_cues: List[AlignedSubtitleCue] = []
        ai_success_count = 0
        for idx, batch in enumerate(batches):
            cues = self._segment_batch(batch, provider, idx, len(batches))
            if cues:
                all_cues.extend(cues)
                if any(c.timing_source == 'ai' for c in cues):
                    ai_success_count += 1

        if not all_cues:
            raise AISegmentationError('所有批次均未生成 cue')

        self.logger.info(
            'AI 智能分段完成：批次 %d 个，AI 成功 %d 个，共 %d 条 cue',
            len(batches), ai_success_count, len(all_cues),
        )
        return enforce_rhythm(all_cues, self.config)

    def _segment_batch(
        self,
        batch: _Batch,
        provider: str,
        idx: int,
        total: int,
    ) -> List[AlignedSubtitleCue]:
        """单批次三级降级：字级 AI → 段级 AI → 基线对齐。"""
        label = f'批次 {idx + 1}/{total}'
        # 第一级：字级 AI
        if batch.has_word_timestamps and batch.words:
            try:
                cues = self._call_ai_word_level(batch, provider)
                if cues:
                    self.logger.info('%s 字级 AI 分段成功，%d 条 cue', label, len(cues))
                    return cues
            except Exception as exc:
                self.logger.warning('%s 字级 AI 分段失败，降级段级：%s', label, exc)
            # 第二级：段级 AI（字级失败时，从 segments 重建段输入）
            if not batch.segments:
                segs, _, _ = _flatten_segments_from_words(batch.words)
                batch.segments = segs
            if batch.segments:
                try:
                    cues = self._call_ai_segment_level(batch, provider)
                    if cues:
                        self.logger.info('%s 段级 AI 分段成功，%d 条 cue', label, len(cues))
                        return cues
                except Exception as exc:
                    self.logger.warning('%s 段级 AI 分段失败，回退基线：%s', label, exc)
        else:
            # 无字级时间戳，直接段级 AI
            if batch.segments:
                try:
                    cues = self._call_ai_segment_level(batch, provider)
                    if cues:
                        self.logger.info('%s 段级 AI 分段成功，%d 条 cue', label, len(cues))
                        return cues
                except Exception as exc:
                    self.logger.warning('%s 段级 AI 分段失败，回退基线：%s', label, exc)

        # 第三级：基线对齐
        self.logger.info('%s 回退基线对齐', label)
        return _baseline_align_batch(batch, provider)

    def _create_client(self):
        client_config = {
            'OPENAI_API_KEY': self.config.resolved_api_key,
            'OPENAI_BASE_URL': self.config.resolved_base_url,
            'OPENAI_TIMEOUT_SECONDS': self.config.request_timeout_s,
        }
        return get_openai_client(client_config)

    def _call_ai_word_level(
        self,
        batch: _Batch,
        provider: str,
    ) -> List[AlignedSubtitleCue]:
        system_prompt = get_smart_segment_system_prompt(
            has_word_timestamps=True,
            min_duration_s=self.config.min_cue_duration_s,
            max_duration_s=self.config.max_cue_duration_s,
            max_cps=self.config.max_cps,
        )
        payload = _build_word_payload(batch.words)
        parsed = self._call_with_retry(system_prompt, payload, batch.char_count)
        cues_data = _parse_cues_response(
            parsed, batch.time_start_s, batch.time_end_s, len(batch.words),
        )
        if not cues_data:
            raise AISegmentationError('字级 AI 返回无有效 cue')
        return _cues_from_response(cues_data, timing_source='ai', provider=provider)

    def _call_ai_segment_level(
        self,
        batch: _Batch,
        provider: str,
    ) -> List[AlignedSubtitleCue]:
        if not batch.segments:
            raise AISegmentationError('段级 AI 无段输入')
        system_prompt = get_smart_segment_system_prompt(
            has_word_timestamps=False,
            min_duration_s=self.config.min_cue_duration_s,
            max_duration_s=self.config.max_cue_duration_s,
            max_cps=self.config.max_cps,
        )
        payload = _build_segment_payload(batch.segments)
        parsed = self._call_with_retry(system_prompt, payload, batch.char_count)
        cues_data = _parse_cues_response(
            parsed, batch.time_start_s, batch.time_end_s, len(batch.segments),
        )
        if not cues_data:
            raise AISegmentationError('段级 AI 返回无有效 cue')
        return _cues_from_response(cues_data, timing_source='ai', provider=provider)

    def _call_with_retry(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
        char_count: int,
    ) -> Optional[Dict[str, Any]]:
        client = self._create_client()
        max_tokens = _estimate_max_tokens(char_count)
        last_exc: Optional[Exception] = None
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                delay = min(2 ** attempt, 8)  # 指数退避，上限 8s
                self.logger.info('AI 分段重试等待 %ds...', delay)
                time.sleep(delay)
            try:
                return _request_json_object(
                    client=client,
                    model_name=self.config.resolved_model_name,
                    system_prompt=system_prompt,
                    payload=payload,
                    max_tokens=max_tokens,
                    temperature=self.config.temperature,
                    thinking_enabled=self.config.thinking_enabled,
                    logger_obj=self.logger,
                    scene_name=f'ai_segmentation_attempt{attempt + 1}',
                    user_content=json.dumps(payload, ensure_ascii=False),
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


def _flatten_segments_from_words(words: List[AsrWordTiming]) -> Tuple[List[AsrSegmentTiming], float, float]:
    """字级失败降级段级时，把词序列按句末标点聚合成段。"""
    if not words:
        return [], 0.0, 0.0
    segs: List[AsrSegmentTiming] = []
    unit: List[AsrWordTiming] = []
    for w in words:
        unit.append(w)
        if _SENTENCE_SPLIT_RE.search(str(w.text or '')):
            text = ''.join(str(x.text or '') for x in unit).strip()
            if text:
                segs.append(AsrSegmentTiming(
                    start_s=unit[0].start_s, end_s=unit[-1].end_s, text=text, words=list(unit),
                ))
            unit = []
    if unit:
        text = ''.join(str(x.text or '') for x in unit).strip()
        if text:
            segs.append(AsrSegmentTiming(
                start_s=unit[0].start_s, end_s=unit[-1].end_s, text=text, words=list(unit),
            ))
    if not segs:
        return [], 0.0, 0.0
    return segs, segs[0].start_s, segs[-1].end_s


# ---------------------------------------------------------------------------
# Agent 智能分段器（上下文感知 + 边界精炼）
# ---------------------------------------------------------------------------

class AgentSegmenter:
    """上下文感知的 AI 智能分段器，替代原 AISegmenter。

    与 AISegmenter 的区别：
    1. 滑动上下文窗口：处理批次 N 时注入 N-1 的末尾 cue 作为参考
    2. 边界精炼 pass：所有批次完成后，对相邻批次边界进行二次审视
    """

    def __init__(self, config: AISegmentationConfig, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

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

        return enforce_rhythm(all_cues, self.config)

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

        # 第一级：字级 AI（带上下文）
        if batch.has_word_timestamps and batch.words:
            try:
                cues = self._call_ai_word_level(batch, provider, context_cues)
                if cues:
                    self.logger.info('%s 字级 AI 分段成功%s，%d 条 cue', label, '(含上下文)' if has_ctx else '', len(cues))
                    return cues
            except Exception as exc:
                self.logger.warning('%s 字级 AI 分段失败，降级段级：%s', label, exc)
            # 第二级：段级 AI
            if not batch.segments:
                segs, _, _ = _flatten_segments_from_words(batch.words)
                batch.segments = segs
            if batch.segments:
                try:
                    cues = self._call_ai_segment_level(batch, provider, context_cues)
                    if cues:
                        self.logger.info('%s 段级 AI 分段成功%s，%d 条 cue', label, '(含上下文)' if has_ctx else '', len(cues))
                        return cues
                except Exception as exc:
                    self.logger.warning('%s 段级 AI 分段失败，回退基线：%s', label, exc)
        else:
            if batch.segments:
                try:
                    cues = self._call_ai_segment_level(batch, provider, context_cues)
                    if cues:
                        self.logger.info('%s 段级 AI 分段成功%s，%d 条 cue', label, '(含上下文)' if has_ctx else '', len(cues))
                        return cues
                except Exception as exc:
                    self.logger.warning('%s 段级 AI 分段失败，回退基线：%s', label, exc)

        # 第三级：基线对齐
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
        parsed = self._call_with_retry(system_prompt, payload, batch.char_count)
        cues_data = _parse_cues_response(
            parsed, batch.time_start_s, batch.time_end_s, len(batch.words),
        )
        if not cues_data:
            raise AISegmentationError('字级 AI 返回无有效 cue')
        return _cues_from_response(cues_data, timing_source='ai', provider=provider)

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
        parsed = self._call_with_retry(system_prompt, payload, batch.char_count)
        cues_data = _parse_cues_response(
            parsed, batch.time_start_s, batch.time_end_s, len(batch.segments),
        )
        if not cues_data:
            raise AISegmentationError('段级 AI 返回无有效 cue')
        return _cues_from_response(cues_data, timing_source='ai', provider=provider)

    def _call_with_retry(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
        char_count: int,
    ) -> Optional[Dict[str, Any]]:
        client = self._create_client()
        max_tokens = _estimate_max_tokens(char_count)
        last_exc: Optional[Exception] = None
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                delay = min(2 ** attempt, 8)
                self.logger.info('AI 分段重试等待 %ds...', delay)
                time.sleep(delay)
            try:
                return _request_json_object(
                    client=client,
                    model_name=self.config.resolved_model_name,
                    system_prompt=system_prompt,
                    payload=payload,
                    max_tokens=max_tokens,
                    temperature=self.config.temperature,
                    thinking_enabled=self.config.thinking_enabled,
                    logger_obj=self.logger,
                    scene_name=f'agent_segmentation_attempt{attempt + 1}',
                    user_content=json.dumps(payload, ensure_ascii=False),
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

    def _create_client(self):
        client_config = {
            'OPENAI_API_KEY': self.config.resolved_api_key,
            'OPENAI_BASE_URL': self.config.resolved_base_url,
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
            payload = {
                'words': [
                    {'i': idx, 'text': str(w.text or ''), 'start_s': round(float(w.start_s), 3), 'end_s': round(float(w.end_s), 3)}
                    for idx, w in enumerate(boundary_words)
                ],
                'current_cues': [
                    {'start_s': round(float(c.start_s), 3), 'end_s': round(float(c.end_s), 3), 'text': str(c.text or '')}
                    for c in current_boundary_cues
                ],
            }

            try:
                system_prompt = get_boundary_refine_system_prompt(
                    min_duration_s=self.config.min_cue_duration_s,
                    max_duration_s=self.config.max_cue_duration_s,
                    max_cps=self.config.max_cps,
                )
                parsed = self._call_with_retry(system_prompt, payload, sum(len(w.text or '') for w in boundary_words))
                refined_cues_data = _parse_cues_response(
                    parsed,
                    boundary_prev[0].start_s,
                    boundary_next[-1].end_s,
                    len(boundary_words),
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
