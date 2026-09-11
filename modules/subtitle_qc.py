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
SHORT_DURATION_THRESHOLD_S = 0.5  # 单条字幕显示时长低于此值视为过短（QC 安全网，独立于 AI 分段阈值）
MAX_REPEATED_SAMPLE_PER_TEXT = 3
TOP_REPEATED_TEXT_LIMIT = 5
HIGH_CONFIDENCE_RULE_SCORE_THRESHOLD = 0.85
ADVISORY_MODE_HARD_FAIL_REASONS = {
    'hallucination_meta',
    'credit_like_phrase',
    'noise_command_phrase',
    'template_like_phrase',
}
# 时间轴维度阈值：这些量决定「字幕是否贯穿全片、节奏是否可用」，
# 此前质检只有文本维度，无法识别 VAD 失败造成的句界错位与时间轴崩坏。
#
# 阈值取值原则：硬失败只抓**病态**情形（例如 VAD 只跑完前段就中断、字幕只覆盖
# 开头一小截），不能误杀本来就稀疏但合法的内容 —— 含大量静音、长镜头、
# 纯音乐段的视频，正常覆盖率也可能只有 20–30%。因此硬失败线定得低，
# 可疑线负责把边界情况交给 AI 复核。
# 这些数值是**兜底默认**：真实取值由 SPEECH_PIPELINE_DEFAULTS / DEFAULT_CONFIG
# 的 SUBTITLE_QC_* 键提供（可在设置页调整），二者由 tests/test_subtitle_qc_timeline.py
# 的一致性用例锁死，避免再次出现「两套默认值分叉」。
TIMELINE_MIN_COVERAGE_RATIO = 0.15          # cue 时长并集 / 视频时长（低于此值视为未覆盖全片）
TIMELINE_SUSPICIOUS_COVERAGE_RATIO = 0.35
TIMELINE_MIN_LAST_CUE_END_RATIO = 0.60      # 末条 cue 结束位置 / 视频时长
TIMELINE_SUSPICIOUS_LAST_CUE_END_RATIO = 0.85
TIMELINE_MAX_FIRST_CUE_START_RATIO = 0.25   # 首条 cue 起点 / 视频时长（片头长镜头常见）
TIMELINE_SUSPICIOUS_FIRST_CUE_START_RATIO = 0.08
TIMELINE_MAX_GAP_SECONDS = 90.0             # 相邻 cue 间最大空档（绝对秒）
TIMELINE_MAX_GAP_RATIO = 0.20               # 相邻 cue 间最大空档（占总时长）
TIMELINE_SUSPICIOUS_MAX_GAP_SECONDS = 20.0
TIMELINE_MAX_OVERLAP_RATIO = 0.05           # 重叠 cue 对数 / 总 cue 数
TIMELINE_SUSPICIOUS_OVERLAP_RATIO = 0.01
TIMELINE_MAX_CPS_OUTLIER_RATIO = 0.20       # CPS 异常条目占比（>25 或 <2）
TIMELINE_SUSPICIOUS_CPS_OUTLIER_RATIO = 0.08
TIMELINE_CPS_UPPER = 25.0
TIMELINE_CPS_LOWER = 2.0
TIMELINE_HARD_STUTTER_RUN = 4               # 相邻同文本且间隔 <0.3s 的连续条数
TIMELINE_SUSPICIOUS_STUTTER_RUN = 3
TIMELINE_STUTTER_GAP_S = 0.3
# 退化 cue（零时长 / 逆序）：VAD 崩坏最典型的产物就是「有文本、没有时长」。
# 此前这类条目被 `end > start` 的判断整体排除出 timeline_pairs，于是最需要检查的
# 输入反而让整个时间轴维度静默关闭（timeline_checked=False 且无任何 warning）。
# 现在它们计入统计并单独度量，占比超阈值即硬失败。
# 取值理由：零时长硬线 25% + 最少 3 条 —— 真实素材里偶发 1–2 条零时长 cue
# （音频块边界重合）完全正常，即使它们占满 10 条字幕的 20% 也不该判死；
# 而 1/4 以上的 cue 完全没有时长，只可能是时间戳生成崩坏。
TIMELINE_MAX_ZERO_DURATION_RATIO = 0.25
TIMELINE_MIN_ZERO_DURATION_COUNT = 3
TIMELINE_SUSPICIOUS_ZERO_DURATION_RATIO = 0.10
# 逆序 cue（start > end）在 SRT 语义上不该出现，单条按偶发处理（仅送 AI 复核），
# 达到 2 条且超过 5% 才判系统性倒挂 —— 5% 与重叠率硬线同口径；
# 占比达到 25% 时无论条数都判病态（小样本兜底，例如 4 条里 1 条倒挂）。
TIMELINE_MAX_REVERSED_CUE_RATIO = 0.05
TIMELINE_MIN_REVERSED_CUE_COUNT = 2
TIMELINE_EXTREME_DEGENERATE_RATIO = 0.25
# 片尾留白容忍带：结束卡 / 黑屏 / 静音吃掉末尾十几秒是常态，
# 因此「末条提前结束」只有在**绝对秒数**也超出该容忍带时才视为可疑。
# 50 分钟以上的视频若末条只到 83%，600s 的缺口远超 30s，仍会被抓住。
TIMELINE_TAIL_GRACE_SECONDS = 30.0


