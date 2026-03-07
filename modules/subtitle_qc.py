#!/usr/bin/env python
# -*- coding: utf-8 -*-

import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .subtitle_translator import SubtitleReader
from .utils import strip_reasoning_thoughts, openai_chat_create_with_thinking_control

logger = logging.getLogger('subtitle_qc')


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


_PLACEHOLDER_RE = re.compile(r'^[\s\.,，。．…\-—_·•]+$')
_NON_CONTENT_RE = re.compile(r'[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+', re.UNICODE)
_REASON_TOKEN_RE = re.compile(r'[^a-z0-9]+')


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


def _build_openai_client(api_key: str, base_url: str):
    import openai

    options: Dict[str, Any] = {}
    if base_url:
        options['base_url'] = base_url
    return openai.OpenAI(api_key=api_key, **options)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = strip_reasoning_thoughts(text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    m = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def compute_subtitle_qc_fingerprint(items: List[Any]) -> str:
    parts: List[str] = []
    for it in items:
        try:
            start_time = str(getattr(it, 'start_time', '') or '').strip()
            end_time = str(getattr(it, 'end_time', '') or '').strip()
            text = ' '.join(str(getattr(it, 'source_text', '') or '').split())
        except Exception:
            continue
        parts.append(f'{start_time}|{end_time}|{text}')
    payload = '\n'.join(parts).encode('utf-8')
    return hashlib.sha1(payload).hexdigest()


def build_subtitle_qc_fingerprint(srt_path: str) -> str:
    items = SubtitleReader.read_srt(srt_path)
    return compute_subtitle_qc_fingerprint(items)


def _build_item_stats(items: List[Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    stats: List[Dict[str, Any]] = []
    usable_normalized: List[str] = []

    for idx, it in enumerate(items):
        text = (getattr(it, 'source_text', '') or '').strip()
        normalized = _normalize_line(text) if text else ''
        low_content = _is_low_content(text) if text else True
        if text and not low_content and normalized:
            usable_normalized.append(normalized)
        stats.append({
            'index': idx,
            'item': it,
            'text': text,
            'normalized': normalized,
            'low_content': low_content,
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
    avg_len = (
        sum(len(normalized) for normalized in usable_normalized) / usable_count
        if usable_count
        else 0.0
    )

    metrics = {
        'total_items': len(items),
        'non_empty_count': non_empty_count,
        'usable_count': usable_count,
        'low_content_count': low_content_count,
        'low_content_ratio': (low_content_count / max(1, non_empty_count)) if non_empty_count else 1.0,
        'top_frequency': top_frequency,
        'top_ratio': top_ratio,
        'unique_ratio': unique_ratio,
        'avg_len': avg_len,
    }
    return stats, metrics


def _estimate_rule_score(metrics: Dict[str, Any]) -> float:
    usable_count = int(metrics.get('usable_count', 0) or 0)
    low_content_ratio = float(metrics.get('low_content_ratio', 1.0) or 0.0)
    top_ratio = float(metrics.get('top_ratio', 1.0) or 0.0)
    unique_ratio = float(metrics.get('unique_ratio', 0.0) or 0.0)
    avg_len = float(metrics.get('avg_len', 0.0) or 0.0)

    score = 1.0
    if usable_count < 8:
        score -= min(0.25, (8 - usable_count) * 0.04)
    score -= min(0.35, max(0.0, low_content_ratio - 0.25) * 0.70)
    score -= min(0.35, max(0.0, top_ratio - 0.30) * 0.80)
    score -= min(0.25, max(0.0, 0.55 - unique_ratio) * 0.70)
    score -= min(0.20, max(0.0, 3.0 - avg_len) * 0.10)
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
    top_ratio = float(metrics['top_ratio'])
    unique_ratio = float(metrics['unique_ratio'])
    avg_len = float(metrics['avg_len'])
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
    selected_keys = set()

    def append_index(item_index: int):
        if item_index < 0 or item_index >= len(item_stats):
            return
        stat = item_stats[item_index]
        if not stat['text']:
            return
        key = stat['normalized'] or stat['text'].strip().lower()
        if key in selected_keys:
            return
        selected_keys.add(key)
        selected_indices.append(item_index)

    low_content_candidates = [stat['index'] for stat in non_empty_stats if stat['low_content']]
    for idx in low_content_candidates:
        append_index(idx)
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
        append_index(stat['index'])
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

    system = (
        '你是字幕质检员。目标：判断转录字幕是否可用。\n'
        '采用宽松标准：只要字幕整体有意义、不是明显的系统错误（如全是占位符、极端重复、完全乱码），就应判定为通过。\n'
        '常见转录误差、少量重复、口语化表达都属于可接受范围。\n'
        '请只输出严格 JSON，不要输出额外文本。输出格式示例：'
        '{"passed": true, "score": 0.75, "reason": "ok"}'
    )

    user = {
        'task': 'subtitle_qc',
        'rules': '仅在字幕明显不可用时判定 failed。少量错误或口语化都应放行。',
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
                'temperature': 0.2,
            },
            thinking_enabled=config.get('SUBTITLE_OPENAI_THINKING_ENABLED', False),
            logger=logger,
            scene_name='subtitle_qc',
        )
        content = (resp.choices[0].message.content or '').strip()
        parsed = _extract_json(content)
        if not parsed:
            return None, None, None, 'ai_return_not_json'

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

        score = parsed.get('score', None)
        try:
            score_f = float(score) if score is not None else None
        except Exception:
            score_f = None
        return passed_bool, score_f, parsed, 'ok'
    except Exception as e:
        return None, None, None, f'ai_error:{normalize_qc_reason_token(str(e))}'


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
        threshold_val = _to_float(config.get('SUBTITLE_QC_THRESHOLD', 0.35), 0.35)

    items = SubtitleReader.read_srt(srt_path)
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
        return SubtitleQCResult(
            passed=True,
            score=float(rule_result.score),
            reason='qc_skipped:provider_disabled',
            rule_score=float(rule_result.score),
            ai_score=None,
            raw_ai={'decision': 'needs_ai', 'ai_status': 'provider_disabled', **metrics},
            decision='needs_ai',
            sample_items=0,
            sample_chars=0,
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
        return SubtitleQCResult(
            passed=True,
            score=float(rule_result.score),
            reason='qc_skipped:empty_sample',
            rule_score=float(rule_result.score),
            ai_score=None,
            raw_ai={'decision': 'needs_ai', 'ai_status': 'empty_sample', **metrics},
            decision='needs_ai',
            sample_items=0,
            sample_chars=0,
        )

    ai_passed, ai_score, raw_ai, ai_status = _call_ai_judge(sample_text, metrics=metrics, config=config)
    if ai_status != 'ok':
        return SubtitleQCResult(
            passed=True,
            score=float(rule_result.score),
            reason=f'qc_skipped:{normalize_qc_reason_token(ai_status)}',
            rule_score=float(rule_result.score),
            ai_score=None,
            raw_ai={'decision': 'needs_ai', 'ai_status': ai_status, **metrics},
            decision='needs_ai',
            sample_items=int(sample_meta.get('sample_items', 0) or 0),
            sample_chars=int(sample_meta.get('sample_chars', 0) or 0),
        )

    if ai_passed is not None:
        passed = bool(ai_passed)
    elif ai_score is not None:
        passed = float(ai_score) >= float(threshold_val)
    else:
        passed = True

    final_score = float(ai_score) if ai_score is not None else float(rule_result.score)
    raw_reason = ''
    if raw_ai and isinstance(raw_ai, dict):
        raw_reason = normalize_qc_reason_token(raw_ai.get('reason') or '')
    if not raw_reason:
        raw_reason = 'ok' if passed else 'ai_fail'

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
                **metrics,
            }
        ),
        decision='needs_ai',
        sample_items=int(sample_meta.get('sample_items', 0) or 0),
        sample_chars=int(sample_meta.get('sample_chars', 0) or 0),
    )
