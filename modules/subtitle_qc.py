#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .subtitle_translator import SubtitleReader
from .utils import strip_reasoning_thoughts

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


_PLACEHOLDER_RE = re.compile(r'^[\s\.,，。．…\-—_·•]+$')
_NON_CONTENT_RE = re.compile(r'[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+', re.UNICODE)


def _normalize_line(text: str) -> str:
    t = (text or '').strip().lower()
    if not t:
        return ''
    t = _NON_CONTENT_RE.sub('', t)
    return t


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

    # 尝试抽取第一个 JSON 对象
    m = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _sample_items(items: List[Any], max_items: int, max_chars: int) -> str:
    if not items:
        return ''

    n = len(items)
    max_items = max(1, min(max_items, n))

    head = max(1, int(math.ceil(max_items * 0.3)))
    tail = max(1, int(math.ceil(max_items * 0.3)))
    mid = max_items - head - tail
    if mid < 0:
        mid = 0
        # 重新分配
        head = max_items // 2
        tail = max_items - head

    def pick_segment(start: int, end: int, k: int) -> List[int]:
        if k <= 0 or end <= start:
            return []
        length = end - start
        if k >= length:
            return list(range(start, end))
        step = length / k
        idxs = []
        for i in range(k):
            idx = start + int(i * step)
            idxs.append(min(end - 1, max(start, idx)))
        # 去重保持顺序
        seen = set()
        out = []
        for i in idxs:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    head_idxs = pick_segment(0, min(n, max(1, int(n * 0.35))), head)
    tail_start = max(0, n - max(1, int(n * 0.35)))
    tail_idxs = pick_segment(tail_start, n, tail)
    mid_idxs = []
    if mid > 0 and tail_start > len(head_idxs):
        mid_idxs = pick_segment(len(head_idxs), tail_start, mid)

    indices = head_idxs + mid_idxs + tail_idxs
    indices = sorted(set(indices), key=indices.index)

    lines: List[str] = []
    total_chars = 0
    for i in indices:
        it = items[i]
        # SubtitleItem: index/start_time/end_time/source_text
        try:
            time_range = f"{it.start_time} --> {it.end_time}"
            text = (it.source_text or '').strip()
        except Exception:
            time_range = ''
            text = str(it).strip()

        line = f"{i+1}. {time_range}\n{text}\n"
        if total_chars + len(line) > max_chars:
            break
        lines.append(line)
        total_chars += len(line)

    return '\n'.join(lines).strip()


def _rule_check(items: List[Any]) -> Tuple[float, str]:
    if not items:
        return 0.0, 'empty_subtitle'

    texts = [(getattr(it, 'source_text', '') or '').strip() for it in items]
    texts = [t for t in texts if t]
    if not texts:
        return 0.0, 'empty_subtitle'

    total = len(texts)
    low_content = sum(1 for t in texts if _is_low_content(t))
    low_content_ratio = low_content / max(1, total)

    normalized = [_normalize_line(t) for t in texts]
    normalized = [t for t in normalized if t]
    if not normalized:
        return 0.0, 'low_content'

    freq: Dict[str, int] = {}
    for t in normalized:
        freq[t] = freq.get(t, 0) + 1

    top = max(freq.values()) if freq else 0
    top_ratio = top / max(1, len(normalized))
    unique_ratio = (len(freq) / max(1, len(normalized)))

    avg_len = sum(len(t) for t in normalized) / max(1, len(normalized))

    score = 1.0
    reason_parts: List[str] = []

    # 强信号：大量重复
    if (len(normalized) >= 10 and top_ratio >= 0.35) or (len(normalized) >= 5 and top_ratio >= 0.6):
        score -= 0.6
        reason_parts.append('high_repetition')

    if unique_ratio < 0.3 and len(normalized) >= 15:
        score -= 0.3
        if 'high_repetition' not in reason_parts:
            reason_parts.append('low_variety')

    # 小样本但极低多样性
    if unique_ratio < 0.4 and len(normalized) >= 6:
        score -= 0.2
        if 'high_repetition' not in reason_parts and 'low_variety' not in reason_parts:
            reason_parts.append('low_variety')

    if low_content_ratio >= 0.4:
        score -= 0.4
        reason_parts.append('mostly_low_content')

    if avg_len < 3.0 and len(normalized) >= 10:
        score -= 0.2
        reason_parts.append('too_short')

    score = max(0.0, min(1.0, score))
    reason = ','.join(reason_parts) if reason_parts else 'ok'
    return score, reason


