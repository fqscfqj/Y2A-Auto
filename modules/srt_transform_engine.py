#!/usr/bin/env python
# -*- coding: utf-8 -*-

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .subtitle_pipeline_types import (
    AlignedSubtitleCue,
    AsrSegmentTiming,
    AsrTranscriptionResult,
)


_WHITESPACE_RE = re.compile(r'\s+')
_BLOCK_SPLIT_RE = re.compile(r'\n\s*\n')
_PUNCTUATION_SPACE_RE = re.compile(r'([.!?,:;])(?=\S)')
_TRIM_TEXT_RE = re.compile(r'^[\s\W_]+|[\s\W_]+$')
_NON_WORD_RE = re.compile(r'[\W_]+', re.UNICODE)
_FILLER_PATTERNS = [
    # 仅句首语气词：`like` / `you know` 在句中常是实词，删除会破坏语义，故移除。
    re.compile(r'(?:^|(?<=[.!?,;:]\s))(?:um|uh|er|ah|hmm)\b', re.IGNORECASE),
    # 仅句尾语气词。
    re.compile(r'\b(?:um|uh|er|ah|hmm)(?=\s*[.!?,;:]|$)', re.IGNORECASE),
    # 句中独立成词的填充词：`so um well I think` 里的 `um` 也应清理。
    # 用非字母边界而不是 \b，避免把 `mm` 这类纯字母短串误判成词内片段；
    # 只收录在句中几乎不承载语义的填充词，`er`/`ah` 保持不变以免误删
    # `weather`/`ahead` 之类的词内片段。
    re.compile(r'(?<![A-Za-z])(?:um|uh|uhm|erm|h+m?|mm)(?![A-Za-z])', re.IGNORECASE),
    # 中文语气词：只在整条就是短语气、或位于句尾时命中。
    # 用 `+` 而不是 `{1,3}`：`嗯嗯嗯嗯` 这类连续语气词同样应该被整体清理。
    re.compile(r'^[嗯啊呃哦唔]+$'),
    re.compile(r'[嗯啊哦呃唔]+(?=\s*[。！？!?]?\s*$)'),
    re.compile(
        r'\b(doo|da|dee|ch|sh|tickle|scratch|tap|click|pop|mouth|sound|noise|'
        r'chew|eat|drink|slurp|gulp|swallow|breath|whisper|lip|smack|tongue)\b',
        re.IGNORECASE,
    ),
    re.compile(r'\*[^*]*\*', re.IGNORECASE),
    re.compile(r'\[[^\]]*\]', re.IGNORECASE),
    re.compile(r'\([^)]*\)', re.IGNORECASE),
]
# 仅在拉丁词上折叠重复，避免把 CJK 的「好好好」压成「好」。
_REPEATED_WORD_RE = re.compile(r'\b([A-Za-z]{2,})(?:[,\s]+\1\b)+', re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r'([.!?。！？;；,，]+\s*)')
_SENTENCE_PUNCT_RE = re.compile(r'[.!?。！？;；,，]+\s*')
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?")
_CJK_CHAR_RE = re.compile(r'[\u3400-\u9fff]')
_VISIBLE_TEXT_RE = re.compile(r'[\w\u3400-\u9fff]', re.UNICODE)
_HALLUCINATION_RE = re.compile(r'(.{2,30}?)(?:\s*\1){2,}', re.IGNORECASE)
_CREDIT_LIKE_RE = re.compile(
    r'\b(?:transcription|transcribed|subtitled|subtitle|captioned|captions?)\s+by\b',
    re.IGNORECASE,
)
_NOISE_COMMAND_RE = re.compile(
    r'^\s*(?:ignore noise|click|tap|beep|mouse click|keyboard click|background noise|noise only)[.!。！]?\s*$',
    re.IGNORECASE,
)
_NOISE_TAG_RE = re.compile(
    r'^\s*[\[\(（【]\s*(?:music|noise|applause|laughter|silence|background noise|音乐|噪声|掌声|笑声|静音)\s*[\]\)）】]\s*$',
    re.IGNORECASE,
)
# 订阅/引流类话术（ASR 幻觉高发），仅在文本很短时判可疑，避免误杀正常长句提及。
_SUBSCRIPTION_LIKE_RE = re.compile(
    r'点赞|订阅|转发|分享|打赏|关注|收藏|一键三连|'
    r'\b(?:like|subscribe|share|bell|notification|patreon)\b|'
    r'チャンネル登録|高評価|グッドボタン|登録お願い',
    re.IGNORECASE,
)
# 纯符号/装饰行（含反复的音乐符号）没有可读内容。
_SYMBOL_ONLY_RE = re.compile(r'^[\s\W_♪♫♩♬·…]+$', re.UNICODE)
# ASS/SSA 格式标签：\h（硬空格）、\N（换行）、\n（软换行）、{\...}（样式覆盖）
_ASS_TAG_RE = re.compile(r'\\[hHnN]|{\\[^}]*}')

# 换行用的 CJK 边界（含日文假名），只在 wrap_text 内部使用。
_WRAP_CJK_LIKE_RE = re.compile(r'[\u3040-\u30ff\u31f0-\u31ff\u3400-\u9fff]')
# 折行禁则：这些字符不得出现在行末 / 行首。
_NO_LINE_END_CHARS = '「『（【〔［｛“‘'
_NO_LINE_START_CHARS = '、。，！？；：）」』】〕］｝”’·ー～…'
# max_line_length 达到该值即视为「不做换行」（task_manager 用 999 关掉换行）。
_WRAP_DISABLED_LINE_LENGTH = 999

_MIN_GAP_S = 0.01
_MIN_VISIBLE_DUR_S = 0.05
_INVALID_TS_FALLBACK_S = 0.5
#: 时长上限比较的容差。``_clamp_end_within_limits`` 产出的是**恰好**
#: ``start + max_duration``，而 ``(start + 8.0) - start`` 在很多起点上并不精确
#: 等于 8.0（例如 ``10.41296942654103`` 得到 ``8.000000000000002``）。若按严格
#: 大于比较，这些被夹到上限的 cue 会在 ``_merge_within_limits`` 复查时被误拒，
#: 末条碎片的文本随之被丢弃（实测 241/4000 轮）。容差远小于一毫秒，不影响
#: 任何真实的上限判定。
_DURATION_EPSILON = 1e-9


def _join_texts(left: str, right: str) -> str:
    """拼接两段文本：两侧都是 CJK 时直接相连，否则用单个空格分隔。"""
    left_text = str(left or '').strip()
    right_text = str(right or '').strip()
    if not left_text:
        return right_text
    if not right_text:
        return left_text
    if _CJK_CHAR_RE.match(left_text[-1]) and _CJK_CHAR_RE.match(right_text[0]):
        return left_text + right_text
    return left_text + ' ' + right_text


@dataclass
class SrtTransformConfig:
    max_line_length: int = 42
    max_lines: int = 2
    split_long_cues: bool = True
    preserve_line_breaks: bool = False
    normalize_punctuation: bool = True
    filter_filler_words: bool = True
    time_offset_s: float = 0.0
    min_cue_duration_s: float = 0.6
    merge_gap_s: float = 0.3
    min_text_length: int = 2
    max_cue_duration_s: float = 8.0
    max_chars_per_second: float = 20.0
    cross_window_dup_tolerance_s: float = 2.0
    cross_window_dup_ratio: float = 0.8


