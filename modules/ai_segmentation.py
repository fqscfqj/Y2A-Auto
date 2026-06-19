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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .ai_enhancer import _request_json_object, get_openai_client
from .prompt_manager import get_smart_segment_system_prompt
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
    # 切不动：按中点等分
    mid_s = cue.start_s + duration / 2
    mid_text = len(text) // 2
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
