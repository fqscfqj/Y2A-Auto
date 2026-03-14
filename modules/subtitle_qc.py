#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .utils import extract_chat_message_json, get_chat_message_text

logger = logging.getLogger('subtitle_qc')
SHORT_LINE_NORMALIZED_LEN = 8
MAX_REPEATED_SAMPLE_PER_TEXT = 3
TOP_REPEATED_TEXT_LIMIT = 5
HIGH_CONFIDENCE_RULE_SCORE_THRESHOLD = 0.85
ADVISORY_MODE_HARD_FAIL_REASONS = {
    'hallucination_meta',
    'credit_like_phrase',
    'noise_command_phrase',
    'template_like_phrase',
}


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


@dataclass
class SubtitleQCResult:
    passed: bool
    score: float
    reason: str
    rule_score: float
    ai_score: Optional[float] = None
    raw_ai: Optional[Dict[str, Any]] = None
    decision: str = ''
    sample_items: int = 0
    sample_chars: int = 0


@dataclass
class RuleCheckResult:
    decision: str
    score: float
    reason: str
    metrics: Dict[str, Any]
    boundary_level: str = 'boundary'


@dataclass
class QCSubtitleItem:
    start_time: str
    end_time: str
    source_text: str


_PLACEHOLDER_RE = re.compile(r'^[\s\.,，。．…\-—_·•]+$')
_NON_CONTENT_RE = re.compile(r'[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+', re.UNICODE)
_REASON_TOKEN_RE = re.compile(r'[^a-z0-9]+')
_SRT_TIMESTAMP_RE = re.compile(
    r'^\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*$'
)
_WORD_RE = re.compile(r"[a-z0-9']+")
_CREDIT_PATTERNS = [
    re.compile(r'\b(?:transcription|transcribed|subtitled|subtitle|captioned|captions?)\s+by\b', re.IGNORECASE),
    re.compile(r'\bcastingwords\b', re.IGNORECASE),
]
_NOISE_COMMAND_PATTERNS = [
    re.compile(r'^\s*ignore noise[.!]?\s*$', re.IGNORECASE),
    re.compile(r'^\s*click[.!]?\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:tap|beep|mouse click|keyboard click|background noise|noise only)[.!]?\s*$', re.IGNORECASE),
]


def _normalize_line(text: str) -> str:
    t = (text or '').strip().lower()
    if not t:
        return ''
    t = _NON_CONTENT_RE.sub('', t)
    return t


def normalize_qc_reason_token(reason: str, default: str = 'unknown') -> str:
    token = _REASON_TOKEN_RE.sub('_', str(reason or '').strip().lower()).strip('_')
    return token or default


def _is_low_content(text: str) -> bool:
    t = (text or '').strip()
    if not t:
        return True
    if _PLACEHOLDER_RE.match(t):
        return True
    normalized = _normalize_line(t)
    return len(normalized) < 2


def _is_short_content_line(normalized: str) -> bool:
    return 0 < len(normalized) <= SHORT_LINE_NORMALIZED_LEN


def _parse_srt_timestamp_seconds(value: str) -> Optional[float]:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        hh, mm, rest = raw.replace('.', ',').split(':')
        ss, ms = rest.split(',')
        return (int(hh) * 3600) + (int(mm) * 60) + int(ss) + (int(ms) / 1000.0)
    except Exception:
        return None


