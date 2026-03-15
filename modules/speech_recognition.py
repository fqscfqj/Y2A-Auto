#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
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
    vad_enabled: bool = False
    vad_provider: str = 'silero-vad'
    vad_threshold: float = 0.55
    vad_min_speech_ms: int = 300
    vad_min_silence_ms: int = 320
    vad_max_speech_s: int = 120
    vad_speech_pad_ms: int = 120
    chunk_window_s: float = 30.0
    chunk_overlap_s: float = 0.4
    vad_merge_gap_s: float = 0.35
    vad_min_segment_s: float = 0.8
    vad_max_segment_s: float = 30.0
    vad_max_segment_s_for_split: float = 30.0
    vad_refinement_enabled: bool = True
    vad_min_speech_coverage_ratio: float = 0.015
    language: str = ''
    prompt: str = ''
    translate: bool = False
    asr_word_timestamps_enabled: bool = True
    voxtral_timestamp_granularities: str = 'segment,word'
    voxtral_diarize: bool = False
    voxtral_context_bias: str = ''
    voxtral_max_audio_duration_s: float = 10800.0
    voxtral_long_audio_margin_s: float = 5.0
    voxtral_enforce_max_duration: bool = True
    max_workers: int = 3
    max_subtitle_line_length: int = 42
    max_subtitle_lines: int = 2
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
    fallback_to_fixed_chunks: bool = False
    request_timeout_s: float = 300.0


class VadFailure(RuntimeError):
    pass


class WhisperFailure(RuntimeError):
    pass


