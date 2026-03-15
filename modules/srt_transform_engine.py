#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

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
    re.compile(r'\b(um|uh|er|ah|hmm|like|you know)\b', re.IGNORECASE),
    re.compile(r'[嗯啊呃哦唔]+'),
    re.compile(
        r'\b(doo|da|dee|ch|sh|tickle|scratch|tap|click|pop|mouth|sound|noise|'
        r'chew|eat|drink|slurp|gulp|swallow|breath|whisper|lip|smack|tongue)\b',
        re.IGNORECASE,
    ),
    re.compile(r'\*[^*]*\*', re.IGNORECASE),
    re.compile(r'\[[^\]]*\]', re.IGNORECASE),
    re.compile(r'\([^)]*\)', re.IGNORECASE),
]
_REPEATED_WORD_RE = re.compile(r'\b(\w+)(?:[,\s]+\1\b)+', re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r'([.!?。！？;；,，]+\s*)')
_SENTENCE_PUNCT_RE = re.compile(r'[.!?。！？;；,，]+\s*')
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?")
_CJK_CHAR_RE = re.compile(r'[\u3400-\u9fff]')
_VISIBLE_TEXT_RE = re.compile(r'[\w\u3400-\u9fff]', re.UNICODE)
_HALLUCINATION_RE = re.compile(r'(.{2,30}?)(?:\s*\1){2,}', re.IGNORECASE)