def _call_ai_judge(
    sample_text: str,
    metrics: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[Optional[float], Optional[Dict[str, Any]], str]:
    api_key = (config.get('SUBTITLE_OPENAI_API_KEY') or config.get('OPENAI_API_KEY') or '').strip()
    base_url = (config.get('SUBTITLE_OPENAI_BASE_URL') or config.get('OPENAI_BASE_URL') or '').strip()

    model_name = (
        (config.get('SUBTITLE_QC_MODEL_NAME') or '').strip()
        or (config.get('SUBTITLE_OPENAI_MODEL_NAME') or '').strip()
        or (config.get('OPENAI_MODEL_NAME') or 'gpt-3.5-turbo')
    )

    if not api_key:
        return None, None, 'missing_openai_api_key'

    client = _build_openai_client(api_key=api_key, base_url=base_url)

    system = (
        '你是字幕质检员。目标：判断字幕是否为“正常字幕”。\n'
        '正常字幕应与语音内容相关、语句多样且有信息量，不应是大量重复句、占位符(…/...)、乱序或明显胡话。\n'
        '请只输出严格 JSON，不要输出额外文本。'
    )

    user = {
        'task': 'subtitle_qc',
        'rules': '若字幕明显异常请判定 fail。',
        'metrics': metrics,
        'subtitle_sample': sample_text,
        'output_schema': {
            'pass': 'boolean',
            'score': 'number in [0,1], higher means more normal',
            'reason': 'short string reason'
        }
    }

    try:
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(user, ensure_ascii=False)}
            ],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or '').strip()
        parsed = _extract_json(content)
        if not parsed:
            return None, None, 'ai_return_not_json'
        score = parsed.get('score', None)
        try:
            score_f = float(score) if score is not None else None
        except Exception:
            score_f = None
        return score_f, parsed, 'ok'
    except Exception as e:
        return None, None, f'ai_error:{e}'


def run_subtitle_qc(
    srt_path: str,
    config: Dict[str, Any],
    threshold: Optional[float] = None,
) -> SubtitleQCResult:
    """对 SRT 进行最终字幕质检。失败时应跳过烧录字幕，但保留字幕文件并继续上传原视频。"""
    max_items = _to_int(config.get('SUBTITLE_QC_SAMPLE_MAX_ITEMS', 80), 80)
    max_chars = _to_int(config.get('SUBTITLE_QC_MAX_CHARS', 9000), 9000)
    enable_ai = _to_bool(config.get('SUBTITLE_QC_ENABLE_AI', True), True)

    threshold_val = threshold
    if threshold_val is None:
        threshold_val = _to_float(config.get('SUBTITLE_QC_THRESHOLD', 0.6), 0.6)

    items = SubtitleReader.read_srt(srt_path)

    rule_score, rule_reason = _rule_check(items)

    metrics = {
        'path': srt_path,
        'total_items': len(items),
        'rule_score': rule_score,
        'rule_reason': rule_reason,
        'checked_at': datetime.utcnow().isoformat() + 'Z',
    }

    ai_score: Optional[float] = None
    raw_ai: Optional[Dict[str, Any]] = None
    ai_status = 'skipped'

    if enable_ai and str(config.get('SUBTITLE_QC_PROVIDER', 'openai')).lower().strip() == 'openai':
        sample = _sample_items(items, max_items=max_items, max_chars=max_chars)
        if sample:
            ai_score, raw_ai, ai_status = _call_ai_judge(sample, metrics=metrics, config=config)
        else:
            ai_status = 'empty_sample'

    # 综合判定：优先规则硬拦截重复/空洞；AI 分数用于兜底
    # 规则明显异常时直接 FAIL，避免 AI 误放行
    hard_fail = (rule_score <= 0.3) or (rule_reason in {'empty_subtitle', 'low_content'})

    combined = rule_score
    if ai_score is not None:
        combined = 0.55 * rule_score + 0.45 * ai_score

    passed = (combined >= float(threshold_val)) and (not hard_fail)

    reason = 'ok'
    if not passed:
        if hard_fail:
            reason = f'rule_fail:{rule_reason}'
        elif ai_score is not None:
            reason = f'ai_or_combined_fail:{raw_ai.get("reason", "unknown") if raw_ai else "unknown"}'
        else:
            reason = f'rule_fail:{rule_reason}'

    return SubtitleQCResult(
        passed=passed,
        score=float(combined),
        reason=reason,
        rule_score=rule_score,
        ai_score=ai_score,
        raw_ai=(raw_ai if raw_ai is not None else {'ai_status': ai_status}),
    )