def _read_srt_items(srt_path: str) -> List[QCSubtitleItem]:
    text = Path(srt_path).read_text(encoding='utf-8', errors='replace')
    blocks = [block.strip() for block in re.split(r'\r?\n\r?\n', text) if block.strip()]
    items: List[QCSubtitleItem] = []

    for block in blocks:
        lines = [line.strip('\ufeff').strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue

        timestamp_line_index = 1 if len(lines) >= 2 and _SRT_TIMESTAMP_RE.match(lines[1]) else 0
        if timestamp_line_index >= len(lines):
            continue

        match = _SRT_TIMESTAMP_RE.match(lines[timestamp_line_index])
        if not match:
            continue

        text_lines = lines[timestamp_line_index + 1:]
        if not text_lines:
            continue

        items.append(
            QCSubtitleItem(
                start_time=match.group(1).replace('.', ','),
                end_time=match.group(2).replace('.', ','),
                source_text=' '.join(text_lines).strip(),
            )
        )

    return items


def _looks_like_repeated_clause(text: str) -> bool:
    words = _WORD_RE.findall((text or '').lower())
    if len(words) < 6 or len(words) % 2 != 0:
        return False
    half = len(words) // 2
    return words[:half] == words[half:]


def _classify_suspicious_text(text: str, normalized: str) -> Optional[str]:
    raw = (text or '').strip()
    if not raw:
        return None

    for pattern in _CREDIT_PATTERNS:
        if pattern.search(raw):
            return 'credit_like_phrase'

    for pattern in _NOISE_COMMAND_PATTERNS:
        if pattern.search(raw):
            return 'noise_command_phrase'

    if _looks_like_repeated_clause(raw):
        return 'template_like_phrase'

    if normalized and normalized in {'ignorenoise', 'click'}:
        return 'noise_command_phrase'

    return None


def _build_openai_client(api_key: str, base_url: str):
    import openai

    options: Dict[str, Any] = {}
    if base_url:
        options['base_url'] = base_url
    return openai.OpenAI(api_key=api_key, **options)


def _build_item_stats(items: List[Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    stats: List[Dict[str, Any]] = []
    usable_normalized: List[str] = []
    suspicious_examples: List[str] = []
    suspicious_counts = Counter()
    normalized_examples: Dict[str, str] = {}
    total_text_chars = 0
    short_line_count = 0
    max_repeat_run = 0
    current_repeat_run = 0
    previous_normalized = ''
    earliest_start: Optional[float] = None
    latest_end: Optional[float] = None

    for idx, it in enumerate(items):
        text = (getattr(it, 'source_text', '') or '').strip()
        normalized = _normalize_line(text) if text else ''
        low_content = _is_low_content(text) if text else True
        suspicious_kind = _classify_suspicious_text(text, normalized)
        start_seconds = _parse_srt_timestamp_seconds(getattr(it, 'start_time', ''))
        end_seconds = _parse_srt_timestamp_seconds(getattr(it, 'end_time', ''))
        if text and not low_content and normalized:
            usable_normalized.append(normalized)
            normalized_examples.setdefault(normalized, text[:120])
            total_text_chars += len(normalized)
            if _is_short_content_line(normalized):
                short_line_count += 1
            if normalized == previous_normalized:
                current_repeat_run += 1
            else:
                current_repeat_run = 1
                previous_normalized = normalized
            max_repeat_run = max(max_repeat_run, current_repeat_run)
        else:
            current_repeat_run = 0
            previous_normalized = ''
        if suspicious_kind:
            suspicious_counts[suspicious_kind] += 1
            if len(suspicious_examples) < 6:
                suspicious_examples.append(text[:120])
        if start_seconds is not None:
            earliest_start = start_seconds if earliest_start is None else min(earliest_start, start_seconds)
        if end_seconds is not None:
            latest_end = end_seconds if latest_end is None else max(latest_end, end_seconds)
        stats.append({
            'index': idx,
            'item': it,
            'text': text,
            'normalized': normalized,
            'low_content': low_content,
            'suspicious_kind': suspicious_kind,
        })

    freq = Counter(usable_normalized)
    non_empty_count = sum(1 for stat in stats if stat['text'])
    low_content_count = sum(1 for stat in stats if stat['text'] and stat['low_content'])
    usable_count = sum(1 for stat in stats if stat['text'] and not stat['low_content'] and stat['normalized'])

    for stat in stats:
        normalized = stat['normalized']
        stat['frequency'] = freq.get(normalized, 0) if normalized else 0

    top_frequency = max(freq.values()) if freq else 0
    top_ratio = (top_frequency / usable_count) if usable_count else 1.0
    unique_ratio = (len(freq) / usable_count) if usable_count else 0.0
    repeat_mass_ratio = (
        sum(count for count in freq.values() if count >= 2) / usable_count
        if usable_count
        else 1.0
    )
    avg_len = (
        sum(len(normalized) for normalized in usable_normalized) / usable_count
        if usable_count
        else 0.0
    )
    credit_like_count = int(suspicious_counts.get('credit_like_phrase', 0))
    noise_command_count = int(suspicious_counts.get('noise_command_phrase', 0))
    template_like_count = int(suspicious_counts.get('template_like_phrase', 0))
    suspicious_phrase_count = credit_like_count + noise_command_count + template_like_count
    timeline_span_seconds = 0.0
    if earliest_start is not None and latest_end is not None and latest_end >= earliest_start:
        timeline_span_seconds = max(0.0, latest_end - earliest_start)
    chars_per_minute = (
        total_text_chars / max(timeline_span_seconds / 60.0, 1e-6)
        if timeline_span_seconds > 0
        else float(total_text_chars)
    )
    top_repeated_texts = [
        {
            'text': normalized_examples.get(normalized, normalized)[:120],
            'count': int(count),
        }
        for normalized, count in freq.most_common(TOP_REPEATED_TEXT_LIMIT)
        if count >= 2
    ]

    metrics = {
        'total_items': len(items),
        'non_empty_count': non_empty_count,
        'usable_count': usable_count,
        'low_content_count': low_content_count,
        'low_content_ratio': (low_content_count / max(1, non_empty_count)) if non_empty_count else 1.0,
        'top_frequency': top_frequency,
        'top_ratio': top_ratio,
        'unique_ratio': unique_ratio,
        'repeat_mass_ratio': repeat_mass_ratio,
        'avg_len': avg_len,
        'total_text_chars': total_text_chars,
        'short_line_count': short_line_count,
        'short_line_ratio': (short_line_count / max(1, usable_count)) if usable_count else 0.0,
        'max_repeat_run': max_repeat_run,
        'timeline_span_seconds': timeline_span_seconds,
        'chars_per_minute': chars_per_minute,
        'top_repeated_texts': top_repeated_texts,
        'credit_like_count': credit_like_count,
        'noise_command_count': noise_command_count,
        'template_like_count': template_like_count,
        'suspicious_phrase_count': suspicious_phrase_count,
        'suspicious_phrase_ratio': (
            suspicious_phrase_count / max(1, non_empty_count)
            if non_empty_count
            else 0.0
        ),
        'suspicious_examples': suspicious_examples,
    }
    return stats, metrics


def _estimate_rule_score(metrics: Dict[str, Any]) -> float:
    usable_count = int(metrics.get('usable_count', 0) or 0)
    low_content_ratio = float(metrics.get('low_content_ratio', 1.0) or 0.0)
    top_ratio = float(metrics.get('top_ratio', 1.0) or 0.0)
    unique_ratio = float(metrics.get('unique_ratio', 0.0) or 0.0)
    repeat_mass_ratio = float(metrics.get('repeat_mass_ratio', 1.0) or 0.0)
    avg_len = float(metrics.get('avg_len', 0.0) or 0.0)
    short_line_ratio = float(metrics.get('short_line_ratio', 0.0) or 0.0)
    max_repeat_run = int(metrics.get('max_repeat_run', 0) or 0)
    chars_per_minute = float(metrics.get('chars_per_minute', 0.0) or 0.0)
    suspicious_phrase_ratio = float(metrics.get('suspicious_phrase_ratio', 0.0) or 0.0)
    credit_like_count = int(metrics.get('credit_like_count', 0) or 0)
    noise_command_count = int(metrics.get('noise_command_count', 0) or 0)
    template_like_count = int(metrics.get('template_like_count', 0) or 0)

    score = 1.0
    if usable_count < 8:
        score -= min(0.25, (8 - usable_count) * 0.04)
    score -= min(0.35, max(0.0, low_content_ratio - 0.25) * 0.70)
    score -= min(0.35, max(0.0, top_ratio - 0.30) * 0.80)
    score -= min(0.25, max(0.0, repeat_mass_ratio - 0.35) * 0.75)
    score -= min(0.25, max(0.0, 0.55 - unique_ratio) * 0.70)
    score -= min(0.20, max(0.0, 3.0 - avg_len) * 0.10)
    score -= min(0.22, max(0.0, short_line_ratio - 0.45) * 0.40)
    score -= min(0.16, max(0, max_repeat_run - 2) * 0.08)
    if chars_per_minute > 0:
        score -= min(0.18, max(0.0, 35.0 - chars_per_minute) * 0.006)
    score -= min(0.35, suspicious_phrase_ratio * 1.20)
    score -= min(0.30, credit_like_count * 0.20)
    score -= min(0.25, noise_command_count * 0.10)
    score -= min(0.20, template_like_count * 0.08)
    return max(0.0, min(1.0, score))


def _rule_check(items: List[Any]) -> RuleCheckResult:
    item_stats, metrics = _build_item_stats(items)
    metrics['checked_by'] = 'rule'
    metrics['boundary_level'] = 'boundary'
    metrics['rule_score'] = _estimate_rule_score(metrics)
    metrics['item_stats'] = item_stats

    non_empty_count = int(metrics['non_empty_count'])
    usable_count = int(metrics['usable_count'])
    low_content_ratio = float(metrics['low_content_ratio'])
    top_frequency = int(metrics.get('top_frequency', 0) or 0)
    top_ratio = float(metrics['top_ratio'])
    unique_ratio = float(metrics['unique_ratio'])
    repeat_mass_ratio = float(metrics['repeat_mass_ratio'])
    avg_len = float(metrics['avg_len'])
    total_text_chars = int(metrics.get('total_text_chars', 0) or 0)
    short_line_ratio = float(metrics.get('short_line_ratio', 0.0) or 0.0)
    max_repeat_run = int(metrics.get('max_repeat_run', 0) or 0)
    chars_per_minute = float(metrics.get('chars_per_minute', 0.0) or 0.0)
    credit_like_count = int(metrics['credit_like_count'])
    noise_command_count = int(metrics['noise_command_count'])
    template_like_count = int(metrics['template_like_count'])
    suspicious_phrase_count = int(metrics['suspicious_phrase_count'])
    suspicious_phrase_ratio = float(metrics['suspicious_phrase_ratio'])
    rule_score = float(metrics['rule_score'])

    if non_empty_count == 0 or usable_count < 3:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:empty_or_too_short',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if low_content_ratio >= 0.85:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:mostly_low_content',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if usable_count <= 4 and top_frequency >= 3 and unique_ratio <= 0.50:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:ultra_short_repeat',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if (
        usable_count <= 5
        and total_text_chars <= 40
        and short_line_ratio >= 0.80
        and repeat_mass_ratio >= 0.60
    ):
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:ultra_short_low_info',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if credit_like_count >= 1:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:credit_like_phrase',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if noise_command_count >= 2:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:noise_command_phrase',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if (
        suspicious_phrase_count >= 2
        and (top_ratio >= 0.30 or repeat_mass_ratio >= 0.45)
    ):
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:suspicious_repeat_mass',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if usable_count >= 12 and top_ratio >= 0.75:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:extreme_repetition',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if usable_count >= 20 and unique_ratio <= 0.15:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:very_low_variety',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if usable_count >= 12 and avg_len < 1.8:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason='rule_fail:too_short',
            metrics=metrics,
            boundary_level='suspicious',
        )

    if (
        usable_count >= 8
        and low_content_ratio <= 0.25
        and top_ratio <= 0.30
        and unique_ratio >= 0.55
        and avg_len >= 3.0
        and suspicious_phrase_count == 0
    ):
        return RuleCheckResult(
            decision='rule_pass',
            score=rule_score,
            reason='rule_pass:healthy_distribution',
            metrics=metrics,
            boundary_level='boundary',
        )

    boundary_level = 'boundary'
    if (
        usable_count < 6
        or low_content_ratio >= 0.45
        or top_ratio >= 0.50
        or unique_ratio <= 0.35
        or avg_len < 2.4
        or short_line_ratio >= 0.75
        or max_repeat_run >= 3
        or (total_text_chars <= 48 and usable_count <= 6)
        or (chars_per_minute > 0 and chars_per_minute <= 25.0)
        or suspicious_phrase_count > 0
        or template_like_count > 0
        or repeat_mass_ratio >= 0.40
        or suspicious_phrase_ratio >= 0.12
    ):
        boundary_level = 'suspicious'

    metrics['boundary_level'] = boundary_level
    return RuleCheckResult(
        decision='needs_ai',
        score=rule_score,
        reason=f'needs_ai:{boundary_level}',
        metrics=metrics,
        boundary_level=boundary_level,
    )


