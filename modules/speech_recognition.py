#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Audio Transcription Module – Broad VAD-Guided ASR Timeline Alignment.

Architecture:
  1. **VadProcessor** (vad_processor.py)   – High-recall, lenient VAD that
     produces broad "search windows" (not subtitle boundaries).
  2. **AsrApiClient** (asr_api_client.py)  – Sends each window to the
     OpenAI-compatible Whisper API with ``response_format="srt"`` and
     retrieves the ASR engine's own precise timestamps.
  3. **SrtTransformEngine** (srt_transform_engine.py) – Parses relative SRT
     timestamps, calibrates them to the global timeline
     (``Global = Segment_Start + Relative``), cleans hallucinations,
     resolves overlaps, and renders the final SRT.

This orchestrator module glues the three engines together while keeping the
same public API consumed by ``task_manager.py``:

  * ``SpeechRecognizer``
  * ``create_speech_recognizer_from_config()``
"""

import os
import tempfile
import subprocess
import logging
import json
import shutil
import wave
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any

from .ffmpeg_manager import get_ffmpeg_path, get_ffprobe_path
from .vad_processor import VadProcessor, VadConfig
from .asr_api_client import AsrApiClient, AsrConfig
from .srt_transform_engine import SrtTransformEngine, SrtTransformConfig


# ---------------------------------------------------------------------------
# Task-scoped logger factory
# ---------------------------------------------------------------------------

def _setup_task_logger(task_id: str) -> logging.Logger:
    """Create a task-scoped logger that writes into logs/task_{task_id}.log."""
    from .utils import get_app_subdir
    from logging.handlers import RotatingFileHandler

    log_dir = get_app_subdir('logs')
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(f'speech_recognition_{task_id}')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = RotatingFileHandler(
            os.path.join(log_dir, f'task_{task_id}.log'),
            maxBytes=10485760, backupCount=5, encoding='utf-8',
        )
        fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        fh.setLevel(logging.INFO)
        logger.addHandler(fh)
        logger.propagate = False
    return logger


# ---------------------------------------------------------------------------
# Unified configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class SpeechRecognitionConfig:
    provider: str = 'whisper'
    api_provider: str = 'whisper'
    api_key: str = ''
    base_url: str = ''
    model_name: str = 'whisper-1'
    # Deprecated fields (kept for config compatibility)
    detect_api_key: str = ''
    detect_base_url: str = ''
    detect_model_name: str = ''
    # Quality gate
    min_lines_enabled: bool = True
    min_lines_threshold: int = 5

    # VAD settings (broad / lenient)
    vad_enabled: bool = False
    vad_provider: str = 'silero-vad'
    vad_threshold: float = 0.5
    vad_min_speech_ms: int = 250
    vad_min_silence_ms: int = 500       # Broad: avoid mid-word cuts
    vad_max_speech_s: int = 120
    vad_speech_pad_ms: int = 500        # Dynamic padding 500 ms+

    # Audio chunking
    chunk_window_s: float = 25.0
    chunk_overlap_s: float = 0.2

    # VAD post-processing (broad)
    vad_merge_gap_s: float = 1.0        # Merge gaps < 1 s
    vad_min_segment_s: float = 1.0
    vad_max_segment_s_for_split: float = 29.0

    # Transcription
    language: str = ''
    prompt: str = ''
    translate: bool = False
    max_workers: int = 3                # Concurrent segment uploads

    # Text post-processing
    max_subtitle_line_length: int = 42
    max_subtitle_lines: int = 2
    normalize_punctuation: bool = True
    filter_filler_words: bool = True

    # Final cue post-processing
    subtitle_time_offset_s: float = 0.0
    subtitle_min_cue_duration_s: float = 0.6
    subtitle_merge_gap_s: float = 0.3
    subtitle_min_text_length: int = 2

    # Retry / fallback
    max_retries: int = 3
    retry_delay_s: float = 2.0
    fallback_to_fixed_chunks: bool = True
    request_timeout_s: float = 300.0


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class VadFailure(RuntimeError):
    pass


class WhisperFailure(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# SpeechRecognizer – Orchestrator
# ---------------------------------------------------------------------------

class SpeechRecognizer:
    """Orchestrates the Broad-VAD → ASR-SRT → Global-Calibration pipeline."""

    def __init__(self, config: SpeechRecognitionConfig, task_id: Optional[str] = None):
        self.config = config
        self.task_id = task_id or 'unknown'
        self.logger = _setup_task_logger(self.task_id)
        self.last_warning_message: str = ''
        self.last_error_message: str = ''
        self._temp_dirs: List[str] = []

        # Validate provider
        if config.provider not in ('whisper', 'fireredasr'):
            self.logger.error(f"Unsupported speech recognition provider: {config.provider}")
            raise ValueError(f"Unsupported speech recognition provider: {config.provider}")

        # Build sub-components -------------------------------------------
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
                max_segment_s_for_split=config.vad_max_segment_s_for_split,
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
                max_retries=config.max_retries,
                retry_delay_s=config.retry_delay_s,
                max_workers=config.max_workers,
                request_timeout_s=config.request_timeout_s,
            ),
            logger=self.logger,
        )
        self._srt = SrtTransformEngine(
            SrtTransformConfig(
                max_line_length=config.max_subtitle_line_length,
                max_lines=config.max_subtitle_lines,
                normalize_punctuation=config.normalize_punctuation,
                filter_filler_words=config.filter_filler_words,
                time_offset_s=config.subtitle_time_offset_s,
                min_cue_duration_s=config.subtitle_min_cue_duration_s,
                merge_gap_s=config.subtitle_merge_gap_s,
                min_text_length=config.subtitle_min_text_length,
            ),
            logger=self.logger,
        )

    # ==================================================================
    # Main public entry-point
    # ==================================================================

    def transcribe_video_to_subtitles(
        self, video_path: str, output_path: str
    ) -> Optional[str]:
        """Transcribe *video_path* to SRT subtitles saved at *output_path*.

        Pipeline:
          1. Extract 16 kHz mono WAV.
          2. Broad VAD → search windows.
          3. ASR (SRT format) per window, concurrently.
          4. Global timestamp calibration.
          5. Hallucination cleaning + overlap resolution.
          6. Text normalisation, cue finalisation, SRT rendering.
          7. Quality gate.

        Returns *output_path* on success, *None* otherwise.
        """
        try:
            self.last_warning_message = ''
            self.last_error_message = ''

            if not self._asr.client:
                self.logger.error("ASR client not initialised")
                return None
            if not os.path.exists(video_path):
                self.logger.error(f"Video file not found: {video_path}")
                return None

            # Step 1 – Audio extraction
            self.logger.info("Step 1/6: Extracting audio (16 kHz mono WAV)")
            audio_wav = self._extract_audio_wav(video_path)
            if not audio_wav:
                return None

            total_duration = self._probe_media_duration(audio_wav)
            if total_duration is None:
                total_duration = self._probe_media_duration(video_path) or 0.0
            self.logger.info(f"Audio duration: {total_duration:.1f}s")

            cues: List[Dict[str, Any]] = []
            force_fixed_chunks = False

            # Step 2 – Broad VAD segmentation
            if self.config.vad_enabled:
                self.logger.info("Step 2/6: Broad VAD segmentation (search windows)")
                try:
                    vad_segments = self._vad.detect_speech_segments(
                        audio_wav, total_duration,
                    )
                    if vad_segments:
                        self.logger.info(
                            f"VAD produced {len(vad_segments)} search windows"
                        )

                        # Language detection (from first & last segments)
                        lang_hint = self._asr.detect_language_from_segments(
                            audio_wav, vad_segments,
                            extract_clip_fn=self._extract_audio_clip,
                        )
                        if lang_hint and lang_hint.lower() != 'unknown':
                            self._asr.set_language_hint(lang_hint)
                            self.logger.info(f"Detected language: {lang_hint}")
                        else:
                            self._asr.set_language_hint('')

                        # Step 3 – Concurrent ASR transcription (SRT format)
                        self.logger.info("Step 3/6: ASR transcription (concurrent, SRT format)")
                        segment_inputs = self._prepare_segment_inputs(
                            audio_wav, vad_segments,
                        )
                        if segment_inputs:
                            asr_results = self._asr.transcribe_segments_concurrent(
                                segment_inputs,
                            )

                            # Check for total failures
                            success_count = sum(
                                1 for _, srt in asr_results if srt
                            )
                            if success_count == 0:
                                self.logger.warning(
                                    "No ASR results from VAD segments – falling back"
                                )
                            else:
                                # Step 4 – Global timestamp calibration
                                self.logger.info(
                                    f"Step 4/6: Global timestamp calibration "
                                    f"({success_count}/{len(asr_results)} segments)"
                                )
                                cues = self._srt.calibrate_segments(asr_results)
                    else:
                        if self.config.fallback_to_fixed_chunks:
                            self.last_warning_message = (
                                "VAD returned no segments – falling back to fixed chunks"
                            )
                            self.logger.warning(self.last_warning_message)
                            force_fixed_chunks = True
                        else:
                            self.logger.warning("VAD returned no segments")
                except VadFailure as exc:
                    self.last_warning_message = f"VAD failed, falling back: {exc}"
                    self.logger.warning(str(self.last_warning_message))
                    force_fixed_chunks = True
                except WhisperFailure:
                    raise
                except Exception as exc:
                    self.last_warning_message = f"VAD exception, falling back: {exc}"
                    self.logger.warning(str(self.last_warning_message))
                    force_fixed_chunks = True

            # Fallback: fixed-chunk transcription
            if not cues:
                cues = self._fallback_transcription(
                    audio_wav, total_duration, force_fixed_chunks,
                )
                if not cues:
                    self.logger.error("No subtitles generated")
                    return None

            # Step 5 – Hallucination cleaning + overlap resolution
            self.logger.info("Step 5/6: Hallucination cleaning & overlap resolution")
            cues = self._srt.clean_hallucinations(cues)
            cues = self._srt.resolve_overlaps(cues, total_duration)

            # Text normalisation
            cues = self._srt.apply_text_processing(cues)

            # Final cue timing post-processing
            cues = self._srt.finalize_cues(cues, total_duration)

            if not cues:
                self.logger.error("No cues remaining after post-processing")
                return None

            # Log timestamp range
            first_s = float(cues[0].get('start', 0))
            last_e = float(cues[-1].get('end', 0))
            self.logger.info(
                f"Subtitle range: {first_s:.2f}s – {last_e:.2f}s "
                f"(duration: {total_duration:.2f}s)"
            )

            # Step 6 – Render & save SRT
            self.logger.info(f"Step 6/6: Rendering SRT ({len(cues)} cues)")
            srt_text = self._srt.render_srt(cues)
            if not srt_text:
                self.logger.error("Failed to render SRT")
                return None

            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(srt_text)

            # Quality gate
            if self.config.min_lines_enabled:
                cue_count = SrtTransformEngine.count_cues(output_path)
                self.logger.info(f"Subtitle cue count: {cue_count}")
                if isinstance(cue_count, int) and cue_count < max(0, self.config.min_lines_threshold):
                    try:
                        os.remove(output_path)
                    except Exception as cleanup_exc:
                        self.logger.warning(
                            f"Failed to remove low-quality subtitle file '{output_path}': {cleanup_exc}"
                        )
                    self.logger.info(
                        f"Subtitle count ({cue_count}) below threshold "
                        f"({self.config.min_lines_threshold}) – discarded"
                    )
                    return None

            self.logger.info(f"✓ Transcription complete: {output_path}")
            return output_path
        except WhisperFailure as exc:
            if not self.last_error_message:
                self.last_error_message = str(exc)
            self.logger.error(f"Transcription failed: {exc}")
            return None
        except Exception as exc:
            if not self.last_error_message:
                self.last_error_message = f"Transcription failed: {exc}"
            self.logger.error(f"Transcription failed: {exc}")
            import traceback
            self.logger.error(traceback.format_exc())
            return None
        finally:
            self._cleanup_temp_files()

    # ==================================================================
    # Fallback: fixed-chunk or whole-audio transcription
    # ==================================================================

    def _fallback_transcription(
        self,
        audio_wav: str,
        total_duration: float,
        force_chunks: bool,
    ) -> List[Dict[str, Any]]:
        """Transcribe without VAD using fixed chunks or as a single clip."""
        if force_chunks or total_duration > self.config.chunk_window_s * 2:
            self.logger.info("Fallback: fixed-window chunked transcription")
            chunks = self._create_audio_chunks(total_duration)
            segment_inputs = self._prepare_chunk_inputs(audio_wav, chunks)
            if not segment_inputs:
                return []
            asr_results = self._asr.transcribe_segments_concurrent(segment_inputs)
            return self._srt.calibrate_segments(asr_results)
        else:
            self.logger.info("Fallback: whole-audio transcription")
            srt_text = self._asr.transcribe_segment(audio_wav)
            if not srt_text:
                return []
            return self._srt.parse_srt(srt_text, base_offset_s=0.0)

    # ==================================================================
    # Segment / chunk preparation helpers
    # ==================================================================

    def _prepare_segment_inputs(
        self,
        audio_wav: str,
        vad_segments: List[Tuple[float, float]],
    ) -> List[Tuple[float, str]]:
        """Extract audio clips for VAD segments and return (offset, path) pairs."""
        inputs: List[Tuple[float, str]] = []
        for seg_start, seg_end in vad_segments:
            seg_start = max(0.0, float(seg_start))
            seg_end = max(seg_start + 0.01, float(seg_end))
            clip = self._extract_audio_clip(audio_wav, seg_start, seg_end)
            if clip:
                inputs.append((seg_start, clip))
        return inputs

    def _prepare_chunk_inputs(
        self,
        audio_wav: str,
        chunks: List[Tuple[float, float]],
    ) -> List[Tuple[float, str]]:
        """Extract audio clips for fixed chunks."""
        inputs: List[Tuple[float, str]] = []
        for chunk_start, chunk_end in chunks:
            clip = self._extract_audio_clip(audio_wav, chunk_start, chunk_end)
            if clip:
                inputs.append((chunk_start, clip))
        return inputs

    def _create_audio_chunks(
        self, total_duration_s: float
    ) -> List[Tuple[float, float]]:
        """Create overlapping fixed-size chunks."""
        window = self.config.chunk_window_s
        overlap = self.config.chunk_overlap_s
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
        self.logger.info(
            f"Fixed chunks: {total_duration_s:.1f}s total, "
            f"window {window}s, overlap {overlap}s, {len(chunks)} chunks"
        )
        return chunks

    # ==================================================================
    # Audio extraction helpers
    # ==================================================================

    def _extract_audio_wav(self, video_path: str) -> Optional[str]:
        """Extract 16 kHz mono WAV from video using ffmpeg."""
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger)
            if ffmpeg_bin and os.path.exists(ffmpeg_bin):
                self.logger.info(f"Using ffmpeg: {ffmpeg_bin}")
            else:
                ffmpeg_bin = 'ffmpeg'

            tmp_dir = tempfile.mkdtemp(prefix='y2a_audio_')
            self._temp_dirs.append(tmp_dir)
            audio_path = os.path.join(tmp_dir, 'audio.wav')
            cmd = [
                ffmpeg_bin, '-y', '-i', video_path,
                '-vn', '-ac', '1', '-ar', '16000',
                '-acodec', 'pcm_s16le', '-f', 'wav',
                audio_path,
            ]
            self.logger.info(f"Extracting audio: {' '.join(cmd)}")
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=600,
            )
            if result.returncode != 0 or not os.path.exists(audio_path):
                self.logger.error(f"Audio extraction failed: {result.stderr}")
                return None
            return audio_path
        except Exception as exc:
            self.logger.error(f"Audio extraction exception: {exc}")
            return None

    def _extract_audio_clip(
        self, wav_path: str, start_s: float, end_s: float
    ) -> Optional[str]:
        """Extract a WAV clip from *wav_path*."""
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger) or 'ffmpeg'
            out_dir = tempfile.mkdtemp(prefix='y2a_clip_')
            self._temp_dirs.append(out_dir)
            out_wav = os.path.join(out_dir, 'clip.wav')
            dur = max(0.01, end_s - start_s)
            cmd = [
                ffmpeg_bin, '-y',
                '-ss', f"{start_s:.3f}", '-t', f"{dur:.3f}",
                '-i', wav_path,
                '-ac', '1', '-ar', '16000', '-acodec', 'pcm_s16le', '-f', 'wav',
                out_wav,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=120,
            )
            if result.returncode != 0 or not os.path.exists(out_wav):
                self.logger.warning(f"Clip extraction failed: {result.stderr[:200]}")
                return None

            with wave.open(out_wav, 'rb') as wf:
                actual = wf.getnframes() / wf.getframerate() if wf.getframerate() > 0 else 0.0
                if actual < 0.1:
                    self.logger.warning(f"Clip too short ({actual:.3f}s) – skipping")
                    return None
            return out_wav
        except Exception as exc:
            self.logger.warning(f"Clip extraction exception: {exc}")
            return None

    def _probe_media_duration(self, media_path: str) -> Optional[float]:
        """Get media duration in seconds via ffprobe."""
        try:
            ffmpeg_bin = get_ffmpeg_path(logger=self.logger)
            if not ffmpeg_bin:
                return None
            ffprobe_bin = get_ffprobe_path(ffmpeg_path=ffmpeg_bin, logger=self.logger)
            if not ffprobe_bin:
                return None
            cmd = [
                ffprobe_bin, '-v', 'quiet',
                '-print_format', 'json', '-show_format', media_path,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=60,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or '{}')
            return float(data.get('format', {}).get('duration', 0.0))
        except Exception:
            return None

    # ==================================================================
    # Cleanup
    # ==================================================================

    def _cleanup_temp_files(self):
        """Remove all temporary directories."""
        self._vad.cleanup()
        for d in self._temp_dirs:
            try:
                if os.path.exists(d):
                    shutil.rmtree(d)
            except Exception as exc:
                # Best-effort cleanup: log and continue without failing the task.
                self.logger.debug(f"Failed to remove temp dir {d}: {exc}")
        self._temp_dirs.clear()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_speech_recognizer_from_config(
    app_config: dict, task_id: Optional[str] = None
) -> Optional[SpeechRecognizer]:
    """Build a ``SpeechRecognizer`` from the application config dict."""
    try:
        def _to_bool(value) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            return str(value).strip().lower() in ('true', '1', 'on', 'yes')

        speech_enabled = _to_bool(app_config.get('SPEECH_RECOGNITION_ENABLED', False))
        firered_enabled = _to_bool(app_config.get('FIREREDASR_ENABLED', False))

        if not speech_enabled and not firered_enabled:
            return None

        provider = (app_config.get('SPEECH_RECOGNITION_PROVIDER') or 'whisper').lower()
        use_fireredasr = firered_enabled

        if use_fireredasr:
            asr_provider = 'fireredasr2s'
            asr_base_url = app_config.get('FIREREDASR_BASE_URL') or ''
            asr_api_key = app_config.get('FIREREDASR_API_KEY') or ''
            # FireRed `/v1/process_all` mode does not consume model/language/prompt params.
            asr_model = ''
            asr_language = ''
            asr_prompt = ''
            asr_max_retries = int(
                app_config.get('FIREREDASR_MAX_RETRIES', 3) or 3
            )
            asr_timeout_s = float(
                app_config.get('FIREREDASR_TIMEOUT', 300) or 300.0
            )
        else:
            asr_provider = 'whisper'
            asr_base_url = (
                app_config.get('WHISPER_BASE_URL')
                or app_config.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')
            )
            asr_api_key = (
                app_config.get('WHISPER_API_KEY')
                or app_config.get('OPENAI_API_KEY', '')
            )
            asr_model = app_config.get('WHISPER_MODEL_NAME') or 'whisper-1'
            asr_language = app_config.get('WHISPER_LANGUAGE') or ''
            asr_prompt = app_config.get('WHISPER_PROMPT') or ''
            asr_max_retries = int(app_config.get('WHISPER_MAX_RETRIES', 3) or 3)
            asr_timeout_s = 300.0

        config = SpeechRecognitionConfig(
            provider=provider,
            api_provider=asr_provider,
            api_key=asr_api_key,
            base_url=asr_base_url,
            model_name=asr_model,
            min_lines_enabled=app_config.get(
                'SPEECH_RECOGNITION_MIN_SUBTITLE_LINES_ENABLED', True,
            ),
            min_lines_threshold=int(
                app_config.get('SPEECH_RECOGNITION_MIN_SUBTITLE_LINES', 5) or 0
            ),
            # VAD
            vad_enabled=bool(app_config.get('VAD_ENABLED', False)),
            vad_provider=app_config.get('VAD_PROVIDER') or 'silero-vad',
            vad_threshold=float(app_config.get('VAD_SILERO_THRESHOLD', 0.5) or 0.5),
            vad_min_speech_ms=int(app_config.get('VAD_SILERO_MIN_SPEECH_MS', 250) or 250),
            vad_min_silence_ms=int(app_config.get('VAD_SILERO_MIN_SILENCE_MS', 500) or 500),
            vad_max_speech_s=int(app_config.get('VAD_SILERO_MAX_SPEECH_S', 120) or 120),
            vad_speech_pad_ms=int(app_config.get('VAD_SILERO_SPEECH_PAD_MS', 500) or 500),
            chunk_window_s=float(app_config.get('AUDIO_CHUNK_WINDOW_S', 25.0) or 25.0),
            chunk_overlap_s=float(app_config.get('AUDIO_CHUNK_OVERLAP_S', 0.2) or 0.2),
            vad_merge_gap_s=float(app_config.get('VAD_MERGE_GAP_S', 1.0) or 1.0),
            vad_min_segment_s=float(app_config.get('VAD_MIN_SEGMENT_S', 1.0) or 1.0),
            vad_max_segment_s_for_split=float(
                app_config.get('VAD_MAX_SEGMENT_S_FOR_SPLIT', 29.0) or 29.0
            ),
            # Transcription
            language=asr_language,
            prompt=asr_prompt,
            translate=bool(app_config.get('WHISPER_TRANSLATE', False)) if not use_fireredasr else False,
            max_workers=int(app_config.get('WHISPER_MAX_WORKERS', 3) or 3),
            # Text processing
            max_subtitle_line_length=int(
                app_config.get('SUBTITLE_MAX_LINE_LENGTH', 42) or 42
            ),
            max_subtitle_lines=int(app_config.get('SUBTITLE_MAX_LINES', 2) or 2),
            normalize_punctuation=bool(
                app_config.get('SUBTITLE_NORMALIZE_PUNCTUATION', True)
            ),
            filter_filler_words=bool(
                app_config.get('SUBTITLE_FILTER_FILLER_WORDS', True)
            ),
            subtitle_time_offset_s=float(
                app_config.get('SUBTITLE_TIME_OFFSET_S', 0.0) or 0.0
            ),
            subtitle_min_cue_duration_s=float(
                app_config.get('SUBTITLE_MIN_CUE_DURATION_S', 0.6) or 0.6
            ),
            subtitle_merge_gap_s=float(
                app_config.get('SUBTITLE_MERGE_GAP_S', 0.3) or 0.3
            ),
            subtitle_min_text_length=int(
                app_config.get('SUBTITLE_MIN_TEXT_LENGTH', 2) or 2
            ),
            max_retries=asr_max_retries,
            retry_delay_s=float(app_config.get('WHISPER_RETRY_DELAY_S', 2.0) or 2.0),
            fallback_to_fixed_chunks=bool(
                app_config.get('WHISPER_FALLBACK_TO_FIXED_CHUNKS', True)
            ),
            request_timeout_s=asr_timeout_s,
        )
        return SpeechRecognizer(config, task_id)
    except Exception:
        return None
