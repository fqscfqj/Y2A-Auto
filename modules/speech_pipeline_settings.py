#!/usr/bin/env python
# -*- coding: utf-8 -*-

from typing import Any, Dict, Tuple


QUALITY_FIRST_SPEECH_DEFAULTS: Dict[str, Any] = {
    "VAD_ENABLED": True,
    "VAD_SILERO_MIN_SILENCE_MS": 320,
    "VAD_SILERO_SPEECH_PAD_MS": 120,
    "VAD_MAX_SEGMENT_S": 15.0,
    "AUDIO_CHUNK_WINDOW_S": 15.0,
    "AUDIO_CHUNK_OVERLAP_S": 0.4,
    "VAD_MERGE_GAP_S": 0.35,
    "VAD_MIN_SEGMENT_S": 0.8,
    "VAD_MAX_SEGMENT_S_FOR_SPLIT": 15.0,
    "VAD_REFINEMENT_ENABLED": True,
    "VAD_MIN_SPEECH_COVERAGE_RATIO": 0.015,
}

LEGACY_SPEECH_DEFAULTS_FOR_MIGRATION: Dict[str, Any] = {
    "VAD_ENABLED": False,
    "VAD_SILERO_MIN_SILENCE_MS": 500,
    "VAD_SILERO_SPEECH_PAD_MS": 500,
    "VAD_MAX_SEGMENT_S": 30.0,
    "AUDIO_CHUNK_WINDOW_S": 30.0,
    "AUDIO_CHUNK_OVERLAP_S": 0.4,
    "VAD_MERGE_GAP_S": 1.0,
    "VAD_MIN_SEGMENT_S": 1.0,
    "VAD_MAX_SEGMENT_S_FOR_SPLIT": 30.0,
}