def _is_high_rule_score_clean_boundary_sample(rule_result: RuleCheckResult) -> bool:
    metrics = rule_result.metrics or {}
    return (
        rule_result.boundary_level == 'boundary'
        and float(rule_result.score) >= HIGH_CONFIDENCE_RULE_SCORE_THRESHOLD
        and int(metrics.get('suspicious_phrase_count', 0) or 0) == 0
        and float(metrics.get('top_ratio', 1.0) or 0.0) <= 0.30
        and float(metrics.get('repeat_mass_ratio', 1.0) or 0.0) <= 0.15
        and int(metrics.get('max_repeat_run', 0) or 0) <= 1
        and float(metrics.get('short_line_ratio', 1.0) or 0.0) <= 0.50
        and int(metrics.get('total_text_chars', 0) or 0) >= 40
    )


def _pick_segment(start: int, end: int, k: int) -> List[int]:
    if k <= 0 or end <= start:
        return []
    length = end - start
    if k >= length:
        return list(range(start, end))
    step = length / k
    result: List[int] = []
    seen = set()
    for i in range(k):
        idx = start + int(i * step)
        idx = min(end - 1, max(start, idx))
        if idx in seen:
            continue
        seen.add(idx)
        result.append(idx)
    return result


def _sample_items(
    items: List[Any],
    item_stats: List[Dict[str, Any]],
    max_items: int,
    max_chars: int,
    boundary_level: str,
) -> Tuple[str, Dict[str, Any]]:
    non_empty_stats = [stat for stat in item_stats if stat['text']]
    if not non_empty_stats:
        return '', {
            'sample_items': 0,
            'sample_chars': 0,
            'sample_limit_items': 0,
            'sample_limit_chars': 0,
            'sample_boundary_level': boundary_level,
        }

    if boundary_level == 'suspicious':
        sample_limit_items = max(1, min(max_items, 60))
        sample_limit_chars = max(1, min(max_chars, 7500))
    else:
        sample_limit_items = max(1, min(max_items, 36))
        sample_limit_chars = max(1, min(max_chars, 4500))

    selected_indices: List[int] = []
    selected_index_set = set()
    selected_key_counts: Counter[str] = Counter()

    def append_index(item_index: int, max_per_key: int = 1):
        if item_index < 0 or item_index >= len(item_stats):
            return
        stat = item_stats[item_index]
        if not stat['text']:
            return
        if item_index in selected_index_set:
            return
        key = stat['normalized'] or stat['text'].strip().lower()
        if selected_key_counts[key] >= max_per_key:
            return
        selected_key_counts[key] += 1
        selected_index_set.add(item_index)
        selected_indices.append(item_index)

    suspicious_candidates = [
        stat['index'] for stat in non_empty_stats if stat.get('suspicious_kind')
    ]
    for idx in suspicious_candidates:
        append_index(idx, max_per_key=MAX_REPEATED_SAMPLE_PER_TEXT)
        if len(selected_indices) >= sample_limit_items:
            break

    low_content_candidates = [stat['index'] for stat in non_empty_stats if stat['low_content']]
    for idx in low_content_candidates:
        append_index(idx, max_per_key=MAX_REPEATED_SAMPLE_PER_TEXT)
        if len(selected_indices) >= sample_limit_items:
            break

    repeated_candidates = sorted(
        (
            stat for stat in non_empty_stats
            if stat['frequency'] >= 2 and stat['normalized']
        ),
        key=lambda stat: (-stat['frequency'], stat['index'])
    )
    for stat in repeated_candidates:
        append_index(stat['index'], max_per_key=MAX_REPEATED_SAMPLE_PER_TEXT)
        if len(selected_indices) >= sample_limit_items:
            break

    ordered_indices = [stat['index'] for stat in non_empty_stats]
    n = len(ordered_indices)
    head_count = max(1, int(math.ceil(n * 0.2)))
    tail_count = max(1, int(math.ceil(n * 0.2)))
    head_indices = _pick_segment(0, min(n, head_count), head_count)
    tail_start = max(0, n - tail_count)
    tail_indices = _pick_segment(tail_start, n, tail_count)
    remaining = max(0, sample_limit_items - len(selected_indices))
    middle_budget = max(0, remaining - len(head_indices) - len(tail_indices))
    middle_indices = _pick_segment(len(head_indices), tail_start, middle_budget)

    for relative_index in head_indices + middle_indices + tail_indices:
        if relative_index < 0 or relative_index >= n:
            continue
        append_index(ordered_indices[relative_index])
        if len(selected_indices) >= sample_limit_items:
            break

    selected_indices = sorted(selected_indices)
    rendered_lines: List[str] = []
    total_chars = 0
    actual_count = 0
    for idx in selected_indices:
        stat = item_stats[idx]
        it = stat['item']
        try:
            time_range = f"{it.start_time} --> {it.end_time}"
            text = stat['text']
        except Exception:
            time_range = ''
            text = stat['text']
        line = f"{idx + 1}. {time_range}\n{text}\n"
        if total_chars + len(line) > sample_limit_chars:
            break
        rendered_lines.append(line)
        total_chars += len(line)
        actual_count += 1

    return '\n'.join(rendered_lines).strip(), {
        'sample_items': actual_count,
        'sample_chars': total_chars,
        'sample_limit_items': sample_limit_items,
        'sample_limit_chars': sample_limit_chars,
        'sample_boundary_level': boundary_level,
    }


