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
    "VAD_MIN_SPEECH_COVERAGE_RATIO": 0.01,
    "VAD_DROP_ISOLATED_SHORT": True,
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
    # 留空时继承全局 OpenAI 地址；创建 ASR 客户端前会移除生成端点后缀。
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
    "VAD_MIN_SPEECH_COVERAGE_RATIO": 0.01,
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
    "SUBTITLE_PREFER_SINGLE_LINE": True,
    "SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE": 0.60,
    # 字幕质量硬上限（防止「文本干净但节奏崩坏」的合并产物落到成片）
    "SUBTITLE_MAX_CUE_DURATION_S": 8.0,
    "SUBTITLE_MAX_CPS": 20.0,
    # ASR 来源退化时是否禁止烧录（False 时退回「仅记录警告后照常烧录」的旧行为）
    "ASR_FAILURE_BLOCKS_EMBED": True,
    # 字幕质检的时间轴维度（需要视频总时长才生效）
    "SUBTITLE_QC_TIMELINE_ENABLED": True,
    "SUBTITLE_QC_MIN_COVERAGE_RATIO": 0.15,
    "SUBTITLE_QC_MAX_GAP_S": 90.0,
    "SUBTITLE_QC_MAX_CPS": 25.0,
    # 翻译配对与批次预算
    "SUBTITLE_TRANSLATION_ALLOW_PARTIAL": False,
    "SUBTITLE_TRANSLATION_MAX_CHARS_PER_BATCH": 2000,
    # Whisper 解码质量参数（抑制幻觉与时间戳漂移）
    "WHISPER_TEMPERATURE": 0.0,
    "WHISPER_NO_SPEECH_THRESHOLD": 0.6,
    "WHISPER_CONDITION_ON_PREVIOUS_TEXT": False,
    # VAD 孤立极短段处理
    "VAD_DROP_ISOLATED_SHORT": True,
    # AI 智能分段（基于字级时间戳的语义重分段）
    "AI_SEGMENTATION_ENABLED": False,  # 总开关；需 ASR 支持字级时间戳（如 parakeet-crispasr），否则自动降级
    "AI_SEGMENTATION_BASE_URL": "",  # 留空则回退到 OPENAI_BASE_URL
    "AI_SEGMENTATION_API_KEY": "",  # 留空则回退到 OPENAI_API_KEY
    "AI_SEGMENTATION_MODEL_NAME": "",  # 留空则回退到 OPENAI_MODEL_NAME
    "AI_SEGMENTATION_THINKING_ENABLED": False,  # 智能分段模型独立思考开关
    "AI_SEGMENTATION_MIN_CUE_DURATION_S": 1.5,  # 最短显示时长（秒），低于此值合并/延长（电影级字幕节奏下限）
    "AI_SEGMENTATION_MAX_CUE_DURATION_S": 5.0,  # 最长段长（秒），超过则拆分（避免单条过长）
    "AI_SEGMENTATION_MAX_CPS": 15.0,  # 最大每秒可见字符数（中文友好），超过则拆分
    "AI_SEGMENTATION_BATCH_WINDOW_S": 120.0,  # VAD 窗口合并目标时长（秒），长上下文利于分段准确性
    "AI_SEGMENTATION_MAX_CHARS_PER_BATCH": 4000,  # 单批次送检最大字符数硬上限，超限则进一步拆分
    "AI_SEGMENTATION_TEMPERATURE": 0.1,  # AI 分段采样温度（低温度更稳定）
    "AI_SEGMENTATION_MAX_RETRIES": 2,  # 单批次 AI 失败重试次数（仍失败则降级）
    "AI_SEGMENTATION_CONTEXT_WINDOW": 3,  # 滑动上下文窗口：前一批次末尾 N 条 cue 注入下一批 prompt
    "AI_SEGMENTATION_BOUNDARY_REFINE_ENABLED": False,  # 边界精炼：索引制下通常不需要，可手动开启
    "AI_SEGMENTATION_BOUNDARY_WINDOW": 3,  # 边界精炼：每侧取 N 条 cue 进行审视
    "AI_SEGMENTATION_RHYTHM_ENABLED": False,  # 节奏后处理（合并过短/拆分过长）；关闭时直接信任 AI 分段结果
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
    'SUBTITLE_PREFER_SINGLE_LINE',
    'WHISPER_TRANSLATE',
    'WHISPER_CONDITION_ON_PREVIOUS_TEXT',
    'VOXTRAL_DIARIZE',
    'VOXTRAL_ENFORCE_MAX_DURATION',
    'ASR_FAILURE_BLOCKS_EMBED',
    'SUBTITLE_QC_TIMELINE_ENABLED',
    'SUBTITLE_TRANSLATION_ALLOW_PARTIAL',
    'VAD_DROP_ISOLATED_SHORT',
    'AI_SEGMENTATION_ENABLED',
    'AI_SEGMENTATION_THINKING_ENABLED',
    'AI_SEGMENTATION_RHYTHM_ENABLED',
    'AI_SEGMENTATION_BOUNDARY_REFINE_ENABLED',
]