def _pipeline_timeline_default(key: str, fallback: float) -> float:
    """从配置中心读取时间轴阈值默认值，保证与设置页/DEFAULT_CONFIG 单一来源。"""
    try:
        from .config_manager import DEFAULT_CONFIG
    except Exception:
        return fallback
    value = DEFAULT_CONFIG.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


TIMELINE_MIN_COVERAGE_RATIO = _pipeline_timeline_default(
    'SUBTITLE_QC_MIN_COVERAGE_RATIO', TIMELINE_MIN_COVERAGE_RATIO
)
TIMELINE_MAX_GAP_SECONDS = _pipeline_timeline_default(
    'SUBTITLE_QC_MAX_GAP_S', TIMELINE_MAX_GAP_SECONDS
)
TIMELINE_CPS_UPPER = _pipeline_timeline_default('SUBTITLE_QC_MAX_CPS', TIMELINE_CPS_UPPER)
# strict 模式下允许的最低保底规则分（AI 不可用时的放行下限）
STRICT_MIN_RULE_SCORE = 0.85
# advisory 覆盖的最低 AI 分：低于该值视为 AI 明确否决，禁止被 advisory 推翻
ADVISORY_OVERRIDE_MIN_AI_SCORE = 0.4
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


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in ('true', '1', 'on', 'yes')


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


def _build_openai_client(api_key: str, base_url: str, model_name: str = None):
    """构建 AI 客户端（统一走 ai_fallback_client，主端点不可用时自动切换兜底端点）。

    兜底端点（FALLBACK_OPENAI_*）由 get_ai_client 从传入配置或全局配置统一解析；
    同时显式透传全局配置的 OPENAI_TIMEOUT_SECONDS，避免统一客户端回退到 600s
    而忽略用户配置、并改掉 QC 原先的超时语义。
    """
    from modules.ai_fallback_client import get_ai_client
    from modules.config_manager import load_config

    # QC 使用**独立配置键** SUBTITLE_QC_TIMEOUT_SECONDS（默认 120s），
    # 不再依赖「全局 OPENAI_TIMEOUT_SECONDS 是否显式配置」来判断——
    # 因为 load_config() 会把 DEFAULT_CONFIG 的 600 合并进返回值，
    # 普通默认安装中该键永远为 600，is-explicit 判断恒为假、120s 分支永不生效
    # （reviewer 第三轮指出的行为回归）。独立键语义清晰、无回归：
    # 未配置 → 120s（QC 原默认）；配置 → 用之。
    # 该键已纳入 DEFAULT_CONFIG + 设置页 + _perform_settings_save（数值字段 + 10–600 范围校验），
    # 故可正常持久化、不再被 _prune_unknown_config_keys 删除。此处再做兜底净化，
    # 防止手工改坏 config.json 或旧版本残留导致非数字 / 非有限 / 越界值透传给 httpx。
    _qc_timeout_raw = (load_config() or {}).get('SUBTITLE_QC_TIMEOUT_SECONDS')
    _qc_timeout = 120
    try:
        _qc_tv = float(str(_qc_timeout_raw).strip()) if _qc_timeout_raw is not None else None
        if _qc_tv is not None and _qc_tv == _qc_tv and 10 <= _qc_tv <= 600:  # NaN 自检 + 范围
            _qc_timeout = int(_qc_tv)
    except (ValueError, TypeError):
        pass

    cfg: Dict[str, Any] = {
        'OPENAI_API_KEY': api_key,
        'OPENAI_BASE_URL': base_url,
        'OPENAI_TIMEOUT_SECONDS': _qc_timeout,
    }
    if model_name:
        cfg['OPENAI_MODEL_NAME'] = model_name
    return get_ai_client(cfg)