SPEECH_PIPELINE_DEFAULTS: Dict[str, Any] = {
    "SPEECH_RECOGNITION_ENABLED": False,
    "SPEECH_RECOGNITION_PROVIDER": "whisper",
    "WHISPER_API_KEY": "",
    "WHISPER_BASE_URL": "",
    "WHISPER_MODEL_NAME": "whisper-1",
    "WHISPER_TIMESTAMP_GRANULARITIES": "segment,word",
    "VOXTRAL_API_KEY": "",
    "VOXTRAL_BASE_URL": "https://api.mistral.ai/v1",
    "VOXTRAL_MODEL_NAME": "voxtral-mini-latest",
    "VOXTRAL_TIMESTAMP_GRANULARITIES": "segment,word",
    "VOXTRAL_DIARIZE": False,
    "VOXTRAL_CONTEXT_BIAS": "",
    "VOXTRAL_LANGUAGE": "",
    "VOXTRAL_MAX_AUDIO_DURATION_S": 10800,
    "VOXTRAL_LONG_AUDIO_MARGIN_S": 5,
    "VOXTRAL_ENFORCE_MAX_DURATION": True,
    "VAD_ENABLED": True,
    "VAD_PROVIDER": "silero-vad",
    "VAD_SILERO_THRESHOLD": 0.55,
    "VAD_SILERO_MIN_SPEECH_MS": 300,
    "VAD_SILERO_MIN_SILENCE_MS": 320,
    "VAD_SILERO_MAX_SPEECH_S": 120,
    "VAD_SILERO_SPEECH_PAD_MS": 120,
    "VAD_MAX_SEGMENT_S": 15.0,
    "AUDIO_CHUNK_WINDOW_S": 15.0,
    "AUDIO_CHUNK_OVERLAP_S": 0.4,
    "VAD_MERGE_GAP_S": 0.35,
    "VAD_MIN_SEGMENT_S": 0.8,
    "VAD_MAX_SEGMENT_S_FOR_SPLIT": 15.0,
    "VAD_REFINEMENT_ENABLED": True,
    "VAD_MIN_SPEECH_COVERAGE_RATIO": 0.015,
    "WHISPER_LANGUAGE": "",
    "WHISPER_PROMPT": "",
    "WHISPER_TRANSLATE": False,
    "WHISPER_MAX_WORKERS": 3,
    "WHISPER_MAX_RETRIES": 3,
    "WHISPER_RETRY_DELAY_S": 2.0,
    "SUBTITLE_MAX_LINE_LENGTH": 42,
    "SUBTITLE_MAX_LINES": 2,
    "SUBTITLE_NORMALIZE_PUNCTUATION": True,
    "SUBTITLE_FILTER_FILLER_WORDS": False,
    "SUBTITLE_TIME_OFFSET_S": 0.0,
    "SUBTITLE_MIN_CUE_DURATION_S": 0.6,
    "SUBTITLE_MERGE_GAP_S": 0.3,
    "SUBTITLE_MIN_TEXT_LENGTH": 2,
    "SUBTITLE_TIME_OFFSET_ENABLED": False,
    "SUBTITLE_MIN_CUE_DURATION_ENABLED": False,
    "SUBTITLE_MERGE_GAP_ENABLED": False,
    "SUBTITLE_MIN_TEXT_LENGTH_ENABLED": False,
    "SUBTITLE_MAX_LINE_LENGTH_ENABLED": False,
    "SUBTITLE_MAX_LINES_ENABLED": False,
    # AI 智能分段（基于字级时间戳的语义重分段）
    "AI_SEGMENTATION_ENABLED": False,  # 总开关；需 ASR 支持字级时间戳（如 parakeet-crispasr），否则自动降级
    "AI_SEGMENTATION_BASE_URL": "",  # 留空则回退到 OPENAI_BASE_URL
    "AI_SEGMENTATION_API_KEY": "",  # 留空则回退到 OPENAI_API_KEY
    "AI_SEGMENTATION_MODEL_NAME": "",  # 留空则回退到 OPENAI_MODEL_NAME
    "AI_SEGMENTATION_THINKING_ENABLED": False,  # 智能分段模型独立思考开关
    "AI_SEGMENTATION_MIN_CUE_DURATION_S": 0.8,  # 最短显示时长（秒），低于此值合并/延长
    "AI_SEGMENTATION_MAX_CUE_DURATION_S": 7.0,  # 最长段长（秒），超过则拆分
    "AI_SEGMENTATION_MAX_CPS": 18.0,  # 最大每秒可见字符数（中文友好），超过则拆分
    "AI_SEGMENTATION_BATCH_WINDOW_S": 120.0,  # VAD 窗口合并目标时长（秒），长上下文利于分段准确性
    "AI_SEGMENTATION_MAX_CHARS_PER_BATCH": 8000,  # 单批次送检最大字符数硬上限，超限则进一步拆分
    "AI_SEGMENTATION_TEMPERATURE": 0.2,  # AI 分段采样温度（低温度更稳定）
    "AI_SEGMENTATION_MAX_RETRIES": 2,  # 单批次 AI 失败重试次数（仍失败则降级）
}

SPEECH_PIPELINE_CHECKBOXES = [
    'SPEECH_RECOGNITION_ENABLED',
    'VAD_ENABLED',
    'VAD_REFINEMENT_ENABLED',
    'SUBTITLE_NORMALIZE_PUNCTUATION',
    'SUBTITLE_FILTER_FILLER_WORDS',
    'SUBTITLE_TIME_OFFSET_ENABLED',
    'SUBTITLE_MIN_CUE_DURATION_ENABLED',
    'SUBTITLE_MERGE_GAP_ENABLED',
    'SUBTITLE_MIN_TEXT_LENGTH_ENABLED',
    'SUBTITLE_MAX_LINE_LENGTH_ENABLED',
    'SUBTITLE_MAX_LINES_ENABLED',
    'WHISPER_TRANSLATE',
    'VOXTRAL_DIARIZE',
    'VOXTRAL_ENFORCE_MAX_DURATION',
    'AI_SEGMENTATION_ENABLED',
    'AI_SEGMENTATION_THINKING_ENABLED',
]