def _call_ai_judge(
    sample_text: str,
    metrics: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[Optional[bool], Optional[float], Optional[Dict[str, Any]], str]:
    api_key = (config.get('SUBTITLE_OPENAI_API_KEY') or config.get('OPENAI_API_KEY') or '').strip()
    base_url = (config.get('SUBTITLE_OPENAI_BASE_URL') or config.get('OPENAI_BASE_URL') or '').strip()

    model_name = (
        (config.get('SUBTITLE_QC_MODEL_NAME') or '').strip()
        or (config.get('SUBTITLE_OPENAI_MODEL_NAME') or '').strip()
        or (config.get('OPENAI_MODEL_NAME') or 'gpt-3.5-turbo')
    )

    if not api_key:
        return None, None, None, 'missing_openai_api_key'

    client = _build_openai_client(api_key=api_key, base_url=base_url)
    from .utils import openai_chat_create_with_thinking_control

    system = (
        "你是严格的字幕质检员。判断字幕是否可进入翻译和烧录。"
        "若存在署名行、Ignore noise、Click、明显机器幻觉、机械重复、超短低信息重复，必须判 failed。"
        "术语密集但语义正常的教学/讲解字幕不等于机械重复。"
        '只返回 JSON：{"passed":false,"score":0.10,"reason":"hallucination_meta"}。'
    )

    user = {
        'task': 'subtitle_qc',
        'metrics': metrics,
        'subtitle_sample': sample_text,
        'output_schema': {
            'passed': 'boolean',
            'score': 'number in [0,1], higher means more normal',
            'reason': 'short string reason'
        }
    }

    try:
        resp = openai_chat_create_with_thinking_control(
            client=client,
            create_kwargs={
                'model': model_name,
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': json.dumps(user, ensure_ascii=False)}
                ],
                'temperature': 0.0,
                'max_tokens': 120,
                'response_format': {'type': 'json_object'},
            },
            thinking_enabled=config.get('SUBTITLE_OPENAI_THINKING_ENABLED', False),
            logger=logger,
            scene_name='subtitle_qc',
        )
        message = resp.choices[0].message
        parsed_raw = extract_chat_message_json(message, expected_type=dict)
        if not isinstance(parsed_raw, dict) or not parsed_raw:
            logger.warning(f"字幕QC未返回有效JSON，响应预览: {get_chat_message_text(message)[:200]}")
            return None, None, None, 'ai_return_not_json'
        parsed: Dict[str, Any] = parsed_raw

        passed_val = parsed.get('passed', None)
        if passed_val is None:
            passed_val = parsed.get('pass', None)

        passed_bool: Optional[bool] = None
        if passed_val is not None:
            if isinstance(passed_val, bool):
                passed_bool = passed_val
            else:
                s = str(passed_val).strip().lower()
                if s in {'1', 'true', 'yes', 'y', 'on'}:
                    passed_bool = True
                elif s in {'0', 'false', 'no', 'n', 'off'}:
                    passed_bool = False

        if passed_bool is None:
            return None, None, parsed, 'missing_passed_bool'

        score = parsed.get('score', None)
        try:
            score_f = float(score) if score is not None else None
        except Exception:
            score_f = None
        return passed_bool, score_f, parsed, 'ok'
    except Exception as e:
        return None, None, None, f'ai_error:{normalize_qc_reason_token(str(e))}'