def _build_item_stats(
    items: List[Any],
    total_duration_s: Optional[float] = None,
    timeline_limits: Optional[Dict[str, float]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    timeline_limits = timeline_limits or {}
    # CPS 上限必须取**运行时配置**，不能锚定模块导入期常量 TIMELINE_CPS_UPPER：
    # 用户把上限放宽到 25 以上（设置页允许 1–200）时，若统计口径仍用导入期默认，
    # 放宽方向恒不生效，同一份字幕在 25 与 30 下得到完全相同的结论。
    cps_upper = _to_float(timeline_limits.get('max_cps', TIMELINE_CPS_UPPER), TIMELINE_CPS_UPPER)
    if not math.isfinite(cps_upper) or cps_upper <= 0:
        cps_upper = TIMELINE_CPS_UPPER
    stats: List[Dict[str, Any]] = []
    usable_normalized: List[str] = []
    suspicious_examples: List[str] = []
    suspicious_counts = Counter()
    normalized_examples: Dict[str, str] = {}
    total_text_chars = 0
    short_line_count = 0
    short_duration_count = 0
    max_repeat_run = 0
    current_repeat_run = 0
    previous_normalized = ''
    earliest_start: Optional[float] = None
    latest_end: Optional[float] = None
    # 时间轴维度采集：这些量回答「字幕是否贯穿全片、节奏是否可用」，
    # 文本维度无法覆盖 VAD 失败导致的句界错位与时间轴崩坏。
    timeline_pairs: List[Tuple[float, float, str]] = []
    previous_text_normalized = ''
    previous_end_for_stutter: Optional[float] = None
    current_stutter_run = 0
    max_stutter_run = 0
    overlap_pairs = 0
    comparable_pairs = 0
    cps_outlier_count = 0
    cps_comparable_count = 0
    cps_values: List[float] = []
    previous_start_for_overlap: Optional[float] = None
    previous_end_for_overlap: Optional[float] = None
    zero_duration_count = 0
    reversed_cue_count = 0

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
        if (
            text
            and start_seconds is not None
            and end_seconds is not None
            and (end_seconds - start_seconds) < SHORT_DURATION_THRESHOLD_S
        ):
            short_duration_count += 1

        # 时间轴度量（仅统计有有效时间与文本的条目）。
        # 零时长 / 逆序 cue 必须**计入**：它们正是 VAD 崩坏的产物，
        # 排除掉会让整个时间轴维度在最需要检查的输入下静默关闭。
        if text and start_seconds is not None and end_seconds is not None:
            span_seconds = end_seconds - start_seconds
            if span_seconds > 0:
                pair_start, pair_end = start_seconds, end_seconds
            else:
                # 退化 cue 折成零长度点：不贡献并集、不参与重叠链，
                # 因此既不会让维度关闭，也不会被误报成 rule_fail:timeline_overlap。
                pair_start = pair_end = min(start_seconds, end_seconds)
                if span_seconds < 0:
                    reversed_cue_count += 1
                else:
                    zero_duration_count += 1
            timeline_pairs.append((pair_start, pair_end, normalized))
            # CPS 只统计正时长条目：退化 cue 的「每秒字符数」无意义（除以 0），
            # 它们由 zero_duration_ratio / reversed_cue_ratio 单独度量，
            # 避免同一处缺陷被 cps_outlier 二次计入、把 1–2 条偶发条目放大成硬失败。
            if normalized and span_seconds > 0:
                cps = len(normalized) / span_seconds
                cps_comparable_count += 1
                cps_values.append(cps)
                if cps > cps_upper or cps < TIMELINE_CPS_LOWER:
                    cps_outlier_count += 1
            # 相邻 cue 间隔 / 重叠 / 结巴：只让正时长条目进入链条，
            # 退化点落在后一条字幕内部时不应被算作重叠。
            if span_seconds > 0:
                if previous_start_for_overlap is not None and previous_end_for_overlap is not None:
                    comparable_pairs += 1
                    if start_seconds < previous_end_for_overlap - 1e-6:
                        overlap_pairs += 1
                previous_start_for_overlap = start_seconds
                previous_end_for_overlap = end_seconds
                gap_from_prev = (
                    start_seconds - previous_end_for_stutter
                    if previous_end_for_stutter is not None
                    else None
                )
                if (
                    normalized
                    and normalized == previous_text_normalized
                    and gap_from_prev is not None
                    and abs(gap_from_prev) < TIMELINE_STUTTER_GAP_S
                ):
                    current_stutter_run += 1
                else:
                    current_stutter_run = 1
                previous_text_normalized = normalized
                previous_end_for_stutter = end_seconds
                max_stutter_run = max(max_stutter_run, current_stutter_run if normalized else 0)

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

    # ---- 时间轴聚合度量 ----
    timeline_metrics: Dict[str, Any] = {
        'timeline_checked': False,
        'timeline_skipped': True,
    }
    duration_bound = float(total_duration_s or 0.0)
    if timeline_pairs:
        ordered = sorted(timeline_pairs, key=lambda pair: pair[0])
        # 几何量（并集 / 首尾位置 / 最大空档）只由**正时长**条目决定：
        # 退化点不代表真实覆盖，也不能把一个大空档切成两半从而掩盖问题；
        # 若全部条目都退化，则退回用点位置计算，保证维度仍给出诊断数值。
        positive_pairs = [pair for pair in ordered if pair[1] > pair[0]]
        geometry_pairs = positive_pairs or ordered
        merged_union = 0.0
        cursor_start, cursor_end = geometry_pairs[0][0], geometry_pairs[0][1]
        for pair_start, pair_end, _ in geometry_pairs[1:]:
            if pair_start <= cursor_end + 1e-6:
                cursor_end = max(cursor_end, pair_end)
            else:
                merged_union += max(0.0, cursor_end - cursor_start)
                cursor_start, cursor_end = pair_start, pair_end
        merged_union += max(0.0, cursor_end - cursor_start)

        first_cue_start = geometry_pairs[0][0]
        last_cue_end = max(pair[1] for pair in geometry_pairs)
        max_gap = 0.0
        for prev_pair, next_pair in zip(geometry_pairs, geometry_pairs[1:]):
            max_gap = max(max_gap, max(0.0, next_pair[0] - prev_pair[1]))

        timed_cue_count = len(ordered)
        timeline_metrics = {
            'timeline_checked': True,
            'timeline_skipped': False,
            'timeline_cue_count': timed_cue_count,
            'timeline_positive_cue_count': len(positive_pairs),
            'zero_duration_count': zero_duration_count,
            'reversed_cue_count': reversed_cue_count,
            'zero_duration_ratio': (zero_duration_count / timed_cue_count) if timed_cue_count else 0.0,
            'reversed_cue_ratio': (reversed_cue_count / timed_cue_count) if timed_cue_count else 0.0,
            'timeline_union_seconds': merged_union,
            'timeline_span_seconds': max(0.0, last_cue_end - first_cue_start),
            'first_cue_start_seconds': first_cue_start,
            'last_cue_end_seconds': last_cue_end,
            'max_gap_seconds': max_gap,
            'overlap_ratio': (overlap_pairs / comparable_pairs) if comparable_pairs else 0.0,
            'cps_outlier_ratio': (cps_outlier_count / cps_comparable_count) if cps_comparable_count else 0.0,
            'cps_values': list(cps_values),
            'stutter_run': int(max_stutter_run),
        }
        if duration_bound > 0:
            timeline_metrics.update({
                'total_duration_seconds': duration_bound,
                'coverage_ratio': merged_union / max(duration_bound, 0.01),
                'last_cue_end_ratio': last_cue_end / max(duration_bound, 0.01),
                'first_cue_start_ratio': first_cue_start / max(duration_bound, 0.01),
                'max_gap_ratio': max_gap / max(duration_bound, 0.01),
            })
        else:
            # 拿不到视频总时长时只保留绝对值维度供诊断，**判定维度整体关闭**，
            # 与 run_subtitle_qc 的契约一致（「取不到时自动跳过这些维度，
            # 不影响既有文本维度判定」）。此前仅把 coverage_ratio 置空，
            # timeline_checked 仍为 True，导致 gap/overlap/cps/stutter 这些
            # 绝对值维度继续参与硬失败判定 —— 视频缺失或探测失败时，
            # 合法但稀疏的字幕会被 rule_fail:timeline_* 误杀。
            timeline_metrics['coverage_ratio'] = None
            timeline_metrics['timeline_checked'] = False
            timeline_metrics['timeline_skipped'] = True
            timeline_metrics['timeline_absolute_only'] = True

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
        'short_duration_count': short_duration_count,
        'short_duration_ratio': (short_duration_count / max(1, non_empty_count)) if non_empty_count else 0.0,
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
        'timeline_metrics': timeline_metrics,
        **timeline_metrics,
    }
    return stats, metrics


def _tail_within_grace(metrics: Dict[str, Any]) -> bool:
    """末条结束位置距视频结尾是否在容忍带内（片尾黑屏/静音/结束卡的常态留白）。

    没有总时长或末条位置时返回 False（无法证明是良性留白，按可疑处理）。
    """
    duration = float(metrics.get('total_duration_seconds', 0.0) or 0.0)
    last_cue_end = metrics.get('last_cue_end_seconds')
    if duration <= 0 or last_cue_end is None:
        return False
    return (duration - float(last_cue_end)) <= TIMELINE_TAIL_GRACE_SECONDS


def _timeline_materially_deficient(metrics: Dict[str, Any]) -> bool:
    """时间轴是否存在**实质缺陷**：覆盖率严重不足，或末条明显提前结束。

    只有这两类信号才禁止在 AI 不可用时放行 —— 它们是时间轴维度真正要抓的目标
    （VAD 中途崩坏、字幕只覆盖前段）。其余可疑信号（轻微重叠 / 语速异常 /
    空档偏大 / 文本维度软信号）在规则分达到高置信线时允许放行，避免良性字幕
    仅因「末条没压到最后 85%」而在未配置 AI 时永远无法烧录。
    """
    if not metrics.get('timeline_checked'):
        return False
    coverage_ratio = metrics.get('coverage_ratio')
    if coverage_ratio is not None and float(coverage_ratio) < TIMELINE_SUSPICIOUS_COVERAGE_RATIO:
        return True
    last_end_ratio = metrics.get('last_cue_end_ratio')
    if last_end_ratio is not None and float(last_end_ratio) < TIMELINE_SUSPICIOUS_LAST_CUE_END_RATIO:
        return not _tail_within_grace(metrics)
    return False


def _estimate_rule_score(metrics: Dict[str, Any]) -> float:
    usable_count = int(metrics.get('usable_count', 0) or 0)
    low_content_ratio = float(metrics.get('low_content_ratio', 1.0) or 0.0)
    top_ratio = float(metrics.get('top_ratio', 1.0) or 0.0)
    unique_ratio = float(metrics.get('unique_ratio', 0.0) or 0.0)
    repeat_mass_ratio = float(metrics.get('repeat_mass_ratio', 1.0) or 0.0)
    avg_len = float(metrics.get('avg_len', 0.0) or 0.0)
    short_line_ratio = float(metrics.get('short_line_ratio', 0.0) or 0.0)
    short_duration_ratio = float(metrics.get('short_duration_ratio', 0.0) or 0.0)
    max_repeat_run = int(metrics.get('max_repeat_run', 0) or 0)
    chars_per_minute = float(metrics.get('chars_per_minute', 0.0) or 0.0)
    suspicious_phrase_ratio = float(metrics.get('suspicious_phrase_ratio', 0.0) or 0.0)
    credit_like_count = int(metrics.get('credit_like_count', 0) or 0)
    noise_command_count = int(metrics.get('noise_command_count', 0) or 0)
    template_like_count = int(metrics.get('template_like_count', 0) or 0)
    timeline_checked = bool(metrics.get('timeline_checked'))

    score = 1.0
    if usable_count < 8:
        score -= min(0.25, (8 - usable_count) * 0.04)
    score -= min(0.35, max(0.0, low_content_ratio - 0.25) * 0.70)
    score -= min(0.35, max(0.0, top_ratio - 0.30) * 0.80)
    score -= min(0.25, max(0.0, repeat_mass_ratio - 0.35) * 0.75)
    score -= min(0.25, max(0.0, 0.55 - unique_ratio) * 0.70)
    score -= min(0.20, max(0.0, 3.0 - avg_len) * 0.10)
    score -= min(0.22, max(0.0, short_line_ratio - 0.45) * 0.40)
    score -= min(0.20, max(0.0, short_duration_ratio - 0.15) * 0.60)
    score -= min(0.16, max(0, max_repeat_run - 2) * 0.08)
    if chars_per_minute > 0:
        score -= min(0.18, max(0.0, 35.0 - chars_per_minute) * 0.006)
    score -= min(0.35, suspicious_phrase_ratio * 1.20)
    score -= min(0.30, credit_like_count * 0.20)
    score -= min(0.25, noise_command_count * 0.10)
    score -= min(0.20, template_like_count * 0.08)

    # 时间轴质量扣分：与文本维度等权，因为「文本干净但时间轴崩坏」正是
    # VAD 失败最典型的表现，纯文本质检发现不了。
    if timeline_checked:
        coverage_ratio = metrics.get('coverage_ratio')
        if coverage_ratio is not None:
            score -= min(0.40, max(0.0, TIMELINE_SUSPICIOUS_COVERAGE_RATIO - float(coverage_ratio)) * 0.80)
        last_end_ratio = metrics.get('last_cue_end_ratio')
        # 片尾容忍带内的留白不扣分（否则良性字幕会被扣着分送去 AI 复核）。
        if last_end_ratio is not None and not _tail_within_grace(metrics):
            score -= min(0.30, max(0.0, TIMELINE_SUSPICIOUS_LAST_CUE_END_RATIO - float(last_end_ratio)) * 0.75)
        zero_duration_ratio = float(metrics.get('zero_duration_ratio', 0.0) or 0.0)
        score -= min(0.20, max(0.0, zero_duration_ratio - TIMELINE_SUSPICIOUS_ZERO_DURATION_RATIO) * 0.60)
        score -= min(0.20, float(metrics.get('reversed_cue_ratio', 0.0) or 0.0) * 1.20)
        max_gap = float(metrics.get('max_gap_seconds', 0.0) or 0.0)
        score -= min(0.20, max(0.0, max_gap - TIMELINE_SUSPICIOUS_MAX_GAP_SECONDS) * 0.005)
        score -= min(0.20, max(0.0, float(metrics.get('overlap_ratio', 0.0) or 0.0) - TIMELINE_SUSPICIOUS_OVERLAP_RATIO) * 1.20)
        score -= min(0.20, max(0.0, float(metrics.get('cps_outlier_ratio', 0.0) or 0.0) - TIMELINE_SUSPICIOUS_CPS_OUTLIER_RATIO) * 0.70)
        score -= min(0.15, max(0, int(metrics.get('stutter_run', 0) or 0) - 2) * 0.05)
    return max(0.0, min(1.0, score))


def _timeline_hard_fail_reason(metrics: Dict[str, Any], limits: Optional[Dict[str, float]] = None) -> Optional[str]:
    """时间轴硬失败判定。返回 reason token 或 None。"""
    if not metrics.get('timeline_checked'):
        return None
    limits = limits or {}
    min_coverage = float(limits.get('min_coverage_ratio', TIMELINE_MIN_COVERAGE_RATIO))
    max_gap_s = float(limits.get('max_gap_seconds', TIMELINE_MAX_GAP_SECONDS))
    # 退化 cue（零时长 / 逆序）最先判定：这是最具体的诊断。若放到覆盖率 / 重叠
    # 之后，VAD 崩坏的字幕会被统一报成 coverage_too_low / overlap，误导排查方向。
    zero_duration_count = int(metrics.get('zero_duration_count', 0) or 0)
    zero_duration_ratio = float(metrics.get('zero_duration_ratio', 0.0) or 0.0)
    if (
        zero_duration_count >= TIMELINE_MIN_ZERO_DURATION_COUNT
        and zero_duration_ratio >= TIMELINE_MAX_ZERO_DURATION_RATIO
    ):
        return 'rule_fail:timeline_zero_duration'
    reversed_cue_count = int(metrics.get('reversed_cue_count', 0) or 0)
    reversed_cue_ratio = float(metrics.get('reversed_cue_ratio', 0.0) or 0.0)
    if reversed_cue_count > 0 and (
        reversed_cue_ratio >= TIMELINE_EXTREME_DEGENERATE_RATIO
        or (
            reversed_cue_count >= TIMELINE_MIN_REVERSED_CUE_COUNT
            and reversed_cue_ratio > TIMELINE_MAX_REVERSED_CUE_RATIO
        )
    ):
        return 'rule_fail:timeline_reversed_cue'
    stutter_run = int(metrics.get('stutter_run', 0) or 0)
    if stutter_run >= TIMELINE_HARD_STUTTER_RUN:
        return 'rule_fail:timeline_stutter_repeat'
    overlap_ratio = float(metrics.get('overlap_ratio', 0.0) or 0.0)
    if overlap_ratio > TIMELINE_MAX_OVERLAP_RATIO:
        return 'rule_fail:timeline_overlap'
    cps_outlier_ratio = float(metrics.get('cps_outlier_ratio', 0.0) or 0.0)
    if cps_outlier_ratio > TIMELINE_MAX_CPS_OUTLIER_RATIO:
        return 'rule_fail:timeline_cps_outlier'
    coverage_ratio = metrics.get('coverage_ratio')
    if coverage_ratio is not None and float(coverage_ratio) < min_coverage:
        return 'rule_fail:timeline_coverage_too_low'
    last_end_ratio = metrics.get('last_cue_end_ratio')
    if last_end_ratio is not None and float(last_end_ratio) < TIMELINE_MIN_LAST_CUE_END_RATIO:
        return 'rule_fail:timeline_truncated_tail'
    first_start_ratio = metrics.get('first_cue_start_ratio')
    if first_start_ratio is not None and float(first_start_ratio) > TIMELINE_MAX_FIRST_CUE_START_RATIO:
        return 'rule_fail:timeline_late_start'
    max_gap = float(metrics.get('max_gap_seconds', 0.0) or 0.0)
    max_gap_ratio = metrics.get('max_gap_ratio')
    if max_gap > max_gap_s:
        return 'rule_fail:timeline_gap_too_large'
    if max_gap_ratio is not None and float(max_gap_ratio) > TIMELINE_MAX_GAP_RATIO:
        return 'rule_fail:timeline_gap_too_large'
    # CPS 上限（含放宽方向）已在 _build_item_stats 中按运行时配置的 limits['max_cps']
    # 计入 cps_outlier_ratio，这里不再与模块导入期常量 TIMELINE_CPS_UPPER 比较：
    # 那段分支让「放宽到 25 以上」恒不生效（用户设 30 想放过 25–30 区间无效）。
    return None


def _timeline_suspicious(metrics: Dict[str, Any]) -> bool:
    """时间轴可疑判定：命中则必须送 AI 复核（boundary_level 降为 suspicious）。"""
    if not metrics.get('timeline_checked'):
        return False
    if int(metrics.get('reversed_cue_count', 0) or 0) > 0:
        return True
    if (
        int(metrics.get('zero_duration_count', 0) or 0) > 0
        and float(metrics.get('zero_duration_ratio', 0.0) or 0.0) > TIMELINE_SUSPICIOUS_ZERO_DURATION_RATIO
    ):
        return True
    if int(metrics.get('stutter_run', 0) or 0) >= TIMELINE_SUSPICIOUS_STUTTER_RUN:
        return True
    if float(metrics.get('overlap_ratio', 0.0) or 0.0) > TIMELINE_SUSPICIOUS_OVERLAP_RATIO:
        return True
    if float(metrics.get('cps_outlier_ratio', 0.0) or 0.0) > TIMELINE_SUSPICIOUS_CPS_OUTLIER_RATIO:
        return True
    coverage_ratio = metrics.get('coverage_ratio')
    if coverage_ratio is not None and float(coverage_ratio) < TIMELINE_SUSPICIOUS_COVERAGE_RATIO:
        return True
    last_end_ratio = metrics.get('last_cue_end_ratio')
    # 片尾留白（结束卡 / 黑屏 / 静音）是常态：只有绝对缺口也超出容忍带，
    # 也就是「末条明显提前结束」时才降级为可疑。
    if (
        last_end_ratio is not None
        and float(last_end_ratio) < TIMELINE_SUSPICIOUS_LAST_CUE_END_RATIO
        and not _tail_within_grace(metrics)
    ):
        return True
    first_start_ratio = metrics.get('first_cue_start_ratio')
    if first_start_ratio is not None and float(first_start_ratio) > TIMELINE_SUSPICIOUS_FIRST_CUE_START_RATIO:
        return True
    if float(metrics.get('max_gap_seconds', 0.0) or 0.0) > TIMELINE_SUSPICIOUS_MAX_GAP_SECONDS:
        return True
    return False


def _rule_check(
    items: List[Any],
    total_duration_s: Optional[float] = None,
    timeline_limits: Optional[Dict[str, float]] = None,
) -> RuleCheckResult:
    item_stats, metrics = _build_item_stats(
        items, total_duration_s=total_duration_s, timeline_limits=timeline_limits
    )
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

    # 时间轴硬失败：先于文本健康度放行判定，因为「文本干净但时间轴崩坏」
    # 正是 VAD/ASR 降级的典型表现。
    timeline_fail_reason = _timeline_hard_fail_reason(metrics, timeline_limits)
    if timeline_fail_reason:
        return RuleCheckResult(
            decision='rule_fail',
            score=rule_score,
            reason=timeline_fail_reason,
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
        and not _timeline_suspicious(metrics)
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
        or _timeline_suspicious(metrics)
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
    api_key = (
        (config.get('SUBTITLE_QC_API_KEY') or '').strip()
        or (config.get('SUBTITLE_OPENAI_API_KEY') or '').strip()
        or (config.get('OPENAI_API_KEY') or '').strip()
    )
    base_url = (
        (config.get('SUBTITLE_QC_BASE_URL') or '').strip()
        or (config.get('SUBTITLE_OPENAI_BASE_URL') or '').strip()
        or (config.get('OPENAI_BASE_URL') or '').strip()
    )

    model_name = (
        (config.get('SUBTITLE_QC_MODEL_NAME') or '').strip()
        or (config.get('SUBTITLE_OPENAI_MODEL_NAME') or '').strip()
        or (config.get('OPENAI_MODEL_NAME') or 'gpt-3.5-turbo')
    )

    if not api_key:
        return None, None, None, 'missing_openai_api_key'

    client = _build_openai_client(api_key=api_key, base_url=base_url, model_name=model_name)
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
                'response_format': {'type': 'json_object'},
            },
            thinking_enabled=config.get('SUBTITLE_QC_THINKING_ENABLED', False),
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
    strict: bool = False,
) -> SubtitleQCResult:
    """AI 不可用（超时/未配置/解析失败）时的兜底判定。

    此前 boundary 级样本在 AI 不可用时直接 ``passed=True``，等于把「质检没跑成」
    当成「质检通过」，低质量字幕因此静默烧录。现在：
    - ``strict=True``（ASR 来源退化）：一律不放行；
    - 非 strict 且边界等级为 suspicious：时间轴存在实质缺陷（覆盖率严重不足 /
      末条明显提前结束）时一律不放行；否则规则分达到高置信线即可放行 ——
      片尾正常留白不应让良性字幕在未配置 AI 时永远无法烧录；
    - 非 strict 且边界等级为 boundary：仅当规则分达到高置信线才放行。
    """
    sample_meta = sample_meta or {}
    is_suspicious = rule_result.boundary_level == 'suspicious'
    if strict:
        passed = False
    elif is_suspicious:
        passed = (
            float(rule_score) >= STRICT_MIN_RULE_SCORE
            and not _timeline_materially_deficient(metrics)
        )
    else:
        passed = float(rule_score) >= STRICT_MIN_RULE_SCORE
    prefix = 'qc_skipped' if passed else 'ai_fail'
    reason = f'{prefix}:{normalize_qc_reason_token(reason_token)}'
    return SubtitleQCResult(
        passed=passed,
        score=float(rule_score),
        reason=reason,
        rule_score=float(rule_score),
        ai_score=None,
        raw_ai={
            'decision': 'needs_ai',
            'ai_status': normalize_qc_reason_token(reason_token),
            'ai_unavailable_strict': bool(strict),
            **metrics,
        },
        decision='needs_ai',
        sample_items=int(sample_meta.get('sample_items', 0) or 0),
        sample_chars=int(sample_meta.get('sample_chars', 0) or 0),
    )