SPEECH_PIPELINE_INT_FIELDS = {
    'VAD_SILERO_MIN_SPEECH_MS': 300,
    'VAD_SILERO_MIN_SILENCE_MS': 320,
    'VAD_SILERO_MAX_SPEECH_S': 120,
    'VAD_SILERO_SPEECH_PAD_MS': 120,
    'WHISPER_MAX_WORKERS': 3,
    'WHISPER_MAX_RETRIES': 3,
    'VOXTRAL_MAX_AUDIO_DURATION_S': 10800,
    'VOXTRAL_LONG_AUDIO_MARGIN_S': 5,
    'SUBTITLE_MAX_LINE_LENGTH': 42,
    'SUBTITLE_MAX_LINES': 2,
    'SUBTITLE_MIN_TEXT_LENGTH': 2,
    'AI_SEGMENTATION_MAX_CHARS_PER_BATCH': 8000,
    'AI_SEGMENTATION_MAX_RETRIES': 2,
}

SPEECH_PIPELINE_FLOAT_FIELDS = {
    'VAD_SILERO_THRESHOLD': 0.55,
    'VAD_MAX_SEGMENT_S': 15.0,
    'AUDIO_CHUNK_WINDOW_S': 15.0,
    'AUDIO_CHUNK_OVERLAP_S': 0.4,
    'VAD_MERGE_GAP_S': 0.35,
    'VAD_MIN_SEGMENT_S': 0.8,
    'VAD_MAX_SEGMENT_S_FOR_SPLIT': 15.0,
    'VAD_MIN_SPEECH_COVERAGE_RATIO': 0.015,
    'WHISPER_RETRY_DELAY_S': 2.0,
    'SUBTITLE_TIME_OFFSET_S': 0.0,
    'SUBTITLE_MIN_CUE_DURATION_S': 0.6,
    'SUBTITLE_MERGE_GAP_S': 0.3,
    'AI_SEGMENTATION_MIN_CUE_DURATION_S': 0.8,
    'AI_SEGMENTATION_MAX_CUE_DURATION_S': 7.0,
    'AI_SEGMENTATION_MAX_CPS': 18.0,
    'AI_SEGMENTATION_BATCH_WINDOW_S': 120.0,
    'AI_SEGMENTATION_TEMPERATURE': 0.2,
}


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ('true', '1', 'on', 'yes')


def inject_speech_pipeline_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    updated = dict(config or {})
    for key, value in SPEECH_PIPELINE_DEFAULTS.items():
        updated.setdefault(key, value)
    return updated


def _matches_default_value(current: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return coerce_bool(current) == expected
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            return int(float(current)) == expected
        except Exception:
            return False
    if isinstance(expected, float):
        try:
            return abs(float(current) - expected) < 1e-9
        except Exception:
            return False
    return current == expected


def migrate_legacy_speech_pipeline_config(config: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    updated = dict(config or {})
    if not updated:
        return updated, False

    tracked_keys = [
        key for key in LEGACY_SPEECH_DEFAULTS_FOR_MIGRATION
        if key in updated
    ]
    if not tracked_keys:
        return updated, False

    if not all(
        _matches_default_value(updated.get(key), LEGACY_SPEECH_DEFAULTS_FOR_MIGRATION[key])
        for key in tracked_keys
    ):
        return updated, False

    migrated = False
    for key, value in QUALITY_FIRST_SPEECH_DEFAULTS.items():
        if key in LEGACY_SPEECH_DEFAULTS_FOR_MIGRATION and _matches_default_value(
            updated.get(key),
            LEGACY_SPEECH_DEFAULTS_FOR_MIGRATION[key],
        ):
            updated[key] = value
            migrated = True
    return updated, migrated
