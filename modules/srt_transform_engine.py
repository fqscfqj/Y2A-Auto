#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
SRT Transformation Engine – Global Timestamp Calibration & Subtitle Cleanup.

Responsibilities:
  1. Parse SRT text (relative timestamps) into structured cues.
  2. Calibrate cues to the global timeline:
       ``Global_Timestamp = Segment_Start_Offset + ASR_Relative_Timestamp``
  3. Clean hallucinations (repetitive / nonsensical filler in padding regions).
  4. Resolve timing overlaps between adjacent segments.
  5. Normalise text (punctuation, filler words, long-line splitting).
  6. Render the final unified SRT output.
"""

import re
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Pre-compiled patterns
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r'\s+')
_BLOCK_SPLIT_RE = re.compile(r'\n\s*\n')
_PUNCTUATION_SPACE_RE = re.compile(r'([.!?,:;])(?=\S)')
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

# Hallucination: same short phrase repeated 3+ times in succession
_HALLUCINATION_RE = re.compile(r'(.{2,30}?)(?:\s*\1){2,}', re.IGNORECASE)

# Timing constants (seconds)
_MIN_GAP_S = 0.01          # Minimum gap between adjacent cues
_MIN_VISIBLE_DUR_S = 0.05  # Minimum duration for a cue to be visible
_INVALID_TS_FALLBACK_S = 0.5  # Fallback duration when parsed end <= start


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SrtTransformConfig:
    """Configuration for the SRT transformation engine."""
    # Text normalisation
    max_line_length: int = 42
    max_lines: int = 2
    normalize_punctuation: bool = True
    filter_filler_words: bool = True

    # Timing post-processing
    time_offset_s: float = 0.0       # Global shift applied to all cues
    min_cue_duration_s: float = 0.6
    merge_gap_s: float = 0.3
    min_text_length: int = 2


# ---------------------------------------------------------------------------
# SrtTransformEngine
# ---------------------------------------------------------------------------

class SrtTransformEngine:
    """Parses, calibrates, cleans, and renders SRT subtitles."""

    def __init__(
        self,
        config: SrtTransformConfig,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    # ==================================================================
    # 1. SRT Parsing
    # ==================================================================

    def parse_srt(self, srt_text: str, base_offset_s: float = 0.0) -> List[Dict[str, Any]]:
        """Parse SRT text into cue dicts with global timestamps.

        Each cue: ``{'start': float, 'end': float, 'text': str}``

        The formula ``Global = base_offset_s + relative_timestamp`` is applied
        to every parsed timestamp.
        """
        if not srt_text or not srt_text.strip():
            return []

        text = srt_text.strip()
        # Remove BOM if present
        if text.startswith('\ufeff'):
            text = text[1:]
        # Handle WEBVTT header gracefully
        if text.upper().startswith('WEBVTT'):
            lines = text.splitlines()
            idx = 1
            while idx < len(lines) and lines[idx].strip():
                idx += 1
            text = '\n'.join(lines[idx:]).strip()

        blocks = _BLOCK_SPLIT_RE.split(text)
        cues: List[Dict[str, Any]] = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            if len(lines) < 2:
                continue

            # Handle optional index line
            if '-->' not in lines[0] and len(lines) >= 2 and '-->' in lines[1]:
                time_line = lines[1]
                content_lines = lines[2:]
            else:
                time_line = lines[0]
                content_lines = lines[1:]
            if '-->' not in time_line:
                continue
            try:
                start_str, end_str = [p.strip() for p in time_line.split('-->')]
            except ValueError:
                continue

            start_s = self._srt_time_to_seconds(start_str) + base_offset_s
            end_s = self._srt_time_to_seconds(end_str) + base_offset_s
            if end_s <= start_s:
                end_s = start_s + _INVALID_TS_FALLBACK_S

            content = '\n'.join(l.strip() for l in content_lines if l.strip())
            if not content:
                continue

            cues.append({
                'start': max(0.0, start_s),
                'end': max(end_s, start_s + _MIN_VISIBLE_DUR_S),
                'text': content,
            })
        return cues

    # ==================================================================
    # 2. Global Timestamp Calibration  (batch helper)
    # ==================================================================

    def calibrate_segments(
        self,
        segment_results: List[tuple],
    ) -> List[Dict[str, Any]]:
        """Merge ASR results from multiple segments into one global cue list.

        Args:
            segment_results: ``[(segment_offset_s, srt_text_or_None), …]``

        Returns:
            Unified, sorted cue list with global timestamps.
        """
        all_cues: List[Dict[str, Any]] = []
        for offset, srt_text in segment_results:
            if not srt_text:
                continue
            cues = self.parse_srt(srt_text, base_offset_s=offset)
            all_cues.extend(cues)
        # Sort by start time
        all_cues.sort(key=lambda c: (c['start'], c['end']))
        return all_cues

    # ==================================================================
    # 3. Hallucination Cleaning
    # ==================================================================

    def clean_hallucinations(self, cues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove or repair cues that look like ASR hallucinations.

        Detected patterns:
          - Short phrase repeated 3+ times within a single cue.
          - Exact duplicate cues (same text within a close time window).
        """
        if not cues:
            return []

        cleaned: List[Dict[str, Any]] = []
        seen_texts: Dict[str, float] = {}  # text -> last end_time

        for cue in cues:
            text = cue.get('text', '').strip()
            if not text:
                continue

            # a) Internal repetition – collapse to single occurrence
            collapsed = _HALLUCINATION_RE.sub(r'\1', text).strip()
            if collapsed != text:
                self.logger.debug(f"Hallucination collapsed: '{text[:60]}' → '{collapsed[:60]}'")
                text = collapsed
                if not text:
                    continue

            # b) Exact duplicate within 5 s window
            key = _WHITESPACE_RE.sub(' ', text.lower()).strip()
            prev_end = seen_texts.get(key)
            if prev_end is not None and abs(cue['start'] - prev_end) < 5.0:
                self.logger.debug(f"Duplicate cue removed: '{text[:40]}'")
                continue

            seen_texts[key] = cue['end']
            cue['text'] = text
            cleaned.append(cue)

        return cleaned

    # ==================================================================
    # 4. Timeline Overlap Resolution
    # ==================================================================

    def resolve_overlaps(
        self, cues: List[Dict[str, Any]], total_duration_s: float = 0.0
    ) -> List[Dict[str, Any]]:
        """Resolve timing overlaps between adjacent cues.

        Strategy: if cue[i].end > cue[i+1].start, set cue[i].end to
        cue[i+1].start (trim the earlier cue).
        """
        if not cues:
            return []

        cues = sorted(cues, key=lambda c: (c['start'], c['end']))
        for i in range(len(cues) - 1):
            if cues[i]['end'] > cues[i + 1]['start']:
                cues[i]['end'] = cues[i + 1]['start']
            # Ensure minimum gap
            if cues[i]['end'] <= cues[i]['start']:
                cues[i]['end'] = cues[i]['start'] + _MIN_VISIBLE_DUR_S

        # Clamp to total duration if known
        if total_duration_s > 0:
            for c in cues:
                c['start'] = min(c['start'], total_duration_s)
                c['end'] = min(c['end'], total_duration_s)

        return cues

    # ==================================================================
    # 5. Text Normalisation & Splitting
    # ==================================================================

    def normalize_text(self, text: str) -> str:
        """Clean up subtitle text (whitespace, punctuation, fillers)."""
        if not text:
            return ''

        text = _WHITESPACE_RE.sub(' ', text).strip()

        if self.config.normalize_punctuation:
            text = _PUNCTUATION_SPACE_RE.sub(r'\1 ', text)
            text = _WHITESPACE_RE.sub(' ', text).strip()

        if self.config.filter_filler_words:
            for pat in _FILLER_PATTERNS:
                text = pat.sub('', text)
            text = _REPEATED_WORD_RE.sub(r'\1', text)
            text = _WHITESPACE_RE.sub(' ', text).strip()

        return text

    def split_long_cue(self, cue: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Split a cue whose text exceeds line-length limits."""
        text = cue.get('text', '')
        if not text:
            return [cue]

        max_line = self.config.max_line_length
        max_lines = self.config.max_lines
        max_total = max_line * max_lines

        if len(text) <= max_total:
            return [cue]

        # Split into sentences/phrases
        sentences = _SENTENCE_SPLIT_RE.split(text)
        sentences = [s for s in sentences if s.strip()]

        # Re-join sentences with their trailing punctuation
        joined: List[str] = []
        i = 0
        while i < len(sentences):
            if i + 1 < len(sentences) and _SENTENCE_PUNCT_RE.match(sentences[i + 1]):
                joined.append(sentences[i] + sentences[i + 1])
                i += 2
            else:
                joined.append(sentences[i])
                i += 1
        sentences = joined

        result: List[Dict[str, Any]] = []
        current_text = ''
        start_time = cue['start']
        total_chars = len(text)
        duration = cue['end'] - cue['start']

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            test = (current_text + ' ' + sentence).strip() if current_text else sentence
            if len(test) > max_total and current_text:
                chars_in = len(current_text)
                frac = chars_in / total_chars if total_chars > 0 else 0
                cue_dur = max(duration * frac, 0.5)
                cue_dur = min(cue_dur, cue['end'] - start_time)
                result.append({
                    'start': start_time,
                    'end': start_time + cue_dur,
                    'text': current_text,
                })
                start_time += cue_dur
                total_chars -= chars_in
                duration -= cue_dur
                current_text = sentence
            else:
                current_text = test

        if current_text:
            result.append({
                'start': start_time,
                'end': cue['end'],
                'text': current_text,
            })

        return result if result else [cue]

    def apply_text_processing(self, cues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalise text and split long cues for a list of cues."""
        processed: List[Dict[str, Any]] = []
        for cue in cues:
            cue['text'] = self.normalize_text(cue['text'])
            if not cue['text']:
                continue
            processed.extend(self.split_long_cue(cue))
        return processed

    # ==================================================================
    # 6. Final Cue Post-Processing (merge short, enforce duration, etc.)
    # ==================================================================

    def finalize_cues(
        self, cues: List[Dict[str, Any]], total_duration_s: float
    ) -> List[Dict[str, Any]]:
        """Final pass: apply offset, merge tiny fragments, enforce min duration."""
        if not cues:
            return []

        offset = self.config.time_offset_s
        merge_gap = max(0.0, self.config.merge_gap_s)
        min_text = max(0, self.config.min_text_length)
        min_dur = max(0.05, self.config.min_cue_duration_s)

        # Sort
        cues = sorted(cues, key=lambda c: float(c.get('start', 0)))

        # Apply offset & clamp
        for c in cues:
            try:
                c['start'] = max(0.0, min(total_duration_s, float(c['start']) + offset))
                c['end'] = max(0.0, min(total_duration_s, float(c['end']) + offset))
                if c['end'] <= c['start']:
                    c['end'] = min(total_duration_s, c['start'] + _MIN_VISIBLE_DUR_S)
            except Exception:
                continue

        # Merge adjacent short/close cues
        merged: List[Dict[str, Any]] = []
        for c in cues:
            if not merged:
                merged.append(c)
                continue
            prev = merged[-1]
            gap = float(c['start']) - float(prev['end'])
            prev_text = (prev.get('text') or '').strip()
            cur_text = (c.get('text') or '').strip()
            prev_dur = float(prev['end']) - float(prev['start'])
            cur_dur = float(c['end']) - float(c['start'])
            combined_dur = float(c['end']) - float(prev['start'])

            need_merge = False
            if gap <= merge_gap:
                if combined_dur < 7.0:
                    need_merge = True
                elif prev_dur < 1.0 or cur_dur < 1.0:
                    need_merge = True
            elif len(prev_text) < min_text or len(cur_text) < min_text:
                if gap <= merge_gap * 2:
                    need_merge = True

            if need_merge:
                prev['text'] = _WHITESPACE_RE.sub(' ', (prev_text + ' ' + cur_text).strip())
                prev['end'] = max(prev['end'], c['end'])
            else:
                merged.append(c)

        # Enforce minimum duration
        finalized: List[Dict[str, Any]] = []
        for i, c in enumerate(merged):
            start = float(c['start'])
            end = float(c['end'])
            dur = end - start
            text_len = len((c.get('text') or '').strip())
            if dur < min_dur:
                next_start = float(merged[i + 1]['start']) if i + 1 < len(merged) else total_duration_s
                gap_to_next = next_start - start
                if gap_to_next > min_dur + _MIN_GAP_S:
                    target_end = start + min_dur
                elif gap_to_next > _MIN_VISIBLE_DUR_S:
                    target_end = next_start - _MIN_GAP_S
                else:
                    target_end = next_start
                if target_end > end:
                    c['end'] = target_end
                else:
                    # Can't extend to minimum duration – merge with adjacent cue
                    if i + 1 < len(merged):
                        merged[i + 1]['start'] = start
                        merged[i + 1]['text'] = (c['text'].strip() + ' ' + merged[i + 1]['text'].strip()).strip()
                        continue
                    elif finalized:
                        finalized[-1]['end'] = max(finalized[-1]['end'], end)
                        finalized[-1]['text'] = (finalized[-1]['text'].strip() + ' ' + c['text'].strip()).strip()
                        continue
            finalized.append(c)

        # Drop ultra-short / invisible fragments
        cleaned: List[Dict[str, Any]] = []
        for c in finalized:
            text = (c.get('text') or '').strip()
            dur = float(c['end']) - float(c['start'])
            if dur < _MIN_VISIBLE_DUR_S:
                self.logger.debug(f"Dropping invisible cue: '{text[:30]}' ({dur:.3f}s)")
                continue
            if len(text) < min_text and dur < min_dur:
                self.logger.debug(f"Dropping ultra-short cue: '{text}' ({dur:.2f}s)")
                continue
            cleaned.append(c)

        return cleaned

    # ==================================================================
    # 7. SRT Rendering
    # ==================================================================

    def render_srt(self, cues: List[Dict[str, Any]]) -> Optional[str]:
        """Render cues as a standard SRT string with sequential indices."""
        cues = [c for c in cues if (c.get('text') or '').strip()]
        if not cues:
            return None

        lines: List[str] = []
        for idx, c in enumerate(cues, start=1):
            lines.append(str(idx))
            lines.append(
                f"{self._format_timestamp(c['start'])} --> {self._format_timestamp(c['end'])}"
            )
            lines.append((c['text'] or '').strip())
            lines.append('')
        return '\n'.join(lines).strip() + '\n'

    # ==================================================================
    # Utility helpers
    # ==================================================================

    @staticmethod
    def _srt_time_to_seconds(time_str: str) -> float:
        """Convert ``HH:MM:SS,mmm`` (or ``.mmm``) to seconds."""
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
        """Convert seconds to ``HH:MM:SS,mmm``."""
        if seconds is None:
            seconds = 0.0
        try:
            ms = int(round(seconds * 1000))
            h = ms // 3600000
            ms %= 3600000
            m = ms // 60000
            ms %= 60000
            s = ms // 1000
            ms %= 1000
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
        except Exception:
            return '00:00:00,000'

    @staticmethod
    def count_cues(file_path: str) -> Optional[int]:
        """Count the number of SRT cues in a file."""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            blocks = _BLOCK_SPLIT_RE.split(content.strip())
            return sum(1 for b in blocks if '-->' in b)
        except Exception:
            return None