def run_subtitle_qc(
    srt_path: str,
    config: Dict[str, Any],
    threshold: Optional[float] = None,
    total_duration_s: Optional[float] = None,
    strict: bool = False,
) -> SubtitleQCResult:
    """对 ASR 生成的 SRT 做预检。失败时跳过字幕使用，但保留字幕文件并继续上传原视频。

    ``total_duration_s`` 提供视频总时长后，质检会额外校验时间轴维度
    （覆盖率、尾部截断、首条起点、最大空档、重叠、语速异常、重复刷屏）。
    取不到时自动跳过这些维度，不影响既有文本维度判定。
    ``strict=True`` 用于 ASR 来源退化的场景：AI 不可用时不再放行。
    """
    max_items = _to_int(config.get('SUBTITLE_QC_SAMPLE_MAX_ITEMS', 80), 80)
    max_chars = _to_int(config.get('SUBTITLE_QC_MAX_CHARS', 9000), 9000)

    threshold_val = threshold
    if threshold_val is None:
        threshold_val = _to_float(config.get('SUBTITLE_QC_THRESHOLD', 0.60), 0.60)

    # 时间轴维度开关与阈值：允许用户收紧/放宽，未配置时用模块默认。
    timeline_enabled = _to_bool(config.get('SUBTITLE_QC_TIMELINE_ENABLED', True), True)
    timeline_limits = {
        'min_coverage_ratio': _to_float(
            config.get('SUBTITLE_QC_MIN_COVERAGE_RATIO', TIMELINE_MIN_COVERAGE_RATIO),
            TIMELINE_MIN_COVERAGE_RATIO,
        ),
        'max_gap_seconds': _to_float(
            config.get('SUBTITLE_QC_MAX_GAP_S', TIMELINE_MAX_GAP_SECONDS),
            TIMELINE_MAX_GAP_SECONDS,
        ),
        'max_cps': _to_float(
            config.get('SUBTITLE_QC_MAX_CPS', TIMELINE_CPS_UPPER),
            TIMELINE_CPS_UPPER,
        ),
    }

    items = _read_srt_items(srt_path)
    rule_result = _rule_check(
        items,
        total_duration_s=total_duration_s if timeline_enabled else None,
        timeline_limits=timeline_limits,
    )
    if not timeline_enabled:
        rule_result.metrics['timeline_checked'] = False
        rule_result.metrics['timeline_skipped'] = True
        rule_result.metrics['timeline_disabled_by_config'] = True
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
            strict=strict,
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
            strict=strict,
        )

    ai_passed, ai_score, raw_ai, ai_status = _call_ai_judge(sample_text, metrics=metrics, config=config)
    if ai_status != 'ok':
        return _resolve_ai_unavailable_result(
            rule_result=rule_result,
            metrics=metrics,
            rule_score=float(rule_result.score),
            reason_token=ai_status,
            sample_meta=sample_meta,
            strict=strict,
        )

    raw_reason = ''
    if raw_ai and isinstance(raw_ai, dict):
        raw_reason = normalize_qc_reason_token(raw_ai.get('reason') or '')
    advisory_mode = _is_high_rule_score_clean_boundary_sample(rule_result) and not strict
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
        elif ai_score is not None and float(ai_score) < ADVISORY_OVERRIDE_MIN_AI_SCORE:
            # AI 给出强否定（低分）时禁止 advisory 覆盖：advisory 判据全为文本维度，
            # 无法识别时间轴崩坏，不能被用来推翻 AI 的明确否决。
            final_score = float(ai_score)
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
