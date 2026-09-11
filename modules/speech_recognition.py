#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

from .asr_api_client import AsrApiClient, AsrConfig
from .ffmpeg_manager import get_ffmpeg_path, get_ffprobe_path
from .speech_pipeline_settings import coerce_bool
from .srt_transform_engine import SrtTransformConfig, SrtTransformEngine
from .subtitle_pipeline_types import (
    AlignedSubtitleCue,
    AsrTranscriptionResult,
    DetectedSpeechWindow,
)
from .vad_processor import VadConfig, VadProcessor

try:  # 默认值统一从配置中心取，避免同一键在多处硬编码后分叉
    from .config_manager import DEFAULT_CONFIG as _DEFAULT_CONFIG
except Exception:  # pragma: no cover - 防御性兜底
    _DEFAULT_CONFIG = {}

_SUBTITLE_DEFAULT_LINE_LENGTH = int(_DEFAULT_CONFIG.get('SUBTITLE_MAX_LINE_LENGTH', 42) or 42)
_SUBTITLE_DEFAULT_LINES = int(_DEFAULT_CONFIG.get('SUBTITLE_MAX_LINES', 2) or 2)

try:  # AI 智能分段为可选增强，导入失败不应阻断语音识别主流程
    from .ai_segmentation import AISegmentationConfig, AISegmenter, AISegmentationError
except Exception:  # pragma: no cover - 防御性兜底
    AISegmentationConfig = None  # type: ignore[assignment]
    AISegmenter = None  # type: ignore[assignment]
    AISegmentationError = Exception  # type: ignore[assignment]


def _config_float(config: Dict[str, Any], key: str, default: float) -> float:
    value = config.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _config_positive_float(config: Dict[str, Any], key: str, default: float) -> float:
    value = _config_float(config, key, default)
    return value if value > 0.0 else default


def _config_int(config: Dict[str, Any], key: str, default: int) -> int:
    value = config.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ASR/VAD 警告 token → 质量结局分类。烧录侧据此决定是否允许把字幕烧进成片。
# failed：来源不可信或无有效产出，禁止烧录；
# degraded：有产出但来源退化，必须通过严格质检（含时间轴维度）才允许烧录。
_QUALITY_FAILED_TOKEN_PREFIXES = (
    'vad_no_speech',
    'asr_failed',
    'no subtitles generated',
    'no cues remaining after post-processing',
    'failed to render srt',
)
_QUALITY_DEGRADED_TOKEN_PREFIXES = (
    'vad_failed',
    'vad_no_usable_window',
    'vad_partial_chunks',
    'vad_low_coverage',
    'asr_no_timestamps',
)
# 窗口级 ASR 失败占比阈值：达到该值即视为来源退化，全部失败即视为不可信。
_QUALITY_DEGRADED_FAILURE_RATIO = 0.5
# 送往 ASR 的最小窗口时长：过短窗口只会产出噪声或幻觉。
_MIN_ASR_WINDOW_S = 0.4
# 相邻窗口间隔小于该值时先合并，避免把同一句话拆成两次 ASR 请求。
_PRE_MERGE_WINDOW_GAP_S = 0.15


def _setup_task_logger(task_id: str) -> logging.Logger:
    from .utils import get_app_subdir
    from logging.handlers import RotatingFileHandler

    log_dir = get_app_subdir('logs')
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger(f'speech_recognition_{task_id}')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(
            os.path.join(log_dir, f'task_{task_id}.log'),
            maxBytes=10_485_760,
            backupCount=5,
            encoding='utf-8',
        )
        handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)
        logger.propagate = False
    return logger