class SrtTransformEngine:
    def __init__(self, config: SrtTransformConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _text_density_metrics(text: str) -> Dict[str, float]:
        normalized = str(text or '').strip()
        if not normalized:
            return {'visible_chars': 0.0, 'word_like_units': 0.0}
        return {
            'visible_chars': float(len(_VISIBLE_TEXT_RE.findall(normalized))),
            'word_like_units': float(
                len(_LATIN_WORD_RE.findall(normalized)) + len(_CJK_CHAR_RE.findall(normalized))
            ),
        }

    @classmethod
    def _is_implausibly_dense_cue(cls, text: str, duration_s: float) -> bool:
        normalized = str(text or '').strip()
        if not normalized:
            return False
        metrics = cls._text_density_metrics(normalized)
        safe_duration = max(float(duration_s or 0.0), 0.1)
        chars_per_second = metrics['visible_chars'] / safe_duration
        units_per_second = metrics['word_like_units'] / safe_duration
        if safe_duration < 8.0 and metrics['visible_chars'] > 280:
            return True
        if safe_duration < 15.0 and metrics['visible_chars'] > 420:
            return True
        if chars_per_second > 45.0 or units_per_second > 8.0:
            return True
        return False

    @staticmethod
    def _visual_char_units(char: str) -> float:
        if not char:
            return 0.0
        if char.isspace():
            return 0.35
        if _CJK_CHAR_RE.match(char):
            return 1.0
        if char.isascii():
            if char.isalnum():
                return 0.6
            return 0.45
        return 0.8

    @classmethod
    def _visual_text_units(cls, text: str) -> float:
        return sum(cls._visual_char_units(char) for char in str(text or ''))

    @classmethod
    def _is_suspicious_hallucination_text(cls, text: str) -> bool:
        normalized = str(text or '').strip()
        if not normalized:
            return False
        if _CREDIT_LIKE_RE.search(normalized):
            return True
        if _NOISE_COMMAND_RE.match(normalized) or _NOISE_TAG_RE.match(normalized):
            return True
        # 纯符号/装饰行（如 ♪♪♪）没有任何可读内容。
        if _SYMBOL_ONLY_RE.match(normalized):
            return True
        # 订阅/引流话术只在文本很短时判可疑：长句里的「点赞」多为正常表达。
        if len(normalized) <= 30 and _SUBSCRIPTION_LIKE_RE.search(normalized):
            return True
        return False

    def parse_srt(self, srt_text: str, base_offset_s: float = 0.0) -> List[Dict[str, Any]]:
        if not srt_text or not srt_text.strip():
            return []
        text = srt_text.strip()
        if text.startswith('\ufeff'):
            text = text[1:]
        if text.upper().startswith('WEBVTT'):
            lines = text.splitlines()
            idx = 1
            while idx < len(lines) and lines[idx].strip():
                idx += 1
            text = '\n'.join(lines[idx:]).strip()

        cues: List[Dict[str, Any]] = []
        #: 畸形时间戳块的文本：并入相邻 cue（与 ``finalize_cues`` /
        #: ``_normalize_output_cues`` 的「不丢内容」策略同一口径），而不是连文本一起丢。
        pending_texts: List[str] = []
        for block in _BLOCK_SPLIT_RE.split(text):
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            if len(lines) < 2:
                continue
            if '-->' not in lines[0] and len(lines) >= 2 and '-->' in lines[1]:
                time_line = lines[1]
                content_lines = lines[2:]
            else:
                time_line = lines[0]
                content_lines = lines[1:]
            if '-->' not in time_line:
                continue
            try:
                start_str, end_str = [part.strip() for part in time_line.split('-->')]
            except ValueError:
                continue
            content = '\n'.join(line.strip() for line in content_lines if line.strip())
            start_value = self._srt_time_to_seconds(start_str)
            end_value = self._srt_time_to_seconds(end_str)
            if start_value is None or end_value is None:
                # 畸形时间戳不再静默退化为 0.0：把起点搬到 0.0 会**抬高**覆盖率并
                # 压低 first_cue_start_ratio，即畸形时间戳反而帮助时间轴质检通过。
                # 时间轴不可用，但文本仍要安置：并入相邻 cue（挂起后并入下一个
                # 合法块；全部畸形时由下面的兜底处理）。此前这里连文本一起丢掉，
                # 与同一文件里 R4a「吸收文本、不丢内容」的策略相反。
                self.logger.warning(
                    'Cue with malformed timestamp %r --> %r: timeline dropped, text kept: %r',
                    start_str,
                    end_str,
                    content[:120],
                )
                if content:
                    pending_texts.append(content)
                continue
            start_s = start_value + base_offset_s
            end_s = end_value + base_offset_s
            if end_s <= start_s:
                end_s = start_s + _INVALID_TS_FALLBACK_S
            if not content:
                continue
            if pending_texts:
                content = '\n'.join(pending_texts + [content])
                pending_texts.clear()
            cues.append({
                'start': max(0.0, start_s),
                'end': max(end_s, start_s + _MIN_VISIBLE_DUR_S),
                'text': content,
                'timing_source': 'srt',
                'alignment_confidence': 0.45,
                'provider': '',
            })
        if pending_texts:
            if cues:
                # 畸形块在末尾：没有后续 cue 可挂，并入最后一条（仍不伪造时间轴）。
                cues[-1]['text'] = '\n'.join([str(cues[-1]['text'])] + pending_texts)
            else:
                # 一个可用时间轴都没有：**不**新造 0.0 的 cue（那正是 R4b 修掉的
                # 「畸形时间戳抬高覆盖率」），只能丢文本并如实告警。
                self.logger.warning(
                    'SRT 全部时间戳均不可用，%d 段文本无法安置已丢弃: %r',
                    len(pending_texts),
                    ' | '.join(pending_texts)[:120],
                )
        return cues

    def calibrate_segments(self, segment_results: List[tuple]) -> List[Dict[str, Any]]:
        results: List[AsrTranscriptionResult] = []
        for offset, srt_text in segment_results:
            if not srt_text:
                continue
            cues = self.parse_srt(srt_text, base_offset_s=offset)
            results.append(
                AsrTranscriptionResult(
                    provider='legacy',
                    response_format='srt',
                    timestamp_mode='srt',
                    text='\n'.join(c['text'] for c in cues),
                    metadata={'legacy_cues': cues},
                )
            )
        aligned = self.align_transcription_results(results)
        return [cue.to_dict() for cue in aligned]

    def align_transcription_results(
        self,
        results: Sequence[AsrTranscriptionResult],
        total_duration_s: float = 0.0,
    ) -> List[AlignedSubtitleCue]:
        aligned: List[AlignedSubtitleCue] = []
        for index, result in enumerate(results):
            aligned.extend(self._align_single_result(result, source_window_index=index))
        return self.stitch_aligned_cues(aligned, total_duration_s=total_duration_s)

    def _align_single_result(
        self,
        result: AsrTranscriptionResult,
        *,
        source_window_index: int,
    ) -> List[AlignedSubtitleCue]:
        if result.metadata.get('legacy_cues'):
            return [
                AlignedSubtitleCue(
                    start_s=float(cue['start']),
                    end_s=float(cue['end']),
                    text=str(cue['text'] or ''),
                    provider=result.provider,
                    timing_source='srt',
                    alignment_confidence=0.45,
                    source_window_index=source_window_index,
                    metadata={'legacy': True},
                )
                for cue in result.metadata['legacy_cues']
            ]

        if result.timestamp_mode == 'srt':
            base_offset = result.window.start_s if result.window else 0.0
            return [
                AlignedSubtitleCue(
                    start_s=float(cue['start']),
                    end_s=float(cue['end']),
                    text=str(cue['text'] or ''),
                    provider=result.provider,
                    timing_source='srt',
                    alignment_confidence=0.45,
                    source_window_index=source_window_index,
                )
                for cue in self.parse_srt(result.text, base_offset_s=base_offset)
            ]

        cues: List[AlignedSubtitleCue] = []
        for segment in result.segments:
            cue = self._align_segment(segment, result, source_window_index=source_window_index)
            if cue:
                cues.append(cue)
        return cues

    def _align_segment(
        self,
        segment: AsrSegmentTiming,
        result: AsrTranscriptionResult,
        *,
        source_window_index: int,
    ) -> Optional[AlignedSubtitleCue]:
        text = str(segment.text or '').strip()
        if not text:
            return None

        timing_source = 'segment'
        confidence = 0.72
        local_start = float(segment.start_s or 0.0)
        local_end = float(segment.end_s or 0.0)

        valid_words = [word for word in segment.words if str(word.text or '').strip() and word.end_s > word.start_s]
        if valid_words:
            timing_source = 'word'
            confidence = 0.95
            local_start = float(valid_words[0].start_s)
            local_end = float(valid_words[-1].end_s)
        elif local_end <= local_start and result.window:
            timing_source = 'window'
            confidence = 0.35
            local_start = 0.0
            local_end = result.window.duration_s

        if local_end <= local_start:
            local_end = local_start + _INVALID_TS_FALLBACK_S

        base_offset = result.window.start_s if result.window else 0.0
        global_start = base_offset + local_start
        global_end = base_offset + local_end

        if result.window:
            global_start = max(result.window.start_s, global_start)
            global_end = min(result.window.end_s, global_end)
            global_start = max(result.window.ownership_start_s, global_start)
            global_end = min(result.window.ownership_end_s, global_end)

        if global_end <= global_start:
            global_end = global_start + _MIN_VISIBLE_DUR_S

        return AlignedSubtitleCue(
            start_s=max(0.0, global_start),
            end_s=max(global_end, global_start + _MIN_VISIBLE_DUR_S),
            text=text,
            provider=result.provider,
            timing_source=timing_source,
            alignment_confidence=confidence,
            source_window_index=source_window_index,
            metadata={
                'response_format': result.response_format,
                'timestamp_mode': result.timestamp_mode,
                'language': result.language,
                'segment_confidence': segment.confidence,
            },
        )

    def stitch_aligned_cues(
        self,
        cues: Sequence[AlignedSubtitleCue],
        total_duration_s: float = 0.0,
    ) -> List[AlignedSubtitleCue]:
        if not cues:
            return []
        ordered = sorted(cues, key=lambda cue: (float(cue.start_s), float(cue.end_s), -float(cue.alignment_confidence)))
        stitched: List[AlignedSubtitleCue] = []
        for cue in ordered:
            text = str(cue.text or '').strip()
            if not text:
                continue
            if stitched:
                merged = self._merge_if_continuation(stitched[-1], cue)
                if merged:
                    stitched[-1] = merged
                    continue
                if self._is_duplicate_cue(stitched[-1], cue) or self._is_cross_window_dup(stitched[-1], cue):
                    stitched[-1] = self._pick_better_duplicate(stitched[-1], cue)
                    continue
            stitched.append(cue)

        if total_duration_s > 0:
            clamped: List[AlignedSubtitleCue] = []
            for cue in stitched:
                start_s = min(max(0.0, cue.start_s), total_duration_s)
                end_s = min(max(start_s + _MIN_VISIBLE_DUR_S, cue.end_s), total_duration_s)
                clamped.append(
                    AlignedSubtitleCue(
                        start_s=start_s,
                        end_s=end_s,
                        text=cue.text,
                        provider=cue.provider,
                        timing_source=cue.timing_source,
                        alignment_confidence=cue.alignment_confidence,
                        source_window_index=cue.source_window_index,
                        metadata=dict(cue.metadata or {}),
                    )
                )
            return clamped
        return stitched

    def _is_duplicate_cue(self, left: AlignedSubtitleCue, right: AlignedSubtitleCue) -> bool:
        left_key = self._normalize_compare_text(left.text)
        right_key = self._normalize_compare_text(right.text)
        if not left_key or not right_key:
            return False
        same_text = left_key == right_key
        close_in_time = abs(float(left.start_s) - float(right.start_s)) <= 1.0 or float(right.start_s) <= float(left.end_s)
        return same_text and close_in_time

    @staticmethod
    def _window_index_of(cue: Any) -> int:
        if isinstance(cue, dict):
            raw = cue.get('source_window_index', -1)
        else:
            raw = getattr(cue, 'source_window_index', -1)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return -1

    def _is_cross_window_dup(self, left: Any, right: Any) -> bool:
        """跨识别窗口的重复 cue：不同窗口 + 起点接近 + 文本相同或高度相似。"""
        left_window = self._window_index_of(left)
        right_window = self._window_index_of(right)
        if left_window < 0 or right_window < 0 or left_window == right_window:
            return False
        try:
            start_delta = abs(float(left.start_s) - float(right.start_s))
        except (TypeError, ValueError):
            return False
        tolerance = max(0.0, float(self.config.cross_window_dup_tolerance_s or 0.0))
        if start_delta > tolerance:
            return False
        left_key = self._normalize_compare_text(left.text)
        right_key = self._normalize_compare_text(right.text)
        if not left_key or not right_key:
            return False
        if left_key == right_key:
            return True
        ratio = float(self.config.cross_window_dup_ratio or 0.0)
        if ratio <= 0.0:
            return False
        return difflib.SequenceMatcher(None, left_key, right_key).ratio() >= ratio

    def _max_expected_duration(self, text: str) -> float:
        """按语速上限估算一段文本合理的最长显示时长，并夹在 [1.0, 6.0] 秒内。"""
        try:
            chars_per_second = float(self.config.max_chars_per_second or 0.0)
        except (TypeError, ValueError):
            chars_per_second = 0.0
        if chars_per_second <= 0.0:
            chars_per_second = 20.0
        raw = len(str(text or '')) / max(1.0, chars_per_second)
        return min(6.0, max(1.0, raw))

    def _pick_better_duplicate(self, left: AlignedSubtitleCue, right: AlignedSubtitleCue) -> AlignedSubtitleCue:
        if right.alignment_confidence > left.alignment_confidence:
            better = right
        elif right.timing_source == 'word' and left.timing_source != 'word':
            better = right
        else:
            better = left
        start_s = min(left.start_s, right.start_s)
        end_s = max(left.end_s, right.end_s)
        # 合并出的 cue 不得因为「取最晚结束时间」而变成超长显示。
        end_s = min(end_s, start_s + self._max_expected_duration(better.text))
        return AlignedSubtitleCue(
            start_s=start_s,
            end_s=max(end_s, start_s + _MIN_VISIBLE_DUR_S),
            text=better.text,
            provider=better.provider or left.provider,
            timing_source=better.timing_source,
            alignment_confidence=max(left.alignment_confidence, right.alignment_confidence),
            source_window_index=better.source_window_index,
            metadata=dict(better.metadata or {}),
        )

    def _merge_if_continuation(
        self,
        left: AlignedSubtitleCue,
        right: AlignedSubtitleCue,
    ) -> Optional[AlignedSubtitleCue]:
        gap = float(right.start_s) - float(left.end_s)
        if gap > max(0.4, float(self.config.merge_gap_s or 0.0)):
            return None
        merged_text = self._merge_text_with_overlap(left.text, right.text)
        if not merged_text:
            return None
        if merged_text == left.text and abs(right.start_s - left.start_s) > 1.0:
            return None
        max_chars = max(0, int(self.config.max_line_length) * int(self.config.max_lines))
        if max_chars > 0 and len(merged_text) > max_chars:
            return None
        start_s = min(float(left.start_s), float(right.start_s))
        end_s = max(float(left.end_s), float(right.end_s))
        end_s = min(end_s, start_s + self._max_expected_duration(merged_text))
        return AlignedSubtitleCue(
            start_s=start_s,
            end_s=max(end_s, start_s + _MIN_VISIBLE_DUR_S),
            text=merged_text,
            provider=left.provider or right.provider,
            timing_source=left.timing_source if left.alignment_confidence >= right.alignment_confidence else right.timing_source,
            alignment_confidence=max(float(left.alignment_confidence), float(right.alignment_confidence)),
            source_window_index=left.source_window_index,
            metadata=dict(left.metadata or {}),
        )

    @staticmethod
    def _normalize_compare_text(text: str) -> str:
        normalized = _WHITESPACE_RE.sub(' ', str(text or '').strip().lower())
        normalized = _NON_WORD_RE.sub('', normalized)
        return normalized

    def _merge_text_with_overlap(self, left: str, right: str) -> str:
        left_text = _WHITESPACE_RE.sub(' ', str(left or '').strip())
        right_text = _WHITESPACE_RE.sub(' ', str(right or '').strip())
        if not left_text:
            return right_text
        if not right_text:
            return left_text
        if self._normalize_compare_text(left_text) == self._normalize_compare_text(right_text):
            return left_text if len(left_text) >= len(right_text) else right_text
        if right_text in left_text:
            return left_text
        if left_text in right_text:
            return right_text

        left_tokens = left_text.split(' ')
        right_tokens = right_text.split(' ')
        # CJK文本通常没有空格分词，使用字符级重叠检测作为回退
        if len(left_tokens) <= 1 and len(right_tokens) <= 1 and len(left_text) > 1 and len(right_text) > 1:
            max_char_overlap = min(len(left_text), len(right_text), 20)
            for overlap in range(max_char_overlap, 0, -1):
                if left_text[-overlap:] == right_text[:overlap]:
                    return left_text + right_text[overlap:]
            return ''
        max_overlap = min(len(left_tokens), len(right_tokens), 8)
        for overlap in range(max_overlap, 0, -1):
            left_tail = ' '.join(left_tokens[-overlap:])
            right_head = ' '.join(right_tokens[:overlap])
            if self._normalize_compare_text(left_tail) == self._normalize_compare_text(right_head):
                suffix = ' '.join(right_tokens[overlap:]).strip()
                return (left_text + (' ' + suffix if suffix else '')).strip()
        return ''

    @staticmethod
    def _speech_coverage_ratio(start_s: float, end_s: float, speech_spans: Sequence[Any]) -> float:
        duration = max(float(end_s) - float(start_s), 0.0)
        if duration <= 0.0:
            return 0.0
        covered = 0.0
        for span in speech_spans:
            try:
                span_start = float(span[0])
                span_end = float(span[1])
            except (TypeError, ValueError, IndexError):
                continue
            overlap = min(float(end_s), span_end) - max(float(start_s), span_start)
            if overlap > 0.0:
                covered += overlap
        return min(1.0, covered / duration)

    def clean_hallucinations(
        self,
        cues: Sequence[Any],
        speech_spans: Optional[Sequence[Tuple[float, float]]] = None,
    ) -> List[Dict[str, Any]]:
        normalized_cues = self._coerce_cue_dicts(cues)
        cleaned: List[Dict[str, Any]] = []
        seen_texts: Dict[str, float] = {}
        spans = [span for span in (speech_spans or []) if span]
        for cue in normalized_cues:
            text = str(cue.get('text') or '').strip()
            if not text:
                continue
            # 清洗 ASS/SSA 格式标签
            text = text.replace('\\h', ' ').replace('\\H', ' ')
            text = text.replace('\\N', ' ').replace('\\n', ' ')
            text = _ASS_TAG_RE.sub('', text)
            text = _WHITESPACE_RE.sub(' ', text).strip()
            if not text:
                continue
            duration = max(float(cue.get('end', 0.0)) - float(cue.get('start', 0.0)), 0.0)
            if self._is_suspicious_hallucination_text(text):
                continue
            if self._is_implausibly_dense_cue(text, duration):
                continue
            collapsed = _HALLUCINATION_RE.sub(r'\1', text).strip()
            if not collapsed:
                continue
            dedupe_key = _WHITESPACE_RE.sub(' ', collapsed.lower()).strip()
            prev_end = seen_texts.get(dedupe_key)
            if prev_end is not None and abs(float(cue.get('start', 0.0)) - prev_end) < 5.0:
                continue
            # 静音段重复上一句：cue 与语音区间的交集过少，且内容与上一条保留 cue 高度相似。
            if spans and cleaned and self._is_repeat_in_silence(cue, collapsed, cleaned[-1], spans):
                continue
            seen_texts[dedupe_key] = float(cue.get('end', 0.0))
            cue['text'] = collapsed
            cleaned.append(cue)
        return cleaned

    def _is_repeat_in_silence(
        self,
        cue: Dict[str, Any],
        collapsed_text: str,
        previous_cue: Dict[str, Any],
        speech_spans: Sequence[Any],
    ) -> bool:
        start_s = float(cue.get('start', 0.0) or 0.0)
        end_s = float(cue.get('end', 0.0) or 0.0)
        coverage = self._speech_coverage_ratio(start_s, end_s, speech_spans)
        if coverage >= 0.2:
            return False
        current_key = self._normalize_compare_text(collapsed_text)
        previous_key = self._normalize_compare_text(previous_cue.get('text'))
        if not current_key or not previous_key:
            return False
        return difflib.SequenceMatcher(None, current_key, previous_key).ratio() >= 0.6

    def resolve_overlaps(self, cues: Sequence[Any], total_duration_s: float = 0.0) -> List[Dict[str, Any]]:
        normalized_cues = sorted(self._coerce_cue_dicts(cues), key=lambda cue: (cue['start'], cue['end']))
        if not normalized_cues:
            return []
        max_merge_chars = int(self.config.max_line_length) * int(self.config.max_lines)
        resolved: List[Dict[str, Any]] = []
        for cue in normalized_cues:
            if not resolved:
                resolved.append(cue)
                continue
            prev = resolved[-1]
            if float(prev['end']) <= float(cue['start']):
                resolved.append(cue)
                continue

            prev_text = str(prev.get('text') or '').strip()
            cue_text = str(cue.get('text') or '').strip()
            if self._normalize_compare_text(prev_text) == self._normalize_compare_text(cue_text):
                prev['end'] = max(float(prev['end']), float(cue['end']))
                prev['alignment_confidence'] = max(
                    float(prev.get('alignment_confidence', 0.0)),
                    float(cue.get('alignment_confidence', 0.0)),
                )
                continue

            continuity = self._merge_text_with_overlap(prev_text, cue_text)
            if continuity and (max_merge_chars <= 0 or len(continuity) <= max_merge_chars):
                prev['text'] = continuity
                prev['end'] = max(float(prev['end']), float(cue['end']))
                prev['alignment_confidence'] = max(
                    float(prev.get('alignment_confidence', 0.0)),
                    float(cue.get('alignment_confidence', 0.0)),
                )
                continue

            boundary = (float(prev['end']) + float(cue['start'])) / 2.0
            next_start = boundary + _MIN_GAP_S
            if boundary - float(prev['start']) >= _MIN_VISIBLE_DUR_S and float(cue['end']) - next_start >= _MIN_VISIBLE_DUR_S:
                prev['end'] = boundary
                cue['start'] = next_start
                resolved.append(cue)
                continue
            if float(cue['end']) - (float(prev['end']) + _MIN_GAP_S) >= _MIN_VISIBLE_DUR_S:
                cue['start'] = float(prev['end']) + _MIN_GAP_S
                resolved.append(cue)
                continue
            if float(cue['start']) - float(prev['start']) >= _MIN_VISIBLE_DUR_S:
                prev['end'] = float(cue['start'])
                resolved.append(cue)
                continue

            prev_conf = float(prev.get('alignment_confidence', 0.0))
            cue_conf = float(cue.get('alignment_confidence', 0.0))
            if cue_conf > prev_conf:
                resolved[-1] = cue

        if total_duration_s > 0:
            for cue in resolved:
                cue['start'] = min(cue['start'], total_duration_s)
                cue['end'] = min(cue['end'], total_duration_s)
        return resolved

    def _normalize_text_line(self, text: str) -> str:
        # 先清洗 ASS/SSA 格式标签（YouTube 自动生成字幕可能带这些）
        text = text.replace('\\h', ' ').replace('\\H', ' ')
        text = text.replace('\\N', ' ').replace('\\n', ' ')
        text = _ASS_TAG_RE.sub('', text)  # 移除 {\b1} 等样式覆盖标签
        text = _WHITESPACE_RE.sub(' ', text).strip()
        if self.config.normalize_punctuation:
            text = _PUNCTUATION_SPACE_RE.sub(r'\1 ', text)
            text = _WHITESPACE_RE.sub(' ', text).strip()
        if self.config.filter_filler_words:
            for pattern in _FILLER_PATTERNS:
                text = pattern.sub('', text)
            text = _REPEATED_WORD_RE.sub(r'\1', text)
            text = _WHITESPACE_RE.sub(' ', text).strip()
        return text

    def normalize_text(self, text: str) -> str:
        if not text:
            return ''
        normalized = str(text)
        if not self.config.preserve_line_breaks:
            return self._normalize_text_line(normalized)
        lines = []
        for raw_line in normalized.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
            cleaned = self._normalize_text_line(raw_line)
            if cleaned:
                lines.append(cleaned)
        return '\n'.join(lines).strip()

    def wrap_text(
        self,
        text: str,
        max_line_length: Optional[int] = None,
        max_lines: Optional[int] = None,
    ) -> str:
        """把一条字幕折成最多 max_lines 行的真实换行文本。

        `max_line_length >= 999` 或 `max_lines <= 0` 时原样返回：task_manager 用
        (999, 99) 构造引擎并明确要求「不要在这里切分传入的 SRT cue」。
        """
        raw = str(text or '')
        if not raw:
            return text
        limit = int(self.config.max_line_length if max_line_length is None else max_line_length)
        line_limit = int(self.config.max_lines if max_lines is None else max_lines)
        if limit <= 0 or line_limit <= 0 or limit >= _WRAP_DISABLED_LINE_LENGTH:
            return text

        segments = [
            part.strip()
            for part in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        ]
        segments = [part for part in segments if part]
        if not segments:
            return text

        wrapped: List[str] = []
        for segment in segments:
            wrapped.extend(self._wrap_single_segment(segment, limit))
        wrapped = [line for line in wrapped if line]
        if not wrapped:
            return text
        if len(wrapped) > line_limit:
            head = wrapped[:line_limit - 1]
            folded = wrapped[line_limit - 1]
            for extra in wrapped[line_limit:]:
                folded = _join_texts(folded, extra)
            wrapped = head + [folded]
        return '\n'.join(wrapped).strip()

    def _wrap_single_segment(self, segment: str, limit: int) -> List[str]:
        if not _WRAP_CJK_LIKE_RE.search(segment):
            return self._wrap_latin_segment(segment, limit)
        return self._wrap_token_segment(segment, limit)

    def _wrap_latin_segment(self, segment: str, limit: int) -> List[str]:
        """拉丁文本按空格折行，不切断单词；只有单个超长词才按边界硬断。

        超长词优先在"字符类别切换处"（字母↔数字、大小写切换、连字符/下划线）
        断开，避免把 `Supercalifragilisticexpialidocious` 这类无空格长串从中间
        随机切断；找不到合适断点时才退化为按字符硬切。
        """
        words = [word for word in _WHITESPACE_RE.split(segment) if word]
        if not words:
            return [segment]
        lines: List[str] = []
        current = ''
        for word in words:
            if current and len(current) + 1 + len(word) <= limit:
                current = current + ' ' + word
                continue
            if current:
                lines.append(current)
                current = ''
            while len(word) > limit:
                head = self._find_break_point(word, limit)
                lines.append(word[:head])
                word = word[head:]
            current = word
        if current:
            lines.append(current)
        return lines or [segment]

    @staticmethod
    def _find_break_point(word: str, limit: int) -> int:
        """为必须硬断的超长词找一个尽量自然的断点（返回切片位置）。

        只在"切点足够靠后"时采用自然断点（字母↔数字、连字符/下划线/点），
        否则按 limit 硬切。这样 `ABC12345DEF67890GHI` 不再被切成 `ABC`/`12345`，
        而纯字母长词仍是等宽硬切（无自然断点可依）。

        返回 1..limit 之间的整数，保证必然前进。
        """
        upper = min(limit, len(word))
        if upper <= 1:
            return max(1, upper)
        floor = max(2, int(upper * 0.6))
        for index in range(upper, floor - 1, -1):
            prev_char = word[index - 1]
            char = word[index]
            if prev_char in '-_./' or char in '-_./':
                return index
            if prev_char.isdigit() != char.isdigit():
                return index
            if prev_char.islower() and char.isupper():
                return index
        return upper

    @staticmethod
    def _wrap_tokens(segment: str) -> List[Tuple[str, bool]]:
        """按空格与 CJK 边界切 token，返回 (文本, 前面是否有空白)。"""
        tokens: List[Tuple[str, bool]] = []
        text = str(segment or '')
        pending_space = False
        index = 0
        while index < len(text):
            char = text[index]
            if char.isspace():
                pending_space = True
                index += 1
                continue
            if _WRAP_CJK_LIKE_RE.match(char):
                tokens.append((char, pending_space))
                pending_space = False
                index += 1
                continue
            start = index
            while index < len(text) and not text[index].isspace() and not _WRAP_CJK_LIKE_RE.match(text[index]):
                index += 1
            tokens.append((text[start:index], pending_space))
            pending_space = False
        return tokens

    @staticmethod
    def _wrap_prefix_index(text: str, limit: int) -> int:
        """返回视觉宽度不超过 limit 的最长前缀字符数（至少 1）。"""
        used = 0.0
        for idx, char in enumerate(str(text or '')):
            used += SrtTransformEngine._visual_char_units(char)
            if used > limit + 1e-9:
                return max(1, idx)
        return len(str(text or ''))

    @staticmethod
    def _adjust_wrap_break_index(text: str, index: int) -> int:
        """按禁则微调折行点：行末不留前引号类字符，行首不留标点类字符。"""
        text = str(text or '')
        length = len(text)
        if length <= 1:
            return length
        cut = max(1, min(int(index), length))
        guard = 0
        while cut < length and text[cut - 1] in _NO_LINE_END_CHARS and guard < 4:
            cut += 1
            guard += 1
        guard = 0
        while 1 < cut < length and text[cut] in _NO_LINE_START_CHARS and guard < 4:
            cut -= 1
            guard += 1
        return max(1, min(cut, length))

    def _wrap_token_segment(self, segment: str, limit: int) -> List[str]:
        """CJK/混排文本：按 token 贪心累加视觉宽度，并遵守折行禁则。"""
        tokens = self._wrap_tokens(segment)
        if not tokens:
            return [segment]
        lines: List[str] = []
        current = ''
        for token_text, glue_space in tokens:
            if not current:
                current = token_text
            else:
                candidate = _join_texts(current, token_text) if glue_space else current + token_text
                if (
                    self._visual_text_units(candidate) <= limit + 1e-9
                    or self._ends_with_no_break_char(current)
                    or self._starts_with_no_break_char(token_text)
                ):
                    current = candidate
                else:
                    cut = self._adjust_wrap_break_index(current, len(current))
                    head = current[:cut].strip()
                    tail = current[cut:]
                    if head:
                        lines.append(head)
                    if tail:
                        current = _join_texts(tail, token_text) if glue_space else tail + token_text
                    else:
                        current = token_text
            # 单个 token 本身超宽（例如超长 URL）：按视觉宽度硬断，保证不丢字。
            while self._visual_text_units(current) > limit + 1e-9 and len(current) > 1:
                cut = self._adjust_wrap_break_index(current, self._wrap_prefix_index(current, limit))
                if cut <= 0 or cut >= len(current):
                    break
                head = current[:cut].strip()
                if head:
                    lines.append(head)
                current = current[cut:]
        if current.strip():
            lines.append(current.strip())
        return [line for line in lines if line] or [segment]

    @staticmethod
    def _starts_with_no_break_char(text: str) -> bool:
        return bool(text) and text[0] in _NO_LINE_START_CHARS

    @staticmethod
    def _ends_with_no_break_char(text: str) -> bool:
        return bool(text) and text[-1] in _NO_LINE_END_CHARS

    def split_long_cue(self, cue: Dict[str, Any]) -> List[Dict[str, Any]]:
        text = cue.get('text', '')
        if not text or not self.config.split_long_cues:
            return [cue]
        max_line = int(self.config.max_line_length)
        max_lines = int(self.config.max_lines)
        max_total = max_line * max_lines
        text_units = self._visual_text_units(text)
        if text_units <= max_total:
            return [cue]

        sentences = [part for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
        joined: List[str] = []
        idx = 0
        while idx < len(sentences):
            if idx + 1 < len(sentences) and _SENTENCE_PUNCT_RE.match(sentences[idx + 1]):
                joined.append(sentences[idx] + sentences[idx + 1])
                idx += 2
            else:
                joined.append(sentences[idx])
                idx += 1

        result: List[Dict[str, Any]] = []
        current_text = ''
        start_time = cue['start']
        total_units = max(1.0, text_units)
        duration = cue['end'] - cue['start']
        for sentence in joined:
            sentence = sentence.strip()
            if not sentence:
                continue
            test = _join_texts(current_text, sentence) if current_text else sentence
            if self._visual_text_units(test) > max_total and current_text:
                units_in = self._visual_text_units(current_text)
                frac = units_in / total_units
                cue_duration = max(duration * frac, 0.5)
                cue_duration = min(cue_duration, cue['end'] - start_time)
                # 切分后仍可能超出单条时长上限：压缩到上限，且下一段从压缩后的时间点继续，
                # 避免产生重叠的 cue。
                cue_duration = self._clamp_split_duration(start_time, start_time + cue_duration) - start_time
                result.append({'start': start_time, 'end': start_time + cue_duration, 'text': current_text})
                start_time += cue_duration
                total_units = max(1.0, total_units - units_in)
                duration -= cue_duration
                current_text = sentence
            else:
                current_text = test
        if current_text:
            result.append({'start': start_time, 'end': self._clamp_split_duration(start_time, cue['end']), 'text': current_text})
        return result or [cue]

    def _clamp_split_duration(self, start_s: float, end_s: float) -> float:
        max_duration = float(self.config.max_cue_duration_s or 0.0)
        if max_duration <= 0.0:
            return float(end_s)
        return min(float(end_s), float(start_s) + max_duration)

    def apply_text_processing(self, cues: Sequence[Any]) -> List[Dict[str, Any]]:
        processed: List[Dict[str, Any]] = []
        for cue in self._coerce_cue_dicts(cues):
            cue['text'] = self.normalize_text(cue['text'])
            if not cue['text']:
                continue
            processed.extend(self.split_long_cue(cue))
        return processed

    def _clamp_end_within_limits(self, start_s: float, end_s: float, cue) -> float:
        """把打算写入的 cue 结束时间收敛到「最长时长上限」之内。

        `finalize_cues` 的时长修正（延长到 min_dur、或延长到「下一条起点 − 最小
        间隔」）此前不做上限复查，于是当 `min_cue_duration_s` 接近甚至大于
        `max_cue_duration_s` 时会产出超出上限的 cue —— 而这是落盘链路的最后一步，
        下游已无任何时长校验。

        字速上限不在这里额外收紧：延长时长只会**降低**字速，因此延长本身不会
        让原本合规的 cue 违反字速上限；真正可能越界的只有时长。
        """
        max_duration = float(self.config.max_cue_duration_s or 0.0)
        if max_duration <= 0.0:
            return float(end_s)
        return min(float(end_s), float(start_s) + max_duration)

    def finalize_cues(self, cues: Sequence[Any], total_duration_s: float) -> List[Dict[str, Any]]:
        normalized_cues = sorted(self._coerce_cue_dicts(cues), key=lambda cue: float(cue.get('start', 0.0)))
        if not normalized_cues:
            return []
        offset = float(self.config.time_offset_s or 0.0)
        merge_gap = max(0.0, float(self.config.merge_gap_s or 0.0))
        min_text = max(0, int(self.config.min_text_length or 0))
        min_dur = max(0.05, float(self.config.min_cue_duration_s or 0.05))
        # 最短时长不得大于最长时长上限：否则「把过短 cue 延长到 min_dur」这条
        # 修正必然产出越界 cue。两个配置项独立校验时这种组合是合法提交。
        max_dur_limit = float(self.config.max_cue_duration_s or 0.0)
        if max_dur_limit > 0.0:
            min_dur = min(min_dur, max_dur_limit)
        drop_dur = min(0.3, min_dur)

        for cue in normalized_cues:
            cue['start'] = max(0.0, min(total_duration_s, float(cue['start']) + offset))
            cue['end'] = max(0.0, min(total_duration_s, float(cue['end']) + offset))
            if cue['end'] <= cue['start']:
                cue['end'] = min(total_duration_s, cue['start'] + min_dur)

        max_merge_chars = int(self.config.max_line_length) * int(self.config.max_lines)
        merged: List[Dict[str, Any]] = []
        for cue in normalized_cues:
            if not merged:
                merged.append(cue)
                continue
            prev = merged[-1]
            gap = float(cue['start']) - float(prev['end'])
            prev_text = str(prev.get('text') or '').strip()
            cur_text = str(cue.get('text') or '').strip()
            should_merge = False
            if gap <= merge_gap:
                continuity = self._merge_text_with_overlap(prev_text, cur_text)
                if continuity or gap < 0.0:
                    should_merge = True
                elif len(prev_text) < min_text or len(cur_text) < min_text:
                    should_merge = True
            merged_text = ''
            if should_merge:
                merged_text = self._merge_text_with_overlap(prev_text, cur_text) or _join_texts(prev_text, cur_text)
                merged_text = _WHITESPACE_RE.sub(' ', merged_text).strip()
                if max_merge_chars > 0 and len(merged_text) > max_merge_chars:
                    should_merge = False
                elif not self._merge_within_limits(
                    min(float(prev['start']), float(cue['start'])),
                    max(float(prev['end']), float(cue['end'])),
                    merged_text,
                ):
                    should_merge = False
            if should_merge:
                prev['text'] = merged_text
                prev['end'] = max(float(prev['end']), float(cue['end']))
                prev['alignment_confidence'] = max(float(prev.get('alignment_confidence', 0.0)), float(cue.get('alignment_confidence', 0.0)))
            else:
                merged.append(cue)

        finalized: List[Dict[str, Any]] = []
        for idx, cue in enumerate(merged):
            start = float(cue['start'])
            end = float(cue['end'])
            dur = end - start
            if dur < min_dur:
                next_start = float(merged[idx + 1]['start']) if idx + 1 < len(merged) else total_duration_s
                gap_to_next = next_start - start
                if gap_to_next > min_dur + _MIN_GAP_S:
                    cue['end'] = self._clamp_end_within_limits(start, start + min_dur, cue)
                elif gap_to_next > _MIN_VISIBLE_DUR_S:
                    cue['end'] = self._clamp_end_within_limits(start, next_start - _MIN_GAP_S, cue)
                elif idx + 1 < len(merged):
                    # 把下一条的起点提前到本条起点会让它的跨度变大，可能越过
                    # 「最长时长 / 最高字速」上限 —— 必须先验证再改，否则这里
                    # 是整个后处理链里唯一一处不做上限复查的时长修改点。
                    candidate = dict(merged[idx + 1])
                    candidate['start'] = start
                    candidate['text'] = _join_texts(str(cue['text']), str(candidate['text']))
                    if self._merge_within_limits(
                        start, float(candidate['end']), str(candidate['text'])
                    ):
                        merged[idx + 1] = candidate
                        continue
                    # 上限不允许前移时退回「延长自身」，且**不得越过下一条起点** ——
                    # 否则会造出 overlap（subtitle_qc 的 timeline_overlap 会判失败）。
                    # 延长不足 drop_dur 时交给下游 cleaned 阶段的吸收逻辑处理（那里
                    # 有完整的上限校验与丢弃告警）。
                    cue['end'] = max(
                        float(cue['end']),
                        min(start + min_dur, next_start - _MIN_GAP_S),
                    )
                else:
                    # 末条过短：先尝试延长到 min_dur；已到视频末尾延不了时，
                    # 若前面已有可用 cue 就把这句话并进去，避免留下不可读的碎片。
                    # 并入必须走 ``_absorb_cue_into``：它内含「最长时长 / 最高字速 /
                    # 合并字数」三道上限复查。此前这里直接
                    # ``finalized[-1]['end'] = max(prev_end, extended_end)`` 抬高前一条的
                    # 结束时间，绕过了全部上限 —— 默认配置下能把一条普通字幕钉在
                    # 屏幕上直到片尾（实测 10.0→600.0s，跨度 590s ≫ 8s 上限）。
                    extended_end = self._clamp_end_within_limits(
                        start, min(total_duration_s, start + min_dur), cue
                    )
                    if extended_end - start >= drop_dur or not finalized:
                        cue['end'] = extended_end
                    else:
                        # 并入前一条：把「延长」限制在时长上限内（end_limit_s），
                        # 这样既不丢这条文本，也不会为了保住它而把前一条钉到片尾。
                        absorb_end = self._clamp_end_within_limits(
                            float(finalized[-1]['start']),
                            max(float(finalized[-1]['end']), extended_end),
                            finalized[-1],
                        )
                        if self._absorb_cue_into(finalized[-1], cue, end_limit_s=absorb_end):
                            continue
                        # 前一条已到上限、装不下这段文本：保留这条短 cue，
                        # 交给下游 cleaned 阶段按同一套上限继续安置
                        # （并入前一条 → 并入后一条 → 延长自身 → 记 warning 丢弃）。
                        cue['end'] = extended_end
            finalized.append(cue)

        cleaned: List[Dict[str, Any]] = []
        for idx, cue in enumerate(finalized):
            text = str(cue.get('text') or '').strip()
            start = float(cue['start'])
            dur = float(cue['end']) - start
            if dur < drop_dur:
                # 过短的 cue 不允许连文本一起丢掉（修复前这里直接 continue，实测
                # alpha/gamma/epsilon/eta 四段输入会丢掉 'gamma delta' 且不留任何日志，
                # 而这是 speech_recognition 落盘链路的最后一步，下游没有文本覆盖率校验）。
                # 安置顺序：并入前一条 → 并入后一条 → 延长自身到 min_dur。
                # 只有三者在时间轴上真的都放不下时才丢弃，并记 warning 带上被丢弃的文本。
                # 并入前一条时同样要把「为了容纳本 cue 而做的延长」限制在时长上限内
                # （``end_limit_s``）：末条碎片的 end 贴着片尾，不夹上限就会得到
                # 「前一条起点 → 片尾」的越限跨度，进而被 ``_merge_within_limits``
                # 拒绝、文本被丢弃（实测 0.05s < 时长 ≤ min_dur+0.01s 这一档全部丢字）。
                if cleaned and self._absorb_cue_into(
                    cleaned[-1],
                    cue,
                    end_limit_s=self._clamp_end_within_limits(
                        float(cleaned[-1]['start']),
                        max(float(cleaned[-1]['end']), float(cue['end'])),
                        cleaned[-1],
                    ),
                ):
                    continue
                next_cue = finalized[idx + 1] if idx + 1 < len(finalized) else None
                # 并入后一条时起点最多提前到「前一条末尾 + 最小间隔」，否则会造出 overlap。
                absorb_floor = (float(cleaned[-1]['end']) + _MIN_GAP_S) if cleaned else 0.0
                if next_cue is not None and self._absorb_cue_into(
                    next_cue, cue, prepend=True, floor_start_s=absorb_floor
                ):
                    continue
                # 邻居都装不下时延长自身：上限取「下一条起点 − 最小间隔」，末条取总时长，
                # 都不能跨越下一条，否则会造出 overlap（subtitle_qc 的 timeline_overlap 判失败）。
                extend_limit = (
                    float(next_cue['start']) - _MIN_GAP_S if next_cue is not None else float(total_duration_s)
                )
                cue['end'] = max(float(cue['end']), min(extend_limit, start + min_dur))
                if float(cue['end']) - start < drop_dur:
                    self.logger.warning(
                        'Dropping unplaceable short cue %.3f-%.3fs (%.3fs), text lost: %r',
                        start,
                        float(cue['end']),
                        dur,
                        text,
                    )
                    continue
                cleaned.append(cue)
                continue
            if len(text) < min_text and dur < min_dur:
                # 文本过短且时长不够，既不可读也塞不进邻居的可见时长里。
                # 这里同样不允许静默丢弃：留下被丢的文本，便于追查字幕缺词。
                self.logger.warning(
                    'Dropping too-short-text cue %.3f-%.3fs (%.3fs), text dropped: %r',
                    start,
                    float(cue['end']),
                    dur,
                    text,
                )
                continue
            cleaned.append(cue)
        return cleaned

    def _absorb_cue_into(
        self,
        target: Dict[str, Any],
        cue: Dict[str, Any],
        prepend: bool = False,
        floor_start_s: float = 0.0,
        end_limit_s: Optional[float] = None,
    ) -> bool:
        """把过短 cue 的文本并入相邻 cue；邻居装不下时返回 False。

        prepend=True 表示并入后一条（文本按时间轴顺序排在前）。并入后的文本同样要过
        「合并最长时长」与「最高字速」两道上限，否则宁可让调用方延长这条 cue，
        也不把文字塞进一条读不完或超宽的字幕。

        ``end_limit_s`` 限制并入后的结束时间上界（末条碎片并入前一条时用它把
        「延长」限制在时长上限之内，避免为了保住文本而把前一条钉到片尾）。

        并入后一条时会把它的起点提前到被吸收 cue 的起点（受 floor_start_s 限制，
        调用方传入「已定稿前一条的末尾 + 最小间隔」）：跨度变大才能同时满足字速上限
        与「不丢字」，而只提前到前一条之后就不会造出 subtitle_qc 会判失败的 overlap。
        """
        cue_text = str(cue.get('text') or '').strip()
        if not cue_text:
            return True
        target_text = str(target.get('text') or '').strip()
        merged_text = _join_texts(cue_text, target_text) if prepend else _join_texts(target_text, cue_text)
        merged_text = _WHITESPACE_RE.sub(' ', merged_text).strip()
        max_merge_chars = int(self.config.max_line_length) * int(self.config.max_lines)
        if max_merge_chars > 0 and len(merged_text) > max_merge_chars:
            return False
        start_s = float(target['start'])
        cue_end = float(cue['end'])
        if end_limit_s is not None:
            # 只限制「为了容纳本 cue 而做的延长」：不得缩短邻居自身的结束时间。
            cue_end = min(cue_end, float(end_limit_s))
        end_s = max(float(target['end']), cue_end)
        if prepend:
            start_s = max(min(start_s, float(cue['start'])), float(floor_start_s))
        if not self._merge_within_limits(start_s, end_s, merged_text):
            return False
        target['text'] = merged_text
        target['start'] = start_s
        target['end'] = end_s
        target['alignment_confidence'] = max(
            float(target.get('alignment_confidence', 0.0) or 0.0),
            float(cue.get('alignment_confidence', 0.0) or 0.0),
        )
        return True

    def _merge_within_limits(self, start_s: float, end_s: float, text: str) -> bool:
        """合并后的 cue 必须同时满足「最长时长」与「最高字速」两个上限。

        时长比较带 ``_DURATION_EPSILON`` 容差：``_clamp_end_within_limits`` 的
        结果在浮点下可能比上限大一个 ulp，按严格大于比较会把「刚好夹到上限」的
        并入判定误拒，进而丢掉末条碎片文本。
        """
        duration = max(0.0, float(end_s) - float(start_s))
        max_duration = float(self.config.max_cue_duration_s or 0.0)
        if max_duration > 0.0 and duration > max_duration + _DURATION_EPSILON:
            return False
        max_chars_per_second = float(self.config.max_chars_per_second or 0.0)
        if max_chars_per_second > 0.0 and duration > 0.0:
            if len(str(text or '')) / duration > max_chars_per_second:
                return False
        return True

    def render_srt(self, cues: Sequence[Any]) -> Optional[str]:
        lines: List[str] = []
        normalized_cues = self._coerce_cue_dicts(cues)
        normalized_cues = [cue for cue in normalized_cues if str(cue.get('text') or '').strip()]
        if not normalized_cues:
            return None
        for idx, cue in enumerate(normalized_cues, start=1):
            lines.append(str(idx))
            lines.append(f"{self._format_timestamp(cue['start'])} --> {self._format_timestamp(cue['end'])}")
            lines.append(self._render_cue_text(str(cue.get('text') or '').strip()))
            lines.append('')
        return '\n'.join(lines).strip() + '\n'

    def _render_cue_text(self, text: str) -> str:
        """渲染单条 cue 的文本。

        已含换行的文本**原样保留**：调用方（例如烧录前的流媒体 SRT 生成）
        已经按真实视频尺寸做过分行与字号安全判断，此处再重排会把它精心折好的
        行合并回一行，导致成片字幕超宽。只有单行文本才交给 ``wrap_text`` 折行 ——
        这正是 ASR 直接落盘 SRT 的场景。
        """
        if '\n' in text:
            return text
        return self.wrap_text(text)

    def _coerce_cue_dicts(self, cues: Sequence[Any]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for cue in cues or []:
            if isinstance(cue, dict):
                normalized.append({
                    'start': float(cue.get('start', cue.get('start_s', 0.0)) or 0.0),
                    'end': float(cue.get('end', cue.get('end_s', 0.0)) or 0.0),
                    'text': str(cue.get('text') or ''),
                    'provider': cue.get('provider', ''),
                    'timing_source': cue.get('timing_source', 'segment'),
                    'alignment_confidence': float(cue.get('alignment_confidence', 0.0) or 0.0),
                    # 跨窗去重依赖窗口索引，coerce 时必须保留，否则后续阶段丢失来源信息。
                    'source_window_index': self._window_index_of(cue),
                    'metadata': dict(cue.get('metadata') or {}),
                })
            elif isinstance(cue, AlignedSubtitleCue):
                normalized.append(cue.to_dict())
            else:
                normalized.append({
                    'start': float(getattr(cue, 'start_s', 0.0) or 0.0),
                    'end': float(getattr(cue, 'end_s', 0.0) or 0.0),
                    'text': str(getattr(cue, 'text', '') or ''),
                    'provider': getattr(cue, 'provider', ''),
                    'timing_source': getattr(cue, 'timing_source', 'segment'),
                    'alignment_confidence': float(getattr(cue, 'alignment_confidence', 0.0) or 0.0),
                    'source_window_index': self._window_index_of(cue),
                    'metadata': dict(getattr(cue, 'metadata', {}) or {}),
                })
        return normalized

    @staticmethod
    def _srt_time_to_seconds(time_str: str):
        """时间戳 → 秒；**畸形时间戳返回 ``None``**（调用方负责跳过并告警）。

        此前畸形输入被静默退化为 ``0.0``，于是「畸形起点」被搬到视频开头：
        既让时间轴覆盖率虚高（覆盖率是质检放行的依据之一），又让一切内容都
        挤在 0.0 附近，掩盖了真正的时间戳崩坏。返回 ``None`` 让调用方显式
        处理，避免用默认值伪造一个看似合法的时间点。
        """
        if not time_str:
            return None
        try:
            normalized = time_str.strip().replace('.', ',')
            hh, mm, rest = normalized.split(':')
            sec, ms = rest.split(',')
            return int(hh) * 3600 + int(mm) * 60 + int(sec) + int(ms) / 1000.0
        except Exception:
            return None

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        try:
            total_ms = int(round(float(seconds or 0.0) * 1000))
            hours = total_ms // 3600000
            total_ms %= 3600000
            minutes = total_ms // 60000
            total_ms %= 60000
            secs = total_ms // 1000
            millis = total_ms % 1000
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
        except Exception:
            return '00:00:00,000'

    @staticmethod
    def count_cues(file_path: str) -> Optional[int]:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as file_obj:
                content = file_obj.read()
            blocks = _BLOCK_SPLIT_RE.split(content.strip())
            return sum(1 for block in blocks if '-->' in block)
        except Exception:
            return None