# 以下两张表只声明"哪些键需要做整数 / 浮点归一化"（消费方仅取键集合）。
# 数值一律从 SPEECH_PIPELINE_DEFAULTS 派生，避免同一键在本文件内出现两套默认值
# （此前 SUBTITLE_MAX_LINE_LENGTH 在此为 999、在 SPEECH_PIPELINE_DEFAULTS 为 1 等
# 自相矛盾，导致「是否保存过一次设置页」会切换 SRT 换行行为）。
# 设置页保存失败时的回退值一律由 config_manager.DEFAULT_CONFIG 提供。
SPEECH_PIPELINE_INT_FIELDS = (
    'VAD_SILERO_MIN_SPEECH_MS',
    'VAD_SILERO_MIN_SILENCE_MS',
    'VAD_SILERO_MAX_SPEECH_S',
    'VAD_SILERO_SPEECH_PAD_MS',
    'WHISPER_MAX_WORKERS',
    'WHISPER_MAX_RETRIES',
    'VOXTRAL_MAX_AUDIO_DURATION_S',
    'VOXTRAL_LONG_AUDIO_MARGIN_S',
    'SUBTITLE_MAX_LINE_LENGTH',
    'SUBTITLE_MAX_LINES',
    'SUBTITLE_MIN_TEXT_LENGTH',
    'SUBTITLE_TRANSLATION_MAX_CHARS_PER_BATCH',
    'AI_SEGMENTATION_MAX_CHARS_PER_BATCH',
    'AI_SEGMENTATION_CONTEXT_WINDOW',
    'AI_SEGMENTATION_BOUNDARY_WINDOW',
    'AI_SEGMENTATION_MAX_RETRIES',
)

# 同 SPEECH_PIPELINE_INT_FIELDS：只声明键集合，数值由 SPEECH_PIPELINE_DEFAULTS 提供。
SPEECH_PIPELINE_FLOAT_FIELDS = (
    'VAD_SILERO_THRESHOLD',
    'VAD_MAX_SEGMENT_S',
    'AUDIO_CHUNK_WINDOW_S',
    'AUDIO_CHUNK_OVERLAP_S',
    'VAD_MERGE_GAP_S',
    'VAD_MIN_SEGMENT_S',
    'VAD_MAX_SEGMENT_S_FOR_SPLIT',
    'VAD_MIN_SPEECH_COVERAGE_RATIO',
    'WHISPER_RETRY_DELAY_S',
    'WHISPER_TEMPERATURE',
    'WHISPER_NO_SPEECH_THRESHOLD',
    'SUBTITLE_TIME_OFFSET_S',
    'SUBTITLE_MIN_CUE_DURATION_S',
    'SUBTITLE_MERGE_GAP_S',
    'SUBTITLE_MAX_CUE_DURATION_S',
    'SUBTITLE_MAX_CPS',
    'SUBTITLE_QC_MIN_COVERAGE_RATIO',
    'SUBTITLE_QC_MAX_GAP_S',
    'SUBTITLE_QC_MAX_CPS',
    'SUBTITLE_SINGLE_LINE_MIN_FONT_SCALE',
    'AI_SEGMENTATION_MIN_CUE_DURATION_S',
    'AI_SEGMENTATION_MAX_CUE_DURATION_S',
    'AI_SEGMENTATION_MAX_CPS',
    'AI_SEGMENTATION_BATCH_WINDOW_S',
    'AI_SEGMENTATION_TEMPERATURE',
)


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


# 旧设置页用 hidden 输入把 SUBTITLE_MAX_LINE_LENGTH / SUBTITLE_MAX_LINES 钉死为
# 999 / 1，并把两个 _ENABLED 强制写为 on —— 这个组合用户主动选不出来（控件当时
# 是 disabled 的），只可能是旧模板写入的。它会让 SRT 交付物永不换行、单行超长。
# 只要四值同时命中该组合，就视为"从未真正选择过"，迁移回可换行的默认值。
LEGACY_PINNED_SUBTITLE_WRAP = {
    'SUBTITLE_MAX_LINE_LENGTH': 999,
    'SUBTITLE_MAX_LINES': 1,
    'SUBTITLE_MAX_LINE_LENGTH_ENABLED': True,
    'SUBTITLE_MAX_LINES_ENABLED': True,
}


def migrate_pinned_subtitle_wrap_config(config: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """把旧模板钉死的 999/1 换行组合迁移回默认的 42/2。

    同一组合若走设置页提交（app.py 的数值范围校验）也会被整组回退到
    42/2/False/False：那里只有越界的 999 会被单独拦截，SUBTITLE_MAX_LINES=1
    落在合法区间会被保留，落盘成 42/1/True/True，与本函数结果不一致，
    因此设置页侧补了同样的整组归一。两条路径的结果必须一致，改动任一侧时
    请同步另一侧（tests/test_settings_guards.py 有跨路径一致性断言）。
    """
    updated = dict(config or {})
    if not updated:
        return updated, False
    if not all(
        _matches_default_value(updated.get(key), value)
        for key, value in LEGACY_PINNED_SUBTITLE_WRAP.items()
    ):
        return updated, False
    for key in LEGACY_PINNED_SUBTITLE_WRAP:
        updated[key] = SPEECH_PIPELINE_DEFAULTS[key]
    return updated, True