_MIN_GAP_S = 0.01
_MIN_VISIBLE_DUR_S = 0.05
_INVALID_TS_FALLBACK_S = 0.5


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
            start_s = self._srt_time_to_seconds(start_str) + base_offset_s
            end_s = self._srt_time_to_seconds(end_str) + base_offset_s
            if end_s <= start_s:
                end_s = start_s + _INVALID_TS_FALLBACK_S
            content = '\n'.join(line.strip() for line in content_lines if line.strip())
            if not content:
                continue
            cues.append({
                'start': max(0.0, start_s),
                'end': max(end_s, start_s + _MIN_VISIBLE_DUR_S),
                'text': content,
                'timing_source': 'srt',
                'alignment_confidence': 0.45,
                'provider': '',
            })
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
                if self._is_duplicate_cue(stitched[-1], cue):
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

    def _pick_better_duplicate(self, left: AlignedSubtitleCue, right: AlignedSubtitleCue) -> AlignedSubtitleCue:
        if right.alignment_confidence > left.alignment_confidence:
            better = right
        elif right.timing_source == 'word' and left.timing_source != 'word':
            better = right
        else:
            better = left
        start_s = min(left.start_s, right.start_s)
        end_s = max(left.end_s, right.end_s)
        return AlignedSubtitleCue(
            start_s=start_s,
            end_s=end_s,
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
        return AlignedSubtitleCue(
            start_s=min(float(left.start_s), float(right.start_s)),
            end_s=max(float(left.end_s), float(right.end_s)),
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
        max_overlap = min(len(left_tokens), len(right_tokens), 8)
        for overlap in range(max_overlap, 0, -1):
            left_tail = ' '.join(left_tokens[-overlap:])
            right_head = ' '.join(right_tokens[:overlap])
            if self._normalize_compare_text(left_tail) == self._normalize_compare_text(right_head):
                suffix = ' '.join(right_tokens[overlap:]).strip()
                return (left_text + (' ' + suffix if suffix else '')).strip()
        return ''

    def clean_hallucinations(self, cues: Sequence[Any]) -> List[Dict[str, Any]]:
        normalized_cues = self._coerce_cue_dicts(cues)
        cleaned: List[Dict[str, Any]] = []
        seen_texts: Dict[str, float] = {}
        for cue in normalized_cues:
            text = str(cue.get('text') or '').strip()
            if not text:
                continue
            duration = max(float(cue.get('end', 0.0)) - float(cue.get('start', 0.0)), 0.0)
            if self._is_implausibly_dense_cue(text, duration):
                continue
            collapsed = _HALLUCINATION_RE.sub(r'\1', text).strip()
            if not collapsed:
                continue
            dedupe_key = _WHITESPACE_RE.sub(' ', collapsed.lower()).strip()
            prev_end = seen_texts.get(dedupe_key)
            if prev_end is not None and abs(float(cue.get('start', 0.0)) - prev_end) < 5.0:
                continue
            seen_texts[dedupe_key] = float(cue.get('end', 0.0))
            cue['text'] = collapsed
            cleaned.append(cue)
        return cleaned

    def resolve_overlaps(self, cues: Sequence[Any], total_duration_s: float = 0.0) -> List[Dict[str, Any]]:
        normalized_cues = sorted(self._coerce_cue_dicts(cues), key=lambda cue: (cue['start'], cue['end']))
        if not normalized_cues:
            return []
        for idx in range(len(normalized_cues) - 1):
            current = normalized_cues[idx]
            nxt = normalized_cues[idx + 1]
            if current['end'] > nxt['start']:
                current['end'] = nxt['start']
            if current['end'] <= current['start']:
                room = nxt['start'] - current['start']
                if room > _MIN_VISIBLE_DUR_S:
                    current['end'] = current['start'] + min(_MIN_VISIBLE_DUR_S, room - _MIN_GAP_S)
                else:
                    current['end'] = nxt['start']
        if total_duration_s > 0:
            for cue in normalized_cues:
                cue['start'] = min(cue['start'], total_duration_s)
                cue['end'] = min(cue['end'], total_duration_s)
        return normalized_cues

    def _normalize_text_line(self, text: str) -> str:
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

    def split_long_cue(self, cue: Dict[str, Any]) -> List[Dict[str, Any]]:
        text = cue.get('text', '')
        if not text or not self.config.split_long_cues:
            return [cue]
        max_line = int(self.config.max_line_length)
        max_lines = int(self.config.max_lines)
        max_total = max_line * max_lines
        if len(text) <= max_line or len(text) <= max_total:
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
        total_chars = max(1, len(text))
        duration = cue['end'] - cue['start']
        for sentence in joined:
            sentence = sentence.strip()
            if not sentence:
                continue
            test = (current_text + ' ' + sentence).strip() if current_text else sentence
            if len(test) > max_total and current_text:
                chars_in = len(current_text)
                frac = chars_in / total_chars
                cue_duration = max(duration * frac, 0.5)
                cue_duration = min(cue_duration, cue['end'] - start_time)
                result.append({'start': start_time, 'end': start_time + cue_duration, 'text': current_text})
                start_time += cue_duration
                total_chars -= chars_in
                duration -= cue_duration
                current_text = sentence
            else:
                current_text = test
        if current_text:
            result.append({'start': start_time, 'end': cue['end'], 'text': current_text})
        return result or [cue]

    def apply_text_processing(self, cues: Sequence[Any]) -> List[Dict[str, Any]]:
        processed: List[Dict[str, Any]] = []
        for cue in self._coerce_cue_dicts(cues):
            cue['text'] = self.normalize_text(cue['text'])
            if not cue['text']:
                continue
            processed.extend(self.split_long_cue(cue))
        return processed

    def finalize_cues(self, cues: Sequence[Any], total_duration_s: float) -> List[Dict[str, Any]]:
        normalized_cues = sorted(self._coerce_cue_dicts(cues), key=lambda cue: float(cue.get('start', 0.0)))
        if not normalized_cues:
            return []
        offset = float(self.config.time_offset_s or 0.0)
        merge_gap = max(0.0, float(self.config.merge_gap_s or 0.0))
        min_text = max(0, int(self.config.min_text_length or 0))
        min_dur = max(0.05, float(self.config.min_cue_duration_s or 0.05))

        for cue in normalized_cues:
            cue['start'] = max(0.0, min(total_duration_s, float(cue['start']) + offset))
            cue['end'] = max(0.0, min(total_duration_s, float(cue['end']) + offset))
            if cue['end'] <= cue['start']:
                cue['end'] = min(total_duration_s, cue['start'] + _MIN_VISIBLE_DUR_S)

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
            prev_dur = float(prev['end']) - float(prev['start'])
            cur_dur = float(cue['end']) - float(cue['start'])
            should_merge = False
            if gap <= merge_gap:
                continuity = self._merge_text_with_overlap(prev_text, cur_text)
                if continuity:
                    should_merge = True
                elif gap < 0.0 or prev_dur < 0.9 or cur_dur < 0.9:
                    should_merge = True
                elif len(prev_text) < min_text or len(cur_text) < min_text:
                    should_merge = True
            if should_merge and max_merge_chars > 0 and len(prev_text) + 1 + len(cur_text) > max_merge_chars:
                should_merge = False
            if should_merge:
                merged_text = self._merge_text_with_overlap(prev_text, cur_text) or (prev_text + ' ' + cur_text).strip()
                prev['text'] = _WHITESPACE_RE.sub(' ', merged_text).strip()
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
                    cue['end'] = start + min_dur
                elif gap_to_next > _MIN_VISIBLE_DUR_S:
                    cue['end'] = next_start - _MIN_GAP_S
                elif idx + 1 < len(merged):
                    merged[idx + 1]['start'] = start
                    merged[idx + 1]['text'] = (str(cue['text']).strip() + ' ' + str(merged[idx + 1]['text']).strip()).strip()
                    continue
                elif finalized:
                    finalized[-1]['end'] = max(float(finalized[-1]['end']), end)
                    finalized[-1]['text'] = (str(finalized[-1]['text']).strip() + ' ' + str(cue['text']).strip()).strip()
                    continue
            finalized.append(cue)

        cleaned: List[Dict[str, Any]] = []
        for cue in finalized:
            text = str(cue.get('text') or '').strip()
            dur = float(cue['end']) - float(cue['start'])
            if dur < _MIN_VISIBLE_DUR_S:
                continue
            if len(text) < min_text and dur < min_dur:
                continue
            cleaned.append(cue)
        return cleaned

    def render_srt(self, cues: Sequence[Any]) -> Optional[str]:
        lines: List[str] = []
        normalized_cues = self._coerce_cue_dicts(cues)
        normalized_cues = [cue for cue in normalized_cues if str(cue.get('text') or '').strip()]
        if not normalized_cues:
            return None
        for idx, cue in enumerate(normalized_cues, start=1):
            lines.append(str(idx))
            lines.append(f"{self._format_timestamp(cue['start'])} --> {self._format_timestamp(cue['end'])}")
            lines.append(str(cue.get('text') or '').strip())
            lines.append('')
        return '\n'.join(lines).strip() + '\n'

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
                    'metadata': dict(getattr(cue, 'metadata', {}) or {}),
                })
        return normalized

    @staticmethod
    def _srt_time_to_seconds(time_str: str) -> float:
        if not time_str:
            return 0.0
        try:
            normalized = time_str.strip().replace('.', ',')
            hh, mm, rest = normalized.split(':')
            sec, ms = rest.split(',')
            return int(hh) * 3600 + int(mm) * 60 + int(sec) + int(ms) / 1000.0
        except Exception:
            return 0.0

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