@dataclass
class SpeechRecognitionConfig:
    provider: str = 'whisper'
    api_provider: str = 'whisper'
    api_key: str = ''
    base_url: str = ''
    model_name: str = 'whisper-1'
    vad_enabled: bool = True
    vad_provider: str = 'silero-vad'
    vad_threshold: float = 0.55
    vad_min_speech_ms: int = 300
    vad_min_silence_ms: int = 320
    vad_max_speech_s: int = 120
    vad_speech_pad_ms: int = 120
    chunk_window_s: float = 15.0
    chunk_overlap_s: float = 0.4
    vad_merge_gap_s: float = 0.35
    vad_min_segment_s: float = 0.8
    vad_max_segment_s: float = 15.0
    vad_max_segment_s_for_split: float = 15.0
    vad_refinement_enabled: bool = True
    vad_min_speech_coverage_ratio: float = 0.015
    # 孤立极短段（合并不进任何相邻窗口的碎片）是否丢弃。该开关为既有 UI 项，
    # 此前未接入配置对象导致勾选与否行为不变，故在此显式承载。
    vad_drop_isolated_short: bool = True
    language: str = ''
    prompt: str = ''
    translate: bool = False
    whisper_timestamp_granularities: str = 'segment,word'
    voxtral_timestamp_granularities: str = 'segment,word'
    voxtral_diarize: bool = False
    voxtral_context_bias: str = ''
    voxtral_max_audio_duration_s: float = 10800.0
    voxtral_long_audio_margin_s: float = 5.0
    voxtral_enforce_max_duration: bool = True
    # Whisper 解码质量参数（OpenAI Whisper API 的合法 form 字段）。
    # 缺失时幻觉与时间戳漂移只能靠后处理猜，故给出保守默认值。
    whisper_temperature: Optional[float] = 0.0
    whisper_condition_on_previous_text: bool = False
    whisper_no_speech_threshold: float = 0.6
    max_workers: int = 3
    max_subtitle_line_length: int = 42
    max_subtitle_lines: int = 2
    # 字幕质量硬上限：单条 cue 最长时长与每秒最大字符数。两者是后处理链的
    # 必要组成，必须随运行时配置变化，不得在导入期被固化成常量。
    subtitle_max_cue_duration_s: float = 8.0
    subtitle_max_cps: float = 20.0
    normalize_punctuation: bool = True
    filter_filler_words: bool = False
    subtitle_time_offset_s: float = 0.0
    subtitle_min_cue_duration_s: float = 0.6
    subtitle_merge_gap_s: float = 0.3
    subtitle_min_text_length: int = 2
    subtitle_time_offset_enabled: bool = False
    subtitle_min_cue_duration_enabled: bool = False
    subtitle_merge_gap_enabled: bool = False
    subtitle_min_text_length_enabled: bool = False
    subtitle_max_line_length_enabled: bool = False
    subtitle_max_lines_enabled: bool = False
    max_retries: int = 3
    retry_delay_s: float = 2.0
    request_timeout_s: float = 300.0
    # AI 智能分段（基于字级时间戳的语义重分段）
    ai_segmentation_enabled: bool = False
    ai_segmentation_config: Optional[Any] = None