def _resolve_ai_unavailable_result(
    rule_result: RuleCheckResult,
    metrics: Dict[str, Any],
    rule_score: float,
    reason_token: str,
    sample_meta: Optional[Dict[str, Any]] = None,
) -> SubtitleQCResult:
    sample_meta = sample_meta or {}
    is_suspicious = rule_result.boundary_level == 'suspicious'
    passed = not is_suspicious
    prefix = 'qc_skipped' if passed else 'ai_fail'
    reason = f'{prefix}:{normalize_qc_reason_token(reason_token)}'
    return SubtitleQCResult(
        passed=passed,
        score=float(rule_score),
        reason=reason,
        rule_score=float(rule_score),
        ai_score=None,
        raw_ai={'decision': 'needs_ai', 'ai_status': normalize_qc_reason_token(reason_token), **metrics},
        decision='needs_ai',
        sample_items=int(sample_meta.get('sample_items', 0) or 0),
        sample_chars=int(sample_meta.get('sample_chars', 0) or 0),
    )


def run_subtitle_qc(
    srt_path: str,
    config: Dict[str, Any],
    threshold: Optional[float] = None,
) -> SubtitleQCResult:
    """对 ASR 生成的 SRT 做预检。失败时跳过字幕使用，但保留字幕文件并继续上传原视频。"""
    max_items = _to_int(config.get('SUBTITLE_QC_SAMPLE_MAX_ITEMS', 80), 80)
    max_chars = _to_int(config.get('SUBTITLE_QC_MAX_CHARS', 9000), 9000)

    threshold_val = threshold
    if threshold_val is None:
        threshold_val = _to_float(config.get('SUBTITLE_QC_THRESHOLD', 0.60), 0.60)

    items = _read_srt_items(srt_path)
    rule_result = _rule_check(items)
    item_stats = list(rule_result.metrics.get('item_stats') or [])
    rule_metrics = {k: v for k, v in rule_result.metrics.items() if k != 'item_stats'}
    checked_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    metrics = {
        'path': srt_path,
        'checked_at': checked_at,
        **rule_metrics,
    }

    if rule_result.decision == 'rule_pass':
        return SubtitleQCResult(
            passed=True,
            score=float(rule_result.score),
            reason=rule_result.reason,
            rule_score=float(rule_result.score),
            ai_score=None,
            raw_ai={'decision': 'rule_pass', 'ai_status': 'skipped', **metrics},
            decision='rule_pass',
            sample_items=0,
            sample_chars=0,
        )

    if rule_result.decision == 'rule_fail':
        return SubtitleQCResult(
            passed=False,
            score=float(rule_result.score),
            reason=rule_result.reason,
            rule_score=float(rule_result.score),
            ai_score=None,
            raw_ai={'decision': 'rule_fail', 'ai_status': 'skipped', **metrics},
            decision='rule_fail',
            sample_items=0,
            sample_chars=0,
        )

    provider = str(config.get('SUBTITLE_QC_PROVIDER', 'openai')).lower().strip()
    if provider != 'openai':
        return _resolve_ai_unavailable_result(
            rule_result=rule_result,
            metrics=metrics,
            rule_score=float(rule_result.score),
            reason_token='provider_disabled',
        )

    sample_text, sample_meta = _sample_items(
        items,
        item_stats=item_stats,
        max_items=max_items,
        max_chars=max_chars,
        boundary_level=rule_result.boundary_level,
    )
    metrics.update(sample_meta)

    if not sample_text:
        return _resolve_ai_unavailable_result(
            rule_result=rule_result,
            metrics=metrics,
            rule_score=float(rule_result.score),
            reason_token='empty_sample',
            sample_meta=sample_meta,
        )

    ai_passed, ai_score, raw_ai, ai_status = _call_ai_judge(sample_text, metrics=metrics, config=config)
    if ai_status != 'ok':
        return _resolve_ai_unavailable_result(
            rule_result=rule_result,
            metrics=metrics,
            rule_score=float(rule_result.score),
            reason_token=ai_status,
            sample_meta=sample_meta,
        )

    raw_reason = ''
    if raw_ai and isinstance(raw_ai, dict):
        raw_reason = normalize_qc_reason_token(raw_ai.get('reason') or '')
    advisory_mode = _is_high_rule_score_clean_boundary_sample(rule_result)
    ai_override = False

    if advisory_mode:
        final_score = float(rule_result.score)
        if not raw_reason:
            raw_reason = 'ok' if ai_passed else 'unknown'

        if bool(ai_passed):
            passed = float(final_score) >= float(threshold_val)
            prefix = 'ai_pass'
        elif raw_reason in ADVISORY_MODE_HARD_FAIL_REASONS:
            final_score = (
                min(float(rule_result.score), float(ai_score))
                if ai_score is not None
                else float(rule_result.score)
            )
            passed = False
            prefix = 'ai_fail'
        else:
            passed = float(final_score) >= float(threshold_val)
            prefix = 'ai_warn'
            ai_override = True
    else:
        final_score = float(rule_result.score)
        if ai_score is not None:
            final_score = min(float(rule_result.score), float(ai_score))
        passed = bool(ai_passed) and float(final_score) >= float(threshold_val)
        if not raw_reason:
            raw_reason = 'ok' if passed else 'hallucination_meta'
        prefix = 'ai_pass' if passed else 'ai_fail'

    reason = f'{prefix}:{raw_reason}'

    return SubtitleQCResult(
        passed=passed,
        score=float(final_score),
        reason=reason,
        rule_score=float(rule_result.score),
        ai_score=ai_score,
        raw_ai=(
            {
                **(raw_ai or {}),
                'decision': 'needs_ai',
                'ai_status': 'ok',
                'ai_mode': 'advisory_only' if advisory_mode else 'strict',
                'ai_override': ai_override,
                'ai_override_reason': raw_reason if ai_override else '',
                **metrics,
            }
        ),
        decision='needs_ai',
        sample_items=int(sample_meta.get('sample_items', 0) or 0),
        sample_chars=int(sample_meta.get('sample_chars', 0) or 0),
    )