class SpeechRecognizer:
    def __init__(self, config: SpeechRecognitionConfig, task_id: Optional[str] = None):
        self.config = config
        self.task_id = task_id or 'unknown'
        self.logger = _setup_task_logger(self.task_id)
        self.last_warning_message: str = ''
        self.last_error_message: str = ''
        self._temp_dirs: List[str] = []

        if config.provider not in ('whisper', 'fireredasr', 'voxtral'):
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
                timestamp_granularities=config.voxtral_timestamp_granularities,
                diarize=config.voxtral_diarize,
                context_bias=config.voxtral_context_bias,
                max_retries=config.max_retries,
                retry_delay_s=config.retry_delay_s,
                max_workers=config.max_workers,
                request_timeout_s=config.request_timeout_s,
                voxtral_max_audio_duration_s=config.voxtral_max_audio_duration_s,
                voxtral_enforce_max_duration=config.voxtral_enforce_max_duration,
                word_timestamps_enabled=config.asr_word_timestamps_enabled,
            ),
            logger=self.logger,
        )
        self._srt = SrtTransformEngine(
            SrtTransformConfig(
                max_line_length=config.max_subtitle_line_length if config.subtitle_max_line_length_enabled else 42,
                max_lines=config.max_subtitle_lines if config.subtitle_max_lines_enabled else 2,
                normalize_punctuation=config.normalize_punctuation,
                filter_filler_words=config.filter_filler_words,
                time_offset_s=config.subtitle_time_offset_s if config.subtitle_time_offset_enabled else 0.0,
                min_cue_duration_s=config.subtitle_min_cue_duration_s if config.subtitle_min_cue_duration_enabled else 0.6,
                merge_gap_s=config.subtitle_merge_gap_s if config.subtitle_merge_gap_enabled else 0.3,
                min_text_length=config.subtitle_min_text_length if config.subtitle_min_text_length_enabled else 2,
            ),
            logger=self.logger,
        )

    def transcribe_video_to_subtitles(self, video_path: str, output_path: str) -> Optional[str]:
        try:
            self.last_warning_message = ''
            self.last_error_message = ''

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

            cue_dicts = self._srt.clean_hallucinations(cues)
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
        except WhisperFailure as exc:
            self.last_error_message = self.last_error_message or str(exc)
            return None
        except Exception as exc:
            self.last_error_message = self.last_error_message or f"Transcription failed: {exc}"
            self.logger.exception("Transcription failed")
            return None
        finally:
            self._cleanup_temp_files()

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
        if not aligned and result.failure_token:
            self.last_warning_message = result.failure_token
        return aligned

    def _pick_failure_token(self, results: List[AsrTranscriptionResult]) -> str:
        for result in results:
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

    def _prepare_window_inputs(
        self,
        audio_wav: str,
        windows: List[DetectedSpeechWindow],
    ) -> List[Tuple[DetectedSpeechWindow, str]]:
        inputs: List[Tuple[DetectedSpeechWindow, str]] = []
        for window in windows:
            clip = self._extract_audio_clip(audio_wav, window.start_s, window.end_s)
            if clip:
                inputs.append((window, clip))
        return inputs

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
        window = max(0.1, float(self.config.chunk_window_s or 15.0))
        overlap = max(0.0, float(self.config.chunk_overlap_s or 0.0))
        if self.config.api_provider == 'voxtral' and self.config.voxtral_enforce_max_duration:
            max_duration_s = max(1.0, float(self.config.voxtral_max_audio_duration_s or 10800.0))
            margin_s = max(0.0, float(self.config.voxtral_long_audio_margin_s or 0.0))
            window = min(window, max(1.0, max_duration_s - margin_s))
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
        use_fireredasr = provider == 'fireredasr'
        use_voxtral = provider == 'voxtral'

        if use_fireredasr:
            api_provider = 'fireredasr2s'
            api_key = app_config.get('FIREREDASR_API_KEY') or ''
            base_url = app_config.get('FIREREDASR_BASE_URL') or ''
            model_name = ''
            language = ''
            prompt = ''
            max_retries = int(app_config.get('FIREREDASR_MAX_RETRIES', 3) or 3)
            timeout_s = float(app_config.get('FIREREDASR_TIMEOUT', 300) or 300.0)
        elif use_voxtral:
            api_provider = 'voxtral'
            api_key = app_config.get('VOXTRAL_API_KEY') or ''
            base_url = app_config.get('VOXTRAL_BASE_URL') or 'https://api.mistral.ai/v1'
            model_name = app_config.get('VOXTRAL_MODEL_NAME') or 'voxtral-mini-latest'
            language = app_config.get('VOXTRAL_LANGUAGE') or ''
            prompt = ''
            max_retries = int(app_config.get('WHISPER_MAX_RETRIES', 3) or 3)
            timeout_s = float(app_config.get('FIREREDASR_TIMEOUT', 300) or 300.0)
        else:
            api_provider = 'whisper'
            api_key = app_config.get('WHISPER_API_KEY') or app_config.get('OPENAI_API_KEY', '')
            base_url = app_config.get('WHISPER_BASE_URL') or app_config.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')
            model_name = app_config.get('WHISPER_MODEL_NAME') or 'whisper-1'
            language = app_config.get('WHISPER_LANGUAGE') or ''
            prompt = app_config.get('WHISPER_PROMPT') or ''
            max_retries = int(app_config.get('WHISPER_MAX_RETRIES', 3) or 3)
            timeout_s = float(app_config.get('FIREREDASR_TIMEOUT', 300) or 300.0)

        config = SpeechRecognitionConfig(
            provider=provider,
            api_provider=api_provider,
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            vad_enabled=coerce_bool(app_config.get('VAD_ENABLED', False)),
            vad_provider=app_config.get('VAD_PROVIDER') or 'silero-vad',
            vad_threshold=float(app_config.get('VAD_SILERO_THRESHOLD', 0.55) or 0.55),
            vad_min_speech_ms=int(app_config.get('VAD_SILERO_MIN_SPEECH_MS', 300) or 300),
            vad_min_silence_ms=int(app_config.get('VAD_SILERO_MIN_SILENCE_MS', 320) or 320),
            vad_max_speech_s=int(app_config.get('VAD_SILERO_MAX_SPEECH_S', 120) or 120),
            vad_speech_pad_ms=int(app_config.get('VAD_SILERO_SPEECH_PAD_MS', 120) or 120),
            chunk_window_s=float(app_config.get('AUDIO_CHUNK_WINDOW_S', 30.0) or 30.0),
            chunk_overlap_s=float(app_config.get('AUDIO_CHUNK_OVERLAP_S', 0.4) or 0.4),
            vad_merge_gap_s=float(app_config.get('VAD_MERGE_GAP_S', 0.35) or 0.35),
            vad_min_segment_s=float(app_config.get('VAD_MIN_SEGMENT_S', 0.8) or 0.8),
            vad_max_segment_s=float(app_config.get('VAD_MAX_SEGMENT_S', 30.0) or 30.0),
            vad_max_segment_s_for_split=float(app_config.get('VAD_MAX_SEGMENT_S_FOR_SPLIT', 30.0) or 30.0),
            vad_refinement_enabled=coerce_bool(app_config.get('VAD_REFINEMENT_ENABLED', True)),
            vad_min_speech_coverage_ratio=float(app_config.get('VAD_MIN_SPEECH_COVERAGE_RATIO', 0.015) or 0.015),
            language=language,
            prompt=prompt,
            translate=coerce_bool(app_config.get('WHISPER_TRANSLATE', False)) if not use_voxtral and not use_fireredasr else False,
            asr_word_timestamps_enabled=coerce_bool(app_config.get('ASR_WORD_TIMESTAMPS_ENABLED', True)),
            voxtral_timestamp_granularities=app_config.get('VOXTRAL_TIMESTAMP_GRANULARITIES') or 'segment,word',
            voxtral_diarize=coerce_bool(app_config.get('VOXTRAL_DIARIZE', False)),
            voxtral_context_bias=app_config.get('VOXTRAL_CONTEXT_BIAS') or '',
            voxtral_max_audio_duration_s=float(app_config.get('VOXTRAL_MAX_AUDIO_DURATION_S', 10800) or 10800.0),
            voxtral_long_audio_margin_s=float(app_config.get('VOXTRAL_LONG_AUDIO_MARGIN_S', 5) or 5.0),
            voxtral_enforce_max_duration=coerce_bool(app_config.get('VOXTRAL_ENFORCE_MAX_DURATION', True)),
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
            max_retries=max_retries,
            retry_delay_s=float(app_config.get('WHISPER_RETRY_DELAY_S', 2.0) or 2.0),
            fallback_to_fixed_chunks=coerce_bool(app_config.get('WHISPER_FALLBACK_TO_FIXED_CHUNKS', False)),
            request_timeout_s=timeout_s,
        )
        return SpeechRecognizer(config, task_id)
    except Exception:
        return None