class SpeechRecognizer:
    def __init__(self, config: SpeechRecognitionConfig, task_id: Optional[str] = None):
        self.config = config
        self.task_id = task_id or 'unknown'
        self.logger = _setup_task_logger(self.task_id)
        self.last_warning_message: str = ''
        self.last_error_message: str = ''
        # 结构化质量结局：ok / degraded / failed，以及触发降级的具体原因列表。
        self.last_quality_state: str = 'ok'
        self.last_degraded_reasons: List[str] = []
        self._temp_dirs: List[str] = []
        # 本轮 VAD 命中的实际语音区间。幻觉清洗需要它判定「该 cue 落在静音段」，
        # 否则静音段重复检测在生产路径上恒不触发。
        self._last_vad_spans: Tuple[Tuple[float, float], ...] = ()

        if config.provider not in ('whisper', 'voxtral'):
            raise ValueError(f"Unsupported speech recognition provider: {config.provider}")

        self._vad = VadProcessor(
            VadConfig(
                provider=config.vad_provider,
                threshold=config.vad_threshold,
                min_speech_ms=config.vad_min_speech_ms,
                min_silence_ms=config.vad_min_silence_ms,
                max_speech_s=config.vad_max_speech_s,
                speech_pad_ms=config.vad_speech_pad_ms,
                chunk_window_s=config.chunk_window_s,
                chunk_overlap_s=config.chunk_overlap_s,
                merge_gap_s=config.vad_merge_gap_s,
                min_segment_s=config.vad_min_segment_s,
                max_segment_s=config.vad_max_segment_s,
                max_segment_s_for_split=config.vad_max_segment_s_for_split,
                refinement_enabled=config.vad_refinement_enabled,
                min_speech_coverage_ratio=config.vad_min_speech_coverage_ratio,
                drop_isolated_short=config.vad_drop_isolated_short,
            ),
            logger=self.logger,
        )
        self._asr = AsrApiClient(
            AsrConfig(
                provider=config.api_provider,
                api_key=config.api_key,
                base_url=config.base_url,
                model_name=config.model_name,
                language=config.language,
                prompt=config.prompt,
                translate=config.translate,
                timestamp_granularities=(
                    config.voxtral_timestamp_granularities
                    if config.api_provider == 'voxtral'
                    else config.whisper_timestamp_granularities
                ),
                diarize=config.voxtral_diarize,
                context_bias=config.voxtral_context_bias,
                max_retries=config.max_retries,
                retry_delay_s=config.retry_delay_s,
                max_workers=config.max_workers,
                request_timeout_s=config.request_timeout_s,
                voxtral_max_audio_duration_s=config.voxtral_max_audio_duration_s,
                voxtral_enforce_max_duration=config.voxtral_enforce_max_duration,
                # Whisper 解码质量参数：抑制幻觉与时间戳漂移
                temperature=config.whisper_temperature,
                condition_on_previous_text=config.whisper_condition_on_previous_text,
                no_speech_threshold=config.whisper_no_speech_threshold,
            ),
            logger=self.logger,
        )
        self._srt = SrtTransformEngine(
            SrtTransformConfig(
                max_line_length=(
                    config.max_subtitle_line_length
                    if config.subtitle_max_line_length_enabled
                    else _SUBTITLE_DEFAULT_LINE_LENGTH
                ),
                max_lines=(
                    config.max_subtitle_lines
                    if config.subtitle_max_lines_enabled
                    else _SUBTITLE_DEFAULT_LINES
                ),
                normalize_punctuation=config.normalize_punctuation,
                filter_filler_words=config.filter_filler_words,
                time_offset_s=config.subtitle_time_offset_s if config.subtitle_time_offset_enabled else 0.0,
                min_cue_duration_s=config.subtitle_min_cue_duration_s if config.subtitle_min_cue_duration_enabled else 0.6,
                merge_gap_s=config.subtitle_merge_gap_s if config.subtitle_merge_gap_enabled else 0.3,
                min_text_length=config.subtitle_min_text_length if config.subtitle_min_text_length_enabled else 2,
                # 字幕质量硬上限从运行时配置对象读取，而非导入期常量，
                # 否则改 config.json / overrides / 设置页三条路径全部失效。
                max_cue_duration_s=config.subtitle_max_cue_duration_s,
                max_chars_per_second=config.subtitle_max_cps,
            ),
            logger=self.logger,
        )

    def transcribe_video_to_subtitles(self, video_path: str, output_path: str) -> Optional[str]:
        try:
            self.last_warning_message = ''
            self.last_error_message = ''
            self.last_quality_state = 'ok'
            self.last_degraded_reasons = []
            self._last_vad_spans = ()

            if not self._asr.client:
                self.last_error_message = 'ASR client not initialised'
                return None
            if not os.path.exists(video_path):
                self.last_error_message = f"Video file not found: {video_path}"
                return None

            audio_wav = self._extract_audio_wav(video_path)
            if not audio_wav:
                self.last_error_message = 'Audio extraction failed'
                return None

            total_duration = self._probe_media_duration(audio_wav)
            if total_duration is None:
                total_duration = self._probe_media_duration(video_path) or 0.0

            cues: List[AlignedSubtitleCue] = []
            if self.config.vad_enabled:
                cues = self._transcribe_with_vad(audio_wav, total_duration)

            if not cues:
                cues = self._fallback_transcription(audio_wav, total_duration)
            if not cues:
                self.last_error_message = self.last_error_message or 'No subtitles generated'
                return None

            # 透传 VAD 语音区间：srt_transform 的「静音段重复上一句」判定依赖它，
            # 此前生产路径恒不传参，该分支永不执行。无 VAD 数据（未启用/失败/全量
            # fallback）时保持 None，不伪造区间。
            cue_dicts = self._srt.clean_hallucinations(
                cues,
                speech_spans=self._last_vad_spans or None,
            )
            cue_dicts = self._srt.resolve_overlaps(cue_dicts, total_duration)
            cue_dicts = self._srt.apply_text_processing(cue_dicts)
            cue_dicts = self._srt.finalize_cues(cue_dicts, total_duration)
            if not cue_dicts:
                self.last_error_message = 'No cues remaining after post-processing'
                return None

            srt_text = self._srt.render_srt(cue_dicts)
            if not srt_text:
                self.last_error_message = 'Failed to render SRT'
                return None

            with open(output_path, 'w', encoding='utf-8') as file_obj:
                file_obj.write(srt_text)
            self.logger.info("Subtitle range: %.2fs -> %.2fs", cue_dicts[0]['start'], cue_dicts[-1]['end'])
            return output_path
        except Exception as exc:
            self.last_error_message = self.last_error_message or f"转录失败: {exc}"
            self.logger.exception("Transcription failed")
            return None
        finally:
            self._resolve_quality_state(output_path)
            self._cleanup_temp_files()

    def _resolve_quality_state(self, output_path: Optional[str]) -> None:
        """按实际产出与警告 token 裁决本次转录的质量结局。

        这是「VAD/ASR 已报错却照样烧录」的修复核心：烧录侧读取
        ``last_quality_state``，``failed`` 直接拒绝烧录，``degraded`` 必须先
        通过严格质检（含时间轴维度）。
        """
        self.last_quality_state = 'ok'
        self.last_degraded_reasons = []

        produced = bool(output_path) and os.path.exists(str(output_path))
        tokens: List[str] = []
        for raw in (self.last_warning_message, self.last_error_message):
            text = str(raw or '').strip()
            if text:
                tokens.append(text)

        failed_reasons = [
            token for token in tokens
            if any(token.lower().startswith(prefix) for prefix in _QUALITY_FAILED_TOKEN_PREFIXES)
        ]
        degraded_reasons = [
            token for token in tokens
            if any(token.lower().startswith(prefix) for prefix in _QUALITY_DEGRADED_TOKEN_PREFIXES)
        ]

        # 窗口级失败占比：全部失败视为不可信，过半视为退化。
        failure_ratio = float(getattr(self._asr, 'last_failure_ratio', 0.0) or 0.0)
        window_count = int(getattr(self._asr, 'last_window_count', 0) or 0)
        if window_count > 0 and failure_ratio >= 1.0:
            failed_reasons.append(f"asr_all_windows_failed: {failure_ratio:.2f}")
        elif failure_ratio >= _QUALITY_DEGRADED_FAILURE_RATIO:
            degraded_reasons.append(f"asr_high_failure_ratio: {failure_ratio:.2f}")

        if not produced:
            self.last_quality_state = 'failed'
            self.last_degraded_reasons = failed_reasons or ['no_subtitle_output']
        elif failed_reasons:
            self.last_quality_state = 'failed'
            self.last_degraded_reasons = failed_reasons
        elif degraded_reasons:
            self.last_quality_state = 'degraded'
            self.last_degraded_reasons = degraded_reasons

        self.logger.info(
            "ASR quality state: state=%s, produced=%s, reasons=%s",
            self.last_quality_state,
            produced,
            self.last_degraded_reasons,
        )

    def _transcribe_with_vad(self, audio_wav: str, total_duration: float) -> List[AlignedSubtitleCue]:
        try:
            windows = self._vad.detect_speech_windows(audio_wav, total_duration)
        except Exception as exc:
            self.last_warning_message = f"vad_failed: {exc}"
            return []

        vad_state = getattr(self._vad, 'last_result_state', 'unknown')
        coverage_ratio = getattr(self._vad, 'last_speech_coverage_ratio', 0.0)
        if windows is None:
            self.last_warning_message = getattr(self._vad, 'last_failure_reason', 'vad_failed')
            return []
        if not windows:
            self.last_warning_message = getattr(self._vad, 'last_failure_reason', 'vad_no_speech')
            return []
        # 交付给 ASR 之前先固化语音区间：窗口后续会被合并/丢弃，
        # 而幻觉清洗需要的是「原始 VAD 判定有人声」的时间范围。
        self._last_vad_spans = self._collect_speech_spans(windows)
        if vad_state == 'partial':
            # 部分分片失败：仍有字幕产出，但覆盖不完整，必须走严格质检。
            self.last_warning_message = (
                getattr(self._vad, 'last_failure_reason', '') or 'vad_partial_chunks'
            )

        if coverage_ratio < self.config.vad_min_speech_coverage_ratio:
            self.last_warning_message = 'vad_low_coverage'

        lang_hint = self._asr.detect_language_from_segments(audio_wav, windows, extract_clip_fn=self._extract_audio_clip)
        self._asr.set_language_hint(lang_hint if lang_hint and lang_hint.lower() != 'unknown' else '')

        window_inputs = self._prepare_window_inputs(audio_wav, windows)
        if not window_inputs:
            self.last_warning_message = 'vad_no_usable_window'
            return []

        results = self._asr.transcribe_windows_concurrent(window_inputs)
        aligned = self._srt.align_transcription_results(results, total_duration_s=total_duration)
        aligned = self._maybe_apply_ai_segmentation(results, aligned)
        success_count = sum(1 for result in results if result.ok or result.timestamp_mode == 'srt')
        if success_count == 0:
            self.last_warning_message = self._pick_failure_token(results) or 'asr_failed'
        elif any(result.timestamp_mode == 'srt' for result in results):
            self.last_warning_message = self.last_warning_message or 'asr_no_timestamps'

        if vad_state == 'low_coverage' and aligned:
            self.last_warning_message = 'vad_low_coverage'
        return aligned

    def _fallback_transcription(self, audio_wav: str, total_duration: float) -> List[AlignedSubtitleCue]:
        chunk_cues = self._fallback_chunked_transcription(audio_wav, total_duration)
        if chunk_cues:
            return chunk_cues
        if self._can_whole_audio_fallback(total_duration):
            return self._fallback_whole_audio(audio_wav, total_duration)
        return []

    def _fallback_chunked_transcription(self, audio_wav: str, total_duration: float) -> List[AlignedSubtitleCue]:
        chunks = self._create_audio_chunks(total_duration)
        window_inputs = self._prepare_chunk_inputs(audio_wav, chunks)
        if not window_inputs:
            return []
        results = self._asr.transcribe_windows_concurrent(window_inputs)
        aligned = self._srt.align_transcription_results(results, total_duration_s=total_duration)
        aligned = self._maybe_apply_ai_segmentation(results, aligned)
        if not aligned:
            self.last_warning_message = self._pick_failure_token(results) or self.last_warning_message
        return aligned

    def _fallback_whole_audio(self, audio_wav: str, total_duration: float) -> List[AlignedSubtitleCue]:
        whole_window = DetectedSpeechWindow(
            start_s=0.0,
            end_s=total_duration,
            ownership_start_s=0.0,
            ownership_end_s=total_duration,
            source_pass='whole_audio',
        )
        result = self._asr.transcribe_window(audio_wav, window=whole_window, segment_info='whole-audio')
        aligned = self._srt.align_transcription_results([result], total_duration_s=total_duration)
        aligned = self._maybe_apply_ai_segmentation([result], aligned)
        if not aligned and result.failure_token:
            self.last_warning_message = result.failure_token
        return aligned

    def _maybe_apply_ai_segmentation(
        self,
        results: List[AsrTranscriptionResult],
        aligned: List[AlignedSubtitleCue],
    ) -> List[AlignedSubtitleCue]:
        """若启用 AI 智能分段，对 ASR 原始结果（含字级时间戳）做语义重分段。

        三级降级封装在 AISegmenter 内部；此处仅做最外层兜底——
        任何异常都回退到规则分段结果，保证不阻断主流程。
        """
        if not self.config.ai_segmentation_enabled or not self.config.ai_segmentation_config:
            return aligned
        if AISegmenter is None or AISegmentationConfig is None:
            self.logger.warning('AI 智能分段模块未加载，跳过')
            return aligned
        try:
            segmenter = AISegmenter(self.config.ai_segmentation_config, logger=self.logger)
            ai_cues = segmenter.segment(results)
            if ai_cues:
                self.logger.info(
                    'AI 智能分段已应用：ASR 规则对齐 %d 条 → AI 重分段 %d 条',
                    len(aligned), len(ai_cues),
                )
                return ai_cues
        except AISegmentationError as exc:
            self.logger.warning('AI 智能分段未生效，回退规则分段：%s', exc)
        except Exception as exc:
            self.logger.warning('AI 智能分段异常，回退规则分段：%s: %s', exc.__class__.__name__, exc)
        return aligned

    def _pick_failure_token(self, results: List[AsrTranscriptionResult]) -> str:
        for result in results:
            if result.fallback_token:
                return result.fallback_token
            if result.failure_token:
                return result.failure_token
            if result.timestamp_mode == 'srt':
                return 'asr_no_timestamps'
        return ''

    def _can_whole_audio_fallback(self, total_duration: float) -> bool:
        if self.config.api_provider != 'voxtral':
            return True
        if not self.config.voxtral_enforce_max_duration:
            return True
        return total_duration <= max(1.0, float(self.config.voxtral_max_audio_duration_s or 10800.0))

    @staticmethod
    def _collect_speech_spans(
        windows: List[DetectedSpeechWindow],
    ) -> Tuple[Tuple[float, float], ...]:
        """把 VAD 窗口归约为 (start, end) 语音区间元组，供幻觉清洗使用。

        优先采用窗口内的原始语音区间 raw_spans；缺失时退回窗口全长（窗口由 VAD
        直接派生，末端语义与语音区间一致），避免整段数据缺失。
        """
        spans: List[Tuple[float, float]] = []
        for window in windows or []:
            for raw in window.raw_spans or []:
                try:
                    start_s = float(raw[0])
                    end_s = float(raw[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if end_s > start_s:
                    spans.append((start_s, end_s))
            if not window.raw_spans:
                start_s = float(window.start_s)
                end_s = float(window.end_s)
                if end_s > start_s:
                    spans.append((start_s, end_s))
        return tuple(spans)

    def _prepare_window_inputs(
        self,
        audio_wav: str,
        windows: List[DetectedSpeechWindow],
    ) -> List[Tuple[DetectedSpeechWindow, str]]:
        inputs: List[Tuple[DetectedSpeechWindow, str]] = []
        for window in self._merge_narrow_windows(windows):
            # 极短窗口只会产出噪声或幻觉，直接丢弃而不是送给 ASR。
            if float(window.duration_s) < _MIN_ASR_WINDOW_S:
                self.logger.info(
                    "Skip too-short VAD window [%.2fs-%.2fs] duration=%.2fs (< %.2fs)",
                    window.start_s,
                    window.end_s,
                    window.duration_s,
                    _MIN_ASR_WINDOW_S,
                )
                continue
            clip = self._extract_audio_clip(audio_wav, window.start_s, window.end_s)
            if clip:
                inputs.append((window, clip))
        return inputs

    def _merge_narrow_windows(
        self,
        windows: List[DetectedSpeechWindow],
    ) -> List[DetectedSpeechWindow]:
        """把间隔极小的相邻窗口先合并，避免同一句话被拆成两次 ASR 请求。

        合并后必须同步扩展 ownership 边界与 raw_spans，否则下游 ``_align_segment``
        会按 ownership 把刚并入的尾部重新裁掉。
        """
        merged: List[DetectedSpeechWindow] = []
        max_segment_s = float(getattr(self.config, 'vad_max_segment_s', 0.0) or 0.0)
        for window in sorted(windows, key=lambda item: float(item.start_s)):
            if merged:
                prev = merged[-1]
                gap = float(window.start_s) - float(prev.end_s)
                combined = float(window.end_s) - float(prev.start_s)
                within_cap = max_segment_s <= 0.0 or combined <= max_segment_s + 1e-6
                if 0.0 <= gap <= _PRE_MERGE_WINDOW_GAP_S and within_cap:
                    merged[-1] = replace(
                        prev,
                        end_s=max(float(prev.end_s), float(window.end_s)),
                        ownership_end_s=max(float(prev.ownership_end_s), float(window.ownership_end_s)),
                        speech_duration_s=float(prev.speech_duration_s) + float(window.speech_duration_s),
                        raw_spans=list(prev.raw_spans) + list(window.raw_spans),
                    )
                    continue
            merged.append(window)
        return merged

    def _prepare_chunk_inputs(
        self,
        audio_wav: str,
        chunks: List[Tuple[float, float]],
    ) -> List[Tuple[DetectedSpeechWindow, str]]:
        inputs: List[Tuple[DetectedSpeechWindow, str]] = []
        for chunk_start, chunk_end in chunks:
            clip = self._extract_audio_clip(audio_wav, chunk_start, chunk_end)
            if not clip:
                continue
            inputs.append((
                DetectedSpeechWindow(
                    start_s=chunk_start,
                    end_s=chunk_end,
                    ownership_start_s=chunk_start,
                    ownership_end_s=chunk_end,
                    source_pass='fixed_chunk',
                ),
                clip,
            ))
        return inputs

    def _create_audio_chunks(self, total_duration_s: float) -> List[Tuple[float, float]]:
        # 窗口非法（0 / 负数 / 非数值）时回退默认窗口：窗口塌缩会把音频切成
        # 海量分片，且后续每片都要抽一次音频再跑一轮 ASR。
        try:
            window = float(self.config.chunk_window_s)
        except (TypeError, ValueError):
            window = 15.0
        if window <= 0.0:
            window = 15.0
        # 硬下限与原始实现一致（0.1s），保证步进永不为零。
        window = max(0.1, window)
        try:
            overlap = float(self.config.chunk_overlap_s)
        except (TypeError, ValueError):
            overlap = 0.0
        overlap = max(0.0, overlap)
        if self.config.api_provider == 'voxtral' and self.config.voxtral_enforce_max_duration:
            max_duration_s = max(1.0, float(self.config.voxtral_max_audio_duration_s or 10800.0))
            margin_s = max(0.0, float(self.config.voxtral_long_audio_margin_s or 0.0))
            window = min(window, max(1.0, max_duration_s - margin_s))
        # 防御性钳制（与设置页 guard 同口径 window - 1.0）：overlap >= window 时
        # current = end - overlap 不再前进，会造成分片死循环，ASR 线程永不返回、任务卡死。
        # window=5（设置页控件最小值）配 overlap=5（app.py guard 上限）是一条能通过全部
        # 既有校验的合法提交，必须在此兜住。
        #
        # 为什么不是 window - 0.01：步进会退化成 0.01 秒，1 小时素材被切成约 36 万片
        # （每片都要抽一次音频并跑一轮 ASR），等效于把任务拖垮。留 1 秒余量与
        # 设置页 guard 一致；window 极小时（模块硬下限 0.1）钳到 0 即可 ——
        # 步进等于 window 仍为正，终止性不受影响。
        clamped_overlap = max(0.0, min(overlap, max(window - 1.0, 0.0)))
        if clamped_overlap != overlap:
            self.logger.warning(
                "AUDIO_CHUNK_OVERLAP_S=%.3f >= chunk window %.3fs, clamped to %.3fs "
                "to keep chunking finite (step=%.3fs)",
                overlap,
                window,
                clamped_overlap,
                window - clamped_overlap,
            )
        overlap = clamped_overlap
        if total_duration_s <= window:
            return [(0.0, total_duration_s)]

        chunks: List[Tuple[float, float]] = []
        current = 0.0
        while current < total_duration_s:
            end = min(current + window, total_duration_s)
            chunks.append((current, end))
            if end >= total_duration_s:
                break
            current = end - overlap
        return chunks

    def _extract_audio_wav(self, video_path: str) -> Optional[str]:
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger) or 'ffmpeg'
            out_dir = tempfile.mkdtemp(prefix='y2a_audio_')
            self._temp_dirs.append(out_dir)
            audio_path = os.path.join(out_dir, 'audio.wav')
            cmd = [
                ffmpeg_bin, '-y', '-i', video_path,
                '-vn', '-ac', '1', '-ar', '16000',
                '-acodec', 'pcm_s16le', '-f', 'wav', audio_path,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=600,
            )
            if result.returncode != 0 or not os.path.exists(audio_path):
                self.logger.error("Audio extraction failed: %s", result.stderr)
                return None
            return audio_path
        except Exception as exc:
            self.logger.error("Audio extraction exception: %s", exc)
            return None

    def _extract_audio_clip(self, wav_path: str, start_s: float, end_s: float) -> Optional[str]:
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger) or 'ffmpeg'
            out_dir = tempfile.mkdtemp(prefix='y2a_clip_')
            self._temp_dirs.append(out_dir)
            out_wav = os.path.join(out_dir, 'clip.wav')
            duration = max(0.01, float(end_s) - float(start_s))
            cmd = [
                ffmpeg_bin, '-y',
                '-ss', f"{start_s:.3f}", '-t', f"{duration:.3f}",
                '-i', wav_path,
                '-ac', '1', '-ar', '16000',
                '-acodec', 'pcm_s16le', '-f', 'wav', out_wav,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=120,
            )
            if result.returncode != 0 or not os.path.exists(out_wav):
                return None
            with wave.open(out_wav, 'rb') as wf:
                actual = wf.getnframes() / wf.getframerate() if wf.getframerate() > 0 else 0.0
                if actual < 0.1:
                    return None
            return out_wav
        except Exception:
            return None

    def _probe_media_duration(self, media_path: str) -> Optional[float]:
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger)
            if not ffmpeg_bin:
                return None
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=self.logger)
            if not ffprobe_bin:
                return None
            result = subprocess.run(
                [ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_format', media_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=60,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or '{}')
            return float(data.get('format', {}).get('duration', 0.0))
        except Exception:
            return None

    def _cleanup_temp_files(self):
        self._vad.cleanup()
        for temp_dir in self._temp_dirs:
            try:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
            except Exception:
                continue
        self._temp_dirs.clear()


def create_speech_recognizer_from_config(
    app_config: dict,
    task_id: Optional[str] = None,
) -> Optional[SpeechRecognizer]:
    try:
        if not coerce_bool(app_config.get('SPEECH_RECOGNITION_ENABLED', False)):
            return None

        provider = str(app_config.get('SPEECH_RECOGNITION_PROVIDER') or 'whisper').strip().lower()
        if provider not in ('whisper', 'voxtral'):
            provider = 'whisper'
        use_voxtral = provider == 'voxtral'

        if use_voxtral:
            api_provider = 'voxtral'
            api_key = app_config.get('VOXTRAL_API_KEY') or ''
            base_url = app_config.get('VOXTRAL_BASE_URL') or 'https://api.mistral.ai/v1'
            model_name = app_config.get('VOXTRAL_MODEL_NAME') or 'voxtral-mini-latest'
            language = app_config.get('VOXTRAL_LANGUAGE') or ''
            prompt = ''
            max_retries = int(app_config.get('WHISPER_MAX_RETRIES', 3) or 3)
            timeout_s = float(app_config.get('OPENAI_TIMEOUT_SECONDS', 600) or 600.0)
        else:
            api_provider = 'whisper'
            api_key = app_config.get('WHISPER_API_KEY') or app_config.get('OPENAI_API_KEY', '')
            configured_base_url = (
                app_config.get('WHISPER_BASE_URL')
                or app_config.get('OPENAI_BASE_URL')
                or 'https://api.openai.com/v1'
            )
            # 兼容旧配置继续继承全局端点，但不能把完整的 /responses 或
            # /chat/completions 生成地址直接拼接为 /audio/transcriptions。
            from .utils import normalize_openai_base_url
            base_url = normalize_openai_base_url(configured_base_url)
            model_name = app_config.get('WHISPER_MODEL_NAME') or 'whisper-1'
            language = app_config.get('WHISPER_LANGUAGE') or ''
            prompt = app_config.get('WHISPER_PROMPT') or ''
            max_retries = int(app_config.get('WHISPER_MAX_RETRIES', 3) or 3)
            timeout_s = float(app_config.get('OPENAI_TIMEOUT_SECONDS', 600) or 600.0)

        config = SpeechRecognitionConfig(
            provider=provider,
            api_provider=api_provider,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            vad_enabled=coerce_bool(app_config.get('VAD_ENABLED', True)),
            vad_provider=app_config.get('VAD_PROVIDER') or 'silero-vad',
            vad_threshold=_config_float(app_config, 'VAD_SILERO_THRESHOLD', 0.55),
            vad_min_speech_ms=_config_int(app_config, 'VAD_SILERO_MIN_SPEECH_MS', 300),
            vad_min_silence_ms=_config_int(app_config, 'VAD_SILERO_MIN_SILENCE_MS', 320),
            vad_max_speech_s=_config_int(app_config, 'VAD_SILERO_MAX_SPEECH_S', 120),
            vad_speech_pad_ms=_config_int(app_config, 'VAD_SILERO_SPEECH_PAD_MS', 120),
            chunk_window_s=_config_float(app_config, 'AUDIO_CHUNK_WINDOW_S', 15.0),
            chunk_overlap_s=_config_float(app_config, 'AUDIO_CHUNK_OVERLAP_S', 0.4),
            vad_merge_gap_s=_config_float(app_config, 'VAD_MERGE_GAP_S', 0.35),
            vad_min_segment_s=_config_float(app_config, 'VAD_MIN_SEGMENT_S', 0.8),
            vad_max_segment_s=_config_positive_float(app_config, 'VAD_MAX_SEGMENT_S', 15.0),
            vad_max_segment_s_for_split=_config_float(app_config, 'VAD_MAX_SEGMENT_S_FOR_SPLIT', 15.0),
            vad_refinement_enabled=coerce_bool(app_config.get('VAD_REFINEMENT_ENABLED', True)),
            # 默认值与 vad_processor.VadConfig 对齐；口径改为「语音时长占比」后
            # 旧值 0.015 会随之下调一档，见 vad_processor._windows_coverage_ratio。
            vad_min_speech_coverage_ratio=_config_float(app_config, 'VAD_MIN_SPEECH_COVERAGE_RATIO', 0.01),
            vad_drop_isolated_short=coerce_bool(app_config.get('VAD_DROP_ISOLATED_SHORT', True)),
            language=language,
            prompt=prompt,
            translate=coerce_bool(app_config.get('WHISPER_TRANSLATE', False)) if not use_voxtral else False,
            whisper_timestamp_granularities=app_config.get('WHISPER_TIMESTAMP_GRANULARITIES') or 'segment,word',
            voxtral_timestamp_granularities=app_config.get('VOXTRAL_TIMESTAMP_GRANULARITIES') or 'segment,word',
            voxtral_diarize=coerce_bool(app_config.get('VOXTRAL_DIARIZE', False)),
            voxtral_context_bias=app_config.get('VOXTRAL_CONTEXT_BIAS') or '',
            voxtral_max_audio_duration_s=float(app_config.get('VOXTRAL_MAX_AUDIO_DURATION_S', 10800) or 10800.0),
            voxtral_long_audio_margin_s=float(app_config.get('VOXTRAL_LONG_AUDIO_MARGIN_S', 5) or 5.0),
            voxtral_enforce_max_duration=coerce_bool(app_config.get('VOXTRAL_ENFORCE_MAX_DURATION', True)),
            whisper_temperature=_config_float(
                app_config, 'WHISPER_TEMPERATURE', 0.0
            ),
            whisper_condition_on_previous_text=coerce_bool(
                app_config.get('WHISPER_CONDITION_ON_PREVIOUS_TEXT', False)
            ),
            whisper_no_speech_threshold=_config_float(
                app_config, 'WHISPER_NO_SPEECH_THRESHOLD', 0.6
            ),
            max_workers=int(app_config.get('WHISPER_MAX_WORKERS', 3) or 3),
            max_subtitle_line_length=int(app_config.get('SUBTITLE_MAX_LINE_LENGTH', 42) or 42),
            max_subtitle_lines=int(app_config.get('SUBTITLE_MAX_LINES', 2) or 2),
            normalize_punctuation=coerce_bool(app_config.get('SUBTITLE_NORMALIZE_PUNCTUATION', True)),
            filter_filler_words=coerce_bool(app_config.get('SUBTITLE_FILTER_FILLER_WORDS', False)),
            subtitle_time_offset_s=float(app_config.get('SUBTITLE_TIME_OFFSET_S', 0.0) or 0.0),
            subtitle_min_cue_duration_s=float(app_config.get('SUBTITLE_MIN_CUE_DURATION_S', 0.6) or 0.6),
            subtitle_merge_gap_s=float(app_config.get('SUBTITLE_MERGE_GAP_S', 0.3) or 0.3),
            subtitle_min_text_length=int(app_config.get('SUBTITLE_MIN_TEXT_LENGTH', 2) or 2),
            subtitle_time_offset_enabled=coerce_bool(app_config.get('SUBTITLE_TIME_OFFSET_ENABLED', False)),
            subtitle_min_cue_duration_enabled=coerce_bool(app_config.get('SUBTITLE_MIN_CUE_DURATION_ENABLED', False)),
            subtitle_merge_gap_enabled=coerce_bool(app_config.get('SUBTITLE_MERGE_GAP_ENABLED', False)),
            subtitle_min_text_length_enabled=coerce_bool(app_config.get('SUBTITLE_MIN_TEXT_LENGTH_ENABLED', False)),
            subtitle_max_line_length_enabled=coerce_bool(app_config.get('SUBTITLE_MAX_LINE_LENGTH_ENABLED', False)),
            subtitle_max_lines_enabled=coerce_bool(app_config.get('SUBTITLE_MAX_LINES_ENABLED', False)),
            subtitle_max_cue_duration_s=_config_float(app_config, 'SUBTITLE_MAX_CUE_DURATION_S', 8.0),
            subtitle_max_cps=_config_float(app_config, 'SUBTITLE_MAX_CPS', 20.0),
            max_retries=max_retries,
            retry_delay_s=float(app_config.get('WHISPER_RETRY_DELAY_S', 2.0) or 2.0),
            request_timeout_s=timeout_s,
            ai_segmentation_enabled=coerce_bool(app_config.get('AI_SEGMENTATION_ENABLED', False)),
            ai_segmentation_config=(
                AISegmentationConfig.from_app_config(app_config)
                if AISegmentationConfig is not None
                else None
            ),
        )
        return SpeechRecognizer(config, task_id)
    except Exception as e:
        logging.getLogger('speech_recognition').warning(f"创建语音识别器失败: {e}")
        return None
